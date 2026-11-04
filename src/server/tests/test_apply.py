"""확인 반영·되돌리기·상태 전이·정답지 테스트. 문서: docs/v1.1/spec.md P1-5, P1-6, Phase 3 수락 기준."""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest.mock import patch  # noqa: E402

import confirm_flow  # noqa: E402
import worklog_bridge  # noqa: E402
from test_extracts import FakeExtractor  # noqa: E402
from test_parts import SAMPLE_BLOCKS, SAMPLE_MD, split_classifier  # noqa: E402
from worklog import apply, core, parts  # noqa: E402

SCOPE = "team:alpha"
V = "test-v1"
EDITED = "2026-11-01T00:00:00Z"


class ApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "w.sqlite3"
        self.collect(SAMPLE_MD)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def collect(self, md: str, edited: str = EDITED, extractor=None) -> None:
        core.record(self.store, scope=SCOPE, kind="notion_record", conversation_key="notion:sec-1", locator="notion:unit-1",
                    text=parts.md_to_text(md), sent_at=edited)
        built = parts.build_unit_parts("unit-1", "회의록 > 1차 멘토링", md, SAMPLE_BLOCKS)
        parts.save_unit_parts(self.store, SCOPE, "notion", "unit-1", "sec-1", built, last_edited=edited)
        parts.judge_pending(self.store, SCOPE, split_classifier(), budget_tokens=None)
        parts.extract_pending(self.store, SCOPE, extractor or FakeExtractor(), V, budget_tokens=None)

    def items(self, status: str | None = None) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.store)) as c:
            c.row_factory = sqlite3.Row
            query = "SELECT * FROM work_items WHERE scope = ?" + (" AND status = ?" if status else "")
            return c.execute(query, (SCOPE, status) if status else (SCOPE,)).fetchall()

    def test_only_confirming_creates_items_and_cancel_creates_none(self) -> None:  # A7
        self.assertEqual(self.items(), [])  # 추출만으로는 반영되지 않는다
        apply.request_confirm(self.store, SCOPE, "unit-1")
        self.assertEqual(apply.pending_pages(self.store, SCOPE)[0]["page_id"], "unit-1")
        summary = apply.apply_page(self.store, SCOPE, "unit-1", "송지원", V)
        titles = {r["title"] for r in self.items()}
        self.assertIn("AI를 어디에 쓸지", titles)
        self.assertIn("책 등록 방법 정리", titles)
        self.assertGreaterEqual(summary["applied"], 2)
        states = {p["state"] for p in apply.meeting_parts(self.store, SCOPE, "unit-1")}
        self.assertEqual(states, {"applied"})
        with closing(sqlite3.connect(self.store)) as c:
            actors = {r[0] for r in c.execute("SELECT actor FROM revisions")}
        self.assertEqual(actors, {"송지원"})  # 확인한 사람이 반영자

    def test_cancel_skips_the_page(self) -> None:  # A7
        apply.request_confirm(self.store, SCOPE, "unit-1")
        self.assertGreater(apply.skip_page(self.store, SCOPE, "unit-1", "송지원"), 0)
        self.assertEqual(self.items(), [])
        self.assertEqual(apply.pending_pages(self.store, SCOPE), [])

    def test_corrections_are_saved_and_become_the_answer_key(self) -> None:  # A8, A31
        page = apply.preview(self.store, SCOPE, "unit-1", V)
        part_id = next(i["part_id"] for i in page["items"] if i["title"] == "AI를 어디에 쓸지")
        part = next(p for p in apply.meeting_parts(self.store, SCOPE, "unit-1") if p["id"] == part_id)
        before = apply.extracted_items(self.store, part, V)
        after = [dict(i, title="AI 활용처 정하기") if i["title"] == "AI를 어디에 쓸지" else i for i in before]
        after.append({"kind": "feedback", "title": "센서 원가를 줄여라", "quote": "저렴한 센서"})
        apply.save_correction(self.store, part, V, before, after, "송지원")
        with closing(sqlite3.connect(self.store)) as c:
            self.assertEqual(c.execute("SELECT actor FROM extract_corrections").fetchone()[0], "송지원")
        report = apply.extract_eval(self.store, SCOPE, V)
        self.assertEqual(report["parts"], 1)
        self.assertEqual(report["missing_count"], 1)  # 정답지에만 있는 항목
        self.assertEqual(report["wrong_count"], 0)  # 제목은 달라도 인용이 같으면 같은 항목
        self.assertEqual(report["missing"][0]["title"], "센서 원가를 줄여라")

    def test_edits_to_a_confirmed_note_apply_only_new_items(self) -> None:  # A9
        class WithNewLine(FakeExtractor):
            def __call__(self, payload: dict) -> dict:
                answer = super().__call__(payload)
                if "센서 단가를 다시 알아본다" in payload["text"]:
                    answer["items"].append({"kind": "task", "title": "센서 단가 조사", "quote": "센서 단가를 다시 알아본다"})
                return answer

        apply.apply_page(self.store, SCOPE, "unit-1", "송지원", V)
        count = len(self.items())
        edited = SAMPLE_MD.replace("6. 데이터 근거 제출 필요", "6. 데이터 근거 제출 필요\n\t\t7. 센서 단가를 다시 알아본다")
        self.collect(edited, edited="2026-11-02T00:00:00Z", extractor=WithNewLine())
        changes = apply.apply_edits(self.store, SCOPE, V)
        self.assertEqual(len(changes), 1)
        new_titles = [r["title"] for r in changes[0]["results"]]
        self.assertEqual(new_titles, ["센서 단가 조사"])  # 원래 있던 항목은 다시 반영하지 않는다
        self.assertEqual(len(self.items()), count + 1)
        self.assertEqual(apply.apply_edits(self.store, SCOPE, V), [])  # 한 번 반영하면 끝
        self.assertEqual(apply.auto_applied_count(self.store, SCOPE), 1)  # 메인 카드 "반영 n건"
        later = datetime.now(timezone.utc) + timedelta(days=2)
        self.assertEqual(apply.auto_applied_count(self.store, SCOPE, now=later), 0)
        with patch.object(worklog_bridge, "WORKLOG_STORE", self.store):
            lines, buttons = confirm_flow.main_card_alerts(SCOPE)
        self.assertIn("✍️ 확인된 회의록이 수정돼 1건을 반영했어요 (최근 24시간)", lines)
        self.assertIn({"action": "applied_list"}, [b["data"] for b in buttons])

    def test_a_deleted_confirmed_part_keeps_its_items_until_approved(self) -> None:  # A10
        apply.apply_page(self.store, SCOPE, "unit-1", "송지원", V)
        shorter = SAMPLE_MD.split("1. 배낭")[0] + "<empty-block/>\n"
        built = parts.build_unit_parts("unit-1", "회의록 > 1차 멘토링", shorter, None)
        parts.save_unit_parts(self.store, SCOPE, "notion", "unit-1", "sec-1", built, last_edited="2026-11-03T00:00:00Z")
        gone = apply.gone_confirmed_parts(self.store, SCOPE)
        self.assertTrue(gone)
        feedback = {r["title"]: r["status"] for r in self.items()}
        self.assertEqual(feedback["흥미를 끌 주제로"], "active")  # 사라진 결론 부분에서 나온 항목: 확인 전에는 그대로
        removed = sum(apply.resolve_gone_part(self.store, SCOPE, g["id"], True, "송지원") for g in gone)
        self.assertGreater(removed, 0)
        self.assertEqual({r["title"]: r["status"] for r in self.items()}["흥미를 끌 주제로"], "cancelled")
        self.assertEqual({r["title"]: r["status"] for r in self.items()}["AI를 어디에 쓸지"], "active")  # 남은 부분의 항목은 그대로
        self.assertEqual(apply.gone_confirmed_parts(self.store, SCOPE), [])

    def test_low_confidence_items_need_discussion(self) -> None:
        class Unsure(FakeExtractor):
            def __call__(self, payload: dict) -> dict:
                answer = super().__call__(payload)
                for item in answer["items"]:
                    item["confidence"] = 0.4
                return answer

        other = Path(self.tmp.name) / "unsure.sqlite3"
        self.store = other
        self.collect(SAMPLE_MD, extractor=Unsure())
        apply.apply_page(self.store, SCOPE, "unit-1", "송지원", V)
        self.assertTrue(self.items())
        self.assertEqual({r["status"] for r in self.items()}, {"needs_discussion"})


    def test_parts_extracted_before_states_existed_join_the_confirm_flow(self) -> None:
        # Phase 2에서 추출만 해 두고 상태는 '수집'으로 남은 부분: 캐시가 있어 다시 추출되지 않아도 확인 대기로 가야 한다
        with closing(sqlite3.connect(self.store)) as c:
            c.execute("UPDATE source_parts SET state = 'collected' WHERE kind = 'meeting'")
            c.commit()
        parts.extract_pending(self.store, SCOPE, None, V)
        self.assertEqual({p["state"] for p in apply.meeting_parts(self.store, SCOPE, "unit-1")}, {"extracted"})
        self.assertGreater(apply.promote_due(self.store, SCOPE, datetime(2026, 11, 4, tzinfo=timezone.utc)), 0)

class SupersedeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "w.sqlite3"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def meeting(self, unit: str, decision: str, edited: str) -> None:
        md = f"1. 주제 회의\n\t- {decision}"
        core.record(self.store, scope=SCOPE, kind="notion_record", conversation_key="notion:sec", locator=f"notion:{unit}",
                    text=md, sent_at=edited)
        parts.save_unit_parts(self.store, SCOPE, "notion", unit, "sec", parts.build_unit_parts(unit, f"회의록 > {unit}", md, None),
                              last_edited=edited)
        parts.judge_pending(self.store, SCOPE, lambda page: {"parts": [
            {"part_id": p["part_id"], "kind": "meeting", "confidence": 0.9, "split": []} for p in page["parts"]]})
        parts.extract_pending(self.store, SCOPE, lambda payload: {"items": [
            {"kind": "decision", "title": decision, "quote": decision, "confidence": 0.9}], "usage": {}, "model": "fake"}, V)

    def test_a_superseding_decision_is_applied_and_can_be_reverted(self) -> None:  # A11
        self.meeting("m1", "주제는 스마트 책장", "2026-10-01T00:00:00Z")
        apply.apply_page(self.store, SCOPE, "m1", "송지원", V)
        self.meeting("m2", "주제는 스마트 옷장", "2026-10-08T00:00:00Z")

        def judge(item: dict, related: list[dict]) -> dict:
            return {"change_type": "superseding", "target_id": related[0]["id"], "rationale": "주제가 바뀜"}

        summary = apply.apply_page(self.store, SCOPE, "m2", "송지원", V, judge=judge)
        self.assertEqual(summary["superseded"][0]["old"], "주제는 스마트 책장")
        candidate_id = summary["superseded"][0]["candidate_id"]
        with closing(sqlite3.connect(self.store)) as c:
            status = dict(c.execute("SELECT title, status FROM work_items").fetchall())
        self.assertEqual(status, {"주제는 스마트 책장": "superseded", "주제는 스마트 옷장": "active"})
        self.assertEqual(apply.recent_applied(self.store, SCOPE)[0]["id"], candidate_id)

        apply.revert(self.store, candidate_id, "송지원")
        with closing(sqlite3.connect(self.store)) as c:
            status = dict(c.execute("SELECT title, status FROM work_items").fetchall())
            links = c.execute("SELECT COUNT(*) FROM work_links WHERE candidate_id = ?", (candidate_id,)).fetchone()[0]
        self.assertEqual(status, {"주제는 스마트 책장": "active", "주제는 스마트 옷장": "reverted"})
        self.assertEqual(links, 0)
        with self.assertRaises(core.ConflictError):
            apply.revert(self.store, candidate_id, "송지원")  # 두 번 되돌리지 않는다


class PromoteTests(unittest.TestCase):  # A23
    def test_new_notes_wait_two_hours_after_the_last_edit(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Path(tmp.name) / "w.sqlite3"
        now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
        for unit, minutes in (("fresh", 30), ("settled", 150)):
            edited = (now - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
            md = f"1. {unit} 회의\n\t- 메모"
            parts.save_unit_parts(store, SCOPE, "notion", unit, "sec", parts.build_unit_parts(unit, f"회의록 > {unit}", md, None),
                                  last_edited=edited)
        parts.judge_pending(store, SCOPE, lambda page: {"parts": [
            {"part_id": p["part_id"], "kind": "meeting", "confidence": 0.9, "split": []} for p in page["parts"]]})
        parts.extract_pending(store, SCOPE, lambda payload: {"items": [], "usage": {}, "model": "fake"}, V)
        self.assertEqual(apply.promote_due(store, SCOPE, now), 1)
        self.assertEqual([p["page_id"] for p in apply.pending_pages(store, SCOPE)], ["settled"])


if __name__ == "__main__":
    unittest.main()
