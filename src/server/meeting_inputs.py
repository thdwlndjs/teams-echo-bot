"""Notion 밖 회의록 입구: 붙여넣은 텍스트, txt·md·docx 파일(클로바노트에서 내려받은 것 포함). 문서: docs/v1.1/spec.md P1-7.

받은 내용은 md로 바꿔 `source_parts`(origin 'teams' 또는 'clova', 종류 meeting)로 저장하고, 미리보기 → 확인 흐름(4번 ②와
같은 경로)으로 보낸다. 클로바노트 공유 링크는 로그인이 필요해 읽지 않는다.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

from worklog import core, db, parts

CLOVA_LINK_RE = re.compile(r"https?://(?:www\.)?clovanote\.naver\.com/s/\S+")
CLOVA_LINK_REPLY = "공유 링크는 읽을 수 없어요. 노트에서 내려받은 파일이나 텍스트를 보내 주세요."
_CLOVA_LINE_RE = re.compile(r"^(참석자|화자)\s*\d+\s+\d{1,2}:\d{2}", re.MULTILINE)
FILE_TYPES = {".txt", ".md", ".docx"}
MAX_FILE_BYTES = 2_000_000
MIN_CHARS = 20  # 이보다 짧으면 회의록으로 보지 않는다(실수로 보낸 짧은 말)
EMPTY = "<empty-block/>"


def has_clova_link(text: str) -> bool:
    return bool(CLOVA_LINK_RE.search(text or ""))


def text_to_md(text: str) -> str:
    """붙여넣은 글 → md. 빈 줄은 부분 경계(빈 단락), 앞 공백 2칸(또는 탭)마다 한 단계 중첩."""
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip():
            if out and out[-1] != EMPTY:
                out.append(EMPTY)
            continue
        stripped = line.lstrip(" \t")
        lead = line[: len(line) - len(stripped)]
        depth = lead.count("\t") + lead.count(" ") // 2
        out.append("\t" * depth + stripped.rstrip())
    while out and out[-1] == EMPTY:
        out.pop()
    return "\n".join(out)


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def docx_to_md(data: bytes) -> str:
    """docx → md. 제목은 #, 목록은 - / 1.(단계는 탭), 표는 | 표, 빈 문단은 부분 경계."""
    from docx import Document  # 무거워서 쓸 때만 읽는다
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(io.BytesIO(data))
    out: list[str] = []

    def blank() -> None:
        if out and out[-1] != EMPTY:
            out.append(EMPTY)

    for element in document.element.body.iterchildren():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(element, document)
            text = paragraph.text.strip()
            if not text:
                blank()
                continue
            style = (paragraph.style.name if paragraph.style is not None else "") or ""
            num = element.pPr.numPr if element.pPr is not None else None
            level = int(num.ilvl.val) if num is not None and num.ilvl is not None else 0
            if not level and (styled := re.search(r"(?:List|목록).*?(\d)$", style)):  # "List Bullet 2" = 한 단계 안
                level = int(styled.group(1)) - 1
            if style.startswith("Heading") or style.startswith("제목"):
                digits = re.findall(r"\d", style)
                blank()
                out.append("#" * min(int(digits[0]) if digits else 1, 3) + " " + text)
            elif "Number" in style or "번호" in style:
                out.append("\t" * level + "1. " + text)
            elif num is not None or "List" in style or "목록" in style:
                out.append("\t" * level + "- " + text)
            else:
                out.append(text)
        elif tag == "tbl":
            table = Table(element, document)
            rows = [[_cell(cell.text) for cell in row.cells] for row in table.rows]
            if not rows:
                continue
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            out.append("| " + " | ".join(rows[0]) + " |")
            out.append("| " + " | ".join(["---"] * width) + " |")
            out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    while out and out[-1] == EMPTY:
        out.pop()
    while out and out[0] == EMPTY:
        out.pop(0)
    return "\n".join(out)


def file_to_md(name: str, data: bytes) -> str:
    suffix = Path(name).suffix.lower()
    if suffix == ".docx":
        return docx_to_md(data)
    if suffix == ".md":
        return text_to_md(decode_text(data))
    return text_to_md(decode_text(data))


def guess_origin(name: str | None, text: str) -> str:
    """클로바노트에서 내려받은 것인지: 파일 이름이나 '참석자 1 00:00' 같은 줄로 본다."""
    lowered = (name or "").lower()
    if "clova" in lowered or "클로바" in lowered or len(_CLOVA_LINE_RE.findall(text)) >= 2:
        return "clova"
    return "teams"


def title_of(name: str | None, md: str) -> str:
    if name:
        return Path(name).stem[:40] or "회의록"
    for line in md.split("\n"):
        text = parts.plain_title(line) if line.strip() and line.strip() != EMPTY else ""
        if text and text != "(제목 없음)":
            return text[:40]
    return "회의록"


def save_note(store: Path, scope: str, md: str, title: str, origin: str, actor: str) -> str:
    """회의록 하나를 원문·부분으로 저장한다(종류는 회의 메모로 정함). 페이지 ID(같은 내용이면 같은 ID)."""
    page_id = f"{origin}-{db.sha256(md)[:16]}"
    locator = f"{origin}:{page_id}"
    core.record(store, scope=scope, kind="teams_message", conversation_key=locator, locator=locator,
                text=parts.md_to_text(md), author=actor, sent_at=db.now(), hold="reference",
                metadata={"title": title, "origin": origin})
    built = parts.build_unit_parts(page_id, f"회의록 올리기 > {title}", md, None)
    parts.save_unit_parts(store, scope, origin, page_id, None, built, last_edited=db.now())
    for part in parts.list_parts(store, scope, page_id=page_id):
        if part["kind"] != "meeting":
            parts.set_part_kind(store, part["id"], "meeting", actor)
    return page_id


def attachment_files(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """회의록 파일로 볼 첨부만(Teams가 메시지마다 붙이는 text/html 등은 뺀다)."""
    return [a for a in attachments if a.get("url") and Path(a.get("name") or "").suffix.lower() in FILE_TYPES]
