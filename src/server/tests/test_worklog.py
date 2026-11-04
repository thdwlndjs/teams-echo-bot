from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import io
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing, redirect_stdout
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worklog import core as worklog  # noqa: E402
from worklog import db  # noqa: E402
from worklog.__main__ import main  # noqa: E402
from worklog.db import ConflictError, ValidationError  # noqa: E402

SCOPE = "team:alpha"
CONV = "teams:alpha:general"


def scripted_extract(script: dict[str, list[dict]]):
    """Stands in for the LLM: each record text maps to the items it 'says'."""
    calls: list[list[str]] = []

    def extract(records: list[dict]) -> list[dict]:
        calls.append([r["source_id"] for r in records])
        items = []
        for r in records:
            for item in script.get(r["text"], []):
                items.append(dict(item, source_ids=[r["source_id"]]))
        return items

    extract.calls = calls  # type: ignore[attr-defined]
    return extract


def scripted_judge(answers: dict[str, dict]):
    """answers: item title -> {change_type, target_title?, affected_titles?, rationale}."""
    seen: list[tuple[str, list[str]]] = []

    def judge(item: dict, related: list[dict]) -> dict:
        seen.append((item["title"], [r["id"] for r in related]))
        answer = answers.get(item["title"], {"change_type": "proposed"})
        by_title = {r["title"]: r["id"] for r in related}
        return {"change_type": answer["change_type"], "target_id": by_title.get(answer.get("target_title")),
                "affected_task_ids": [by_title[t] for t in answer.get("affected_titles", []) if t in by_title],
                "rationale": answer.get("rationale", "test")}

    judge.seen = seen  # type: ignore[attr-defined]
    return judge


def no_judge(item: dict, related: list[dict]) -> dict:
    raise AssertionError("judge should not be called")


class WorklogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "store.sqlite3"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def say(self, text: str, locator: str | None = None, kind: str = "teams_message", received_at: str | None = None) -> str:
        result = worklog.record(self.store, scope=SCOPE, kind=kind, conversation_key=CONV,
                                locator=locator or f"msg:{abs(hash(text))}", text=text, author="tester",
                                received_at=received_at)
        return result["source_id"]

    def analyze(self, source_ids: list[str], script: dict, answers: dict | None = None, judge=None) -> dict:
        return worklog.analyze_batch(self.store, source_ids, scripted_extract(script),
                                     judge or scripted_judge(answers or {}))

    def count(self, sql: str, *args) -> int:
        with closing(db.connect(self.store)) as c:
            return c.execute(sql, args).fetchone()[0]

    def item(self, item_id: str) -> dict:
        with closing(db.connect(self.store)) as c:
            return dict(c.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone())

    def seed_decision_with_tasks(self) -> tuple[str, list[str]]:
        """Approved state: decision 'DB는 PostgreSQL' affecting two tasks."""
        t1, t2 = self.say("작업: 마이그레이션 스크립트 작성", "m1"), self.say("작업: 백업 정책 문서화", "m2")
        result = self.analyze([t1, t2], {
            "작업: 마이그레이션 스크립트 작성": [{"kind": "task", "title": "마이그레이션 스크립트 작성", "statement": "PostgreSQL 마이그레이션 스크립트를 쓴다", "assignee": "kim"}],
            "작업: 백업 정책 문서화": [{"kind": "task", "title": "백업 정책 문서화", "statement": "PostgreSQL 백업 정책을 정리한다"}],
        })
        tasks = [worklog.approve(self.store, c["id"], "lead")["item"]["id"] for c in result["candidates"]]
        d = self.say("결정: DB는 PostgreSQL로 간다", "m3")
        result = self.analyze([d], {"결정: DB는 PostgreSQL로 간다": [{
            "kind": "decision", "certainty": "decided", "title": "DB는 PostgreSQL", "statement": "운영 DB는 PostgreSQL을 쓴다",
            "areas": ["architecture"], "quote": "DB는 PostgreSQL로 간다"}]},
            {"DB는 PostgreSQL": {"change_type": "proposed", "affected_titles": ["마이그레이션 스크립트 작성", "백업 정책 문서화"]}})
        self.assertEqual(len(result["candidates"]), 1)
        decision = worklog.approve(self.store, result["candidates"][0]["id"], "lead")["item"]["id"]
        return decision, tasks


class PureRuleTests(unittest.TestCase):
    def test_question_and_opinion_are_not_items(self) -> None:
        ids = {"s"}
        self.assertIsNone(worklog.normalize_item({"kind": "opinion", "title": "Redis가 좋아 보임", "source_ids": ["s"]}, ids)[0])
        self.assertIsNone(worklog.normalize_item({"kind": "question", "title": "배포 언제?", "source_ids": ["s"]}, ids)[0])
        undecided = {"kind": "decision", "certainty": "proposed", "title": "캐시 도입", "source_ids": ["s"]}
        self.assertIn("not confirmed", worklog.normalize_item(undecided, ids)[1])
        asked = {"kind": "decision", "certainty": "decided", "title": "캐시 도입", "statement": "Redis로 갈까요?", "source_ids": ["s"]}
        self.assertIn("question", worklog.normalize_item(asked, ids)[1])
        missing = {"kind": "task", "title": "x", "source_ids": ["other"]}
        self.assertIn("no supporting record", worklog.normalize_item(missing, ids)[1])

    def test_progress_becomes_task_update_and_issue_refs_are_read(self) -> None:
        item, _ = worklog.normalize_item({"kind": "progress", "title": "로그인 #12 완료", "status": "done", "source_ids": ["s"]}, {"s"})
        self.assertEqual((item["kind"], item["status"], item["issue_refs"]), ("task", "done", [12]))

    def test_key_decision_rule(self) -> None:
        self.assertTrue(worklog.is_key_decision(["budget"], 0))
        self.assertTrue(worklog.is_key_decision([], 2))
        self.assertFalse(worklog.is_key_decision([], 1))

    def test_token_estimate(self) -> None:
        self.assertEqual(worklog.estimate_tokens("abcd"), 1)
        self.assertEqual(worklog.estimate_tokens("가나다라"), 3)  # 12 bytes


class RecordAndPolicyTests(WorklogTestCase):
    def test_raw_record_is_stored_once_and_edits_are_new_sources(self) -> None:
        a = self.say("회의록 1행", "notion:p:r1", kind="notion_record")
        b = self.say("회의록 1행", "notion:p:r1", kind="notion_record")
        c = self.say("회의록 1행 (수정)", "notion:p:r1", kind="notion_record")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(self.count("SELECT COUNT(*) FROM work_records"), 2)

    def test_policy_waits_then_fires_on_idle_count_or_tokens(self) -> None:
        policy = worklog.AnalysisPolicy(idle_seconds=900, max_messages=3, max_tokens=50)
        for i in range(2):
            self.say(f"m{i}", f"l{i}", received_at="2026-11-02T00:00:00Z")
        self.assertEqual(worklog.due_batches(self.store, "2026-11-02T00:10:00Z", policy), [])
        idle = worklog.due_batches(self.store, "2026-11-02T00:15:00Z", policy)
        self.assertEqual([(b["trigger"], len(b["source_ids"])) for b in idle], [("idle", 2)])
        for i in range(2, 5):
            self.say(f"m{i}", f"l{i}", received_at="2026-11-02T00:14:00Z")
        size = worklog.due_batches(self.store, "2026-11-02T00:14:30Z", policy)
        self.assertEqual([(b["trigger"], len(b["source_ids"])) for b in size], [("size", 3)])  # 2 left wait
        self.say("x" * 400, "big", received_at="2026-11-02T00:14:40Z")  # 100 tokens alone
        tokens = worklog.due_batches(self.store, "2026-11-02T00:14:50Z", policy)
        self.assertIn(("size", 1), [(b["trigger"], len(b["source_ids"])) for b in tokens])


class SourcePolicyTests(WorklogTestCase):
    def test_notion_waits_to_settle_and_only_latest_version_is_analyzed(self) -> None:
        policy = worklog.AnalysisPolicy(idle_seconds=900, max_messages=3, max_tokens=50, settle_seconds=7200)
        for i, minute in enumerate((0, 15, 30, 45)):
            worklog.record(self.store, scope=SCOPE, kind="notion_record", conversation_key="notion:sec", locator="notion:row1",
                           text="초안 v%d" % i + " 내용" * 40, received_at="2026-11-02T10:%02d:00Z" % minute)
        self.assertEqual(worklog.due_batches(self.store, "2026-11-02T12:00:00Z", policy), [])  # 크기로 당기지 않음
        batches = worklog.due_batches(self.store, "2026-11-02T12:45:00Z", policy)
        self.assertEqual([len(b["source_ids"]) for b in batches], [1])
        with closing(db.connect(self.store)) as c:
            latest = c.execute("SELECT id FROM sources WHERE content LIKE '초안 v3%'").fetchone()["id"]
            holds = [r["hold"] for r in c.execute("SELECT hold FROM work_records ORDER BY rowid")]
        self.assertEqual(batches[0]["source_ids"], [latest])
        self.assertEqual(holds, ["superseded", "superseded", "superseded", None])

    def test_held_records_are_kept_but_not_analyzed_until_released(self) -> None:
        worklog.record(self.store, scope=SCOPE, kind="notion_record", conversation_key="notion:wbs", locator="w1",
                       text="WBS 1단계", hold="reference", received_at="2026-11-01T00:00:00Z")
        self.assertEqual(worklog.due_batches(self.store, "2099-01-01T00:00:00Z"), [])
        self.assertEqual(worklog.release(self.store, "notion:wbs", "reference"), 1)
        self.assertEqual(len(worklog.due_batches(self.store, "2099-01-01T00:00:00Z")), 1)


class AnalysisTests(WorklogTestCase):
    def test_reprocessing_same_records_creates_nothing_twice(self) -> None:  # AC1
        s = self.say("작업: 로그인 API 구현 #3")
        script = {"작업: 로그인 API 구현 #3": [{"kind": "task", "title": "로그인 API 구현", "statement": "로그인 API를 만든다 #3"}]}
        first = self.analyze([s], script)
        again = self.analyze([s], script)
        self.assertTrue(again["reused"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM change_candidates"), 1)
        worklog.approve(self.store, first["candidates"][0]["id"], "lead")
        # The same message arrives again (redelivery): stored once, nothing new to analyze.
        self.assertEqual(self.say("작업: 로그인 API 구현 #3"), s)
        self.assertEqual(worklog.due_batches(self.store, "2099-01-01T00:00:00Z"), [])
        # The same statement in a new message is recognized as unchanged, not a second task.
        s2 = self.say("작업: 로그인 API 구현 #3 (다시 공유)", "other")
        result = self.analyze([s2], {"작업: 로그인 API 구현 #3 (다시 공유)": script["작업: 로그인 API 구현 #3"]}, judge=no_judge)
        self.assertEqual(result["candidates"][0]["change_type"], "unchanged")
        self.assertEqual(self.count("SELECT COUNT(*) FROM work_items"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM change_candidates WHERE status = 'recorded'"), 1)

    def test_approving_a_duplicate_new_item_is_refused(self) -> None:  # AC1 under races
        a, b = self.say("A", "a"), self.say("B", "b")
        item = [{"kind": "task", "title": "배포 파이프라인", "statement": "CI 배포를 만든다"}]
        first = self.analyze([a], {"A": item})["candidates"][0]["id"]
        second = self.analyze([b], {"B": item})["candidates"][0]["id"]  # analyzed before the first was approved
        worklog.approve(self.store, first, "lead")
        with self.assertRaises(ConflictError):
            worklog.approve(self.store, second, "lead")
        self.assertEqual(self.count("SELECT COUNT(*) FROM work_items"), 1)

    def test_opinions_questions_and_minor_decisions_never_become_decisions(self) -> None:  # AC2
        s = self.say("잡담")
        result = self.analyze([s], {"잡담": [
            {"kind": "opinion", "title": "Redis 좋아 보여요"},
            {"kind": "question", "title": "배포 언제 하나요?"},
            {"kind": "decision", "certainty": "proposed", "title": "Redis 캐시", "statement": "Redis를 써보자"},
            {"kind": "decision", "certainty": "decided", "title": "캐시 도입", "statement": "Redis 쓰는 건 어떨까요?"},
            {"kind": "decision", "certainty": "decided", "title": "버튼 색", "statement": "버튼을 파란색으로 한다"},
        ]})
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["ignored"]), 5)
        self.assertTrue(any("not a key decision" in i["reason"] for i in result["ignored"]))
        self.assertEqual(self.count("SELECT COUNT(*) FROM work_items"), 0)

    def test_conflicting_record_stays_a_candidate_until_approved(self) -> None:  # AC3 + goal 4
        decision, _ = self.seed_decision_with_tasks()
        s = self.say("근데 DB는 MySQL로 하기로 했어요", "m9")
        result = self.analyze([s], {"근데 DB는 MySQL로 하기로 했어요": [{
            "kind": "decision", "certainty": "decided", "title": "DB는 MySQL", "statement": "운영 DB는 MySQL을 쓴다 PostgreSQL 대신",
            "areas": ["architecture"]}]}, {"DB는 MySQL": {"change_type": "conflicting", "target_title": "DB는 PostgreSQL"}})
        cand = result["candidates"][0]
        self.assertEqual((cand["change_type"], cand["target_id"]), ("conflicting", decision))
        before = self.item(decision)
        self.assertEqual((before["status"], before["version"]), ("active", 1))
        listed = worklog.list_candidates(self.store, SCOPE)["candidates"]
        self.assertEqual(len(listed[0]["impacted_tasks"]), 2)
        self.assertIn(decision, listed[0]["compared_ids"])
        worklog.reject(self.store, cand["id"], "lead", "MySQL 이야기는 다른 시스템 얘기")
        self.assertEqual(self.item(decision)["version"], 1)

    def test_superseding_decision_reports_and_relinks_affected_tasks(self) -> None:  # AC4
        decision, tasks = self.seed_decision_with_tasks()
        self.assertEqual({t["id"] for t in worklog.impacted_tasks(self.store, decision)["tasks"]}, set(tasks))
        s = self.say("DB를 PostgreSQL 대신 Aurora로 바꿉니다", "m10")
        result = self.analyze([s], {"DB를 PostgreSQL 대신 Aurora로 바꿉니다": [{
            "kind": "decision", "certainty": "decided", "title": "DB는 Aurora", "statement": "운영 DB를 PostgreSQL에서 Aurora로 바꾼다",
            "areas": ["architecture", "budget"]}]}, {"DB는 Aurora": {"change_type": "superseding", "target_title": "DB는 PostgreSQL"}})
        pending = worklog.impacted_tasks(self.store, decision)["pending_changes"]
        self.assertEqual([p["change_type"] for p in pending], ["superseding"])
        applied = worklog.approve(self.store, result["candidates"][0]["id"], "cto", "비용 검토 완료")
        self.assertEqual({t["id"] for t in applied["impacted_tasks"]}, set(tasks))
        self.assertEqual(self.item(decision)["status"], "superseded")
        new_id = applied["item"]["id"]
        old = worklog.impacted_tasks(self.store, decision)
        self.assertEqual(old["superseded_by"], [new_id])
        self.assertEqual(json.loads(self.item(new_id)["attributes_json"])["accountable"], "cto")

    def test_history_and_raw_evidence_are_traceable(self) -> None:  # AC5
        decision, tasks = self.seed_decision_with_tasks()
        s = self.say("마이그레이션은 2주 안에 끝내기로 함, 담당 lee", "m11")
        result = self.analyze([s], {"마이그레이션은 2주 안에 끝내기로 함, 담당 lee": [{
            "kind": "progress", "title": "마이그레이션 스크립트 작성", "statement": "2주 안에 끝낸다", "status": "in_progress",
            "due": "2026-11-16", "quote": "2주 안에 끝내기로 함"}]},
            {"마이그레이션 스크립트 작성": {"change_type": "supplementary", "target_title": "마이그레이션 스크립트 작성"}})
        worklog.approve(self.store, result["candidates"][0]["id"], "pm")
        history = worklog.trace(self.store, tasks[0])
        self.assertEqual([(r["version"], r["actor"]) for r in history["revisions"]], [(1, "lead"), (2, "pm")])
        self.assertEqual(history["item"]["status"], "in_progress")
        self.assertEqual(history["item"]["attributes"]["due"], "2026-11-16")
        self.assertEqual(history["item"]["attributes"]["assignee"], "kim")  # supplementary never overwrites
        evidence = [e for cand in history["candidates"] for e in cand["evidence"]]
        self.assertIn("2주 안에 끝내기로 함", [e["excerpt"] for e in evidence])
        self.assertIn("m11", [e["locator"] for e in evidence])
        self.assertTrue(all(cand["compared_ids"] or cand["change_type"] == "proposed" for cand in history["candidates"]))
        with closing(db.connect(self.store)) as c:
            raw = c.execute("SELECT content FROM sources WHERE locator = 'm11'").fetchone()["content"]
        self.assertIn("담당 lee", raw)
        self.assertEqual(worklog.trace(self.store, decision)["links"][0]["type"], "AFFECTS")

    def test_progress_without_matching_task_is_ignored(self) -> None:
        s = self.say("오늘 회의실 예약함")
        result = self.analyze([s], {"오늘 회의실 예약함": [{"kind": "progress", "title": "회의실 예약", "status": "done"}]}, judge=no_judge)
        self.assertEqual(result["candidates"], [])

    def test_stale_candidate_is_not_applied(self) -> None:
        _, tasks = self.seed_decision_with_tasks()
        a, b = self.say("백업 1", "b1"), self.say("백업 2", "b2")
        answers = {"백업 정책 문서화": {"change_type": "supplementary", "target_title": "백업 정책 문서화"}}
        c1 = self.analyze([a], {"백업 1": [{"kind": "progress", "title": "백업 정책 문서화", "statement": "주간 백업"}]}, answers)
        c2 = self.analyze([b], {"백업 2": [{"kind": "progress", "title": "백업 정책 문서화", "statement": "일간 백업"}]}, answers)
        worklog.approve(self.store, c1["candidates"][0]["id"], "lead")
        with self.assertRaisesRegex(ConflictError, "STALE_CANDIDATE"):
            worklog.approve(self.store, c2["candidates"][0]["id"], "lead")

    def test_unusable_judgment_falls_back_to_human_review(self) -> None:
        decision, _ = self.seed_decision_with_tasks()
        s = self.say("DB 관련 결정 변경", "x1")
        result = self.analyze([s], {"DB 관련 결정 변경": [{
            "kind": "decision", "certainty": "decided", "title": "DB 엔진 변경", "statement": "DB PostgreSQL 설정을 바꾼다",
            "areas": ["architecture"]}]}, judge=lambda item, related: {"change_type": "superseding", "target_id": "made-up"})
        cand = worklog.list_candidates(self.store, SCOPE)["candidates"][0]
        self.assertEqual((cand["change_type"], cand["judged_by"], cand["target_id"]), ("conflicting", "fallback", decision))
        self.assertEqual(result["candidates"][0]["id"], cand["id"])

    def test_scopes_do_not_see_each_other(self) -> None:
        self.seed_decision_with_tasks()
        other = worklog.record(self.store, scope="team:beta", kind="teams_message", conversation_key="teams:beta",
                               locator="b-1", text="DB는 PostgreSQL")["source_id"]
        result = worklog.analyze_batch(self.store, [other], scripted_extract({"DB는 PostgreSQL": [{
            "kind": "decision", "certainty": "decided", "title": "DB는 PostgreSQL", "statement": "운영 DB는 PostgreSQL을 쓴다",
            "areas": ["architecture"]}]}), no_judge)
        self.assertEqual(result["candidates"][0]["change_type"], "proposed")

    def test_failed_extraction_keeps_records_pending_and_retries(self) -> None:
        s = self.say("작업: 테스트")

        def broken(records):
            raise RuntimeError("LLM down")

        with self.assertRaises(RuntimeError):
            worklog.analyze_batch(self.store, [s], broken, no_judge)
        self.assertEqual(self.count("SELECT COUNT(*) FROM work_records WHERE run_id IS NULL"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM runs WHERE status = 'failed'"), 1)
        result = self.analyze([s], {"작업: 테스트": [{"kind": "task", "title": "테스트 작성", "statement": "단위 테스트"}]})
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(self.count("SELECT attempts FROM runs"), 2)

    def test_approve_requires_actor_and_pending_state(self) -> None:
        s = self.say("작업: 문서")
        cid = self.analyze([s], {"작업: 문서": [{"kind": "task", "title": "문서", "statement": "문서를 쓴다"}]})["candidates"][0]["id"]
        with self.assertRaises(ValidationError):
            worklog.approve(self.store, cid, " ")
        worklog.approve(self.store, cid, "lead")
        with self.assertRaises(ConflictError):
            worklog.approve(self.store, cid, "lead")


class CliTests(WorklogTestCase):
    def run_cli(self, *argv: str) -> dict:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--store", str(self.store), *argv])
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def test_trace_command_shows_who_applied(self) -> None:
        s = self.say("작업: CLI 확인")
        self.analyze([s], {"작업: CLI 확인": [{"kind": "task", "title": "CLI 확인", "statement": "CLI를 확인한다"}]})
        cid = worklog.list_candidates(self.store, SCOPE)["candidates"][0]["id"]
        applied = worklog.approve(self.store, cid, "lead")
        self.assertEqual(self.run_cli("trace", applied["item"]["id"])["revisions"][0]["actor"], "lead")

    def test_cli_has_no_candidate_commands(self) -> None:  # A35: 반영은 봇 4번 메뉴에서만
        for command in ("candidates", "approve", "reject"):
            with redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                main(["--store", str(self.store), command, "x"])


    def test_extract_eval_command_reports_counts(self) -> None:  # A31 (명령 경로)
        report = self.run_cli("extract-eval", "--scope", SCOPE)
        self.assertEqual((report["parts"], report["missing_count"], report["wrong_count"]), (0, 0, 0))


class StoreTests(unittest.TestCase):
    def test_initialize_is_repeatable_and_keeps_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "s.sqlite3"
            worklog.record(store, scope=SCOPE, kind="teams_message", conversation_key=CONV, locator="a", text="원문")
            self.assertEqual(db.initialize(store)["schema_version"], db.SCHEMA_VERSION)
            with closing(sqlite3.connect(store)) as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
