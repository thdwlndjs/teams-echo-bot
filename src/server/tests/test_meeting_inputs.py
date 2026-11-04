"""Notion 밖 회의록 입구(붙여넣기·파일·클로바노트) 테스트. 문서: docs/v1.1/spec.md P1-7, Phase 4 수락 기준."""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import confirm_flow  # noqa: E402
import main  # noqa: E402
import meeting_inputs  # noqa: E402
import worklog_bridge  # noqa: E402
from test_extracts import FakeExtractor  # noqa: E402
from worklog import apply, parts  # noqa: E402

SCOPE = "team:T1"
HEADERS = {"X-Bot-Secret": "test-secret"}
NOTE = "11/4 회의\n\n1. 인증\n  - OAuth로 확정\n  - 책 등록은 어떻게?\n\n→ 식상한거 하지말고 흥미를 끌만한 주제로\n→ AI 어디에 사용?"


def turn(text: str = "", value: dict | None = None, attachments: list[dict] | None = None) -> dict:
    return {"user": {"aad_id": "u1", "name": "김팀원"}, "team": {"id": "T1", "name": "알파"}, "text": text,
            "value": value, "conversation": {"id": "C1", "type": "channel"}, "message_id": "m",
            "sent_at": "2026-11-04T01:00:00Z", "attachments": attachments or []}


def sample_docx() -> bytes:
    from docx import Document

    document = Document()
    document.add_heading("11/4 회의", level=1)
    document.add_paragraph("참석: 김팀원, 이팀원")
    document.add_paragraph("OAuth로 확정", style="List Bullet")
    document.add_paragraph("책 등록은 어떻게?", style="List Bullet 2")
    document.add_paragraph("배포 일정 정하기", style="List Number")
    document.add_paragraph("")
    table = document.add_table(rows=2, cols=2)
    for (r, c), text in {(0, 0): "할 일", (0, 1): "담당", (1, 0): "책 등록 방법 정리", (1, 1): "김팀원"}.items():
        table.cell(r, c).text = text
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


class ConvertTests(unittest.TestCase):
    def test_docx_paragraphs_lists_and_tables_become_md(self) -> None:  # A26
        md = meeting_inputs.docx_to_md(sample_docx())
        lines = md.split("\n")
        self.assertEqual(lines[0], "# 11/4 회의")
        self.assertIn("참석: 김팀원, 이팀원", lines)
        self.assertIn("- OAuth로 확정", lines)
        self.assertIn("\t- 책 등록은 어떻게?", lines)
        self.assertIn("1. 배포 일정 정하기", lines)
        self.assertIn("| 할 일 | 담당 |", lines)
        self.assertIn("| --- | --- |", lines)
        self.assertIn("| 책 등록 방법 정리 | 김팀원 |", lines)
        self.assertIn(meeting_inputs.EMPTY, lines)  # 빈 문단은 부분 경계

    def test_pasted_text_keeps_nesting_and_blank_lines(self) -> None:
        md = meeting_inputs.text_to_md(NOTE)
        self.assertIn("\t- OAuth로 확정", md.split("\n"))
        self.assertEqual(md.split("\n")[1], meeting_inputs.EMPTY)
        self.assertGreaterEqual(len(parts.build_unit_parts("p", "회의록 올리기 > x", md, None)), 3)

    def test_txt_in_cp949_is_decoded(self) -> None:
        self.assertEqual(meeting_inputs.decode_text("회의 메모".encode("cp949")), "회의 메모")

    def test_clova_files_are_told_apart(self) -> None:
        self.assertEqual(meeting_inputs.guess_origin("클로바노트_1104.txt", ""), "clova")
        self.assertEqual(meeting_inputs.guess_origin(None, "참석자 1 00:01\n안녕하세요\n참석자 2 00:05\n네"), "clova")
        self.assertEqual(meeting_inputs.guess_origin("회의.docx", NOTE), "teams")


class UploadFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "w.sqlite3"
        self.config_path = root / "team_configs.json"
        self.config_path.write_text(json.dumps({SCOPE: {"notion_page_id": "root", "notion_token": "t", "github_token": "g",
                                                        "github_repo": "o/r", "admin_aad_id": "admin"}}), encoding="utf-8")
        extractor = FakeExtractor()
        self.patches = [patch.object(worklog_bridge, "WORKLOG_STORE", self.store),
                        patch.object(main, "TEAM_CONFIG_PATH", str(self.config_path)),
                        patch.object(main, "MEMBER_PROFILE_PATH", str(root / "member_profiles.json")),
                        patch.object(worklog_bridge, "groq_extract_part", lambda payload, priority=None: extractor(payload)),
                        patch.object(confirm_flow, "_user_judge", None)]
        for p in self.patches:
            p.start()
        confirm_flow.upload_sessions.clear()
        confirm_flow.edit_sessions.clear()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        confirm_flow.upload_sessions.clear()
        self.tmp.cleanup()

    def post(self, body: dict) -> str:
        resp = self.client.post("/api/turn", json=body, headers=HEADERS)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def test_pasted_text_becomes_a_teams_part_and_goes_to_preview(self) -> None:  # A25
        self.assertIn("회의록을 보내 주세요", self.post(turn(value={"action": "upload_notes"})))
        reply = self.post(turn(NOTE))
        self.assertIn("회의록을 받았어요(Teams)", reply)
        self.assertIn("part_apply", reply)  # 미리보기 카드
        self.assertIn("책 등록 방법 정리", reply)
        found = parts.list_parts(self.store, SCOPE)
        self.assertEqual({p["origin"] for p in found}, {"teams"})
        self.assertEqual({p["kind"] for p in found}, {"meeting"})
        page_id = found[0]["page_id"]
        self.assertEqual(apply.pending_pages(self.store, SCOPE)[0]["page_id"], page_id)
        self.assertFalse(confirm_flow.has_upload_session("u1"))
        self.post(turn(value={"action": "part_apply", "page_id": page_id}))
        titles = {i["title"] for group in apply.live_items(self.store, SCOPE).values() for i in group}
        self.assertIn("책 등록 방법 정리", titles)

    def test_a_clova_docx_file_is_downloaded_and_converted(self) -> None:
        self.post(turn(value={"action": "upload_notes"}))
        with patch.object(confirm_flow, "download", new=AsyncMock(return_value=sample_docx())) as download:
            reply = self.post(turn(attachments=[{"name": "클로바노트_1104.docx", "content_type": "x",
                                                 "url": "https://files.example/1"}]))
        download.assert_awaited_once_with("https://files.example/1")
        self.assertIn("'클로바노트_1104' 회의록을 받았어요(클로바노트)", reply)
        self.assertEqual({p["origin"] for p in parts.list_parts(self.store, SCOPE)}, {"clova"})

    def test_unsupported_files_and_short_text_are_refused(self) -> None:
        self.post(turn(value={"action": "upload_notes"}))
        self.assertIn("txt·docx 파일만", self.post(turn(attachments=[{"name": "a.png", "url": "https://x/a.png"}])))
        self.assertIn("너무 짧아요", self.post(turn("네")))
        self.assertTrue(confirm_flow.has_upload_session("u1"))  # 다시 보낼 수 있게 남겨 둔다
        self.assertIn("취소했어요", self.post(turn("취소")))
        self.assertFalse(confirm_flow.has_upload_session("u1"))

    def test_clova_share_links_are_not_read(self) -> None:  # A13
        link = "https://clovanote.naver.com/s/AbCdEf123"
        self.assertIn("공유 링크는 읽을 수 없어요", self.post(turn(link)))
        self.post(turn(value={"action": "upload_notes"}))
        self.assertIn("공유 링크는 읽을 수 없어요", self.post(turn(f"이거 봐 주세요 {link}")))
        self.assertTrue(confirm_flow.has_upload_session("u1"))
        self.assertEqual(parts.list_parts(self.store, SCOPE), [])

    def test_first_scan_notice_is_shown_once(self) -> None:
        built = parts.build_unit_parts("doc-1", "팀 정보 > 기획서", "# 기획서\n본문 내용", None)
        parts.save_unit_parts(self.store, SCOPE, "notion", "doc-1", None, built)
        self.assertIsNone(confirm_flow.first_scan_notice(SCOPE))  # 판정 전에는 아직
        for part in parts.list_parts(self.store, SCOPE):
            parts.set_part_kind(self.store, part["id"], "document", "test")
        first = self.post(turn(value=None) | {"kind": "welcome"})
        self.assertIn("회의록을 못 찾았어요", first)
        self.assertIn("upload_notes", first)
        self.assertNotIn("회의록을 못 찾았어요", self.post(turn(value=None) | {"kind": "welcome"}))


if __name__ == "__main__":
    unittest.main()
