"""단계 길잡이(게이트 판정·단계 제안·답) 테스트. 문서: docs/v1.1/spec.md P2-3, P2-4, Phase 5 수락 기준(A15, A16, A28).

판정 LLM은 가짜다(근거 항목 ID 규칙·반복 방지·승인 규칙을 본다). 실제 판정 품질은 실제 Notion으로 따로 확인한다.
"""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import confirm_flow  # noqa: E402
import main  # noqa: E402
import worklog_bridge  # noqa: E402
from worklog import db, people, stages  # noqa: E402

SCOPE = "team:T1"
HEADERS = {"X-Bot-Secret": "test-secret"}


def add_item(store: Path, item_id: str, kind: str, title: str, statement: str = "") -> None:
    db.initialize(store)
    with db.write(store) as c:
        c.execute("""INSERT INTO work_items(id, scope, kind, title, normalized_title, statement_md, status, attributes_json,
                         version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'active', '{}', 1, ?, ?)""",
                  (item_id, SCOPE, kind, title, db.normalize_text(title), statement or title, db.now(), db.now()))


class ScriptedJudge:
    """게이트별 답을 정해 둔 가짜 판정. 받은 입력을 남긴다."""

    def __init__(self, gates: dict, discussing: dict | None = None, overturned: dict | None = None) -> None:
        self.gates, self.discussing, self.overturned = gates, discussing or {"stage": None, "evidence": []}, overturned
        self.calls: list[dict] = []

    def __call__(self, payload: dict) -> dict:
        self.calls.append(payload)
        return {"gates": self.gates, "discussing": self.discussing, "overturned": self.overturned}


def this_team(store: Path) -> None:
    """이 팀의 실제 상황을 줄인 것: 후보 주제와 문제는 있고, 주제는 아직 확정 전(기획서 '현재 상태: 팀 주제 후보')."""
    add_item(store, "i-cand-1", "open", "후보 주제: 공유 우산 대여", "갑자기 비가 올 때 우산이 없다는 문제를 푼다")
    add_item(store, "i-cand-2", "open", "후보 주제: 강의실 빈자리 알림", "빈 강의실을 찾느라 시간을 쓴다는 문제")
    add_item(store, "i-fb-1", "feedback", "식상한 주제는 피하라")


class StageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "w.sqlite3"
        this_team(self.store)
        self.judge = ScriptedJudge({"candidates": {"met": True, "evidence": ["i-cand-1", "i-cand-2"]},
                                    "topic_decided": {"met": False, "evidence": []}})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_this_team_is_at_1_2_missing_the_topic_decision(self) -> None:  # A15
        stages.assess(self.store, SCOPE, {}, self.judge)
        self.assertEqual(stages.first_open_stage(self.store, SCOPE), "1-2")
        found = stages.proposals(self.store, SCOPE)
        self.assertEqual([p["kind"] for p in found], ["stage_init"])
        self.assertIn("1-2 방향 확정", found[0]["text"])
        self.assertIn("주제 확정 결정이 있다", found[0]["text"])
        stages.answer(self.store, SCOPE, "stage_init", found[0]["key"], "start_here", "관리자")
        self.assertEqual(stages.current(self.store, SCOPE), "1-2")
        missing = [p["gate_id"] for p in stages.proposals(self.store, SCOPE) if p["kind"] == "gate_missing"]
        self.assertEqual(missing, ["core_problem", "topic_decided"])
        payload = self.judge.calls[0]
        self.assertEqual({i["id"] for i in payload["items"]}, {"i-cand-1", "i-cand-2", "i-fb-1"})
        self.assertNotIn("backlog", {g["gate_id"] for g in payload["gates"]})  # 코드로 보는 게이트는 LLM에 안 보낸다

    def test_a_true_gate_without_known_evidence_is_not_accepted(self) -> None:
        judge = ScriptedJudge({"candidates": {"met": True, "evidence": []},
                               "topic_decided": {"met": True, "evidence": ["없는-id"]}})
        found = stages.assess(self.store, SCOPE, {}, judge)
        self.assertFalse(found["gates"]["candidates"]["met"])
        self.assertFalse(found["gates"]["topic_decided"]["met"])

    def test_code_gates_use_github_and_insights(self) -> None:
        add_item(self.store, "i-voc", "insight", "고객 5명 중 4명이 아침 옷 고르기를 귀찮아함")
        found = stages.assess(self.store, SCOPE, {"open_issues": 3, "milestones": 0, "closed_issues": 0}, self.judge)
        self.assertEqual(found["gates"]["voc_collected"], {"met": True, "evidence": ["i-voc"], "by": "code"})
        self.assertTrue(found["gates"]["backlog"]["met"])
        self.assertFalse(found["gates"]["milestones"]["met"])

    def test_same_items_are_not_judged_twice(self) -> None:
        stages.assess(self.store, SCOPE, {}, self.judge)
        self.assertIsNone(stages.assess(self.store, SCOPE, {}, self.judge))
        add_item(self.store, "i-new", "decision", "주제는 공유 우산 대여로 확정")
        self.assertIsNotNone(stages.assess(self.store, SCOPE, {}, self.judge))
        self.assertEqual(len(self.judge.calls), 2)

    def test_the_stage_never_changes_without_an_approved_answer(self) -> None:  # A28
        stages.answer(self.store, SCOPE, "stage_init", stages.proposals(self.store, SCOPE)[0]["key"]
                      if stages.assess(self.store, SCOPE, {}, self.judge) else "", "start_here", "관리자")
        all_met = ScriptedJudge({g["gate_id"]: {"met": True, "evidence": ["i-cand-1"]} for g in stages.gates(self.store, SCOPE)})
        stages.assess(self.store, SCOPE, {"open_issues": 1, "milestones": 1, "closed_issues": 1}, all_met, force=True)
        self.assertEqual(stages.current(self.store, SCOPE), "1-2")  # 다 충족돼도 제안만 한다
        advance = [p for p in stages.proposals(self.store, SCOPE) if p["kind"] == "stage_advance"]
        self.assertEqual(len(advance), 1)
        self.assertEqual(stages.answer(self.store, SCOPE, "stage_advance", advance[0]["key"], "stay", "관리자"), "알겠어요.")
        self.assertEqual(stages.current(self.store, SCOPE), "1-2")
        history = stages._row(self.store, SCOPE)["history"]
        self.assertEqual([(h["to"], h["by"]) for h in history], [("1-2", "관리자")])

    def test_an_answered_question_is_not_asked_again_until_the_gate_changes(self) -> None:  # A16
        stages.assess(self.store, SCOPE, {}, self.judge)
        stages.answer(self.store, SCOPE, "stage_init", stages.proposals(self.store, SCOPE)[0]["key"], "start_here", "관리자")
        topic = next(p for p in stages.proposals(self.store, SCOPE) if p.get("gate_id") == "topic_decided")
        stages.answer(self.store, SCOPE, "gate_missing", topic["key"], "decide", "관리자")
        self.assertNotIn("topic_decided", [p.get("gate_id") for p in stages.proposals(self.store, SCOPE)])
        with self.assertRaises(db.ConflictError):
            stages.answer(self.store, SCOPE, "gate_missing", topic["key"], "decide", "관리자")
        # 근거가 바뀌면(새 항목이 다른 판정을 낳으면) 새 질문으로 본다
        add_item(self.store, "i-half", "open", "주제 확정 투표 일정")
        self.judge.gates["topic_decided"] = {"met": False, "evidence": ["i-half"]}
        stages.assess(self.store, SCOPE, {}, self.judge)
        self.assertIn("topic_decided", [p.get("gate_id") for p in stages.proposals(self.store, SCOPE)])

    def test_discussing_ahead_asks_about_skipped_gates_and_skip_removes_them(self) -> None:
        add_item(self.store, "i-voc-plan", "decision", "대학생 10명 인터뷰로 검증")
        judge = ScriptedJudge(self.judge.gates, discussing={"stage": "2-1", "evidence": ["i-voc-plan"]})
        stages.assess(self.store, SCOPE, {}, judge)
        stages.answer(self.store, SCOPE, "stage_init", stages.proposals(self.store, SCOPE)[0]["key"], "start_here", "관리자")
        ahead = [p for p in stages.proposals(self.store, SCOPE) if p["kind"] == "skipped_ahead"]
        self.assertEqual({p["gate_id"] for p in ahead}, {"assumptions", "target_customer"})  # 1-3 단계의 빈 게이트
        target = next(p for p in ahead if p["gate_id"] == "target_customer")
        self.assertIn("건너뛰었어요", stages.answer(self.store, SCOPE, "skipped_ahead", target["key"], "skip", "관리자"))
        self.assertEqual({g["gate_id"]: g["state"] for g in stages.gates(self.store, SCOPE, "1-3")}["target_customer"], "skipped")
        later = next(p for p in stages.proposals(self.store, SCOPE) if p.get("gate_id") == "assumptions")
        stages.answer(self.store, SCOPE, "skipped_ahead", later["key"], "later", "관리자")
        self.assertEqual([g["gate_id"] for g in stages.board(self.store, SCOPE)["earlier"]], [])  # 1-3은 현재(1-2)보다 뒤

    def test_first_proposal_offers_the_discussed_stage_when_it_is_ahead(self) -> None:
        add_item(self.store, "i-voc-run", "insight", "커뮤니티 VOC에서 우산 분실 고민 반복")
        judge = ScriptedJudge(self.judge.gates, discussing={"stage": "2-2", "evidence": ["i-voc-run"]})
        stages.assess(self.store, SCOPE, {}, judge)
        init = stages.proposals(self.store, SCOPE)[0]
        self.assertIn("2-2 VOC 수집 내용까지 논의하고 있어요", init["text"])
        self.assertIn(("start_discussing", "2-2로"), init["answers"])
        stages.answer(self.store, SCOPE, "stage_init", init["key"], "start_discussing", "관리자")
        self.assertEqual(stages.current(self.store, SCOPE), "2-2")
        behind = {p["gate_id"] for p in stages.proposals(self.store, SCOPE) if p["kind"] == "skipped_ahead"}
        # 건너뛴 앞 단계의 빈 게이트를 묻는다(후보 주제는 충족이라 빠진다)
        self.assertEqual(behind, {"topic_decided", "core_problem", "assumptions", "target_customer", "voc_method"})
        missing = {p["gate_id"] for p in stages.proposals(self.store, SCOPE) if p["kind"] == "gate_missing"}
        self.assertEqual(missing, set())  # 2-2의 게이트(VOC 수집)는 insight로 충족

    def test_voc_overturning_an_assumption_offers_to_go_back(self) -> None:
        add_item(self.store, "i-voc-2", "insight", "아무도 대여소 위치 알림을 원하지 않음")
        judge = ScriptedJudge(self.judge.gates, overturned={"assumption": "대여소 위치 알림이 필요하다", "evidence": ["i-voc-2"]})
        stages.assess(self.store, SCOPE, {}, judge)
        with db.write(self.store) as c:
            stages._set_stage(c, SCOPE, "2-3", "관리자", "테스트")
        pivot = next(p for p in stages.proposals(self.store, SCOPE) if p["kind"] == "pivot")
        self.assertIn("돌아갔어요", stages.answer(self.store, SCOPE, "pivot", pivot["key"], "pivot", "관리자"))
        self.assertEqual(stages.current(self.store, SCOPE), "1-3")


class StageCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = root / "w.sqlite3"
        (root / "team_configs.json").write_text(json.dumps({SCOPE: {
            "notion_page_id": "root", "notion_token": "t", "github_token": "g", "github_repo": "o/r",
            "admin_aad_id": "admin"}}), encoding="utf-8")
        self.patches = [patch.object(worklog_bridge, "WORKLOG_STORE", self.store),
                        patch.object(main, "TEAM_CONFIG_PATH", str(root / "team_configs.json")),
                        patch.object(main, "MEMBER_PROFILE_PATH", str(root / "member_profiles.json"))]
        for p in self.patches:
            p.start()
        this_team(self.store)
        stages.assess(self.store, SCOPE, {}, ScriptedJudge({"candidates": {"met": True, "evidence": ["i-cand-1"]}}))
        people.touch(self.store, SCOPE, "plan1", "이기획")
        people.set_role(self.store, "plan1", "plan")
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def post(self, user: str, value: dict | None = None, kind: str = "message") -> str:
        body = {"kind": kind, "user": {"aad_id": user, "name": user}, "team": {"id": "T1", "name": "알파"}, "text": "",
                "value": value, "conversation": {"id": "C1", "type": "channel"}, "message_id": "m",
                "sent_at": "2026-11-04T01:00:00Z"}
        resp = self.client.post("/api/turn", json=body, headers=HEADERS)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def test_main_card_shows_the_proposal_and_only_approvers_can_answer(self) -> None:  # A14, A28
        member = self.post("plan1", kind="welcome")
        self.assertIn("1-2 방향 확정", member)
        self.assertIn("권한자 확인 대기 중", member)
        self.assertNotIn("stage_answer", member)
        admin = self.post("admin", kind="welcome")
        self.assertIn("stage_answer", admin)
        key = stages.proposals(self.store, SCOPE)[0]["key"]
        answer = {"action": "stage_answer", "kind": "stage_init", "key": key, "reply": "start_here"}
        self.assertIn("권한자", self.post("plan1", answer))
        self.assertIsNone(stages.current(self.store, SCOPE))
        self.assertIn("현재 단계를 정했어요: 1-2 방향 확정", self.post("admin", answer))
        self.assertEqual(stages.current(self.store, SCOPE), "1-2")
        self.assertIn("이미 답했거나", self.post("admin", answer))


if __name__ == "__main__":
    unittest.main()
