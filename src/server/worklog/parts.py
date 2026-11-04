"""페이지·DB 행 md → 부분(part) 분할·블록 대조·종류 판정 저장. 문서: docs/v1.1/spec.md P1-2, P1-3.

1. `top_level_blocks(md)`: md를 최상위 블록 단위로 자른다. 중첩은 탭으로만 표시되고, 표 안의 줄과 들여쓰기
   없는 닫는 태그는 앞 블록에 속한다(실제 녹화본 5개가 블록 목록과 1:1로 맞은 규칙).
2. `group_parts(blocks)`: 빈 단락·구분선·제목 사이에 이어진 블록을 한 부분으로 묶는다.
3. `align_blocks(blocks, top_blocks)`: Notion 최상위 블록 목록과 개수·순서를 맞춰 블록 ID를 붙인다.
4. `save_unit_parts` → `judge_pending`: 새로 생기거나 바뀐 부분만 종류를 판정한다(LLM은 호출부가 넘긴다).
   판정할 때 "이 부분 안의 하위 토글은 문서다"라는 답을 받으면 그 토글을 따로 떼어 낸다.

이 모듈은 Notion·LLM을 직접 부르지 않는다(worklog 패키지는 외부 서비스에 의존하지 않는다).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db

KINDS = {"meeting", "document", "link"}
LOW_CONFIDENCE = 0.6  # [가정] 이보다 낮으면 문서로 두고 추출하지 않는다
CHILD_MIN_CHARS = 200  # 판정에 하위 토글로 알려 줄 최소 길이
HEAD_CHARS = 200

PartClassifier = Callable[[dict[str, Any]], dict[str, Any]]


# ── 1. 최상위 블록 ──────────────────────────────────────────

@dataclass
class RawBlock:
    position: int
    lines: list[str]

    @property
    def first(self) -> str:
        return self.lines[0]


def top_level_blocks(md: str) -> list[RawBlock]:
    blocks: list[RawBlock] = []
    in_table = in_code = False
    # splitlines()는   같은 문자에서도 끊으므로 쓰지 않는다.
    for line in md.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if in_table:
            blocks[-1].lines.append(line)
            in_table = not stripped.startswith("</table")
            continue
        if in_code:  # 코드 블록 안의 둘째 줄부터는 들여쓰기가 없어도 그 코드 블록에 속한다
            blocks[-1].lines.append(line)
            in_code = not stripped.startswith("```")
            continue
        # 최상위 토글은 `<details>`와 `<summary>…</summary>`가 둘 다 들여쓰기 없이 나온다. 둘이 한 블록이다.
        toggle_title = stripped.startswith("<summary") and bool(blocks) and blocks[-1].lines[-1].strip() == "<details>"
        if line and not line.startswith("\t") and not stripped.startswith("</") and not toggle_title:
            blocks.append(RawBlock(len(blocks), [line]))
        elif blocks:
            blocks[-1].lines.append(line)
        in_table = stripped.startswith("<table") and "</table" not in stripped and bool(blocks)
        in_code = stripped.startswith("```") and stripped.count("```") == 1 and bool(blocks)
    for block in blocks:  # 블록 사이의 빈 줄은 앞 블록 끝에 붙는다. 내용에 넣지 않는다.
        while len(block.lines) > 1 and not block.lines[-1].strip():
            block.lines.pop()
    return blocks


def line_kind(line: str) -> str:
    """최상위 줄 하나가 어떤 블록인지(md 표기 기준)."""
    text = line.strip()
    if text == "<empty-block/>":
        return "empty"
    if text == "---":
        return "divider"
    if re.match(r"#{1,6} ", text):
        return "heading"
    if text.startswith("<page "):
        return "page"
    if text.startswith("<database "):
        return "database"
    if text.startswith("<table"):
        return "table"
    if re.match(r"- \[[ xX]\] ", text):
        return "to_do"
    if re.match(r"\d+\. ", text):
        return "numbered"
    if text.startswith("- "):
        return "bulleted"
    return "other"


# ── 2. 부분으로 묶기 ────────────────────────────────────────

@dataclass
class RawPart:
    position: int
    title: str
    md: str
    block_positions: list[int] = field(default_factory=list)


def plain_title(line: str) -> str:
    text = re.split(r"<br\s*/?>", line.strip())[0]  # 여러 줄 단락은 첫 줄만
    text = re.sub(r"\s*\{[a-z_]+=\"[^\"]*\"\}\s*$", "", text)  # 색 등 블록 속성 {color="purple_bg"}
    text = re.sub(r"^(\d+\.|- \[[ xX]\]|-|#{1,6})\s+", "", text)
    text = re.sub(r"<[^>]+>", "", text).replace("**", "").replace("\\[", "[").replace("\\]", "]")
    return text.strip() or "(제목 없음)"


def group_parts(blocks: list[RawBlock]) -> list[RawPart]:
    groups: list[list[RawBlock]] = []
    current: list[RawBlock] | None = None
    for block in blocks:
        kind = line_kind(block.first)
        if kind == "empty" and current is not None and line_kind(current[0].first) == "heading":
            continue  # 제목으로 시작한 부분은 빈 단락에서 끝나지 않는다(문서는 제목 뒤에 빈 줄을 두고 본문을 쓴다)
        if kind in {"empty", "divider", "page", "database"}:
            current = None  # 경계. 하위 페이지·DB는 부분이 아니라 지도의 하위 항목이다.
            continue
        if kind == "heading" or current is None:
            current = [block]
            groups.append(current)
        else:
            current.append(block)
    return [RawPart(i, _block_title(g[0]), "\n".join(line for b in g for line in b.lines), [b.position for b in g])
            for i, g in enumerate(groups)]


def _block_title(block: RawBlock) -> str:
    """토글 블록이면 <summary>의 글자, 아니면 첫 줄."""
    if block.first.strip() == "<details>" and len(block.lines) > 1:
        return plain_title(block.lines[1])
    return plain_title(block.first)


# ── 3. 블록 대조 ────────────────────────────────────────────

_STRICT = {"divider": {"divider"}, "numbered_list_item": {"numbered"}, "bulleted_list_item": {"bulleted"},
           "to_do": {"to_do"}, "heading_1": {"heading"}, "heading_2": {"heading"}, "heading_3": {"heading"},
           "table": {"table"}, "child_page": {"page"}, "child_database": {"database"}}
_SEPARATORS = {"empty", "divider", "page", "database", "table"}


def _plain_block_text(block: dict) -> str:
    if "text" in block:  # 녹화본(fixture) 형식
        return block["text"]
    body = block.get(block.get("type", ""), {}) or {}
    return "".join(item.get("plain_text", "") for item in body.get("rich_text", []) or [])


def _matches(kind: str, block: dict) -> bool:
    block_type = block.get("type", "")
    if block_type in _STRICT:
        return kind in _STRICT[block_type]
    if block_type == "paragraph" and not _plain_block_text(block) and not block.get("has_children"):
        return kind == "empty"
    return kind not in _SEPARATORS  # 단락·콜아웃·파일·토글 등: 경계 표기만 아니면 된다


def align_blocks(blocks: list[RawBlock], top_blocks: list[dict]) -> list[str] | None:
    """블록 위치마다 Notion 블록 ID. 개수나 종류가 어긋나면 None(호출부가 ID 없이 저장한다)."""
    if len(blocks) != len(top_blocks):
        return None
    for block, notion_block in zip(blocks, top_blocks):
        if not _matches(line_kind(block.first), notion_block):
            return None
    return [b["id"] for b in top_blocks]


# ── 하위 토글과 텍스트 ──────────────────────────────────────

def details_children(md: str) -> list[dict[str, Any]]:
    """부분 안의 토글(<details>)들: 제목, 줄 범위, 길이. 깊이와 상관없이 위에서부터."""
    lines = md.split("\n")
    found: list[dict[str, Any]] = []
    for start, line in enumerate(lines):
        opened = re.fullmatch(r"(\t*)<details>\s*", line)
        if not opened or start + 1 >= len(lines):
            continue
        summary = re.fullmatch(r"\t*<summary>(.*?)</summary>\s*", lines[start + 1])
        if not summary:
            continue
        end = next((i for i in range(start + 2, len(lines)) if lines[i] == f"{opened.group(1)}</details>"), len(lines) - 1)
        found.append({"title": plain_title(summary.group(1)), "start": start, "end": end,
                      "chars": len("\n".join(lines[start:end + 1]))})
    return found


def md_to_text(md: str) -> str:
    """md → 사람이 읽는 글(저장·요약용). 구조 태그는 빼고 내용은 남긴다."""
    out: list[str] = []
    for line in md.replace("\r\n", "\n").split("\n"):
        indent = line[: len(line) - len(line.lstrip("\t"))]
        text = line.strip()
        if not text or re.fullmatch(r"</?(details|tr|table|synced_block|callout|columns|column)\b[^>]*>|<empty-block/>", text):
            continue
        text = re.sub(r"</?(summary|td|th)>", " ", text).replace("<br>", " / ")
        text = re.sub(r"<[^>]+>", "", text).replace("\\[", "[").replace("\\]", "]").replace("\\>", ">").strip()
        if text:
            out.append(indent + text)
    return "\n".join(out)


# ── 4. 저장과 판정 ──────────────────────────────────────────

def build_unit_parts(unit_id: str, path: str, md: str, top_blocks: list[dict] | None) -> list[dict[str, Any]]:
    """한 페이지(또는 DB 행)의 부분 목록. top_blocks가 있으면 블록 ID를 붙인다(어긋나면 ID 없이)."""
    blocks = top_level_blocks(md)
    ids = align_blocks(blocks, top_blocks) if top_blocks is not None else None
    built = []
    for group in group_parts(blocks):
        block_ids = [ids[p] for p in group.block_positions] if ids else None
        built.append({"id": f"{unit_id}:{block_ids[0]}" if block_ids else f"{unit_id}:p{group.position}",
                      "path": f"{path} > {group.title}", "position": group.position, "md": group.md,
                      "content_hash": db.sha256(group.md), "block_ids": block_ids})
    return built


def blocks_aligned(md: str, top_blocks: list[dict] | None) -> bool:
    return top_blocks is not None and align_blocks(top_level_blocks(md), top_blocks) is not None


def save_unit_parts(store: Path, scope: str, origin: str, page_id: str, section_id: str | None,
                    built: list[dict[str, Any]], last_edited: str | None = None) -> list[str]:
    """부분을 저장하고, 새로 생기거나 내용이 바뀐 부분 ID를 돌려준다(판정 대상).
    사람이 정한 종류는 내용이 바뀌어도 유지한다. 사라진 부분은 gone으로만 표시한다."""
    db.initialize(store)
    ts = db.now()
    changed: list[str] = []
    with db.write(store) as c:
        existing = {r["id"]: r for r in c.execute(
            "SELECT * FROM source_parts WHERE scope = ? AND page_id = ? AND parent_part_id IS NULL", (scope, page_id))}
        # 블록 대조에 실패해 ID가 위치 순번이 된 부분은, 같은 경로(제목)의 기존 부분 ID를 그대로 쓴다.
        # 그러지 않으면 대조가 한 번 어긋날 때마다 모든 부분이 "사라졌다가 새로 생긴" 것이 된다.
        by_path = {r["path"]: r["id"] for r in existing.values() if r["status"] == "active"}
        taken: set[str] = set()
        for part in built:
            if not part["block_ids"] and part["id"] not in existing and by_path.get(part["path"]) not in taken | {None}:
                part["id"] = by_path[part["path"]]
            taken.add(part["id"])
        for part in built:
            old = existing.get(part["id"])
            if old is not None and old["content_hash"] == part["content_hash"] and old["status"] == "active":
                continue
            human = old is not None and old["kind_source"] == "human"
            c.execute("UPDATE source_parts SET status = 'gone', updated_at = ? WHERE parent_part_id = ?", (ts, part["id"]))
            c.execute(
                """INSERT INTO source_parts(id, scope, origin, page_id, section_id, block_ids_json, parent_part_id, path,
                       position, kind, kind_source, kind_note, summary, md, content_hash, chars, state, status,
                       created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'collected', 'active', ?, ?)
                   ON CONFLICT(id) DO UPDATE SET section_id = excluded.section_id, block_ids_json = excluded.block_ids_json,
                       path = excluded.path, position = excluded.position, kind = excluded.kind,
                       kind_source = excluded.kind_source, kind_note = excluded.kind_note, summary = NULL,
                       md = excluded.md, content_hash = excluded.content_hash, chars = excluded.chars,
                       state = 'collected', status = 'active', updated_at = excluded.updated_at""",
                (part["id"], scope, origin, page_id, section_id,
                 json.dumps(part["block_ids"]) if part["block_ids"] else None, part["path"], part["position"],
                 old["kind"] if human else None, "human" if human else None, old["kind_note"] if human else None,
                 part["md"], part["content_hash"], len(part["md"]), ts, ts))
            changed.append(part["id"])
        if last_edited:  # 판정 순서(최근 수정 먼저)에 쓴다
            c.execute("UPDATE source_parts SET last_edited = ? WHERE scope = ? AND page_id = ?", (last_edited, scope, page_id))
        keep = {p["id"] for p in built}
        for part_id in set(existing) - keep:
            c.execute("UPDATE source_parts SET status = 'gone', updated_at = ? WHERE id = ? OR parent_part_id = ?",
                      (ts, part_id, part_id))
    return changed


def _remove_ranges(md: str, ranges: list[tuple[int, int]]) -> str:
    lines = md.split("\n")
    for start, end in sorted(ranges, reverse=True):
        del lines[start:end + 1]
    return "\n".join(lines)


JUDGE_BUDGET_TOKENS = 4000  # [가정] 동기화 한 번에 판정에 쓰는 토큰(추정). 메뉴가 쓸 분당 한도를 남긴다
PROMPT_TOKENS = 450  # 판정 지시문 몫(추정)
RETRIES = 3  # 쉬어 가는 모드(reindex)에서 한도 초과 뒤 다시 시도하는 횟수


def estimate_tokens(payload: Any) -> int:
    """대략적인 토큰 수(한글 1자 ≈ 0.75토큰, JSON 기호 포함). 예산을 넘지 않게 나누는 데만 쓴다."""
    return len(json.dumps(payload, ensure_ascii=False)) * 3 // 4 + PROMPT_TOKENS


def _judge_item(row: dict) -> dict[str, Any]:
    return {"part_id": row["id"], "path": row["path"], "title": row["path"].rsplit(" > ", 1)[-1], "chars": row["chars"],
            "head": md_to_text(row["md"])[:HEAD_CHARS],
            "children": [{"child_path": f"{row['path']} > {child['title']}", "chars": child["chars"], "toggle": True}
                         for child in details_children(row["md"]) if child["chars"] >= CHILD_MIN_CHARS]}


PAGE_OUTLINE_TITLES = 15


def _page_info(store: Path, scope: str, page_id: str | None, page_path: str) -> dict[str, Any]:
    """판정에 같이 보내는 페이지 전체 모습: 제목, 전체 길이, 부분 수, 부분 제목 목차.
    부분 앞 200자만 보면 큰 기획 문서의 제목 단위 부분을 회의 메모로 잘못 볼 수 있다(Phase 2 실측)."""
    with closing(db.connect(store)) as c:
        rows = c.execute("""SELECT path, chars FROM source_parts WHERE scope = ? AND page_id = ? AND status = 'active'
                            AND parent_part_id IS NULL ORDER BY position""", (scope, page_id)).fetchall()
    return {"title": page_path.rsplit(" > ", 1)[-1], "chars": sum(r["chars"] for r in rows), "part_count": len(rows),
            "outline": [r["path"].rsplit(" > ", 1)[-1][:40] for r in rows[:PAGE_OUTLINE_TITLES]]}


def judge_pending(store: Path, scope: str, classify: PartClassifier | None,
                  budget_tokens: int | None = JUDGE_BUDGET_TOKENS, tokens_per_minute: int | None = None,
                  sleep: Callable[[float], None] | None = None, page_id: str | None = None) -> int:
    """종류가 아직 없는 부분을 판정한다. 판정한 부분 수. LLM이 없거나 실패하면 다음에 다시 한다.

    - 최근 수정된 페이지부터. 한 번 호출이 budget_tokens(추정)를 넘지 않게 페이지의 부분을 나눠 보낸다.
    - budget_tokens를 다 쓰면 멈춘다(남은 것은 다음 동기화). None이면 끝까지 한다.
    - tokens_per_minute가 있으면 호출마다 쉬어 가며 그 속도를 넘지 않는다(reindex용).
    """
    if classify is None:
        return 0
    db.initialize(store)
    with closing(db.connect(store)) as c:
        rows = [dict(r) for r in c.execute(
            """SELECT * FROM source_parts WHERE scope = ? AND status = 'active' AND kind IS NULL AND parent_part_id IS NULL
               AND (? IS NULL OR page_id = ?) ORDER BY page_id, position""", (scope, page_id, page_id))]
    pages: dict[str, list[dict]] = {}
    for row in rows:
        pages.setdefault(row["page_id"] or row["id"], []).append(row)
    ordered = sorted(pages.values(), key=lambda rs: max(r["last_edited"] or "" for r in rs), reverse=True)
    batch_limit = min(budget_tokens or JUDGE_BUDGET_TOKENS, JUDGE_BUDGET_TOKENS)
    judged = spent = 0
    for page_rows in ordered:
        page_path = page_rows[0]["path"].rsplit(" > ", 1)[0]
        page_info = _page_info(store, scope, page_rows[0]["page_id"], page_path)
        batches: list[list[dict]] = [[]]
        for row in page_rows:
            if batches[-1] and estimate_tokens([_judge_item(r) for r in batches[-1] + [row]]) > batch_limit:
                batches.append([])
            batches[-1].append(row)
        for batch in batches:
            payload = {"page_path": page_path, "page": page_info, "parts": [_judge_item(r) for r in batch]}
            cost = estimate_tokens(payload["parts"])
            if budget_tokens is not None and spent and spent + cost > budget_tokens:
                return judged  # 이번 몫을 다 씀: 남은 것은 다음 동기화에서
            answer = None
            for attempt in range(RETRIES + 1 if tokens_per_minute else 1):
                try:
                    answer = classify(payload) or {}
                    break
                except Exception:
                    if not tokens_per_minute or attempt == RETRIES:
                        return judged  # 한도 초과(429) 등: 이번에는 멈추고 남은 것은 다음 동기화에서 판정한다
                    (sleep or __import__("time").sleep)(60)  # 쉬어 가는 모드: 분당 한도가 풀릴 때까지 기다렸다 다시
            if answer is None:
                return judged
            spent += cost
            by_id = {a.get("part_id"): a for a in answer.get("parts", []) if isinstance(a, dict)}
            if not by_id:
                return judged  # 빈 답(LLM 없음·형식 오류): 아무것도 굳히지 않고 다음 동기화에서 다시
            with db.write(store) as c:
                for row in batch:
                    found = by_id.get(row["id"])
                    if found is None or found.get("kind") not in KINDS:
                        # 판단할 근거가 없다고 뺀 부분: 보관만 하고 다시 보내지 않는다(매 동기화마다 토큰을 쓰지 않게)
                        found = {"kind": "document", "confidence": 1, "reason": "판정 근거 없음 → 보관만"}
                    _apply_judgment(c, row, found)
                    judged += 1
            if tokens_per_minute:
                (sleep or __import__("time").sleep)(cost / tokens_per_minute * 60)
    return judged


def _apply_judgment(c: Any, row: dict, found: dict) -> None:
    ts = db.now()
    kind, note = found["kind"], found.get("reason") or None
    try:
        confidence = float(found.get("confidence") if found.get("confidence") is not None else 1)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < LOW_CONFIDENCE:
        note = f"확신 낮음({kind}, {confidence:g}) → 보관만" + (f" · {note}" if note else "")
        kind = "document"
    children = {f"{row['path']} > {child['title']}": child for child in details_children(row["md"])}
    ranges = []
    for split in found.get("split") or []:
        child = children.get(split.get("child_path"))
        if child is None or split.get("kind") not in KINDS:
            continue
        ranges.append((child["start"], child["end"]))
        lines = row["md"].split("\n")[child["start"]:child["end"] + 1]
        child_md = "\n".join(lines)
        child_id = f"{row['id']}:{db.sha256(split['child_path'])[:10]}"
        c.execute(
            """INSERT INTO source_parts(id, scope, origin, page_id, section_id, block_ids_json, parent_part_id, path,
                   position, kind, kind_source, kind_note, summary, md, content_hash, chars, state, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 'llm', NULL, ?, ?, ?, ?, 'collected', 'active', ?, ?)
               ON CONFLICT(id) DO UPDATE SET kind = excluded.kind, summary = excluded.summary, md = excluded.md,
                   content_hash = excluded.content_hash, chars = excluded.chars, status = 'active', updated_at = excluded.updated_at""",
            (child_id, row["scope"], row["origin"], row["page_id"], row["section_id"], row["id"], split["child_path"],
             row["position"], split["kind"], split.get("summary"), child_md, db.sha256(child_md), len(child_md), ts, ts))
    md = _remove_ranges(row["md"], ranges) if ranges else row["md"]
    c.execute("""UPDATE source_parts SET kind = ?, kind_source = 'llm', kind_note = ?, summary = ?, md = ?, chars = ?,
                     updated_at = ? WHERE id = ?""",
              (kind, note, found.get("summary") if kind != "meeting" else None, md, len(md), ts, row["id"]))


def set_part_kind(store: Path, part_id: str, kind: str, actor: str) -> None:
    """사람이 종류를 정한다. 같은 블록이면 내용이 바뀌어도 유지된다."""
    if kind not in KINDS:
        raise db.ValidationError(f"종류는 {', '.join(sorted(KINDS))} 중 하나")
    with db.write(store) as c:
        if c.execute("UPDATE source_parts SET kind = ?, kind_source = 'human', kind_note = ?, updated_at = ? WHERE id = ?",
                     (kind, f"{actor}가 정함", db.now(), part_id)).rowcount == 0:
            raise db.NotFoundError(f"부분 {part_id} 없음")


def has_parts(store: Path, scope: str, page_id: str) -> bool:
    """이 페이지(행)의 부분이 저장돼 있는지. 부분이 생기기 전에 모은 행을 한 번 채우는 데 쓴다."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        return c.execute("SELECT 1 FROM source_parts WHERE scope = ? AND page_id = ? LIMIT 1", (scope, page_id)).fetchone() is not None


def list_parts(store: Path, scope: str, page_id: str | None = None, include_gone: bool = False) -> list[dict[str, Any]]:
    db.initialize(store)
    query = "SELECT * FROM source_parts WHERE scope = ?"
    args: list[Any] = [scope]
    if page_id is not None:
        query += " AND page_id = ?"
        args.append(page_id)
    if not include_gone:
        query += " AND status = 'active'"
    query += " ORDER BY page_id, position, parent_part_id IS NOT NULL, path"
    with closing(db.connect(store)) as c:
        return [dict(r) for r in c.execute(query, args)]


# ── 5. 추출 (docs/v1.1/spec.md P1-4) ────────────────────────

ITEM_KINDS = {"decision", "task", "open", "feedback", "insight"}
ROLE_HINTS = {"plan", "design", "dev"}
EXTRACT_BUDGET_TOKENS = 4000  # [가정] 한 번에 보내는 추출 입력(추정)의 상한. 넘는 부분은 줄 단위로 나눠 보낸다

PartExtractor = Callable[[dict[str, Any]], dict[str, Any]]


def extract_hash(part: dict) -> str:
    """추출 키: 떼어 낸 뒤의 실제 md. (원래 md 해시인 content_hash와 다르다.)"""
    return db.sha256(part["md"])


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_items(items: list[Any], text: str) -> tuple[list[dict[str, Any]], int]:
    """형식이 맞고 원문 인용(quote)이 실제 text 안에 있는 항목만 남긴다. (남긴 것, 버린 수)."""
    haystack = _squash(text)
    kept: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict) or raw.get("kind") not in ITEM_KINDS or not str(raw.get("title") or "").strip():
            continue
        quote = _squash(str(raw.get("quote") or ""))
        if not quote or quote not in haystack:
            continue
        try:
            confidence = float(raw.get("confidence") if raw.get("confidence") is not None else 1)
        except (TypeError, ValueError):
            confidence = 0.0
        due = raw.get("due") if isinstance(raw.get("due"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw["due"]) else None
        kept.append({"kind": raw["kind"], "title": _squash(str(raw["title"])), "detail": raw.get("detail") or None,
                     "owner": raw.get("owner") or None, "due": due,
                     "role_hint": raw.get("role_hint") if raw.get("role_hint") in ROLE_HINTS else None,
                     "quote": quote, "confidence": confidence})
    valid = sum(1 for raw in items if isinstance(raw, dict))
    return kept, valid - len(kept)


def _chunks(text: str, limit_tokens: int) -> list[str]:
    """줄 단위로 limit_tokens(추정)를 넘지 않게 나눈다. 한 줄이 넘으면 그 줄만 따로."""
    limit_chars = max(200, (limit_tokens - PROMPT_TOKENS) * 4 // 3)
    out: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        if current and size + len(line) + 1 > limit_chars:
            out.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        out.append("\n".join(current))
    return out


def pending_extracts(store: Path, scope: str, prompt_version: str, page_id: str | None = None) -> list[dict[str, Any]]:
    """추출할 회의 메모 부분(종류 meeting, 이 판의 추출이 아직 없음). 최근 수정된 것부터."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM source_parts WHERE scope = ? AND status = 'active' AND kind = 'meeting' AND (? IS NULL OR page_id = ?)",
            (scope, page_id, page_id))]
        done = {r["content_hash"] for r in c.execute(
            "SELECT content_hash FROM part_extracts WHERE prompt_version = ?", (prompt_version,))}
    pending = [r for r in rows if extract_hash(r) not in done]
    return sorted(pending, key=lambda r: (r["last_edited"] or "", r["page_id"] or "", -r["position"]), reverse=True)


def pending_count(store: Path, scope: str, prompt_version: str) -> int:
    """"n건 정리 중": 종류 판정 전인 부분 + 추출 전인 회의 메모 부분."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        unjudged = c.execute("""SELECT COUNT(*) FROM source_parts WHERE scope = ? AND status = 'active' AND kind IS NULL
                                AND parent_part_id IS NULL""", (scope,)).fetchone()[0]
    return unjudged + len(pending_extracts(store, scope, prompt_version))


def save_extract(store: Path, content_hash: str, prompt_version: str, model: str, items: list[dict], dropped: int,
                 usage: dict | None) -> None:
    usage = usage or {}
    with db.write(store) as c:
        c.execute("""INSERT OR REPLACE INTO part_extracts(content_hash, prompt_version, model, items_json, dropped,
                         input_tokens, output_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                  (content_hash, prompt_version, model, json.dumps(items, ensure_ascii=False), dropped,
                   usage.get("prompt_tokens"), usage.get("completion_tokens"), db.now()))


def _mark_cached_extracted(store: Path, scope: str, prompt_version: str) -> None:
    """이 판의 추출이 이미 있는(같은 글이 전에 추출된) 수집 상태 부분을 추출됨으로 올린다.
    Phase 2에서 상태 없이 추출해 둔 부분도 여기서 확인 대기 흐름에 들어온다."""
    db.initialize(store)
    with db.write(store) as c:
        done = {r["content_hash"] for r in c.execute(
            "SELECT content_hash FROM part_extracts WHERE prompt_version = ?", (prompt_version,))}
        rows = [dict(r) for r in c.execute(
            """SELECT * FROM source_parts WHERE scope = ? AND status = 'active' AND kind = 'meeting'
               AND state = 'collected'""", (scope,))]
        for row in rows:
            if extract_hash(row) in done:
                c.execute("UPDATE source_parts SET state = 'extracted' WHERE id = ?", (row["id"],))


def extract_pending(store: Path, scope: str, extract: PartExtractor | None, prompt_version: str,
                    budget_tokens: int | None = EXTRACT_BUDGET_TOKENS, page_id: str | None = None) -> int:
    """추출 전인 회의 메모 부분을 최근 것부터 추출해 저장한다. 추출한 부분 수.
    LLM이 없거나 실패하면(한도 초과 포함) 멈추고 다음 주기에 이어 한다. 문서(document)는 추출하지 않는다."""
    _mark_cached_extracted(store, scope, prompt_version)
    if extract is None:
        return 0
    done = spent = 0
    for part in pending_extracts(store, scope, prompt_version, page_id):
        text = md_to_text(part["md"])
        pieces = _chunks(text, EXTRACT_BUDGET_TOKENS) if estimate_tokens(text) > EXTRACT_BUDGET_TOKENS else [text]
        cost = sum(estimate_tokens(piece) for piece in pieces)
        if budget_tokens is not None and spent and spent + cost > budget_tokens:
            break
        items: list[Any] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        model = ""
        try:
            for piece in pieces:
                answer = extract({"path": part["path"], "text": piece}) or {}
                if "items" not in answer:
                    return done  # LLM 없음: 이번에는 멈춘다
                items += answer["items"]
                model = answer.get("model") or model
                for key in usage:
                    usage[key] += int((answer.get("usage") or {}).get(key) or 0)
        except Exception:
            return done  # 한도 초과(429·대기열 Busy) 등: 남은 것은 다음 주기에
        kept, dropped = clean_items(items, text)
        save_extract(store, extract_hash(part), prompt_version, model, kept, dropped, usage)
        with db.write(store) as c:  # 상태: 수집 → 추출됨(확인 대기로 올라가기 전 단계, spec P1-5)
            c.execute("UPDATE source_parts SET state = 'extracted' WHERE id = ? AND state = 'collected'", (part["id"],))
        spent += cost
        done += 1
    return done


def meeting_items(store: Path, scope: str, prompt_version: str) -> list[dict[str, Any]]:
    """메뉴용 항목: 회의 메모 부분들의 추출 결과를 합친다. 같은 종류·같은 제목(정규화)은 하나로 합치고
    근거(부분 경로·인용)를 모두 남긴다. 최근 수정된 회의의 항목이 먼저."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM source_parts WHERE scope = ? AND status = 'active' AND kind = 'meeting'", (scope,))]
        extracts = {r["content_hash"]: json.loads(r["items_json"]) for r in c.execute(
            "SELECT content_hash, items_json FROM part_extracts WHERE prompt_version = ?", (prompt_version,))}
    rows.sort(key=lambda r: (r["last_edited"] or "", r["page_id"] or "", -r["position"]), reverse=True)
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        for item in extracts.get(extract_hash(row), []):
            key = (item["kind"], db.normalize_text(item["title"]))
            entry = merged.get(key)
            if entry is None:
                merged[key] = entry = dict(item, sources=[], last_edited=row["last_edited"])
            entry["sources"].append({"part_id": row["id"], "path": row["path"], "quote": item["quote"]})
            for field_name in ("owner", "due", "detail", "role_hint"):
                entry[field_name] = entry.get(field_name) or item.get(field_name)
    return list(merged.values())


def documents(store: Path, scope: str, limit: int = 5) -> list[dict[str, Any]]:
    """1번 메뉴의 주요 문서: 문서로 판정된 부분(한 줄 설명이 있는 것), 최근 수정된 것부터."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        return [dict(r) for r in c.execute(
            """SELECT id, page_id, path, summary, last_edited FROM source_parts
               WHERE scope = ? AND status = 'active' AND kind = 'document' AND summary IS NOT NULL AND summary != ''
               ORDER BY COALESCE(last_edited, '') DESC, chars DESC LIMIT ?""", (scope, limit))]
