"""4번 메뉴의 Notion 변경 반영·최근 반영 내역과 메인 카드 알림. 문서: docs/v1.1/spec.md P1-5, P1-6, P2-5.

흐름: 4번 → ② Notion 변경 반영 → 회의록 고르기 → (지금 읽고 정리) → 미리보기 [반영] [고치기] [취소]
- [반영]: 확인한 사람을 반영자로 worklog에 넣는다. [취소]: 반영하지 않는다(건너뛰기).
- [고치기]: 카드에 입력칸이 없어 채팅으로 고친다. `3: 새 문장`(3번 제목 바꾸기), `+ 할 일: 문장`(추가),
  `- n`(지우기, [삭제 n] 버튼은 12번까지), [다 고쳤어요]. 고친 전후는 정답지(extract_corrections)로 남는다. 세션은 10분.
- 직접 반영이 100초 시한 안에 안 끝나면 "끝나면 메인 카드에 올려 둘게요"라고 답하고 이어서 정리한다.
- 되돌리기·삭제 확인은 권한자만(main._is_approver → worklog.people.can_approve). 다른 팀원에게는 그 버튼이 안 보인다.
"""

from __future__ import annotations

import asyncio
import re
import time
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

import llm_queue
import meeting_inputs
import worklog_bridge
from worklog import apply, parts, stages

READ_BUDGET_SECONDS = 70.0  # 이 안에 읽기·정리가 안 끝나면 안내하고 뒤에서 이어 한다(턴 시한 100초 안쪽)
EDIT_SESSION_SECONDS = 600
KIND_LABELS = {"decision": "결정", "task": "할 일", "open": "미정", "feedback": "피드백", "insight": "인사이트"}
LABEL_KINDS = {v: k for k, v in KIND_LABELS.items()}
_RENAME_RE = re.compile(r"^\s*(\d+)\s*[:：]\s*(.+?)\s*$")
_DELETE_RE = re.compile(r"^\s*(?:-|삭제)\s*(\d+)\s*$")
_ADD_RE = re.compile(r"^\s*\+\s*(결정|할 일|할일|미정|피드백|인사이트)\s*[:：]\s*(.+?)\s*$")

# aad_id → {"team_key", "page_id", "items": [...], "original": [...], "at": monotonic}
edit_sessions: dict[str, dict[str, Any]] = {}


def _store():
    return worklog_bridge.WORKLOG_STORE


def _version() -> str:
    return worklog_bridge.EXTRACT_PROMPT_VERSION


def _user_judge(item: dict, related: list[dict]) -> dict:
    return worklog_bridge.groq_judge(item, related, llm_queue.USER)


def _day(iso: str | None) -> str:
    return f"{iso[5:7]}/{iso[8:10]}" if iso and len(iso) >= 10 else ""


# ── 4번 메뉴 ─────────────────────────────────────────────

SUBMENU = [
    {"title": "✅ 완료한 작업 공유", "data": {"action": "menu", "choice": "4-1"}},
    {"title": "📝 Notion 변경 반영", "data": {"action": "apply_list"}},
    {"title": "↩️ 최근 반영 내역", "data": {"action": "applied_list"}},
    {"title": "📤 회의록 올리기", "data": {"action": "upload_notes"}},
    {"title": "➕ 소스 추가", "data": {"action": "source_add"}},
]


async def show_submenu(turn: Any) -> None:
    await turn.send_buttons("4️⃣ 작업 공유·반영 — 무엇을 할까요?", SUBMENU)


async def show_apply_list(turn: Any, team_key: str) -> None:
    pages = await asyncio.to_thread(apply.recent_pages, _store(), team_key)
    if not pages:
        await turn.send("반영할 회의록을 아직 못 찾았어요. Notion 팀 페이지 안에 회의록이 있는지 확인해 주세요.")
        return
    buttons = [{"title": f"{'✓ ' if p['applied'] else ''}{p['title'][:28]} ({_day(p['last_edited'])})",
                "data": {"action": "apply_pick", "page_id": p["page_id"]}} for p in pages]
    await turn.send_buttons("📝 반영할 회의록을 골라 주세요 (최근 수정순, ✓ = 반영됨)", buttons)


async def pick_page(turn: Any, team_key: str, page_id: str, sync_team: Callable[[], Awaitable[None]]) -> None:
    """지금 읽고(바뀐 곳만) 그 페이지를 정리한 뒤 미리보기를 보인다. 시한 안에 안 끝나면 뒤에서 이어 한다."""
    turn.status("🔍 회의록을 읽고 정리하는 중...")
    work = asyncio.ensure_future(_prepare(team_key, page_id, sync_team))
    try:
        await asyncio.wait_for(asyncio.shield(work), timeout=READ_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        await turn.send("정리가 오래 걸려요. 끝나면 메인 카드에 '확인 대기'로 올려 둘게요.")
        return
    except Exception as error:
        traceback.print_exc()
        await turn.send(f"⚠️ 회의록을 정리하지 못했어요: {error}")
        return
    await show_preview(turn, team_key, page_id)


async def _prepare(team_key: str, page_id: str, sync_team: Callable[[], Awaitable[None]]) -> None:
    await sync_team()
    store = _store()
    await asyncio.to_thread(parts.judge_pending, store, team_key,
                            lambda page: worklog_bridge.groq_classify_parts(page, llm_queue.USER), None, None, None, page_id)
    await asyncio.to_thread(parts.extract_pending, store, team_key,
                            lambda payload: worklog_bridge.groq_extract_part(payload, llm_queue.USER), _version(), None, page_id)
    await asyncio.to_thread(apply.request_confirm, store, team_key, page_id)


def _preview_lines(items: list[dict], numbered: bool) -> list[str]:
    counts = {label: sum(1 for i in items if i["kind"] == kind) for kind, label in KIND_LABELS.items()}
    lines = [" · ".join(f"{label} {n}" for label, n in counts.items() if n) or "뽑힌 항목이 없어요"]
    for n, item in enumerate(items, 1):
        extra = " · ".join(filter(None, [item.get("owner"), item.get("due")]))
        prefix = f"{n}. " if numbered else "- "
        lines.append(f"{prefix}({KIND_LABELS[item['kind']]}) {item['title']}" + (f" · {extra}" if extra else ""))
    return lines


def _page_title(team_key: str, page_id: str) -> str:
    found = apply.meeting_parts(_store(), team_key, page_id)
    return found[0]["path"].rsplit(" > ", 2)[-2] if found and " > " in found[0]["path"] else "회의록"


async def show_preview(turn: Any, team_key: str, page_id: str, items: list[dict] | None = None,
                       edited: bool = False) -> None:
    if items is None:
        found = await asyncio.to_thread(apply.preview, _store(), team_key, page_id, _version())
        items = found["items"]
        if found["not_extracted"] and not items:
            await turn.send("아직 정리 중이에요. 잠시 뒤 메인 카드의 '확인 대기'에서 다시 열어 주세요.")
            return
    title = await asyncio.to_thread(_page_title, team_key, page_id)
    header = f"📝 {title}" + (" (고친 목록)" if edited else "")
    buttons = [{"title": "✅ 반영", "data": {"action": "part_apply", "page_id": page_id}},
               {"title": "✏️ 고치기", "data": {"action": "part_edit", "page_id": page_id}},
               {"title": "✖ 취소", "data": {"action": "part_cancel", "page_id": page_id}}]
    await turn.send_buttons("\n".join([header, *_preview_lines(items, numbered=False)]), buttons)


async def apply_page(turn: Any, team_key: str, page_id: str, approver: bool = True) -> None:
    session = edit_sessions.get(turn.aad_id)
    corrected = None
    if session and session["page_id"] == page_id and session["team_key"] == team_key:
        corrected = _by_part(session["items"], session["original"])
        edit_sessions.pop(turn.aad_id, None)
    actor = turn.user_name or turn.aad_id
    turn.status("✍️ 반영하는 중...")
    summary = await asyncio.to_thread(apply.apply_page, _store(), team_key, page_id, actor, _version(), corrected,
                                      _user_judge)
    lines = [f"✅ 반영했어요: {summary['applied']}건" + (f", 추가 논의 필요 {summary['needs_discussion']}건"
                                                     if summary["needs_discussion"] else "")]
    buttons = []
    for change in summary["superseded"]:
        lines.append(f"🔁 결정이 바뀌었어요: {change['old']} → {change['new']}")
        buttons.append({"title": f"↩️ 되돌리기: {change['new'][:20]}", "data": {"action": "revert",
                                                                              "candidate_id": change["candidate_id"]}})
    buttons = for_viewer(buttons, approver)
    if buttons:
        await turn.send_buttons("\n".join(lines), buttons)
    else:
        await turn.send("\n".join(lines))


async def cancel_page(turn: Any, team_key: str, page_id: str) -> None:
    edit_sessions.pop(turn.aad_id, None)
    await asyncio.to_thread(apply.skip_page, _store(), team_key, page_id, turn.user_name or turn.aad_id)
    await turn.send("반영하지 않았어요. 나중에 4번 ②에서 다시 고를 수 있어요.")


# ── [고치기] ─────────────────────────────────────────────

async def start_edit(turn: Any, team_key: str, page_id: str) -> None:
    found = await asyncio.to_thread(apply.preview, _store(), team_key, page_id, _version())
    edit_sessions[turn.aad_id] = {"team_key": team_key, "page_id": page_id, "items": [dict(i) for i in found["items"]],
                                  "original": [dict(i) for i in found["items"]], "at": time.monotonic()}
    await _show_edit(turn)


async def _show_edit(turn: Any) -> None:
    session = edit_sessions[turn.aad_id]
    lines = ["✏️ 고치기 — 채팅으로 보내 주세요",
             "· 제목 바꾸기: `3: 새 문장`", "· 추가: `+ 할 일: 문장` (결정·할 일·미정·피드백·인사이트)",
             "· 지우기: 아래 [삭제 n] 또는 `- 13`", "", *_preview_lines(session["items"], numbered=True)]
    buttons = [{"title": f"🗑 삭제 {n}", "data": {"action": "edit_delete", "n": n}}
               for n in range(1, min(len(session["items"]), 12) + 1)]
    buttons.append({"title": "👌 다 고쳤어요", "data": {"action": "edit_done"}})
    await turn.send_buttons("\n".join(lines), buttons)


def has_edit_session(aad_id: str) -> bool:
    session = edit_sessions.get(aad_id)
    if session and time.monotonic() - session["at"] > EDIT_SESSION_SECONDS:
        edit_sessions.pop(aad_id, None)
        return False
    return session is not None


async def handle_edit_text(turn: Any, text: str) -> bool:
    """고치기 중인 사용자의 채팅. 처리했으면 True."""
    session = edit_sessions.get(turn.aad_id)
    if session is None:
        return False
    session["at"] = time.monotonic()
    if (match := _DELETE_RE.match(text)) and 1 <= int(match.group(1)) <= len(session["items"]):
        session["items"].pop(int(match.group(1)) - 1)
    elif (match := _RENAME_RE.match(text)) and 1 <= int(match.group(1)) <= len(session["items"]):
        session["items"][int(match.group(1)) - 1]["title"] = match.group(2)
    elif match := _ADD_RE.match(text):
        kind = LABEL_KINDS[match.group(1).replace("할일", "할 일")]
        first = session["original"][0]["part_id"] if session["original"] else None
        session["items"].append({"kind": kind, "title": match.group(2), "quote": "", "confidence": 1.0,
                                 "part_id": first, "added": True})
    else:
        await turn.send("형식을 못 알아봤어요. `3: 새 문장`, `- 3`, `+ 할 일: 문장`처럼 보내 주세요. (그만두려면 '취소')")
        return True
    await _show_edit(turn)
    return True


async def delete_item(turn: Any, n: int) -> None:
    session = edit_sessions.get(turn.aad_id)
    if session is None or not 1 <= n <= len(session["items"]):
        await turn.send("고치기 세션이 끝났어요. 4번 ②에서 다시 열어 주세요.")
        return
    session["items"].pop(n - 1)
    session["at"] = time.monotonic()
    await _show_edit(turn)


async def finish_edit(turn: Any) -> None:
    session = edit_sessions.get(turn.aad_id)
    if session is None:
        await turn.send("고치기 세션이 끝났어요. 4번 ②에서 다시 열어 주세요.")
        return
    await asyncio.to_thread(_save_corrections, session, turn.user_name or turn.aad_id)
    await show_preview(turn, session["team_key"], session["page_id"], session["items"], edited=True)


def _clean(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in {"part_id", "added"}}


def _by_part(items: list[dict], original: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {i["part_id"]: [] for i in original if i.get("part_id")}
    for item in items:
        if item.get("part_id"):
            grouped.setdefault(item["part_id"], []).append(_clean(item))
    return grouped


def _save_corrections(session: dict, actor: str) -> None:
    before = _by_part(session["original"], session["original"])
    after = _by_part(session["items"], session["original"])
    store = _store()
    for part in apply.meeting_parts(store, session["team_key"], session["page_id"]):
        if part["id"] in after and after[part["id"]] != before.get(part["id"], []):
            apply.save_correction(store, part, _version(), before.get(part["id"], []), after[part["id"]], actor)


# ── 최근 반영 내역·되돌리기 ─────────────────────────────────

async def show_applied_list(turn: Any, team_key: str, approver: bool = True) -> None:
    rows = await asyncio.to_thread(apply.recent_applied, _store(), team_key, 10)
    if not rows:
        await turn.send("아직 반영한 항목이 없어요.")
        return
    lines = ["↩️ 최근 반영 내역 (되돌리기는 권한자만)"]
    buttons = []
    for n, row in enumerate(rows, 1):
        change = f" (대체: {row['target_title']})" if row["change_type"] == "superseding" and row["target_title"] else ""
        lines.append(f"{n}. ({KIND_LABELS.get(row['kind'], row['kind'])}) {row['title']}{change} · {row['decided_by']}")
        buttons.append({"title": f"↩️ {n}", "data": {"action": "revert", "candidate_id": row["id"]}})
    buttons = for_viewer(buttons, approver)
    if buttons:
        await turn.send_buttons("\n".join(lines), buttons)
    else:
        await turn.send("\n".join(lines))


async def revert(turn: Any, team_key: str, candidate_id: str, is_approver: bool) -> None:
    if not is_approver:
        await turn.send("되돌리기는 권한자(관리자·개발·권한을 받은 사람)만 할 수 있어요.")
        return
    try:
        await asyncio.to_thread(apply.revert, _store(), candidate_id, turn.user_name or turn.aad_id)
    except Exception as error:
        await turn.send(f"되돌리지 못했어요: {error}")
        return
    await turn.send("↩️ 되돌렸어요.")


async def answer_stage(turn: Any, team_key: str, value: dict, is_approver: bool) -> None:
    if not is_approver:
        await turn.send("단계·게이트 질문은 권한자(관리자·개발·권한을 받은 사람)가 답해요.")
        return
    try:
        message = await asyncio.to_thread(stages.answer, _store(), team_key, str(value.get("kind") or ""),
                                          str(value.get("key") or ""), str(value.get("reply") or ""),
                                          turn.user_name or turn.aad_id)
    except Exception as error:
        await turn.send(f"반영하지 못했어요: {error}")
        return
    await turn.send(f"🧭 {message}")


async def resolve_gone(turn: Any, team_key: str, part_id: str, remove: bool, is_approver: bool) -> None:
    if not is_approver:
        await turn.send("삭제 확인은 권한자(관리자·개발·권한을 받은 사람)만 할 수 있어요.")
        return
    removed = await asyncio.to_thread(apply.resolve_gone_part, _store(), team_key, part_id, remove,
                                      turn.user_name or turn.aad_id)
    await turn.send(f"🗑 그 부분에서 나온 항목 {removed}건을 지웠어요." if remove else "그대로 두었어요.")


# ── 메인 카드 알림 ───────────────────────────────────────

APPROVER_ACTIONS = {"revert", "gone_remove", "gone_keep", "stage_answer", "source_add"}


def for_viewer(buttons: list[dict], approver: bool) -> list[dict]:
    """권한자만 누를 수 있는 버튼(되돌리기·삭제 확인·단계 답)은 다른 팀원에게 보이지 않는다(spec P2-5, A14)."""
    return buttons if approver else [b for b in buttons if b["data"].get("action") not in APPROVER_ACTIONS]


def main_card_alerts(team_key: str, approver: bool = True, admin: bool = False) -> tuple[list[str], list[dict]]:
    """메인 카드 위쪽 알림(최대 5개)과 버튼. Notion·LLM을 부르지 않는다. 권한 없는 사람에게는 답 버튼을 빼고
    "권한자가 답해요"를 붙인다."""
    store = _store()
    lines: list[str] = []
    buttons: list[dict] = []
    pending = apply.pending_pages(store, team_key)
    if pending:
        lines.append(f"📥 새 회의록 {len(pending)}건 확인 대기")
        buttons += [{"title": f"확인하기: {p['title'][:20]}", "data": {"action": "apply_pick_ready", "page_id": p["page_id"]}}
                    for p in pending[:3]]
        stale = apply.stale_pending_once(store, team_key) if admin else []
        if stale:
            lines.append(f"⏰ 확인 대기가 {apply.STALE_DAYS}일 넘은 회의록 {len(stale)}건 — 팀원에게 확인을 부탁해 주세요")
    edited = apply.auto_applied_count(store, team_key)
    if edited:
        lines.append(f"✍️ 확인된 회의록이 수정돼 {edited}건을 반영했어요 (최근 24시간)")
        buttons.append({"title": "↩️ 최근 반영 내역", "data": {"action": "applied_list"}})
    for change in [r for r in apply.recent_applied(store, team_key, 5) if r["change_type"] == "superseding"][:2]:
        lines.append(f"🔁 결정이 바뀌었어요: {change['target_title']} → {change['title']}")
        buttons.append({"title": f"↩️ 되돌리기: {change['title'][:18]}", "data": {"action": "revert", "candidate_id": change["id"]}})
    for gone in apply.gone_confirmed_parts(store, team_key)[:2]:
        title = gone["path"].rsplit(" > ", 1)[-1]
        lines.append(f"🗑 회의록에서 사라진 부분: {title} — 거기서 나온 항목을 지울까요?")
        buttons += [{"title": f"지우기: {title[:16]}", "data": {"action": "gone_remove", "part_id": gone["id"]}},
                    {"title": f"그대로: {title[:16]}", "data": {"action": "gone_keep", "part_id": gone["id"]}}]
    found = stages.proposals(store, team_key)[:2]  # 단계 제안·게이트 질문(spec P2-4)
    stages.mark_asked(store, team_key, found)
    for prompt in found:
        lines.append(f"🧭 {prompt['text']}" + ("" if approver else " (권한자 확인 대기 중)"))
        buttons += [{"title": title, "data": {"action": "stage_answer", "kind": prompt["kind"], "key": prompt["key"],
                                              "reply": reply}} for reply, title in prompt["answers"]]
    working = parts.pending_count(store, team_key, _version())
    if working:
        lines.append(f"⏳ 회의록 {working}건 정리 중")
    shown = for_viewer(buttons, approver)
    lines = lines[:5]
    if len(shown) < len(buttons):
        lines.append("(🔒 되돌리기·삭제 확인은 권한자가 답해요)")
    return lines, shown


# ── 회의록 올리기 (Notion 밖 회의록, spec P1-7) ──────────────────

UPLOAD_SESSION_SECONDS = 600
UPLOAD_PROMPT = ("📤 회의록을 보내 주세요\n"
                 "· 텍스트를 그대로 붙여넣거나\n"
                 "· txt·docx 파일(클로바노트에서 내려받은 파일도 돼요)\n"
                 "받으면 바로 정리해서 미리보기를 보여 드려요. (그만두려면 '취소')")
# aad_id → {"team_key", "at"}
upload_sessions: dict[str, dict[str, Any]] = {}


async def start_upload(turn: Any, team_key: str) -> None:
    edit_sessions.pop(turn.aad_id, None)
    upload_sessions[turn.aad_id] = {"team_key": team_key, "at": time.monotonic()}
    await turn.send(UPLOAD_PROMPT)


def has_upload_session(aad_id: str) -> bool:
    session = upload_sessions.get(aad_id)
    if session and time.monotonic() - session["at"] > UPLOAD_SESSION_SECONDS:
        upload_sessions.pop(aad_id, None)
        return False
    return session is not None


async def download(url: str) -> bytes:
    """첨부 파일 내려받기. Teams 개인 채팅의 downloadUrl은 미리 인증된 주소라 토큰이 필요 없다."""
    import httpx

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        if len(response.content) > meeting_inputs.MAX_FILE_BYTES:
            raise ValueError("파일이 너무 커요(2MB까지)")
        return response.content


async def handle_upload(turn: Any, text: str, attachments: list[dict]) -> None:
    """회의록 올리기 다음에 온 메시지(텍스트나 파일)를 회의록으로 받아 미리보기까지 간다."""
    session = upload_sessions[turn.aad_id]
    team_key = session["team_key"]
    files = meeting_inputs.attachment_files(attachments)
    name: str | None = None
    if files:
        name = files[0].get("name") or "회의록.txt"
        try:
            data = await download(files[0]["url"])
            md = await asyncio.to_thread(meeting_inputs.file_to_md, name, data)
        except Exception as error:
            traceback.print_exc()
            await turn.send(f"파일을 받지 못했어요({error}). 내용을 텍스트로 붙여넣어 주세요.")
            return
    elif attachments and not text.strip():
        await turn.send("txt·docx 파일만 받을 수 있어요. 다른 형식이면 내용을 텍스트로 붙여넣어 주세요.")
        return
    elif meeting_inputs.has_clova_link(text) and len(meeting_inputs.CLOVA_LINK_RE.sub("", text).strip()) < meeting_inputs.MIN_CHARS:
        await turn.send(meeting_inputs.CLOVA_LINK_REPLY)
        return
    else:
        md = meeting_inputs.text_to_md(text)
    if len(parts.md_to_text(md).strip()) < meeting_inputs.MIN_CHARS:
        await turn.send("회의록으로 보기엔 너무 짧아요. 회의 내용을 붙여넣거나 파일을 보내 주세요.")
        return
    upload_sessions.pop(turn.aad_id, None)
    origin = meeting_inputs.guess_origin(name, parts.md_to_text(md))
    title = meeting_inputs.title_of(name, md)
    page_id = await asyncio.to_thread(meeting_inputs.save_note, _store(), team_key, md, title, origin,
                                      turn.user_name or turn.aad_id)
    source = "클로바노트" if origin == "clova" else "Teams"
    await turn.send(f"📥 '{title}' 회의록을 받았어요({source}). 정리할게요.")

    async def nothing_to_sync() -> None:
        return None

    await pick_page(turn, team_key, page_id, nothing_to_sync)


def first_scan_notice(team_key: str) -> tuple[str, list[dict]] | None:
    """첫 수집이 끝났으면(판정 전 부분 없음) 찾은 회의록 수 안내. 아직이면 None. 메인 카드에 한 번만 띄운다."""
    found = parts.list_parts(_store(), team_key)
    if not found or any(p["kind"] is None and p["parent_part_id"] is None for p in found):
        return None
    pages = {p["page_id"] for p in found if p["kind"] == "meeting" and p["origin"] == "notion"}
    if pages:
        return f"📚 Notion에서 회의록 {len(pages)}건을 찾았어요. 확인할 것은 위 '확인 대기'나 4번 ②에 있어요.", []
    return ("📚 Notion에서 회의록을 못 찾았어요. 회의록이 다른 곳에 있나요?",
            [{"title": "➕ 소스 추가", "data": {"action": "source_add"}},
             {"title": "📤 회의록 올리기", "data": {"action": "upload_notes"}}])
