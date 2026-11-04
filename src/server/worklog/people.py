"""사람·역할·권한. 문서: docs/v1.1/spec.md P2-1, P2-2.

- 역할·GitHub·별칭은 사람 단위(`members`), 권한은 팀 단위(`team_members`).
- 권한자(승인·되돌리기·게이트 답·삭제 확인): 관리자이거나, 역할이 개발(dev)이거나, 관리자가 권한을 준 사람.
- 관리자만: 설정, 권한 주기·회수, 역할 고치기, 관리자 추가.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any

from . import db

ROLES = {"plan": "기획", "design": "디자인", "dev": "개발", "other": "기타"}


def _row(c: Any, user_id: str) -> dict[str, Any] | None:
    row = c.execute("SELECT * FROM members WHERE teams_user_id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    member = dict(row)
    member["aliases"] = json.loads(member.pop("aliases_json") or "[]")
    member["asked"] = json.loads(member.pop("asked_json") or "[]")
    return member


def get(store: Path, user_id: str) -> dict[str, Any] | None:
    db.initialize(store)
    with closing(db.connect(store)) as c:
        return _row(c, user_id)


def _ensure(c: Any, user_id: str, display_name: str | None) -> None:
    c.execute("""INSERT INTO members(teams_user_id, display_name, updated_at) VALUES (?, ?, ?)
                 ON CONFLICT(teams_user_id) DO UPDATE SET display_name = COALESCE(excluded.display_name, display_name)""",
              (user_id, display_name, db.now()))


def touch(store: Path, scope: str, user_id: str, display_name: str | None = None) -> dict[str, Any]:
    """이 팀에서 말을 건 사람을 기록한다(팀원 목록·권한 카드용). 사람 정보를 돌려준다."""
    db.initialize(store)
    with db.write(store) as c:
        _ensure(c, user_id, display_name)
        c.execute("INSERT OR IGNORE INTO team_members(scope, teams_user_id) VALUES (?, ?)", (scope, user_id))
        return _row(c, user_id)


def _update(store: Path, user_id: str, **fields: Any) -> None:
    db.initialize(store)
    with db.write(store) as c:
        _ensure(c, user_id, None)
        for key, value in fields.items():
            column = {"aliases": "aliases_json", "asked": "asked_json"}.get(key, key)
            stored = json.dumps(value, ensure_ascii=False) if key in ("aliases", "asked") else value
            c.execute(f"UPDATE members SET {column} = ?, updated_at = ? WHERE teams_user_id = ?", (stored, db.now(), user_id))


def set_role(store: Path, user_id: str, role: str) -> None:
    if role not in ROLES:
        raise db.ValidationError(f"역할은 {', '.join(ROLES)} 중 하나")
    _update(store, user_id, role=role)


def set_github(store: Path, user_id: str, login: str | None) -> None:
    _update(store, user_id, github_login=login)


def set_aliases(store: Path, user_id: str, aliases: list[str]) -> None:
    cleaned = list(dict.fromkeys(a.strip() for a in aliases if a.strip()))
    _update(store, user_id, aliases=cleaned)


def mark_asked(store: Path, user_id: str, what: str) -> None:
    member = get(store, user_id) or {"asked": []}
    if what not in member["asked"]:
        _update(store, user_id, asked=[*member["asked"], what])


def set_admin(store: Path, scope: str, user_id: str, value: bool = True) -> None:
    db.initialize(store)
    with db.write(store) as c:
        _ensure(c, user_id, None)
        c.execute("""INSERT INTO team_members(scope, teams_user_id, is_admin) VALUES (?, ?, ?)
                     ON CONFLICT(scope, teams_user_id) DO UPDATE SET is_admin = excluded.is_admin""",
                  (scope, user_id, int(value)))


def set_can_approve(store: Path, scope: str, user_id: str, value: bool) -> None:
    db.initialize(store)
    with db.write(store) as c:
        _ensure(c, user_id, None)
        c.execute("""INSERT INTO team_members(scope, teams_user_id, can_approve) VALUES (?, ?, ?)
                     ON CONFLICT(scope, teams_user_id) DO UPDATE SET can_approve = excluded.can_approve""",
                  (scope, user_id, int(value)))


def _membership(store: Path, scope: str, user_id: str) -> dict[str, Any]:
    db.initialize(store)
    with closing(db.connect(store)) as c:
        row = c.execute("""SELECT t.can_approve, t.is_admin, m.role FROM team_members t
                           LEFT JOIN members m ON m.teams_user_id = t.teams_user_id
                           WHERE t.scope = ? AND t.teams_user_id = ?""", (scope, user_id)).fetchone()
        if row is None:
            member = _row(c, user_id)
            return {"can_approve": 0, "is_admin": 0, "role": member["role"] if member else None}
        return dict(row)


def is_admin(store: Path, scope: str, user_id: str) -> bool:
    return bool(_membership(store, scope, user_id)["is_admin"])


def can_approve(store: Path, scope: str, user_id: str) -> bool:
    """승인·되돌리기·게이트 답·삭제 확인을 할 수 있는지. 판정은 이 함수 하나로만 한다."""
    m = _membership(store, scope, user_id)
    return bool(m["is_admin"] or m["can_approve"] or m["role"] == "dev")


def team(store: Path, scope: str) -> list[dict[str, Any]]:
    """팀원 목록(권한 카드). 관리자 먼저, 그다음 이름순."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        rows = c.execute("""SELECT m.*, t.can_approve, t.is_admin FROM team_members t
                            JOIN members m ON m.teams_user_id = t.teams_user_id WHERE t.scope = ?""", (scope,)).fetchall()
    out = []
    for row in rows:
        member = dict(row)
        member["aliases"] = json.loads(member.pop("aliases_json") or "[]")
        member.pop("asked_json", None)
        member["approver"] = bool(member["is_admin"] or member["can_approve"] or member["role"] == "dev")
        out.append(member)
    return sorted(out, key=lambda m: (-m["is_admin"], m["display_name"] or m["teams_user_id"]))


def import_legacy(store: Path, profiles: dict[str, dict], configs: dict[str, dict]) -> int:
    """예전 파일의 값을 옮긴다: member_profiles.json의 github_id → members.github_login,
    team_configs.json의 admin_aad_id → team_members.is_admin. 이미 값이 있으면 덮지 않는다. 옮긴 수."""
    moved = 0
    for user_id, profile in profiles.items():
        login = (profile or {}).get("github_id")
        member = get(store, user_id)
        if login and not (member and member["github_login"]):
            set_github(store, user_id, login)
            moved += 1
    for scope, config in configs.items():
        admin = (config or {}).get("admin_aad_id")
        if admin and not is_admin(store, scope, admin):
            set_admin(store, scope, admin)
            moved += 1
    return moved
