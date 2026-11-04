"""사람·역할·권한과 처음 등록 테스트. 문서: docs/v1.1/spec.md P2-1, P2-2, Phase 5 수락 기준(A14, A27)."""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import confirm_flow  # noqa: E402
import main  # noqa: E402
import member_flow  # noqa: E402
import worklog_bridge  # noqa: E402
from test_extracts import FakeExtractor  # noqa: E402
from test_parts import SAMPLE_BLOCKS, SAMPLE_MD, split_classifier  # noqa: E402
from worklog import apply, core, parts, people  # noqa: E402

SCOPE = "team:T1"
HEADERS = {"X-Bot-Secret": "test-secret"}


def turn(text: str = "", value: dict | None = None, user: str = "u1", name: str = "김팀원") -> dict:
    return {"user": {"aad_id": user, "name": name}, "team": {"id": "T1", "name": "알파"}, "text": text, "value": value,
            "conversation": {"id": "C1", "type": "channel"}, "message_id": "m", "sent_at": "2026-11-04T01:00:00Z"}


class PeopleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "w.sqlite3"
        (root / "team_configs.json").write_text(json.dumps({SCOPE: {
            "notion_page_id": "root", "notion_token": "t", "github_token": "g", "github_repo": "o/r",
            "admin_aad_id": "admin"}}), encoding="utf-8")
        self.profiles = root / "member_profiles.json"
        self.patches = [patch.object(worklog_bridge, "WORKLOG_STORE", self.store),
                        patch.object(main, "TEAM_CONFIG_PATH", str(root / "team_configs.json")),
                        patch.object(main, "MEMBER_PROFILE_PATH", str(self.profiles)),
                        patch.object(confirm_flow, "_user_judge", None)]
        for p in self.patches:
            p.start()
        member_flow.sessions.clear()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        member_flow.sessions.clear()
        self.tmp.cleanup()

    def post(self, text: str = "", value: dict | None = None, user: str = "u1", name: str = "김팀원") -> str:
        resp = self.client.post("/api/turn", json=turn(text, value, user, name), headers=HEADERS)
        self.assertEqual(resp.status_code, 200)
        return resp.text


class PermissionTests(PeopleTestCase):
    def test_who_can_approve(self) -> None:
        people.set_admin(self.store, SCOPE, "admin")
        for user, role in (("dev1", "dev"), ("plan1", "plan"), ("plan2", "plan")):
            people.touch(self.store, SCOPE, user, user)
            people.set_role(self.store, user, role)
        people.set_can_approve(self.store, SCOPE, "plan2", True)
        verdict = {u: people.can_approve(self.store, SCOPE, u) for u in ("admin", "dev1", "plan1", "plan2", "stranger")}
        self.assertEqual(verdict, {"admin": True, "dev1": True, "plan1": False, "plan2": True, "stranger": False})
        self.assertFalse(people.can_approve(self.store, "team:other", "plan2"))  # 권한은 팀 단위

    def test_legacy_admin_and_github_move_on_first_turn(self) -> None:
        self.profiles.write_text(json.dumps({"admin": {"github_id": "octo"}}), encoding="utf-8")
        self.post("1", user="admin", name="관리자")
        self.assertTrue(people.is_admin(self.store, SCOPE, "admin"))
        self.assertEqual(people.get(self.store, "admin")["github_login"], "octo")

    def test_admin_grants_and_revokes_approval(self) -> None:
        people.touch(self.store, SCOPE, "plan1", "이기획")
        people.set_role(self.store, "plan1", "plan")
        self.assertIn("관리자만", self.post("권한", user="plan1", name="이기획"))
        card = self.post("권한", user="admin", name="관리자")
        self.assertIn("권한 주기: 이기획", card)
        self.post(value={"action": "perm_toggle", "user": "plan1"}, user="admin", name="관리자")
        self.assertTrue(people.can_approve(self.store, SCOPE, "plan1"))
        self.post(value={"action": "perm_toggle", "user": "plan1"}, user="admin", name="관리자")
        self.assertFalse(people.can_approve(self.store, SCOPE, "plan1"))
        self.assertIn("관리자만", self.post(value={"action": "perm_toggle", "user": "plan1"}, user="plan1"))


class ButtonsByPermissionTests(PeopleTestCase):  # A14
    def setUp(self) -> None:
        super().setUp()
        core.record(self.store, scope=SCOPE, kind="notion_record", conversation_key="notion:sec-1",
                    locator="notion:unit-1", text=parts.md_to_text(SAMPLE_MD), sent_at="2026-11-01T00:00:00Z")
        built = parts.build_unit_parts("unit-1", "회의록 > 1차 멘토링", SAMPLE_MD, SAMPLE_BLOCKS)
        parts.save_unit_parts(self.store, SCOPE, "notion", "unit-1", "sec-1", built, last_edited="2026-11-01T00:00:00Z")
        parts.judge_pending(self.store, SCOPE, split_classifier(), budget_tokens=None)
        parts.extract_pending(self.store, SCOPE, FakeExtractor(), worklog_bridge.EXTRACT_PROMPT_VERSION, budget_tokens=None)
        apply.request_confirm(self.store, SCOPE, "unit-1")
        apply.apply_page(self.store, SCOPE, "unit-1", "admin", worklog_bridge.EXTRACT_PROMPT_VERSION)
        for user, role in (("dev1", "dev"), ("plan1", "plan")):
            people.touch(self.store, SCOPE, user, user)
            people.set_role(self.store, user, role)

    def test_member_without_permission_sees_no_revert_buttons(self) -> None:
        member_view = self.post(value={"action": "applied_list"}, user="plan1")
        self.assertIn("최근 반영 내역", member_view)
        self.assertNotIn('"revert"', member_view)
        self.assertIn('"revert"', self.post(value={"action": "applied_list"}, user="dev1"))
        candidate_id = apply.recent_applied(self.store, SCOPE)[0]["id"]
        self.assertIn("권한자", self.post(value={"action": "revert", "candidate_id": candidate_id}, user="plan1"))
        self.assertIn("되돌렸어요", self.post(value={"action": "revert", "candidate_id": candidate_id}, user="dev1"))

    def test_main_card_hides_answer_buttons_for_members(self) -> None:
        buttons = [{"title": "되돌리기", "data": {"action": "revert", "candidate_id": "c"}},
                   {"title": "확인하기", "data": {"action": "apply_pick_ready", "page_id": "p"}}]
        self.assertEqual([b["data"]["action"] for b in confirm_flow.for_viewer(buttons, False)], ["apply_pick_ready"])
        self.assertEqual(len(confirm_flow.for_viewer(buttons, True)), 2)


class OnboardingTests(PeopleTestCase):  # A27
    def setUp(self) -> None:
        super().setUp()
        self.env = patch.dict(os.environ, {"MEMBER_ONBOARDING": "1"})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        super().tearDown()

    def test_role_then_github_then_aliases_then_the_menu_runs(self) -> None:
        with patch.object(main, "fetch_team_status", new=AsyncMock(return_value=("진행상황입니다", []))) as status:
            self.assertIn("역할을 골라 주세요", self.post("2"))
            self.assertIn("역할을 먼저 골라 주세요", self.post("octo"))  # 역할 없이는 넘어가지 않는다
            self.assertIn("역할은 건너뛸 수 없어요", self.post(value={"action": "onboard_skip"}))
            self.assertIn("GitHub 아이디를 알려 주세요", self.post(value={"action": "set_role", "role": "dev"}))
            self.assertIn("형식이 아니에요", self.post("깃허브 없음!"))
            self.assertIn("이름이 어떻게 적히나요", self.post("octo"))
            done = self.post("김팀원, 팀원")
            self.assertIn("등록했어요", done)
            self.assertIn("진행상황입니다", done)  # 처음 누른 2번이 이어서 돈다
        status.assert_awaited_once()
        member = people.get(self.store, "u1")
        self.assertEqual((member["role"], member["github_login"], member["aliases"]), ("dev", "octo", ["김팀원", "팀원"]))
        self.assertNotIn("역할을 골라", self.post("별칭"))  # 등록한 사람에게는 다시 묻지 않는다

    def test_github_and_aliases_can_be_skipped(self) -> None:
        self.post(value={"action": "menu", "choice": "1"})
        self.post(value={"action": "set_role", "role": "plan"})
        self.assertIn("이름이 어떻게 적히나요", self.post(value={"action": "onboard_skip"}))
        with patch.object(main, "fetch_project_info", new=AsyncMock()) as info:
            self.assertIn("등록했어요", self.post(value={"action": "onboard_skip"}))
        info.assert_awaited_once()
        member = people.get(self.store, "u1")
        self.assertEqual((member["role"], member["github_login"]), ("plan", None))
        self.assertEqual(set(member["asked"]), {"github", "aliases"})

    def test_alias_command_updates_aliases(self) -> None:
        people.touch(self.store, SCOPE, "u1", "김팀원")
        people.set_role(self.store, "u1", "plan")
        self.assertIn("쉼표로", self.post("별칭"))
        self.assertIn("별칭을 저장했어요: 김팀원, KT", self.post("김팀원, KT"))
        self.assertEqual(people.get(self.store, "u1")["aliases"], ["김팀원", "KT"])


if __name__ == "__main__":
    unittest.main()
