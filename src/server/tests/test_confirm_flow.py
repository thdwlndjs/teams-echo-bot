"""4번 메뉴 확인·반영 흐름(봇 턴 경로) 테스트. 문서: docs/v1.1/spec.md P1-5, P1-6, Phase 3 수락 기준."""

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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import confirm_flow  # noqa: E402
import main  # noqa: E402
import worklog_bridge  # noqa: E402
from test_extracts import FakeExtractor  # noqa: E402
from test_parts import SAMPLE_BLOCKS, SAMPLE_MD, split_classifier  # noqa: E402
from worklog import apply, core, parts  # noqa: E402

SCOPE = "team:T1"
HEADERS = {"X-Bot-Secret": "test-secret"}


def turn(value: dict | None, aad_id: str = "u1", text: str = "", message_id: str = "m") -> dict:
    return {"user": {"aad_id": aad_id, "name": "김팀원" if aad_id == "u1" else "관리자"},
            "team": {"id": "T1", "name": "알파"}, "text": text, "value": value,
            "conversation": {"id": "C1", "type": "channel"}, "message_id": message_id,
            "sent_at": "2026-11-04T01:00:00Z"}


class FakeTurn:
    def __init__(self) -> None:
        self.aad_id, self.user_name, self.sent = "u1", "김팀원", []

    def status(self, text: str) -> None:
        pass

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def send_buttons(self, text: str, buttons: list[dict]) -> None:
        self.sent.append(text)


class ConfirmFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "w.sqlite3"
        config_path = root / "team_configs.json"
        config_path.write_text(json.dumps({SCOPE: {"notion_page_id": "root", "notion_token": "t", "github_token": "g",
                                                   "github_repo": "o/r", "admin_aad_id": "admin"}}), encoding="utf-8")
        self.patches = [patch.object(worklog_bridge, "WORKLOG_STORE", self.store),
                        patch.object(main, "TEAM_CONFIG_PATH", str(config_path)),
                        patch.object(main, "MEMBER_PROFILE_PATH", str(root / "member_profiles.json")),
                        patch.object(confirm_flow, "_user_judge", None)]  # 판정 LLM 대신 규칙
        for p in self.patches:
            p.start()
        confirm_flow.edit_sessions.clear()
        core.record(self.store, scope=SCOPE, kind="notion_record", conversation_key="notion:sec-1",
                    locator="notion:unit-1", text=parts.md_to_text(SAMPLE_MD), sent_at="2026-11-01T00:00:00Z")
        built = parts.build_unit_parts("unit-1", "회의록 > 1차 멘토링", SAMPLE_MD, SAMPLE_BLOCKS)
        parts.save_unit_parts(self.store, SCOPE, "notion", "unit-1", "sec-1", built, last_edited="2026-11-01T00:00:00Z")
        parts.judge_pending(self.store, SCOPE, split_classifier(), budget_tokens=None)
        parts.extract_pending(self.store, SCOPE, FakeExtractor(), worklog_bridge.EXTRACT_PROMPT_VERSION,
                              budget_tokens=None)
        apply.request_confirm(self.store, SCOPE, "unit-1")
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        confirm_flow.edit_sessions.clear()
        self.tmp.cleanup()

    def post(self, value: dict | None, aad_id: str = "u1", text: str = "") -> str:
        resp = self.client.post("/api/turn", json=turn(value, aad_id, text), headers=HEADERS)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def titles(self) -> set[str]:
        with closing(sqlite3.connect(self.store)) as c:
            return {r[0] for r in c.execute("SELECT title FROM work_items WHERE scope = ?", (SCOPE,))}

    def test_preview_then_apply_creates_items_with_the_confirmer(self) -> None:  # A7
        preview = self.post({"action": "apply_pick_ready", "page_id": "unit-1"})
        self.assertIn("책 등록 방법 정리", preview)
        self.assertIn("part_apply", preview)
        self.assertEqual(self.titles(), set())  # 미리보기만으로는 반영되지 않는다
        self.assertIn("반영했어요", self.post({"action": "part_apply", "page_id": "unit-1"}))
        self.assertIn("책 등록 방법 정리", self.titles())
        applied = apply.recent_applied(self.store, SCOPE)
        self.assertEqual({r["decided_by"] for r in applied}, {"김팀원"})

    def test_cancel_creates_nothing(self) -> None:  # A7
        self.assertIn("반영하지 않았어요", self.post({"action": "part_cancel", "page_id": "unit-1"}))
        self.assertEqual(self.titles(), set())
        self.assertEqual(apply.pending_pages(self.store, SCOPE), [])

    def test_edits_by_chat_are_applied_and_saved_as_the_answer_key(self) -> None:  # A8
        self.assertIn("고치기", self.post({"action": "part_edit", "page_id": "unit-1"}))
        items = confirm_flow.edit_sessions["u1"]["items"]
        first = items[0]["title"]
        self.post(None, text="1: 바꾼 제목")
        self.post(None, text="+ 할 일: 발표 자료 준비")
        self.assertIn("형식을 못 알아봤어요", self.post(None, text="아무 말"))
        self.post({"action": "edit_delete", "n": 2})
        self.post(None, text="- 1")  # 버튼이 없는 13번 이후도 채팅으로 지울 수 있다
        self.post(None, text="+ 결정: 되살린 첫 항목")
        self.assertIn("고친 목록", self.post({"action": "edit_done"}))
        self.post({"action": "part_apply", "page_id": "unit-1"})
        titles = self.titles()
        self.assertNotIn("바꾼 제목", titles)
        self.assertIn("되살린 첫 항목", titles)
        self.assertIn("발표 자료 준비", titles)
        self.assertNotIn(first, titles)
        with closing(sqlite3.connect(self.store)) as c:
            self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM extract_corrections").fetchone()[0], 1)
        self.assertNotIn("u1", confirm_flow.edit_sessions)

    def test_only_the_approver_can_revert(self) -> None:
        self.post({"action": "part_apply", "page_id": "unit-1"})
        candidate_id = apply.recent_applied(self.store, SCOPE)[0]["id"]
        self.assertIn("권한자", self.post({"action": "revert", "candidate_id": candidate_id}))
        before = self.titles()
        self.assertIn("되돌렸어요", self.post({"action": "revert", "candidate_id": candidate_id}, aad_id="admin"))
        live = {i["title"] for group in apply.live_items(self.store, SCOPE).values() for i in group}
        self.assertLess(len(live), len(before))

    def test_main_card_shows_the_pending_note(self) -> None:
        lines, buttons = confirm_flow.main_card_alerts(SCOPE)
        self.assertIn("📥 새 회의록 1건 확인 대기", lines)
        self.assertEqual(buttons[0]["data"], {"action": "apply_pick_ready", "page_id": "unit-1"})

    def test_admin_is_reminded_once_about_notes_waiting_over_three_days(self) -> None:
        lines, _ = confirm_flow.main_card_alerts(SCOPE, approver=True, admin=False)
        self.assertFalse([line for line in lines if "3일" in line])  # 관리자에게만
        lines, _ = confirm_flow.main_card_alerts(SCOPE, approver=True, admin=True)  # 2026-11-01 수정 → 3일 넘음
        self.assertIn("⏰ 확인 대기가 3일 넘은 회의록 1건 — 팀원에게 확인을 부탁해 주세요", lines)
        lines, _ = confirm_flow.main_card_alerts(SCOPE, approver=True, admin=True)
        self.assertFalse([line for line in lines if "3일" in line])  # 한 번만

    def test_apply_over_deadline_answers_and_keeps_working(self) -> None:  # A32
        finished = []

        async def slow_prepare(team_key, page_id, sync_team):
            await asyncio.sleep(0.2)
            finished.append(page_id)

        async def scenario(fake: FakeTurn) -> None:
            await confirm_flow.pick_page(fake, SCOPE, "unit-1", None)
            await asyncio.sleep(0.3)

        fake = FakeTurn()
        with patch.object(confirm_flow, "READ_BUDGET_SECONDS", 0.05), patch.object(confirm_flow, "_prepare", slow_prepare):
            asyncio.run(scenario(fake))
        self.assertIn("정리가 오래 걸려요", fake.sent[0])
        self.assertEqual(finished, ["unit-1"])  # 안내한 뒤에도 정리는 이어진다

    def test_existing_notes_page_kept(self) -> None:  # A24: 설정 마법사에서 단계는 없앴지만 등록된 값은 계속 읽는다
        config = {"notion_page_id": "team", "meeting_notes_page_id": "notes"}
        self.assertEqual(main._team_roots(config), ["team", "notes"])
        self.assertEqual(main._team_roots({"notion_page_id": "team"}), ["team"])


if __name__ == "__main__":
    unittest.main()
