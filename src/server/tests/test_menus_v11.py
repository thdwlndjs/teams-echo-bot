"""v1.1 메뉴 2·3·4 테스트. 문서: docs/v1.1/spec.md P2-7, Phase 5 수락 기준(A30)."""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import worklog_bridge  # noqa: E402
from test_stages import ScriptedJudge, add_item  # noqa: E402
from worklog import db, people, stages  # noqa: E402

SCOPE = "team:T1"
HEADERS = {"X-Bot-Secret": "test-secret"}


def set_attributes(store: Path, item_id: str, attributes: dict) -> None:
    with db.write(store) as c:
        c.execute("UPDATE work_items SET attributes_json = ? WHERE id = ?", (json.dumps(attributes), item_id))


class MenuTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "w.sqlite3"
        self.config_path = root / "team_configs.json"
        self.config = {"notion_page_id": "root", "notion_token": "t", "github_token": "g", "github_repo": "o/r",
                       "admin_aad_id": "admin"}
        self.config_path.write_text(json.dumps({SCOPE: self.config}), encoding="utf-8")
        self.patches = [patch.object(worklog_bridge, "WORKLOG_STORE", self.store),
                        patch.object(main, "TEAM_CONFIG_PATH", str(self.config_path)),
                        patch.object(main, "MEMBER_PROFILE_PATH", str(root / "member_profiles.json")),
                        patch.object(main, "_github_get_cached", new=AsyncMock(return_value=[])),
                        patch.object(main, "_get_commit_touched_issues", new=AsyncMock(return_value=set()))]
        for p in self.patches:
            p.start()
        add_item(self.store, "i-cand", "open", "후보 주제: 공유 우산 대여")
        add_item(self.store, "i-dec", "decision", "주간 회의는 화요일")
        add_item(self.store, "i-task-dev", "task", "로그인 API 만들기")
        set_attributes(self.store, "i-task-dev", {"role_hint": "dev", "issue_refs": []})
        add_item(self.store, "i-task-plan", "task", "인터뷰 질문지 쓰기")
        set_attributes(self.store, "i-task-plan", {"role_hint": "plan"})
        add_item(self.store, "i-mine", "task", "발표 자료 만들기")
        set_attributes(self.store, "i-mine", {"assignee": "KT"})
        add_item(self.store, "i-fb", "feedback", "식상한 주제는 피하라")
        people.touch(self.store, SCOPE, "u1", "김팀원")
        people.set_role(self.store, "u1", "dev")
        people.set_aliases(self.store, "u1", ["KT", "김팀원"])
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def post(self, text: str = "", value: dict | None = None, user: str = "u1") -> str:
        body = {"user": {"aad_id": user, "name": "김팀원"}, "team": {"id": "T1", "name": "알파"}, "text": text,
                "value": value, "conversation": {"id": "C1", "type": "channel"}, "message_id": "m",
                "sent_at": "2026-11-04T01:00:00Z"}
        resp = self.client.post("/api/turn", json=body, headers=HEADERS)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def test_menu2_shows_the_stage_and_six_groups(self) -> None:
        stages.assess(self.store, SCOPE, {}, ScriptedJudge({"candidates": {"met": True, "evidence": ["i-cand"]}}))
        with patch.object(main, "_stored_notes", new=AsyncMock(return_value={"texts": ["회의 원문"], "as_of": None})):
            text, _ = asyncio.run(main.fetch_team_status(self.config, SCOPE))
        self.assertIn("🧭 현재 단계: 1-2 방향 확정 (제안, 권한자 승인 전)", text)
        self.assertIn("📌 확정된 흐름\n- 주간 회의는 화요일", text)
        self.assertIn("- 정할 것: 주제 확정 결정이 있다", text)
        self.assertIn("🐢 천천히 정해도 되는 것", text)
        self.assertIn("후보 주제: 공유 우산 대여", text)
        self.assertIn("💬 멘토링 피드백", text)

    def test_menu3_lists_my_tasks_and_offers_unowned_tasks_for_my_role(self) -> None:
        with patch.object(main, "_stored_notes", new=AsyncMock(return_value={"texts": [], "as_of": None})):
            self.assertIn("GitHub 아이디를 알려줄래요", self.post(value={"action": "menu", "choice": "3"}))
            self.post("취소")
            reply = self.post(value={"action": "menu", "choice": "3"})  # 두 번째부터는 GitHub 없이 보여 준다
        self.assertIn("📝 회의록에서 맡은 일", reply)
        self.assertIn("발표 자료 만들기", reply)  # 별칭 KT로 맡은 일
        self.assertIn("담당이 없는 개발 할 일", reply)
        self.assertIn('"item_id": "i-task-dev"', reply)
        self.assertNotIn('"item_id": "i-task-plan"', reply)  # 기획 할 일은 개발자에게 권하지 않는다

    def test_take_task_changes_the_owner_and_keeps_a_revision(self) -> None:  # A30
        self.assertIn("맡았어요 (담당: KT)", self.post(value={"action": "take_task", "item_id": "i-task-dev"}))
        with closing(sqlite3.connect(self.store)) as c:
            attributes = json.loads(c.execute("SELECT attributes_json FROM work_items WHERE id = 'i-task-dev'").fetchone()[0])
            revision = c.execute("SELECT reason, actor FROM revisions WHERE object_id = 'i-task-dev'").fetchall()
        self.assertEqual(attributes["assignee"], "KT")
        self.assertEqual(revision, [("내가 맡기", "김팀원")])
        self.assertIn("이미 KT님이 맡았어요", self.post(value={"action": "take_task", "item_id": "i-task-dev"}, user="u2"))

    def test_take_task_also_assigns_the_linked_issue(self) -> None:
        set_attributes(self.store, "i-task-dev", {"role_hint": "dev", "issue_refs": [7]})
        people.set_github(self.store, "u1", "octo")
        with patch.object(main, "claim_issue", new=AsyncMock(return_value="✅ #7 배정")) as claim:
            self.assertIn("✅ #7 배정", self.post(value={"action": "take_task", "item_id": "i-task-dev"}))
        claim.assert_awaited_once()
        self.assertEqual(claim.await_args.args[1:], (7, "octo"))

    def test_source_add_registers_an_extra_page(self) -> None:
        people.set_admin(self.store, SCOPE, "admin")
        self.assertIn("권한자", self.post(value={"action": "source_add"}, user="u3"))
        self.assertIn("링크를 보내 주세요", self.post(value={"action": "source_add"}, user="admin"))
        page = "c" * 32
        with patch.object(main, "_notion_page_accessible", return_value=True), \
                patch.object(main, "_notion_client_for", return_value=object()), \
                patch.object(main, "_register_team_map", new=AsyncMock()) as register:
            self.assertIn("소스를 추가했어요", self.post(f"https://www.notion.so/doc-{page}", user="admin"))
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8"))[SCOPE]["extra_source_ids"], [page])
        self.assertEqual(register.await_args.args[1], ["root", page])
        self.assertEqual(main._team_roots({"notion_page_id": "root", "extra_source_ids": [page]}), ["root", page])


if __name__ == "__main__":
    unittest.main()
