"""처음 등록(역할 → GitHub → 별칭), '별칭' 명령, 관리자의 '권한' 카드. 문서: docs/v1.1/spec.md P2-1, P2-2.

- 역할은 필수. 고르기 전에는 다른 메뉴로 넘어가지 않는다. GitHub·별칭은 [건너뛰기] 가능.
  건너뛴 GitHub는 3번·4번 ①을 처음 누를 때 다시 묻는다(main.py의 GitHub 연결 흐름).
- 등록이 끝나면 처음 하려던 동작(메뉴·버튼)을 이어서 한다.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import worklog_bridge
from worklog import people

SESSION_SECONDS = 600
_GITHUB_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
ROLE_BUTTONS = [{"title": f"{label}", "data": {"action": "set_role", "role": role}} for role, label in people.ROLES.items()]
SKIP = {"title": "⏭ 건너뛰기", "data": {"action": "onboard_skip"}}

# aad_id → {"step": "role"|"github"|"aliases", "team_key", "resume": dict | None, "only": bool, "at": float}
sessions: dict[str, dict[str, Any]] = {}


def enabled() -> bool:
    """테스트는 끈다(tests/isolation.py). 처음 등록을 보는 테스트만 켠다."""
    return os.environ.get("MEMBER_ONBOARDING", "1") != "0"


def _store():
    return worklog_bridge.WORKLOG_STORE


def needs_onboarding(aad_id: str) -> bool:
    member = people.get(_store(), aad_id)
    return member is None or not member["role"]


def has_session(aad_id: str) -> bool:
    session = sessions.get(aad_id)
    if session and time.monotonic() - session["at"] > SESSION_SECONDS:
        sessions.pop(aad_id, None)
        return False
    return session is not None


async def start(turn: Any, resume: dict | None) -> None:
    sessions[turn.aad_id] = {"step": "role", "team_key": turn.team_key, "resume": resume, "only": False,
                             "at": time.monotonic()}
    await turn.send_buttons("👋 처음 오셨네요! 먼저 역할을 골라 주세요 (권한과 3번 메뉴에 쓰여요)", ROLE_BUTTONS)


async def start_aliases(turn: Any) -> None:
    member = people.get(_store(), turn.aad_id) or {"aliases": []}
    sessions[turn.aad_id] = {"step": "aliases", "team_key": turn.team_key, "resume": None, "only": True,
                             "at": time.monotonic()}
    now = ", ".join(member["aliases"]) or "없음"
    await turn.send(f"회의록에 이름이 어떻게 적히나요? 여러 개면 쉼표로 보내 주세요. (지금: {now})\n(그만두려면 '취소')")


async def _ask(turn: Any, step: str) -> None:
    session = sessions[turn.aad_id]
    session.update(step=step, at=time.monotonic())
    if step == "github":
        await turn.send_buttons("GitHub 아이디를 알려 주세요. `thdwlndjs`처럼 아이디만 보내면 돼요.", [SKIP])
    else:
        hint = f" (예: {turn.user_name}, 줄인 이름이나 영어 이름)" if turn.user_name else ""
        await turn.send_buttons(f"회의록에 이름이 어떻게 적히나요? 여러 개면 쉼표로{hint}", [SKIP])


async def _next_after(turn: Any, step: str, resume_action: Callable[[dict], Awaitable[None]]) -> None:
    store = _store()
    member = people.get(store, turn.aad_id) or {}
    if step == "role" and not member.get("github_login"):
        await _ask(turn, "github")
        return
    if step in ("role", "github") and not member.get("aliases"):
        await _ask(turn, "aliases")
        return
    session = sessions.pop(turn.aad_id)
    if session["only"]:
        await turn.send(f"별칭을 저장했어요: {', '.join(member.get('aliases') or []) or '없음'}")
        return
    role = people.ROLES.get(member.get("role") or "", "")
    await turn.send(f"✅ 등록했어요 — 역할 {role}" + (f", GitHub `{member['github_login']}`" if member.get("github_login") else ""))
    if session["resume"]:
        await resume_action(session["resume"])


async def handle_role(turn: Any, role: str, resume_action: Callable[[dict], Awaitable[None]]) -> None:
    if not has_session(turn.aad_id):
        sessions[turn.aad_id] = {"step": "role", "team_key": turn.team_key, "resume": None, "only": False,
                                 "at": time.monotonic()}
    await asyncio.to_thread(people.touch, _store(), turn.team_key, turn.aad_id, turn.user_name)
    await asyncio.to_thread(people.set_role, _store(), turn.aad_id, role)
    await _next_after(turn, "role", resume_action)


async def handle_skip(turn: Any, resume_action: Callable[[dict], Awaitable[None]]) -> None:
    session = sessions.get(turn.aad_id)
    if session is None or session["step"] == "role":
        await turn.send_buttons("역할은 건너뛸 수 없어요. 골라 주세요.", ROLE_BUTTONS)
        return
    await asyncio.to_thread(people.mark_asked, _store(), turn.aad_id, session["step"])
    step = session["step"]
    if step == "github":
        member = people.get(_store(), turn.aad_id) or {}
        if not member.get("aliases"):
            await _ask(turn, "aliases")
            return
    await _finish_skipped(turn, step, resume_action)


async def _finish_skipped(turn: Any, step: str, resume_action: Callable[[dict], Awaitable[None]]) -> None:
    session = sessions.pop(turn.aad_id)
    if session["only"]:
        await turn.send("그대로 두었어요.")
        return
    member = people.get(_store(), turn.aad_id) or {}
    await turn.send(f"✅ 등록했어요 — 역할 {people.ROLES.get(member.get('role') or '', '')}. 건너뛴 것은 '별칭'이나 3번 메뉴에서 다시 정할 수 있어요.")
    if session["resume"]:
        await resume_action(session["resume"])


async def handle_text(turn: Any, text: str, resume_action: Callable[[dict], Awaitable[None]]) -> None:
    session = sessions[turn.aad_id]
    session["at"] = time.monotonic()
    store = _store()
    if session["step"] == "role":
        await turn.send_buttons("역할을 먼저 골라 주세요.", ROLE_BUTTONS)
        return
    if session["step"] == "github":
        login = text.strip().lstrip("@")
        if not _GITHUB_RE.fullmatch(login):
            await turn.send_buttons("⚠️ GitHub 아이디 형식이 아니에요. (예: thdwlndjs)", [SKIP])
            return
        await asyncio.to_thread(people.set_github, store, turn.aad_id, login)
        await _next_after(turn, "github", resume_action)
        return
    aliases = [a for a in re.split(r"[,，、]", text) if a.strip()]
    if not aliases:
        await turn.send_buttons("이름을 쉼표로 구분해 보내 주세요.", [SKIP])
        return
    await asyncio.to_thread(people.set_aliases, store, turn.aad_id, aliases)
    await _next_after(turn, "aliases", resume_action)


# ── 관리자: 권한 카드 ─────────────────────────────────────

async def show_team_card(turn: Any) -> None:
    store = _store()
    if not people.is_admin(store, turn.team_key, turn.aad_id):
        await turn.send("권한 관리는 관리자만 할 수 있어요.")
        return
    members = await asyncio.to_thread(people.team, store, turn.team_key)
    lines = ["👥 팀원과 권한 (✔ = 승인·되돌리기 가능, 개발 역할은 기본으로 가능)"]
    buttons = []
    for m in members:
        name = m["display_name"] or m["teams_user_id"][:8]
        role = people.ROLES.get(m["role"] or "", "역할 없음")
        badge = "관리자" if m["is_admin"] else ("✔" if m["approver"] else "")
        lines.append(f"- {name} · {role} {badge}".rstrip())
        if m["is_admin"]:
            continue
        if m["role"] != "dev":
            buttons.append({"title": f"{'권한 회수' if m['can_approve'] else '권한 주기'}: {name}"[:40],
                            "data": {"action": "perm_toggle", "user": m["teams_user_id"]}})
        buttons.append({"title": f"관리자로: {name}"[:40], "data": {"action": "perm_admin", "user": m["teams_user_id"]}})
    await turn.send_buttons("\n".join(lines), buttons) if buttons else await turn.send("\n".join(lines))


async def toggle_permission(turn: Any, user_id: str, make_admin: bool) -> None:
    store = _store()
    if not people.is_admin(store, turn.team_key, turn.aad_id):
        await turn.send("권한 관리는 관리자만 할 수 있어요.")
        return
    if make_admin:
        await asyncio.to_thread(people.set_admin, store, turn.team_key, user_id, True)
    else:
        current = {m["teams_user_id"]: m for m in people.team(store, turn.team_key)}.get(user_id)
        await asyncio.to_thread(people.set_can_approve, store, turn.team_key, user_id,
                                not (current and current["can_approve"]))
    await show_team_card(turn)
