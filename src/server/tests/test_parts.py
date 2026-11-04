"""부분 분할·블록 대조·종류 판정 저장 테스트. 문서: docs/v1.1/spec.md P1-1~P1-3, Phase 1 수락 기준."""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import notion_md  # noqa: E402
from worklog import parts  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_MD = (FIXTURES / "notion" / "meeting_sample.md").read_text(encoding="utf-8")
SAMPLE_BLOCKS = json.loads((FIXTURES / "notion" / "meeting_sample.blocks.json").read_text(encoding="utf-8"))
LOCAL = FIXTURES / "notion_local"
MENTORING_1 = "05511be9-7ce7-8286-a907-8185eb3fdee7"
MENTORING_2 = "2fe11be9-7ce7-838b-82b2-810b88af7c9b"
SCOPE = "team:alpha"


def split_classifier(kinds: dict[str, str] | None = None, split_titles: tuple[str, ...] = ("구체화",)):
    """가짜 종류 판정: 제목에 따라 종류를 정하고, 하위 토글 중 split_titles는 document로 떼어 낸다."""
    calls: list[dict] = []

    def classify(page: dict) -> dict:
        calls.append(page)
        out = []
        for item in page["parts"]:
            kind = (kinds or {}).get(item["title"], "meeting")
            split = [{"child_path": c["child_path"], "kind": "document", "summary": "예시 기획서"}
                     for c in item["children"] if c["child_path"].rsplit(" > ", 1)[-1] in split_titles]
            out.append({"part_id": item["part_id"], "kind": kind, "confidence": 0.9,
                        "summary": "링크" if kind != "meeting" else None, "split": split})
        return {"parts": out}

    classify.calls = calls  # type: ignore[attr-defined]
    return classify


class SplitTests(unittest.TestCase):
    def test_sample_groups_into_six_parts(self) -> None:  # A1
        groups = parts.group_parts(parts.top_level_blocks(SAMPLE_MD))
        self.assertEqual([g.title for g in groups],
                         ["화분 관리기", "스마트 책장", "배낭", "정기 배송 Agent(긍정적)", "음성 비서",
                          "→ 정기 배송 쪽으로 가면 비즈니스적으로 설명하기는 좋을듯"])
        self.assertIn("녹음 노트 공유", groups[-1].md)  # 결론 → 줄과 녹음 노트 링크가 한 부분

    def test_real_recordings_group_as_expected(self) -> None:  # A1 (실제 녹화본이 있을 때만)
        if not (LOCAL / f"{MENTORING_1}.md").exists():
            self.skipTest("실제 녹화본 없음")
        for page_id, expected in ((MENTORING_1, 6), (MENTORING_2, 5)):
            md = (LOCAL / f"{page_id}.md").read_text(encoding="utf-8")
            self.assertEqual(len(parts.group_parts(parts.top_level_blocks(md))), expected, page_id)

    def test_table_cells_stay_in_their_part(self) -> None:  # A3
        groups = parts.group_parts(parts.top_level_blocks(SAMPLE_MD))
        joined = "\n".join(g.md for g in groups)
        for cell in ("원하는 책을 빨리 찾고 싶다", "선반 조명, 위치 안내", "배송 일시정지"):
            self.assertIn(cell, joined)
        self.assertIn("대출 기록과 반납 알림", groups[1].md)
        self.assertIn("상품 전환", groups[3].md)
        self.assertIn("대출 기록과 반납 알림", parts.md_to_text(groups[1].md))

    def test_alignment_gives_block_ids_and_detects_mismatch(self) -> None:  # A4
        blocks = parts.top_level_blocks(SAMPLE_MD)
        ids = parts.align_blocks(blocks, SAMPLE_BLOCKS)
        self.assertEqual(ids[0], "blk-00")
        self.assertIsNone(parts.align_blocks(blocks, SAMPLE_BLOCKS[:-1]))  # 개수가 다름
        swapped = [dict(b) for b in SAMPLE_BLOCKS]
        swapped[10]["type"] = "paragraph"  # 구분선 자리에 단락
        self.assertIsNone(parts.align_blocks(blocks, swapped))

    def test_details_children_report_toggle_paths_and_sizes(self) -> None:
        groups = parts.group_parts(parts.top_level_blocks(SAMPLE_MD))
        children = parts.details_children(groups[1].md)
        titles = [c["title"] for c in children]
        self.assertEqual(titles, ["기능", "멘토링", "구체화"])
        self.assertGreater(children[2]["chars"], parts.CHILD_MIN_CHARS)
        self.assertGreater(children[2]["chars"], children[0]["chars"] + children[1]["chars"])


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "worklog.sqlite3"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def collect(self, md: str = SAMPLE_MD, blocks: list[dict] | None = SAMPLE_BLOCKS, classify=None) -> list[dict]:
        built = parts.build_unit_parts("unit-1", "회의록 > 1차 멘토링", md, blocks)
        parts.save_unit_parts(self.store, SCOPE, "notion", "unit-1", "sec-1", built)
        parts.judge_pending(self.store, SCOPE, classify or split_classifier())
        return parts.list_parts(self.store, SCOPE, page_id="unit-1")

    def test_document_child_is_split_out_of_its_meeting_part(self) -> None:  # A2
        stored = self.collect()
        by_title = {p["path"].rsplit(" > ", 1)[-1]: p for p in stored}
        self.assertEqual(len(stored), 7)
        document = by_title["구체화"]
        self.assertEqual((document["kind"], document["summary"]), ("document", "예시 기획서"))
        self.assertIn("꽂힌 책을 찾지 않고 바로 꺼낸다", document["md"])
        shelf = by_title["스마트 책장"]
        self.assertEqual(shelf["kind"], "meeting")
        self.assertNotIn("꽂힌 책을 찾지 않고 바로 꺼낸다", shelf["md"])
        self.assertIn("책 등록은 어떻게?", shelf["md"])
        self.assertEqual(document["parent_part_id"], shelf["id"])
        self.assertEqual(json.loads(shelf["block_ids_json"]), ["blk-02"])

    def test_same_content_is_not_judged_again_and_human_kind_is_kept(self) -> None:  # A20
        classify = split_classifier()
        self.collect(classify=classify)
        self.assertEqual(len(classify.calls), 1)
        self.collect(classify=classify)
        self.assertEqual(len(classify.calls), 1)  # 내용이 같으면 다시 판정하지 않는다

        bag = next(p for p in parts.list_parts(self.store, SCOPE, page_id="unit-1") if p["path"].endswith("배낭"))
        parts.set_part_kind(self.store, bag["id"], "document", actor="송지원")
        changed = SAMPLE_MD.replace("태그는 기존 제품으로 되지 않나?", "태그 가격은?")
        stored = self.collect(md=changed, classify=classify)
        bag = next(p for p in stored if p["path"].endswith("배낭"))
        self.assertEqual((bag["kind"], bag["kind_source"]), ("document", "human"))  # 내용이 바뀌어도 사람이 정한 종류
        self.assertIn("태그 가격은?", bag["md"])
        self.assertEqual(len(classify.calls), 1)  # 바뀐 부분은 사람이 정한 것뿐이라 판정 호출이 없다

    def test_low_confidence_becomes_document_and_missing_classifier_waits(self) -> None:
        def unsure(page: dict) -> dict:
            return {"parts": [{"part_id": i["part_id"], "kind": "meeting", "confidence": 0.3, "split": []}
                              for i in page["parts"]]}

        stored = self.collect(classify=unsure)
        self.assertTrue(all(p["kind"] == "document" for p in stored))
        self.assertTrue(all("확신 낮음" in (p["kind_note"] or "") for p in stored))

        other = Path(self.tmp.name) / "other.sqlite3"
        built = parts.build_unit_parts("unit-1", "회의록 > 1차 멘토링", SAMPLE_MD, SAMPLE_BLOCKS)
        parts.save_unit_parts(other, SCOPE, "notion", "unit-1", "sec-1", built)
        self.assertEqual(parts.judge_pending(other, SCOPE, None), 0)  # LLM이 없으면 판정을 미룬다
        self.assertTrue(all(p["kind"] is None for p in parts.list_parts(other, SCOPE)))

    def test_a_failed_judgment_stops_this_round_and_retries_next_time(self) -> None:
        for unit in ("unit-1", "unit-2"):
            built = parts.build_unit_parts(unit, f"회의록 > {unit}", SAMPLE_MD, SAMPLE_BLOCKS)
            parts.save_unit_parts(self.store, SCOPE, "notion", unit, "sec-1", built)
        calls = []

        def rate_limited(page: dict) -> dict:
            calls.append(page["page_path"])
            raise RuntimeError("429 Too Many Requests")

        self.assertEqual(parts.judge_pending(self.store, SCOPE, rate_limited), 0)
        self.assertEqual(len(calls), 1)  # 한도에 걸리면 다음 페이지를 바로 부르지 않는다
        self.assertEqual(parts.judge_pending(self.store, SCOPE, split_classifier(), budget_tokens=None), 12)  # 다음 번에 전부

    def test_paced_mode_waits_a_minute_and_retries_after_a_rate_limit(self) -> None:
        built = parts.build_unit_parts("unit-1", "회의록 > unit-1", SAMPLE_MD, SAMPLE_BLOCKS)
        parts.save_unit_parts(self.store, SCOPE, "notion", "unit-1", "sec-1", built)
        ok = split_classifier()
        answers = iter([RuntimeError("429"), None])

        def flaky(page: dict) -> dict:
            outcome = next(answers)
            if outcome is not None:
                raise outcome
            return ok(page)

        waits: list[float] = []
        judged = parts.judge_pending(self.store, SCOPE, flaky, budget_tokens=None, tokens_per_minute=6000, sleep=waits.append)
        self.assertEqual(judged, 6)
        self.assertEqual(waits[0], 60)  # 한도가 풀릴 때까지 기다린 뒤 다시

    def test_budget_splits_big_pages_and_stops_for_this_round(self) -> None:
        big = "\n".join(f"## 항목 {i}\n" + "내용 " * 300 for i in range(30))  # 부분 30개, 판정 한 번에 다 못 넣는 크기
        parts.save_unit_parts(self.store, SCOPE, "notion", "old-page", "sec-1",
                              parts.build_unit_parts("old-page", "문서", big, None), last_edited="2026-10-01T00:00:00Z")
        parts.save_unit_parts(self.store, SCOPE, "notion", "new-page", "sec-1",
                              parts.build_unit_parts("new-page", "회의록", SAMPLE_MD, None), last_edited="2026-11-01T00:00:00Z")
        classify = split_classifier()
        first = parts.judge_pending(self.store, SCOPE, classify)
        self.assertEqual(classify.calls[0]["page_path"], "회의록")  # 최근 수정된 페이지부터
        self.assertTrue(all(parts.estimate_tokens(c["parts"]) <= parts.JUDGE_BUDGET_TOKENS for c in classify.calls))
        self.assertLess(first, 36)  # 한 번의 몫을 넘으면 멈춘다
        waits: list[float] = []
        rest = parts.judge_pending(self.store, SCOPE, classify, budget_tokens=None, tokens_per_minute=6000, sleep=waits.append)
        self.assertEqual(first + rest, 36)  # 끝까지 하면 전부 판정
        self.assertTrue(waits and all(w > 0 for w in waits))  # 분당 속도를 넘지 않게 쉰다

    def test_parts_the_llm_leaves_out_are_kept_as_documents_but_an_empty_answer_changes_nothing(self) -> None:
        built = parts.build_unit_parts("unit-1", "회의록 > unit-1", SAMPLE_MD, SAMPLE_BLOCKS)
        parts.save_unit_parts(self.store, SCOPE, "notion", "unit-1", "sec-1", built)
        self.assertEqual(parts.judge_pending(self.store, SCOPE, lambda page: {}), 0)  # 빈 답: 그대로 둔다
        self.assertTrue(all(p["kind"] is None for p in parts.list_parts(self.store, SCOPE)))

        def only_first(page: dict) -> dict:
            first = page["parts"][0]
            return {"parts": [{"part_id": first["part_id"], "kind": "meeting", "confidence": 0.9, "split": []}]}

        self.assertEqual(parts.judge_pending(self.store, SCOPE, only_first), 6)
        stored = parts.list_parts(self.store, SCOPE)
        self.assertEqual(stored[0]["kind"], "meeting")
        self.assertTrue(all(p["kind"] == "document" and "근거 없음" in p["kind_note"] for p in stored[1:]))
        self.assertEqual(parts.judge_pending(self.store, SCOPE, only_first), 0)  # 다시 보내지 않는다

    def test_heading_parts_run_past_empty_paragraphs_and_titles_are_clean(self) -> None:
        md = ('## 📈 2. 데이터 수집 전략 {color="purple_bg"}\n<empty-block/>\n수집 채널은 카페와 리뷰\n<empty-block/>\n'
              '- 리뷰 300건\n## 3. 타깃 고객\n20대 직장인\n<empty-block/>\n---\n1. 전화를 통한 VoC<br>a) 맘카페')
        groups = parts.group_parts(parts.top_level_blocks(md))
        self.assertEqual([g.title for g in groups], ["📈 2. 데이터 수집 전략", "3. 타깃 고객", "전화를 통한 VoC"])
        self.assertIn("리뷰 300건", groups[0].md)  # 빈 단락 뒤 본문까지 제목의 부분

    def test_judging_sees_the_whole_page(self) -> None:
        classify = split_classifier()
        self.collect(classify=classify)
        page = classify.calls[0]["page"]
        self.assertEqual((page["title"], page["part_count"]), ("1차 멘토링", 6))
        self.assertEqual(page["outline"][:2], ["화분 관리기", "스마트 책장"])
        self.assertGreater(page["chars"], 2000)  # 페이지 전체 길이(부분 합)

    def test_part_ids_survive_a_sync_where_block_alignment_fails(self) -> None:
        first = {p["path"]: p["id"] for p in self.collect()}
        again = {p["path"]: p["id"] for p in self.collect(blocks=None)}  # 이번에는 블록 목록 없이(대조 실패와 같음)
        self.assertEqual({k: v for k, v in again.items() if k in first}, {k: first[k] for k in again if k in first})
        self.assertFalse([p for p in parts.list_parts(self.store, SCOPE, include_gone=True) if p["status"] == "gone"])

    def test_parts_that_disappear_are_marked_gone(self) -> None:
        self.collect()
        shorter = SAMPLE_MD.split("1. 음성 비서")[0] + "<empty-block/>\n"
        stored = self.collect(md=shorter, blocks=None)
        self.assertNotIn("음성 비서", [p["path"].rsplit(" > ", 1)[-1] for p in stored])
        gone = [p for p in parts.list_parts(self.store, SCOPE, include_gone=True) if p["status"] == "gone"]
        self.assertTrue(any(p["path"].endswith("음성 비서") for p in gone))


class FakeMarkdownClient:
    def __init__(self, pages: dict[str, dict]) -> None:
        self.pages, self.calls = pages, []

    def page_markdown(self, page_id: str) -> dict:
        self.calls.append(page_id)
        if page_id not in self.pages:
            raise RuntimeError("404")
        return self.pages[page_id]


class MarkdownFetchTests(unittest.TestCase):
    def test_truncated_page_is_followed_up_by_unknown_block_ids(self) -> None:  # A19
        client = FakeMarkdownClient({
            "p": {"markdown": "앞부분", "truncated": True, "unknown_block_ids": ["b1", "b2"]},
            "b1": {"markdown": "가운데", "truncated": False, "unknown_block_ids": []},
            "b2": {"markdown": "끝", "truncated": True, "unknown_block_ids": ["b3"]},
            "b3": {"markdown": "더 끝", "truncated": False, "unknown_block_ids": []},
        })
        page = notion_md.fetch_markdown(client, "p")
        self.assertEqual(page.md, "앞부분\n가운데\n끝\n더 끝")
        self.assertEqual(client.calls, ["p", "b1", "b2", "b3"])

    def test_too_many_follow_ups_or_errors_fall_back(self) -> None:  # A19
        endless = {"p": {"markdown": "x", "truncated": True, "unknown_block_ids": [f"b{i}" for i in range(30)]}}
        endless.update({f"b{i}": {"markdown": "y", "truncated": False, "unknown_block_ids": []} for i in range(30)})
        client = FakeMarkdownClient(endless)
        page = notion_md.fetch_markdown(client, "p", max_calls=20)
        self.assertIsNone(page.md)
        self.assertEqual(page.fallback_reason, "too_many_calls")
        self.assertEqual(len(client.calls), 20)
        self.assertEqual(notion_md.fetch_markdown(FakeMarkdownClient({}), "missing").fallback_reason, "error")

    def test_clients_without_markdown_support_fall_back(self) -> None:
        self.assertEqual(notion_md.fetch_markdown(object(), "p").fallback_reason, "unsupported")


if __name__ == "__main__":
    unittest.main()
