"""에코봇 서버 ↔ worklog 통합 테스트. 실행: server 폴더에서 python -m unittest discover -s tests"""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("BOT_SERVER_SECRET", "test-secret")
os.environ["WORKLOG_LOOP"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import notion_map  # noqa: E402
import worklog_bridge  # noqa: E402
from worklog import core as worklog  # noqa: E402

HEADERS = {"X-Bot-Secret": "test-secret"}


def turn(text: str, message_id: str, value: dict | None = None) -> dict:
    return {"user": {"aad_id": "u1", "name": "김팀원"}, "team": {"id": "T1", "name": "알파"}, "text": text,
            "value": value, "conversation": {"id": "C1", "type": "channel"}, "message_id": message_id,
            "sent_at": "2026-11-02T01:00:00Z"}


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "worklog.sqlite3"
        self.patches = [
            patch.object(worklog_bridge, "WORKLOG_STORE", self.store),
            patch.object(main, "TEAM_CONFIG_PATH", str(root / "team_configs.json")),
            patch.object(main, "MEMBER_PROFILE_PATH", str(root / "member_profiles.json")),
        ]
        for p in self.patches:
            p.start()
        main.setup_sessions.clear()
        main.github_onboarding_sessions.clear()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def post(self, body: dict) -> str:
        resp = self.client.post("/api/turn", json=body, headers=HEADERS)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def sources(self) -> list[tuple[str, str]]:
        if not self.store.exists():
            return []
        with closing(sqlite3.connect(self.store)) as c:
            return c.execute("SELECT locator, content FROM sources ORDER BY rowid").fetchall()

    def test_free_text_is_not_stored_and_the_reply_is_unchanged(self) -> None:
        # A33: 메뉴가 아닌 채팅을 몰래 저장하던 기능은 v1.1에서 없앴다(docs/v1.1/spec.md P1-0).
        reply = self.post(turn("배포는 금요일로 확정했습니다", "m1"))
        self.assertIn("올바른 값을 입력하세요", reply)  # 기존 동작 그대로
        self.assertEqual(self.sources(), [])

    def test_background_loop_does_not_run_batch_analysis(self) -> None:
        # A34: 1시간 분석(analyze_due)은 돌리지 않는다. Notion 지도 동기화는 그대로 돈다.
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        sleeps = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
        analyze, sync = MagicMock(return_value=[]), MagicMock(return_value={})
        with patch.object(main.asyncio, "sleep", sleeps), patch.object(worklog_bridge, "analyze_due", analyze), \
                patch.object(main, "_sync_notion_maps", sync):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(main._worklog_loop())
        sync.assert_called()
        analyze.assert_not_called()

    def test_commands_and_setup_answers_are_never_stored(self) -> None:
        for i, text in enumerate(["1", "설정", "hi", "취소"]):
            self.post(turn(text, f"c{i}"))
        self.post(turn("설정", "s0"))  # 마법사 시작 — 이후 답(토큰 포함)은 저장하면 안 된다
        self.post(turn("ghp_SECRETTOKEN", "s1"))
        self.post(turn("취소", "s2"))
        self.post(turn("버튼", "v1", value={"action": "dismiss_claim_card"}))
        self.assertEqual(self.sources(), [])

    def test_recorded_messages_flow_to_candidates_without_changing_state(self) -> None:
        # 후보 경로는 Phase 3에서 없앤다. 그때까지는 원문을 직접 넣어 이 경로를 확인한다.
        worklog_bridge.record(scope="team:T1", kind="teams_message", conversation_key="teams:C1",
                              locator="teams:C1:m2", text="인증은 OAuth로 가기로 결정, 로그인 화면 #4와 토큰 갱신 #5에 영향",
                              author="김팀원", sent_at="2026-11-02T01:00:00Z")

        def fake_groq(system_prompt, payload):
            if system_prompt is worklog_bridge.EXTRACT_PROMPT:
                record = payload["records"][0]
                return {"items": [{"kind": "decision", "certainty": "decided", "title": "인증 방식은 OAuth",
                                   "statement": "인증은 OAuth로 한다", "areas": ["architecture"],
                                   "source_ids": [record["source_id"]], "quote": "OAuth로 가기로 결정"}]}
            raise AssertionError("관련 항목이 없으면 판정 호출이 없어야 한다")

        with patch.object(worklog_bridge, "GROQ_API_KEY", "k"), patch.object(worklog_bridge, "_groq_json", fake_groq), \
                patch.object(worklog_bridge, "POLICY", worklog.AnalysisPolicy(idle_seconds=0)):
            results = worklog_bridge.analyze_due()
        self.assertEqual([c["change_type"] for c in results[0]["candidates"]], ["proposed"])
        listed = worklog.list_candidates(self.store, "team:T1")["candidates"]
        self.assertEqual(listed[0]["evidence"][0]["excerpt"], "OAuth로 가기로 결정")
        self.assertEqual(listed[0]["evidence"][0]["locator"], "teams:C1:m2")
        with closing(sqlite3.connect(self.store)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], 0)  # 승인 전

    def test_analysis_is_skipped_without_groq_key(self) -> None:
        self.post(turn("메모", "m3"))
        with patch.object(worklog_bridge, "GROQ_API_KEY", None):
            self.assertEqual(worklog_bridge.analyze_due(), [])

    def test_team_info_page_is_the_root_of_the_notion_map(self) -> None:
        from test_notion_map import ClientView, team_workspace

        fake = team_workspace()
        main._save_team_configs({"team:T1": {"notion_page_id": "root", "notion_token": "t", "meeting_notes_page_id": "x"}})
        with patch.object(main, "notion_client_for", return_value=ClientView(fake)):
            first = main._sync_notion_maps()["team:T1"]
            again = main._sync_notion_maps()["team:T1"]
        self.assertGreater(first["records_new"], 0)
        self.assertEqual(again["records_new"], 0)
        self.assertIn("notion:r1", [loc for loc, _ in self.sources()])

    def _config(self) -> dict:
        return {"notion_page_id": "root", "notion_token": "secret_t", "meeting_notes_page_id": "x"}

    def test_menu1_answers_from_stored_snapshot_without_calling_notion(self) -> None:
        import asyncio

        from test_notion_map import ClientView, FakeClassifier, team_workspace

        fake = team_workspace()
        with patch.object(main, "notion_client_for", return_value=ClientView(fake)), \
                patch.object(worklog_bridge, "groq_classify_sections", FakeClassifier()):
            asyncio.run(main._register_team_map("team:T1", ["root"], "secret_t"))  # 등록할 때 한 번 읽음
        fake.calls.clear()
        chunks: list[str] = []
        with patch.object(main, "notion_client_for", side_effect=AssertionError("Notion을 다시 읽으면 안 된다")), \
                patch.object(main, "GROQ_API_KEY", None):
            asyncio.run(main.fetch_project_info(self._config(), "team:T1", on_chunk=chunks.append))
        text = "".join(chunks)
        self.assertIn("우리 팀 소개", text)
        self.assertIn("로그인 API 구현 중", text)
        self.assertIn("(Notion 기준 ", text)
        self.assertEqual(fake.calls, [])

    def test_menu1_reads_once_if_registration_scan_never_ran(self) -> None:
        import asyncio

        from test_notion_map import ClientView, FakeClassifier, team_workspace

        fake = team_workspace()
        stages: list[str] = []
        with patch.object(main, "notion_client_for", return_value=ClientView(fake)), \
                patch.object(worklog_bridge, "groq_classify_sections", FakeClassifier()), patch.object(main, "GROQ_API_KEY", None):
            asyncio.run(main.fetch_project_info(self._config(), "team:T2", on_stage=stages.append, on_chunk=lambda c: None))
            first_calls = len(fake.calls)
            fake.calls.clear()
            asyncio.run(main.fetch_project_info(self._config(), "team:T2", on_stage=stages.append, on_chunk=lambda c: None))
        self.assertGreater(first_calls, 0)
        self.assertEqual(fake.calls, [])  # 두 번째부터는 저장본
        self.assertEqual(sum("처음 읽는 중" in s for s in stages), 1)

    def test_registering_the_team_page_in_setup_starts_the_scan(self) -> None:
        page = "a" * 32
        main.setup_sessions["u1"] = {"team_key": "team:T1", "step": 4,
                                     "data": {"github_repo": "o/r", "github_token": "g", "notion_token": "secret_t"}}
        started: list[tuple] = []

        async def fake_register(team_key, root, token):
            started.append((team_key, root, token))

        with patch.object(main, "_notion_page_accessible", return_value=True), \
                patch.object(main, "_register_team_map", fake_register):
            self.post(turn(f"https://www.notion.so/team-{page}", "s5"))
        self.assertEqual(started, [("team:T1", [page], "secret_t")])
        self.assertEqual(main.setup_sessions["u1"]["step"], 5)  # 마법사는 기다리지 않고 다음 질문으로

    def _notes_workspace(self):
        from test_notion_map import ClientView, FakeClassifier, TRUE_ROLES, row, team_workspace

        fake = team_workspace()
        fake.dbs["notes-db"] = [row("n1", "11/3 회의", "2026-11-03T01:00:00Z", body="배포 자동화는 김팀원이 금요일까지")]
        fake.db_titles["notes-db"] = "회의록 DB"
        classify = FakeClassifier(dict(TRUE_ROLES, **{"회의록 DB": "meeting"}))
        return fake, ClientView(fake), classify

    def test_menu2_and_menu3_answer_from_stored_notes_without_calling_notion(self) -> None:
        import asyncio

        fake, client, classify = self._notes_workspace()
        config = {"notion_page_id": "root", "meeting_notes_page_id": "notes-db", "notion_token": "secret_t",
                  "github_repo": "o/r", "github_token": "g"}
        with patch.object(main, "notion_client_for", return_value=client), \
                patch.object(worklog_bridge, "groq_classify_sections", classify):
            asyncio.run(main._register_team_map("team:T1", ["root", "notes-db"], "secret_t"))
        fake.calls.clear()

        async def no_issues(*args, **kwargs):
            return []

        with patch.object(main, "notion_client_for", side_effect=AssertionError("Notion을 다시 읽으면 안 된다")), \
                patch.object(main, "_github_get_cached", no_issues), patch.object(main, "GROQ_API_KEY", None):
            status, _ = asyncio.run(main.fetch_team_status(config, "team:T1"))
            role = asyncio.run(main.fetch_my_role(None, "김팀원", config, "team:T1"))
        self.assertIn("배포 자동화는 김팀원이 금요일까지", status)  # Groq 없으면 회의록 원문 요약줄
        self.assertIn("(Notion 기준 ", status)
        self.assertIn("📝 회의록 언급", role)
        self.assertIn("배포 자동화는 김팀원이 금요일까지", role)
        self.assertEqual(fake.calls, [])

    def test_menu2_reads_once_when_nothing_is_stored_yet(self) -> None:
        import asyncio

        fake, client, classify = self._notes_workspace()
        config = {"notion_page_id": "root", "meeting_notes_page_id": "notes-db", "notion_token": "secret_t",
                  "github_repo": "o/r", "github_token": "g"}
        stages: list[str] = []

        async def no_issues(*args, **kwargs):
            return []

        with patch.object(main, "notion_client_for", return_value=client), \
                patch.object(worklog_bridge, "groq_classify_sections", classify), \
                patch.object(main, "_github_get_cached", no_issues), patch.object(main, "GROQ_API_KEY", None):
            asyncio.run(main.fetch_team_status(config, "team:T9", on_stage=stages.append))
            fake.calls.clear()
            asyncio.run(main.fetch_team_status(config, "team:T9", on_stage=stages.append))
        self.assertEqual(sum("처음 읽는 중" in s for s in stages), 1)
        self.assertEqual(fake.calls, [])


class GroqBudgetTests(unittest.TestCase):
    """실측 보고의 재현: 14만 자 페이지, 413 실패가 캐시되는 문제, 1만 2천 자짜리 회의록 행."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "w.sqlite3"
        self.patch = patch.object(worklog_bridge, "WORKLOG_STORE", self.store)
        self.patch.start()
        main._project_summary_cache.clear()

    def tearDown(self) -> None:
        self.patch.stop()
        self.tmp.cleanup()

    def test_fit_for_groq_keeps_head_or_tail_within_budget(self) -> None:
        text = "가" * 100_000
        for keep in ("head", "tail"):
            fitted = main._fit_for_groq(text, keep=keep)
            self.assertLessEqual(len(fitted.encode("utf-8")), main.GROQ_INPUT_TOKENS * 4 + 64)
        self.assertEqual(main._fit_for_groq("짧은 글"), "짧은 글")

    def test_huge_page_snapshot_fits_the_budget(self) -> None:
        from test_notion_map import ClientView, FakeClassifier, heading, para, team_workspace

        fake = team_workspace()
        fake.pages["root"]["blocks"] += [x for i in range(60) for x in
                                          (heading(f"big{i}", f"자료 {i}", 1), para(f"bigp{i}", "스마트 옷장 자료 " * 300))]
        notion_map.sync(self.store, "team:T", "root", ClientView(fake), classify=FakeClassifier())
        snap = worklog_bridge.team_snapshot("team:T", "root", main._GROQ_INPUT_CHARS)
        self.assertLessEqual(len(snap["text"]), main._GROQ_INPUT_CHARS + 20)
        self.assertIn("우리 팀 소개", snap["text"])  # 위쪽(머리말·기본 정보)부터 담긴다

    def test_failed_summary_is_not_cached_and_is_retried(self) -> None:
        import asyncio

        calls = []

        async def failing(raw, on_chunk=None):
            calls.append(len(raw))
            return None

        snapshot = {"text": "팀 소개 " * 1000, "as_of": None}
        with patch.object(main, "_summarize_project_info", failing):
            first = asyncio.run(main._summarize_snapshot("team:T", snapshot))
            asyncio.run(main._summarize_snapshot("team:T", snapshot))
        self.assertTrue(first.startswith("⚠️ 요약을 만들지 못해"))
        self.assertLess(len(first), 1700)  # 원문 전체가 아니라 앞부분만
        self.assertEqual(len(calls), 2)  # 캐시하지 않아 다시 시도
        self.assertNotIn("team:T", main._project_summary_cache)

    def test_long_meeting_rows_are_capped_so_recent_ones_fit(self) -> None:
        from test_notion_map import ClientView, FakeClassifier, TRUE_ROLES, row, team_workspace

        fake = team_workspace()
        fake.dbs["notes-db"] = [row(f"n{i}", f"{i}차 멘토링", f"2026-11-0{i}T00:00:00Z", body="질문과 답변 " * 2000)
                                for i in range(1, 5)]
        fake.db_titles["notes-db"] = "회의록"
        notion_map.sync_roots(self.store, "team:T", ["root", "notes-db"], ClientView(fake),
                              classify=FakeClassifier(dict(TRUE_ROLES, **{"회의록": "meeting"})))
        notes = worklog_bridge.meeting_notes("team:T", "notes-db", main._GROQ_INPUT_CHARS)
        self.assertLessEqual(sum(len(t) for t in notes["texts"]), main._GROQ_INPUT_CHARS)
        titles = [t for t in notes["texts"] if "멘토링" in t]
        self.assertGreaterEqual(len(titles), 2)  # 한 행이 1만 자 넘어도 최근 몇 건이 같이 들어간다
        self.assertIn("4차 멘토링", notes["texts"][-1])
        self.assertTrue(all("이하 생략" in t for t in titles))


class DisplayTests(unittest.TestCase):
    def test_as_of_is_korean_time_even_without_tz_database(self) -> None:
        with patch("zoneinfo.ZoneInfo", side_effect=Exception("No time zone found with key Asia/Seoul")):  # Windows
            self.assertEqual(main._format_as_of("2026-11-02T07:45:47Z"), "11/02 16:45")

    def test_env_file_is_loaded_before_modules_that_read_it(self) -> None:
        # worklog_bridge·notion_read는 import 순간 GROQ_API_KEY 등을 읽는다. .env가 늦게 읽히면 그쪽만 키가 비어 버린다.
        source = Path(main.__file__).read_text(encoding="utf-8")
        self.assertLess(source.index("load_dotenv(os.environ.get"), source.index("import worklog_bridge"))
        self.assertLess(source.index("load_dotenv(os.environ.get"), source.index("import notion_map"))


if __name__ == "__main__":
    unittest.main()
