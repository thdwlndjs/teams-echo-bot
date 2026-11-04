"""단계 길잡이: 게이트 충족 판정, 논의 중인 단계, 단계 제안과 답. 문서: docs/v1.1/spec.md P2-3, P2-4.

- 단계는 권한자의 답(answer)으로만 바뀐다(A28). 판정은 제안만 만든다.
- 게이트 기본값은 stage_gates의 scope '*' 행(마이그레이션 8). 팀이 고치면 그 팀의 행이 생긴다.
- 사람이 정한 게이트 상태(later·skipped)는 stage_gates에, 충족 여부는 판정 결과(team_stages.assessment_json)에 둔다.
- 같은 질문(종류·단계·게이트·그때의 충족 근거)은 prompts_asked에 답이 있으면 다시 하지 않는다(A16).
- 기본값·판정 규칙은 정답지가 없어 가정이다(docs/v1.1/plan.md "가정으로 진행하는 것의 수정 절차").
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from . import apply, db, parts

log = logging.getLogger("stage_judge")

STAGES = [("1-1", "문제 탐색"), ("1-2", "방향 확정"), ("1-3", "구체화·타당성"), ("2-1", "VOC 설계"), ("2-2", "VOC 수집"),
          ("2-3", "VOC 분석·반영"), ("3-1", "백로그"), ("3-2", "설계"), ("3-3", "스프린트 반복"), ("3-4", "릴리스·검증")]
STAGE_NAMES = dict(STAGES)
ORDER = [s for s, _ in STAGES]
CODE_GATES = {"voc_collected", "backlog", "milestones"}  # 코드로 판정(LLM 안 씀)
MAX_ITEMS = 80  # 판정에 보내는 최근 항목 수
MAX_DOCS = 10  # 함께 보내는 문서 부분 수(기획서 등). 한 줄 설명 + 앞부분
DOC_HEAD_CHARS = 200
STATEMENT_CHARS = 120
BUDGET_TOKENS = 5000  # [가정] 판정 한 번 입력 상한(추정). Groq 무료 요청 한도(413)를 넘지 않게
KIND_ORDER = {"decision": 0, "open": 1, "task": 2, "insight": 3, "document": 4, "needs_discussion": 5, "feedback": 6}
StageJudge = Callable[[dict], dict]


def label(stage: str | None) -> str:
    return f"{stage} {STAGE_NAMES.get(stage or '', '')}".strip() if stage else "시작 전"


def _row(store: Path, scope: str) -> dict[str, Any] | None:
    db.initialize(store)
    with closing(db.connect(store)) as c:
        row = c.execute("SELECT * FROM team_stages WHERE scope = ?", (scope,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["history"] = json.loads(out.pop("history_json") or "[]")
    out["assessment"] = json.loads(out.pop("assessment_json") or "null")
    return out


def current(store: Path, scope: str) -> str | None:
    """승인된 현재 단계. 아직 정하지 않았으면 None(첫 제안에서 권한자가 정한다)."""
    row = _row(store, scope)
    return row["stage"] if row and row["stage"] != "-" else None


def assessment(store: Path, scope: str) -> dict[str, Any] | None:
    row = _row(store, scope)
    return row["assessment"] if row else None


def gates(store: Path, scope: str, stage: str | None = None) -> list[dict[str, Any]]:
    """그 팀의 게이트(공통 기본값 + 팀이 고친 것). removed는 뺀다. 단계 순서대로."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM stage_gates WHERE scope IN ('*', ?)", (scope,))]
    merged: dict[tuple[str, str], dict] = {}
    for row in sorted(rows, key=lambda r: r["scope"] != "*"):  # 공통 먼저, 팀 것이 덮는다
        merged[(row["stage"], row["gate_id"])] = row
    out = [g for g in merged.values() if g["state"] != "removed" and (stage is None or g["stage"] == stage)]
    return sorted(out, key=lambda g: (ORDER.index(g["stage"]) if g["stage"] in ORDER else 99, g["gate_id"]))


def set_gate_state(store: Path, scope: str, stage: str, gate_id: str, state: str, actor: str) -> None:
    template = {(g["stage"], g["gate_id"]): g for g in gates(store, scope)}.get((stage, gate_id))
    if template is None:
        raise db.NotFoundError(f"게이트 {stage} {gate_id} 없음")
    with db.write(store) as c:
        c.execute("""INSERT INTO stage_gates(scope, stage, gate_id, text, state, edited_by, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(scope, stage, gate_id) DO UPDATE SET state = excluded.state,
                         edited_by = excluded.edited_by, updated_at = excluded.updated_at""",
                  (scope, stage, gate_id, template["text"], state, actor, db.now()))


def edit_gate(store: Path, scope: str, stage: str, gate_id: str, text: str | None, actor: str) -> None:
    """권한자가 팀 게이트를 더하거나(새 gate_id) 고치거나(text) 뺀다(text=None → removed)."""
    with db.write(store) as c:
        c.execute("""INSERT INTO stage_gates(scope, stage, gate_id, text, state, edited_by, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(scope, stage, gate_id) DO UPDATE SET text = excluded.text, state = excluded.state,
                         edited_by = excluded.edited_by, updated_at = excluded.updated_at""",
                  (scope, stage, gate_id, text or "(뺌)", "open" if text else "removed", actor, db.now()))


# ── 판정 ────────────────────────────────────────────────

def snapshot(store: Path, scope: str) -> tuple[list[dict[str, Any]], str]:
    """판정 입력: 반영된 항목(MAX_ITEMS개)과 문서 부분(기획서 등, 한 줄 설명과 앞부분)과 그 지문.
    문서는 회의록이 아니라 추출·반영하지 않지만, 주제 후보·현재 상태처럼 단계를 가르는 내용이 문서에 있다.
    문서 근거 ID는 'doc:<부분 ID>'."""
    live = apply.live_items(store, scope)
    items = [{"id": i["id"], "kind": i["kind"], "title": i["title"],
              "statement": (i.get("statement_md") or "")[:STATEMENT_CHARS]}
             for group in live.values() for i in group]
    with closing(db.connect(store)) as c:
        docs = [dict(r) for r in c.execute(
            """SELECT id, path, summary, md FROM source_parts WHERE scope = ? AND status = 'active' AND kind = 'document'
               ORDER BY chars DESC LIMIT ?""", (scope, MAX_DOCS))]
    items += [{"id": f"doc:{d['id']}", "kind": "document", "title": d["path"].rsplit(" > ", 1)[-1],
               "statement": " / ".join(filter(None, [d["summary"], parts.md_to_text(d["md"])[:DOC_HEAD_CHARS]]))}
              for d in docs]
    # 예산 안에서 단계를 가르는 종류부터(결정·미정·할 일·VOC·문서), 멘토 피드백은 남는 만큼
    items.sort(key=lambda i: (KIND_ORDER.get(i["kind"], 9), i["id"]))
    kept: list[dict] = []
    for item in items:
        if len(kept) >= MAX_ITEMS or parts.estimate_tokens(kept + [item]) > BUDGET_TOKENS:
            break
        kept.append(item)
    items = sorted(kept, key=lambda i: i["id"])
    return items, db.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True))


def _code_gate(gate_id: str, items: list[dict], github: dict) -> dict[str, Any]:
    if gate_id == "voc_collected":
        found = [i["id"] for i in items if i["kind"] == "insight"]
        return {"met": bool(found), "evidence": found[:5], "by": "code"}
    if gate_id == "backlog":
        return {"met": github.get("open_issues", 0) > 0, "evidence": ["github:open_issues"] if github.get("open_issues") else [],
                "by": "code"}
    met = github.get("milestones", 0) > 0 and github.get("closed_issues", 0) > 0
    return {"met": met, "evidence": ["github:milestones"] if met else [], "by": "code"}


def judge(store: Path, scope: str, items: list[dict], github: dict, llm: StageJudge | None) -> dict[str, Any]:
    """게이트별 충족과 논의 중인 단계. 의미 판정은 LLM 한 번(대기열 순위 3). 근거 항목 ID가 없는 참은 받지 않는다."""
    all_gates = gates(store, scope)
    ids = {i["id"] for i in items}
    semantic = [g for g in all_gates if g["gate_id"] not in CODE_GATES]
    answer: dict[str, Any] = {}
    if llm is not None and items and semantic:
        answer = llm({"stages": [{"stage": s, "name": n} for s, n in STAGES],
                      "gates": [{"stage": g["stage"], "gate_id": g["gate_id"], "text": g["text"]} for g in semantic],
                      "items": items}) or {}
    raw = answer.get("gates") or {}
    result: dict[str, dict] = {}
    for gate in all_gates:
        if gate["gate_id"] in CODE_GATES:
            result[gate["gate_id"]] = _code_gate(gate["gate_id"], items, github)
            continue
        found = raw.get(gate["gate_id"]) or {}
        evidence = [e for e in found.get("evidence") or [] if e in ids]
        result[gate["gate_id"]] = {"met": bool(found.get("met")) and bool(evidence), "evidence": evidence, "by": "llm"}
    discussing = answer.get("discussing") or {}
    d_stage = discussing.get("stage") if discussing.get("stage") in ORDER else None
    d_evidence = [e for e in discussing.get("evidence") or [] if e in ids]
    overturned = answer.get("overturned") or None
    if overturned and not [e for e in overturned.get("evidence") or [] if e in ids]:
        overturned = None
    out = {"gates": result, "discussing": {"stage": d_stage if d_evidence else None, "evidence": d_evidence},
           "overturned": overturned, "judged": llm is not None and bool(items)}
    log.info("stage_judge scope=%s items=%s result=%s", scope, json.dumps(sorted(ids)),
             json.dumps({"gates": {k: [v["met"], v["evidence"]] for k, v in result.items()},
                         "discussing": out["discussing"], "overturned": bool(overturned)}, ensure_ascii=False))
    return out


def assess(store: Path, scope: str, github: dict, llm: StageJudge | None, force: bool = False) -> dict[str, Any] | None:
    """반영된 항목이 바뀌었을 때만 다시 판정해 저장한다. 판정 결과(바뀌지 않았으면 None)."""
    items, digest = snapshot(store, scope)
    row = _row(store, scope)
    key = f"{digest}:{json.dumps(github, sort_keys=True)}"
    if row and row["assessed_hash"] == key and not force:
        return None
    found = judge(store, scope, items, github, llm)
    with db.write(store) as c:
        c.execute("""INSERT INTO team_stages(scope, stage, assessment_json, assessed_hash) VALUES (?, '-', ?, ?)
                     ON CONFLICT(scope) DO UPDATE SET assessment_json = excluded.assessment_json,
                         assessed_hash = excluded.assessed_hash""",
                  (scope, json.dumps(found, ensure_ascii=False), key if found["judged"] else None))
    return found


# ── 제안과 답 ───────────────────────────────────────────

def _met(found: dict | None, gate_id: str) -> bool:
    return bool(found and (found["gates"].get(gate_id) or {}).get("met"))


def _done(gate: dict, found: dict | None) -> bool:
    return gate["state"] in ("later", "skipped") or _met(found, gate["gate_id"])


def first_open_stage(store: Path, scope: str, found: dict | None = None) -> str:
    """게이트가 다 끝나지 않은 첫 단계(판정상 지금 있어야 할 곳)."""
    found = found if found is not None else assessment(store, scope)
    for stage in ORDER:
        if any(not _done(g, found) for g in gates(store, scope, stage)):
            return stage
    return ORDER[-1]


def _key(*parts: Any) -> str:
    return "|".join(str(p) for p in parts)


def _evidence_key(found: dict | None, gate_id: str) -> str:
    gate = (found or {}).get("gates", {}).get(gate_id) or {}
    return f"{'met' if gate.get('met') else 'open'}:{','.join(sorted(gate.get('evidence') or []))}"


def _gate_text(store: Path, scope: str, stage: str, gate_id: str) -> str:
    return next((g["text"] for g in gates(store, scope, stage) if g["gate_id"] == gate_id), gate_id)


def proposals(store: Path, scope: str) -> list[dict[str, Any]]:
    """지금 물어볼 단계 제안(답한 것은 뺀다). 각 {kind, key, text, answers: [(답, 버튼 글)]}. 판정 전이면 빈 목록."""
    found = assessment(store, scope)
    if not found or not found.get("judged"):
        return []
    now = current(store, scope)
    target = first_open_stage(store, scope, found)
    out: list[dict[str, Any]] = []
    if now is None:
        missing = [g["text"] for g in gates(store, scope, target) if not _done(g, found)]
        ahead = found["discussing"].get("stage")
        answers = [("start_here", "맞아요"), ("start_first", "1-1부터")]
        note = ""
        if ahead and ORDER.index(ahead) > ORDER.index(target):  # 게이트는 비었는데 논의는 더 앞서 있다
            note = f" 회의록은 {label(ahead)} 내용까지 논의하고 있어요."
            answers.insert(1, ("start_discussing", f"{ahead}로"))
        out.append({"kind": "stage_init", "key": _key(target, ahead, *sorted(missing)), "stage": target, "ahead": ahead,
                    "text": f"게이트로 보면 지금 {label(target)} 단계예요" + (f" (남은 것: {', '.join(missing)})" if missing else "")
                            + f".{note} 어디서 시작할까요?",
                    "answers": answers})
    else:
        stage_gates = gates(store, scope, now)
        open_gates = [g for g in stage_gates if not _done(g, found)]
        if not open_gates and now != ORDER[-1]:
            out.append({"kind": "stage_advance", "key": _key(now, *[_evidence_key(found, g["gate_id"]) for g in stage_gates]),
                        "stage": now, "text": f"{label(now)}에서 정할 사안을 완료했어요. 다음 단계로 넘어갈까요?",
                        "answers": [("advance", "넘어갈게요"), ("stay", "아직이요")]})
        for gate in open_gates:
            out.append({"kind": "gate_missing", "key": _key(now, gate["gate_id"], _evidence_key(found, gate["gate_id"])),
                        "stage": now, "gate_id": gate["gate_id"],
                        "text": f"현재 {label(now)} 단계예요. '{gate['text']}'만 확정하고 넘어갈까요?",
                        "answers": [("decide", "정할게요"), ("later", "나중에")]})
        ahead = found["discussing"].get("stage")
        reach = ahead if ahead and ORDER.index(ahead) > ORDER.index(now) else now
        behind = [st for st in ORDER[:ORDER.index(now)]]  # 이미 지나온 단계(첫 제안에서 앞 단계로 건너뛴 경우 등)
        between = ORDER[ORDER.index(now) + 1:ORDER.index(reach)]  # 논의가 앞서 있어 건너뛴 단계
        if behind or between:
            for stage in behind + between:
                for gate in gates(store, scope, stage):
                    if not _done(gate, found):
                        out.append({"kind": "skipped_ahead", "key": _key(stage, gate["gate_id"], reach,
                                                                         _evidence_key(found, gate["gate_id"])),
                                    "stage": stage, "gate_id": gate["gate_id"],
                                    "text": f"이미 {label(reach)} 내용을 진행하고 있는데 '{gate['text']}'({label(stage)})가 없어요.",
                                    "answers": [("decide", "정할게요"), ("later", "나중에 할게요"), ("skip", "건너뛰기")]})
        turned = found.get("overturned")
        if turned and ORDER.index(now) >= ORDER.index("2-1"):
            assumption = str(turned.get("assumption") or "가정")[:60]
            out.append({"kind": "pivot", "key": _key(now, assumption, *sorted(turned.get("evidence") or [])), "stage": now,
                        "text": f"'{assumption}'이 VOC에서 뒤집혔어요. 기획 단계(1-3)로 돌아갈까요?",
                        "answers": [("pivot", "돌아갈게요"), ("stay", "아니요")]})
    answered = _answered(store, scope)
    return [p for p in out if (p["kind"], p["key"]) not in answered]


def _answered(store: Path, scope: str) -> set[tuple[str, str]]:
    with closing(db.connect(store)) as c:
        return {(r["kind"], r["key"]) for r in c.execute(
            "SELECT kind, key FROM prompts_asked WHERE scope = ? AND answer IS NOT NULL", (scope,))}


def mark_asked(store: Path, scope: str, found: list[dict]) -> None:
    with db.write(store) as c:
        for p in found:
            c.execute("INSERT OR IGNORE INTO prompts_asked(scope, kind, key, asked_at) VALUES (?, ?, ?, ?)",
                      (scope, p["kind"], p["key"], db.now()))


def _set_stage(c: Any, scope: str, stage: str, actor: str, reason: str) -> None:
    row = c.execute("SELECT stage, history_json FROM team_stages WHERE scope = ?", (scope,)).fetchone()
    history = json.loads(row["history_json"]) if row else []
    history.append({"from": row["stage"] if row and row["stage"] != "-" else None, "to": stage, "by": actor,
                    "at": db.now(), "reason": reason})
    c.execute("""INSERT INTO team_stages(scope, stage, approved_by, approved_at, history_json) VALUES (?, ?, ?, ?, ?)
                 ON CONFLICT(scope) DO UPDATE SET stage = excluded.stage, approved_by = excluded.approved_by,
                     approved_at = excluded.approved_at, history_json = excluded.history_json""",
              (scope, stage, actor, db.now(), json.dumps(history, ensure_ascii=False)))


def answer(store: Path, scope: str, kind: str, key: str, reply: str, actor: str) -> str:
    """권한자의 답을 반영한다(권한 확인은 부르는 쪽). 단계는 여기서만 바뀐다(A28). 사람에게 보일 결과 한 줄."""
    pending = {(p["kind"], p["key"]): p for p in proposals(store, scope)}
    prompt = pending.get((kind, key))
    if prompt is None:
        raise db.ConflictError("이미 답했거나 상황이 바뀐 질문이에요")
    if reply not in {a for a, _ in prompt["answers"]}:
        raise db.ValidationError(f"알 수 없는 답 {reply}")
    stage, gate_id = prompt["stage"], prompt.get("gate_id")
    message = "알겠어요."
    with db.write(store) as c:
        if reply == "start_here":
            _set_stage(c, scope, stage, actor, "첫 제안")
            message = f"현재 단계를 정했어요: {label(stage)}"
        elif reply == "start_discussing" and prompt.get("ahead"):
            _set_stage(c, scope, prompt["ahead"], actor, "첫 제안(논의 중인 단계)")
            message = f"현재 단계를 정했어요: {label(prompt['ahead'])}. 앞 단계의 빈 게이트는 따로 물어볼게요."
        elif reply == "start_first":
            _set_stage(c, scope, ORDER[0], actor, "첫 제안")
            message = f"현재 단계를 정했어요: {label(ORDER[0])}"
        elif reply == "advance":
            nxt = ORDER[ORDER.index(stage) + 1]
            _set_stage(c, scope, nxt, actor, "게이트 충족")
            message = f"다음 단계로 넘어갔어요: {label(nxt)}"
        elif reply == "pivot":
            _set_stage(c, scope, "1-3", actor, "VOC가 가정을 뒤집음")
            message = f"기획 단계로 돌아갔어요: {label('1-3')}"
        c.execute("""INSERT INTO prompts_asked(scope, kind, key, answer, answered_by, asked_at, answered_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(scope, kind, key) DO UPDATE SET answer = excluded.answer,
                         answered_by = excluded.answered_by, answered_at = excluded.answered_at""",
                  (scope, kind, key, reply, actor, db.now(), db.now()))
    if gate_id and reply in ("decide", "later", "skip"):
        set_gate_state(store, scope, stage, gate_id, {"decide": "open", "later": "later", "skip": "skipped"}[reply], actor)
        message = {"decide": f"'{_gate_text(store, scope, stage, gate_id)}'을 2번 메뉴 '지금 진행 중'에 올렸어요.",
                   "later": "나중에 다시 볼게요. 2번 메뉴 '이전 단계에서 남은 것'에 남겨 둘게요.",
                   "skip": "건너뛰었어요. 목록에서 뺐어요."}[reply]
    return message


def board(store: Path, scope: str) -> dict[str, Any]:
    """2번 메뉴용: 현재 단계, 지금 진행 중인 게이트, 이전 단계에서 남은 게이트."""
    found = assessment(store, scope)
    now = current(store, scope)
    shown = now or (first_open_stage(store, scope, found) if found and found.get("judged") else None)
    doing = [g for g in gates(store, scope, shown) if not _done(g, found)] if shown else []
    earlier = [g for g in gates(store, scope) if shown and ORDER.index(g["stage"]) < ORDER.index(shown)
               and g["state"] == "later"]
    return {"stage": now, "shown": shown, "approved": now is not None, "doing": doing, "earlier": earlier,
            "met": [g for g in gates(store, scope, shown) if _met(found, g["gate_id"])] if shown else []}
