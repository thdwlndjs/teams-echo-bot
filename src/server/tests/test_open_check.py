"""챗봇을 열 때 변경 감지 테스트. 문서: docs/v1.1/spec.md P2-6, Phase 5 수락 기준(A29)."""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402

CONFIG = {"notion_page_id": "root", "notion_token": "t", "meeting_notes_page_id": "notes"}


class OpenCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        main._last_team_check.clear()
        self.spawned: list = []
        self.patches = [patch.dict(os.environ, {"WORKLOG_LOOP": "1"}),
                        patch.object(main, "_spawn", side_effect=lambda coro: (self.spawned.append(coro), coro.close()))]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        main._last_team_check.clear()

    def test_reopening_within_ten_minutes_does_not_call_notion(self) -> None:  # A29
        with patch.object(main.time, "monotonic", return_value=1000.0):
            main._check_on_open("team:T1", CONFIG)
            main._check_on_open("team:T1", CONFIG)
        with patch.object(main.time, "monotonic", return_value=1000.0 + 599):
            main._check_on_open("team:T1", CONFIG)
        self.assertEqual(len(self.spawned), 1)
        with patch.object(main.time, "monotonic", return_value=1000.0 + 601):
            main._check_on_open("team:T1", CONFIG)
        self.assertEqual(len(self.spawned), 2)

    def test_teams_are_checked_separately_and_without_notion_settings_not_at_all(self) -> None:
        with patch.object(main.time, "monotonic", return_value=50.0):
            main._check_on_open("team:T1", CONFIG)
            main._check_on_open("team:T2", CONFIG)
            main._check_on_open("team:T3", {"github_repo": "o/r"})
        self.assertEqual(len(self.spawned), 2)

    def test_the_check_reads_only_changed_pages_of_the_registered_roots(self) -> None:
        calls = []
        with patch.object(main, "_sync_team_map", side_effect=lambda *a: calls.append(a) or {}):
            import asyncio
            with patch.object(main, "_spawn", side_effect=lambda coro: asyncio.run(coro)):
                main._check_on_open("team:T1", CONFIG)
        self.assertEqual(calls, [("team:T1", ["root", "notes"], "t", True)])


if __name__ == "__main__":
    unittest.main()
