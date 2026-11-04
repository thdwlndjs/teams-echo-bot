"""확인된 회의록 부분을 worklog에 반영하고, 되돌린다. 문서: docs/v1.1/spec.md P1-5, P1-6.

부분 상태: collected(수집) → extracted(추출됨) → pending_confirm(확인 대기) → applied(반영됨) / skipped(취소)

- 새 회의록은 사람이 미리보기를 한 번 확인해야 반영된다(`apply_part`). 확인한 사람이 반영자다.
- 이미 확인된 회의록이 수정되면, 다시 추출한 결과에서 바뀐(새로 생긴) 항목만 자동으로 반영한다(`apply_edits`).
- 항목은 기존 판정(core._judge: 같은 항목·보충·대체·충돌)을 거쳐 반영한다. 확신이 0.7 미만이거나 판정에 실패하면
  기존 항목을 바꾸지 않고 "추가 논의 필요"(needs_discussion) 항목으로 둔다.
- 결정 대체는 자동으로 하고 알림을 남긴다. `revert`로 되돌린다(revisions의 이전 스냅샷).

이 모듈은 Notion·LLM을 직접 부르지 않는다. 관련 항목 비교(judge)는 호출부가 넘긴다.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import core, db, parts
from .db import ConflictError, NotFoundError, ValidationError

CONFIDENCE = 0.7  # [가정] 이 아래면 반영하지 않고 "추가 논의 필요"
SETTLE = timedelta(hours=2)  # 자동 감지된 새 회의록은 마지막 수정 후 이만큼 지나야 확인 대기로 올린다(작성 중 초안 방지)
LLM_JUDGED_KINDS = {"task", "decision"}  # 관련 항목과 비교할 때 LLM 판정을 쓰는 종류. 나머지는 규칙만
APPLIED = ("approved", "auto_applied")


def _part(c: Any, part_id: str) -> dict[str, Any]:
    row = c.execute("SELECT * FROM source_parts WHERE id = ?", (part_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"부분 {part_id} 없음")
    return dict(row)


def _source(c: Any, part: dict) -> tuple[str, str] | None:
    """부분의 원문 기록(evidence가 가리킬 sources 행). (id, content)."""
    row = c.execute("SELECT id, content FROM sources WHERE locator = ? ORDER BY captured_at DESC, rowid DESC LIMIT 1",
                    (f"{part['origin']}:{part['page_id']}" if part["origin"] != "notion" else f"notion:{part['page_id']}",)).fetchone()
    return (row["id"], row["content"]) if row else None


def extracted_items(store: Path, part: dict, prompt_version: str) -> list[dict[str, Any]] | None:
    with closing(db.connect(store)) as c:
        row = c.execute("SELECT items_json FROM part_extracts WHERE content_hash = ? AND prompt_version = ?",
                        (parts.extract_hash(part), prompt_version)).fetchone()
    return json.loads(row["items_json"]) if row else None


def _as_core_item(raw: dict, source_id: str) -> dict[str, Any]:
    kind = raw["kind"]
    title = raw["title"]
    return {"extracted_kind": kind, "kind": kind, "title": title, "statement": raw.get("detail") or title,
            "status": "open" if kind == "task" else "active", "areas": [], "assignee": raw.get("owner"),
            "due": raw.get("due"), "issue_refs": sorted({int(n) for n in core._ISSUE_RE.findall(title)}),
            "source_ids": [source_id], "quote": raw.get("quote") or ""}


def _judgment(store: Path, scope: str, item: dict, judge: core.Judge | None) -> dict[str, Any]:
    """관련 항목과 비교한 판정. 할 일·결정만 LLM 판정을 쓰고, 미정·피드백·인사이트는 같은 제목만 본다."""
    with closing(db.connect(store)) as c:
        related = core.find_related(c, scope, item)
    if item["kind"] not in LLM_JUDGED_KINDS or judge is None:
        same = [r for r in related if r["kind"] == item["kind"] and r["normalized_title"] == db.normalize_text(item["title"])]
        if not same:
            return {"change_type": "proposed", "target": None, "affected": [], "judged_by": "rule",
                    "rationale": "같은 제목의 기존 항목이 없다."}
        twin = same[0]
        unchanged = db.normalize_text(twin["statement_md"]) == db.normalize_text(item["statement"])
        return {"change_type": "unchanged" if unchanged else "supplementary", "target": twin, "affected": [],
                "judged_by": "rule", "rationale": "같은 제목의 기존 항목이 있다."}
    return core._judge(item, related, judge) or {"change_type": "unchanged", "target": None, "affected": [],
                                                 "judged_by": "rule", "rationale": "반영할 것이 없다."}


def _apply_one(store: Path, scope: str, part: dict, raw: dict, source: tuple[str, str], actor: str,
               judge: core.Judge | None) -> dict[str, Any]:
    item = _as_core_item(raw, source[0])
    judgment = _judgment(store, scope, item, judge)
    if judgment["change_type"] == "unchanged":
        return {"title": raw["title"], "kind": raw["kind"], "result": "unchanged"}
    unsure = (float(raw.get("confidence") if raw.get("confidence") is not None else 1) < CONFIDENCE
              or judgment["judged_by"] == "fallback" or judgment["change_type"] == "conflicting")
    if unsure:  # 기존 항목은 건드리지 않고 새 항목을 "추가 논의 필요"로 둔다
        judgment = dict(judgment, change_type="proposed", target=None, affected=[],
                        rationale=judgment["rationale"] + " (확신이 낮거나 판정이 갈려 추가 논의 필요로 둔다.)")
    ts = db.now()
    with db.write(store) as c:
        compared = [judgment["target"]["id"]] if judgment.get("target") else []
        candidate_id, _ = core._insert_candidate(c, scope, None, item, judgment, compared, {source[0]: source[1]}, ts)
    try:
        applied = core.approve(store, candidate_id, actor, note=f"부분 {part['id']} 확인 반영")
    except ConflictError as error:
        with db.write(store) as c:
            c.execute("UPDATE change_candidates SET status = 'rejected', decision_note = ?, updated_at = ? WHERE id = ?",
                      (f"반영 못 함: {error}", db.now(), candidate_id))
        return {"title": raw["title"], "kind": raw["kind"], "result": "skipped", "reason": str(error)}
    item_id = applied["item"]["id"]
    with db.write(store) as c:
        c.execute("UPDATE change_candidates SET status = 'auto_applied' WHERE id = ?", (candidate_id,))
        row = c.execute("SELECT attributes_json FROM work_items WHERE id = ?", (item_id,)).fetchone()
        attributes = json.loads(row["attributes_json"])
        attributes["part_ids"] = sorted(set(attributes.get("part_ids") or []) | {part["id"]})
        for key in ("role_hint", "kind_detail"):
            if raw.get(key):
                attributes[key] = raw[key]
        c.execute("UPDATE work_items SET attributes_json = ? WHERE id = ?", (db.to_json(attributes), item_id))
        if unsure:
            core._write_item(c, item_id, {"status": "needs_discussion"}, "확신이 낮거나 판정이 갈림", actor, db.now())
    result = {"title": raw["title"], "kind": raw["kind"], "candidate_id": candidate_id, "item_id": item_id,
              "result": "needs_discussion" if unsure else judgment["change_type"]}
    if judgment["change_type"] == "superseding" and not unsure:
        result["superseded"] = judgment["target"]["title"]
    return result


def apply_part(store: Path, scope: str, part_id: str, actor: str, prompt_version: str,
               items: list[dict] | None = None, judge: core.Judge | None = None) -> dict[str, Any]:
    """확인된 부분 하나를 반영한다. items가 있으면(고친 목록) 그것을, 없으면 저장된 추출 결과를 쓴다."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        part = _part(c, part_id)
        source = _source(c, part)
    if source is None:
        raise ValidationError(f"부분 {part_id}의 원문 기록이 없다")
    if items is None:
        items = extracted_items(store, part, prompt_version)
        if items is None:
            raise ValidationError(f"부분 {part_id}은 아직 추출 전이다")
    results = [_apply_one(store, scope, part, raw, source, actor, judge) for raw in items]
    with db.write(store) as c:
        c.execute("""UPDATE source_parts SET state = 'applied', applied_hash = ?, confirmed_by = ?, confirmed_at = ?,
                         updated_at = ? WHERE id = ?""",
                  (parts.extract_hash(part), actor, db.now(), db.now(), part_id))
    return _summary(results)


def _summary(results: list[dict]) -> dict[str, Any]:
    return {"results": results,
            "applied": sum(1 for r in results if r["result"] in {"proposed", "supplementary", "superseding"}),
            "needs_discussion": sum(1 for r in results if r["result"] == "needs_discussion"),
            "superseded": [{"old": r["superseded"], "new": r["title"], "candidate_id": r["candidate_id"]}
                           for r in results if r.get("superseded")]}


def apply_page(store: Path, scope: str, page_id: str, actor: str, prompt_version: str,
               corrected: dict[str, list[dict]] | None = None, judge: core.Judge | None = None) -> dict[str, Any]:
    """한 페이지(회의록 행)의 확인 대기·추출된 회의 메모 부분을 모두 반영한다. corrected: 부분 ID → 고친 목록."""
    results: list[dict] = []
    for part in meeting_parts(store, scope, page_id):
        if part["state"] == "applied" and part["applied_hash"] == parts.extract_hash(part):
            continue
        items = (corrected or {}).get(part["id"])
        if items is None and extracted_items(store, part, prompt_version) is None:
            continue
        results += apply_part(store, scope, part["id"], actor, prompt_version, items, judge)["results"]
    return _summary(results)


def meeting_parts(store: Path, scope: str, page_id: str) -> list[dict[str, Any]]:
    return [p for p in parts.list_parts(store, scope, page_id=page_id) if p["kind"] == "meeting"]


AUTO_ACTOR = "자동(확인된 회의록 수정)"


def apply_edits(store: Path, scope: str, prompt_version: str, judge: core.Judge | None = None) -> list[dict[str, Any]]:
    """이미 확인된 회의록이 수정됐을 때: 새로 추출한 결과에서 새로 생긴 항목만 자동으로 반영한다(알림용 결과)."""
    changed: list[dict[str, Any]] = []
    with closing(db.connect(store)) as c:
        rows = [dict(r) for r in c.execute(
            """SELECT * FROM source_parts WHERE scope = ? AND status = 'active' AND kind = 'meeting'
               AND confirmed_by IS NOT NULL AND state != 'applied'""", (scope,))]
    for part in rows:
        new_items = extracted_items(store, part, prompt_version)
        if new_items is None:
            continue  # 아직 다시 추출 전
        with closing(db.connect(store)) as c:
            old = c.execute("SELECT items_json FROM part_extracts WHERE content_hash = ? AND prompt_version = ?",
                            (part["applied_hash"] or "", prompt_version)).fetchone()
        seen = {(i["kind"], db.normalize_text(i["title"])) for i in json.loads(old["items_json"])} if old else set()
        fresh = [i for i in new_items if (i["kind"], db.normalize_text(i["title"])) not in seen]
        summary = apply_part(store, scope, part["id"], AUTO_ACTOR, prompt_version, fresh, judge)
        if summary["results"]:
            changed.append({"part_id": part["id"], "path": part["path"], **summary})
    return changed


# ── 상태 전이 ────────────────────────────────────────────

def mark_extracted(store: Path, part_ids: list[str]) -> None:
    with db.write(store) as c:
        for part_id in part_ids:
            c.execute("UPDATE source_parts SET state = 'extracted' WHERE id = ? AND state = 'collected'", (part_id,))


def promote_due(store: Path, scope: str, now: datetime | None = None) -> int:
    """자동 감지된 새 회의록: 마지막 수정 후 2시간이 지난 추출 완료 부분을 확인 대기로 올린다."""
    limit = ((now or datetime.now().astimezone()) - SETTLE).astimezone().isoformat()
    with db.write(store) as c:
        rows = [dict(r) for r in c.execute(
            """SELECT id, last_edited FROM source_parts WHERE scope = ? AND status = 'active' AND kind = 'meeting'
               AND state = 'extracted' AND confirmed_by IS NULL""", (scope,))]
        due = [r["id"] for r in rows if r["last_edited"] and _parse(r["last_edited"]) <= _parse(limit)]
        for part_id in due:
            c.execute("UPDATE source_parts SET state = 'pending_confirm' WHERE id = ?", (part_id,))
    return len(due)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def request_confirm(store: Path, scope: str, page_id: str) -> None:
    """사람이 [반영]을 눌러 직접 요청한 페이지: 2시간을 기다리지 않고 확인 대기로 올린다."""
    with db.write(store) as c:
        c.execute("""UPDATE source_parts SET state = 'pending_confirm' WHERE scope = ? AND page_id = ? AND kind = 'meeting'
                     AND status = 'active' AND state IN ('collected', 'extracted')""", (scope, page_id))


def skip_page(store: Path, scope: str, page_id: str, actor: str) -> int:
    with db.write(store) as c:
        return c.execute("""UPDATE source_parts SET state = 'skipped', confirmed_by = NULL, updated_at = ?
                            WHERE scope = ? AND page_id = ? AND kind = 'meeting' AND status = 'active'
                            AND state IN ('collected', 'extracted', 'pending_confirm')""",
                         (db.now(), scope, page_id)).rowcount


def pending_pages(store: Path, scope: str) -> list[dict[str, Any]]:
    """확인 대기 중인 회의록 페이지: page_id, 제목, 부분 수, 마지막 수정."""
    with closing(db.connect(store)) as c:
        rows = c.execute(
            """SELECT page_id, MIN(path) AS path, COUNT(*) AS n, MAX(last_edited) AS last_edited FROM source_parts
               WHERE scope = ? AND status = 'active' AND kind = 'meeting' AND state = 'pending_confirm'
               GROUP BY page_id ORDER BY last_edited DESC""", (scope,)).fetchall()
    return [{"page_id": r["page_id"], "title": r["path"].rsplit(" > ", 2)[-2] if " > " in r["path"] else r["path"],
             "parts": r["n"], "last_edited": r["last_edited"]} for r in rows]


def recent_pages(store: Path, scope: str, limit: int = 10) -> list[dict[str, Any]]:
    """4번 ② 고를 수 있는 회의록 페이지: 회의 메모가 있는 페이지, 최근 수정순. 상태(반영됨 여부) 포함."""
    with closing(db.connect(store)) as c:
        rows = c.execute(
            """SELECT page_id, MIN(path) AS path, MAX(last_edited) AS last_edited,
                      SUM(state = 'applied') AS applied, COUNT(*) AS n FROM source_parts
               WHERE scope = ? AND status = 'active' AND kind = 'meeting' GROUP BY page_id
               ORDER BY last_edited DESC LIMIT ?""", (scope, limit)).fetchall()
    return [{"page_id": r["page_id"], "title": r["path"].rsplit(" > ", 2)[-2] if " > " in r["path"] else r["path"],
             "last_edited": r["last_edited"], "applied": r["applied"] == r["n"]} for r in rows]


def preview(store: Path, scope: str, page_id: str, prompt_version: str) -> dict[str, Any]:
    """미리보기: 페이지의 회의 메모 부분별 추출 항목(번호를 매긴 평평한 목록)."""
    entries: list[dict[str, Any]] = []
    missing = 0
    for part in meeting_parts(store, scope, page_id):
        items = extracted_items(store, part, prompt_version)
        if items is None:
            missing += 1
            continue
        entries += [dict(item, part_id=part["id"]) for item in items]
    return {"items": entries, "not_extracted": missing}


# ── 되돌리기 ─────────────────────────────────────────────

def _restore(c: Any, item_id: str, reason: str, actor: str) -> None:
    current = c.execute("SELECT version FROM work_items WHERE id = ?", (item_id,)).fetchone()
    before = c.execute("SELECT snapshot_json FROM revisions WHERE object_type = 'work_item' AND object_id = ? AND version = ?",
                       (item_id, current["version"] - 1)).fetchone()
    if before is None:
        return
    snapshot = json.loads(before["snapshot_json"])
    core._write_item(c, item_id, {"statement_md": snapshot["statement_md"], "status": snapshot["status"],
                                  "attributes_json": snapshot["attributes"]}, reason, actor, db.now())


def revert(store: Path, candidate_id: str, actor: str) -> dict[str, Any]:
    """반영 하나를 되돌린다(권한자만, 호출부가 확인). 이전 스냅샷으로 돌리고 그 반영이 만든 링크를 지운다."""
    db.initialize(store)
    reason = f"반영 {candidate_id} 되돌림({actor})"
    with db.write(store) as c:
        cand = core._candidate(c, candidate_id)
        if cand["status"] not in APPLIED:
            raise ConflictError(f"반영 {candidate_id}은 되돌릴 수 없는 상태({cand['status']})")
        change_type, item_id, target_id = cand["change_type"], cand["applied_item_id"], cand["target_id"]
        if change_type == "proposed":
            core._write_item(c, item_id, {"status": "reverted"}, reason, actor, db.now())
        elif change_type == "superseding" or (change_type == "conflicting" and cand["item_kind"] == "decision"):
            core._write_item(c, item_id, {"status": "reverted"}, reason, actor, db.now())
            _restore(c, target_id, reason, actor)
        else:
            _restore(c, item_id, reason, actor)
        c.execute("DELETE FROM work_links WHERE candidate_id = ?", (candidate_id,))
        c.execute("""UPDATE change_candidates SET status = 'reverted', decided_by = ?, decided_at = ?, updated_at = ?
                     WHERE id = ?""", (actor, db.now(), db.now(), candidate_id))
    return {"candidate_id": candidate_id, "change_type": change_type, "item_id": item_id}


def recent_applied(store: Path, scope: str, limit: int = 10) -> list[dict[str, Any]]:
    """4번 ③ 최근 반영 내역: 자동 반영된 것, 최근 것부터."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        rows = c.execute(
            """SELECT cc.id, cc.change_type, cc.decided_by, cc.decided_at, w.title, w.kind, w.status,
                      t.title AS target_title FROM change_candidates cc
               JOIN work_items w ON w.id = cc.applied_item_id LEFT JOIN work_items t ON t.id = cc.target_id
               WHERE cc.scope = ? AND cc.status = 'auto_applied' ORDER BY cc.decided_at DESC, cc.rowid DESC LIMIT ?""",
            (scope, limit)).fetchall()
    return [dict(r) for r in rows]


def auto_applied_count(store: Path, scope: str, hours: int = 24, now: datetime | None = None) -> int:
    """확인된 회의록이 수정돼 자동으로 반영된 항목 수(최근 hours시간). 메인 카드 "반영 n건" 알림."""
    since = ((now or datetime.now().astimezone()) - timedelta(hours=hours)).astimezone()
    db.initialize(store)
    with closing(db.connect(store)) as c:
        rows = c.execute("""SELECT decided_at FROM change_candidates WHERE scope = ? AND status = 'auto_applied'
                            AND decided_by = ?""", (scope, AUTO_ACTOR)).fetchall()
    return sum(1 for r in rows if r["decided_at"] and _parse(r["decided_at"]) >= since)


def gone_confirmed_parts(store: Path, scope: str) -> list[dict[str, Any]]:
    """확인·반영된 뒤 Notion에서 사라진 회의록 부분: 거기서 나온 항목을 지울지 권한자에게 묻는다."""
    with closing(db.connect(store)) as c:
        return [dict(r) for r in c.execute(
            """SELECT id, path FROM source_parts WHERE scope = ? AND status = 'gone' AND kind = 'meeting'
               AND confirmed_by IS NOT NULL AND state = 'applied'""", (scope,))]


def resolve_gone_part(store: Path, scope: str, part_id: str, remove_items: bool, actor: str) -> int:
    """사라진 부분 처리: remove_items면 그 부분에서 나온 항목을 취소(cancelled)한다. 취소한 항목 수."""
    removed = 0
    with db.write(store) as c:
        if remove_items:
            for row in c.execute("SELECT id, attributes_json FROM work_items WHERE scope = ? AND status NOT IN "
                                 "('superseded', 'reverted', 'cancelled')", (scope,)).fetchall():
                if part_id in (json.loads(row["attributes_json"]).get("part_ids") or []):
                    core._write_item(c, row["id"], {"status": "cancelled"}, f"원문 부분 {part_id} 삭제 확인", actor, db.now())
                    removed += 1
        c.execute("UPDATE source_parts SET state = ? WHERE id = ?", ("deleted" if remove_items else "kept", part_id))
    return removed


# ── [고치기]와 정답지 ─────────────────────────────────────

def save_correction(store: Path, part: dict, prompt_version: str, before: list[dict], after: list[dict], actor: str) -> str:
    correction_id = core.new_id()
    with db.write(store) as c:
        c.execute("""INSERT INTO extract_corrections(id, part_id, content_hash, prompt_version, before_json, after_json,
                         actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                  (correction_id, part["id"], parts.extract_hash(part), prompt_version, db.to_json(before),
                   db.to_json(after), actor, db.now()))
    return correction_id


def _match(a: dict, b: dict) -> bool:
    if a["kind"] != b["kind"]:
        return False
    if db.normalize_text(a["title"]) == db.normalize_text(b["title"]):
        return True
    qa, qb = parts._squash(a.get("quote") or ""), parts._squash(b.get("quote") or "")
    return bool(qa and qb and (qa in qb or qb in qa))


def extract_eval(store: Path, scope: str, prompt_version: str) -> dict[str, Any]:
    """정답지(부분마다 가장 최근에 고친 목록)와 지금 추출을 비교한다. 빠진 것·틀린 것(정답에 없는 것) 수와 목록."""
    with closing(db.connect(store)) as c:
        truths = {}
        for row in c.execute("SELECT part_id, after_json FROM extract_corrections ORDER BY created_at"):
            truths[row["part_id"]] = json.loads(row["after_json"])
        rows = {r["id"]: dict(r) for r in c.execute("SELECT * FROM source_parts WHERE scope = ?", (scope,))}
    report = {"parts": 0, "missing": [], "wrong": []}
    for part_id, truth in truths.items():
        part = rows.get(part_id)
        if part is None:
            continue
        current = extracted_items(store, part, prompt_version) or []
        report["parts"] += 1
        report["missing"] += [dict(t, path=part["path"]) for t in truth if not any(_match(t, x) for x in current)]
        report["wrong"] += [dict(x, path=part["path"]) for x in current if not any(_match(x, t) for t in truth)]
    report["missing_count"], report["wrong_count"] = len(report["missing"]), len(report["wrong"])
    return report



def live_items(store: Path, scope: str) -> dict[str, list[dict[str, Any]]]:
    """메뉴용 반영된 항목(종류별, 최근 것 먼저). needs_discussion은 따로 모은다. 담당은 attributes.assignee."""
    db.initialize(store)
    out: dict[str, list[dict[str, Any]]] = {}
    with closing(db.connect(store)) as c:
        for row in c.execute(
                """SELECT * FROM work_items WHERE scope = ? AND status NOT IN ('superseded', 'reverted', 'cancelled', 'done')
                   ORDER BY updated_at DESC, rowid DESC""", (scope,)):
            item = dict(row)
            attributes = json.loads(item.pop("attributes_json"))
            item.update(assignee=attributes.get("assignee"), due=attributes.get("due"), attributes=attributes)
            key = "needs_discussion" if item["status"] == "needs_discussion" else item["kind"]
            out.setdefault(key, []).append(item)
    return out


def take_task(store: Path, scope: str, item_id: str, assignee: str, actor: str) -> dict[str, Any]:
    """3번 [내가 맡기]: 할 일의 담당을 바꾸고 revisions에 남긴다(spec P2-7, A30). 바뀐 항목(이슈 번호 포함)."""
    with db.write(store) as c:
        row = c.execute("SELECT * FROM work_items WHERE id = ? AND scope = ?", (item_id, scope)).fetchone()
        if row is None or row["kind"] != "task":
            raise NotFoundError(f"할 일 {item_id} 없음")
        if row["status"] in ("superseded", "reverted", "cancelled", "done"):
            raise ConflictError("이미 끝났거나 되돌린 할 일이에요")
        attributes = json.loads(row["attributes_json"])
        if attributes.get("assignee") and attributes["assignee"] != assignee:
            raise ConflictError(f"이미 {attributes['assignee']}님이 맡았어요")
        attributes["assignee"] = assignee
        core._write_item(c, item_id, {"attributes_json": attributes}, "내가 맡기", actor, db.now())
    return {"id": item_id, "title": row["title"], "assignee": assignee, "issue_refs": attributes.get("issue_refs") or []}


STALE_DAYS = 3  # [가정] 확인 대기가 이보다 오래 쌓이면 관리자에게 한 번 더 알린다(spec P2-5)


def stale_pending_once(store: Path, scope: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """3일 넘게 확인을 기다리는 회의록 중 아직 관리자에게 알리지 않은 것. 돌려준 것은 알린 것으로 남긴다."""
    limit = (now or datetime.now().astimezone()) - timedelta(days=STALE_DAYS)
    stale = [p for p in pending_pages(store, scope) if p["last_edited"] and _parse(p["last_edited"]) <= limit]
    if not stale:
        return []
    with db.write(store) as c:
        told = {r["key"] for r in c.execute(
            "SELECT key FROM prompts_asked WHERE scope = ? AND kind = 'pending_reminder'", (scope,))}
        fresh = [p for p in stale if p["page_id"] not in told]
        for page in fresh:
            c.execute("""INSERT INTO prompts_asked(scope, kind, key, answer, asked_at, answered_at)
                         VALUES (?, 'pending_reminder', ?, 'shown', ?, ?)""", (scope, page["page_id"], db.now(), db.now()))
    return fresh
