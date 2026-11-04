"""서버 ↔ worklog(같은 폴더의 패키지) 연결.

- 원문 저장: Notion 회의록 행을 worklog 저장소에 넣는다. Teams 채팅 메시지는 v1.1부터 저장하지 않는다.
- 분석(`analyze_due`): 분석 정책에 걸린 묶음을 Groq로 분석해 변경 후보로 저장하는 예전 경로다. v1.1부터 서버 루프에서
  부르지 않고, Phase 3에서 미리보기 확인 반영으로 바뀐다(docs/v1.1/spec.md P1-0).

Notion·GitHub 데이터는 읽기만 하고 절대 바꾸지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

import llm_queue
import metrics
import worklog

log = logging.getLogger("worklog")

WORKLOG_STORE = Path(
    os.environ.get("WORKLOG_STORE")
    or Path(__file__).resolve().parent.parent / "worklog.sqlite3"
)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
# 분석할 묶음이 있는지 확인하는 주기. 확인 자체는 로컬 DB 조회뿐이라 비용이 없고, 분석 시점의 오차만 정한다.
ANALYZE_TICK_SECONDS = int(os.environ.get("WORKLOG_TICK_SECONDS", "300"))
# 서버가 켜진 뒤 첫 수집까지 기다리는 시간(켜자마자 들어오는 사용자 요청과 겹치지 않게).
START_DELAY_SECONDS = int(os.environ.get("WORKLOG_START_DELAY_SECONDS", "60"))
# Notion 접근 주기. 접근할 때마다 페이지 수정 시각 → (바뀌었으면) 구조 지문을 먼저 확인한다.
NOTION_COLLECT_SECONDS = int(os.environ.get("WORKLOG_NOTION_SECONDS", "3600"))
POLICY = worklog.AnalysisPolicy(
    idle_seconds=int(os.environ.get("WORKLOG_IDLE_SECONDS", "3600")),
    max_messages=int(os.environ.get("WORKLOG_MAX_MESSAGES", "20")),
    max_tokens=int(os.environ.get("WORKLOG_MAX_TOKENS", "3000")),
    settle_seconds=int(os.environ.get("WORKLOG_SETTLE_SECONDS", "7200")),
)


def record(*, scope: str, kind: str, conversation_key: str, locator: str, text: str,
           author: str | None = None, sent_at: str | None = None) -> bool:
    """원문 한 건 저장. 새로 저장했으면 True. 실패해도 봇 응답을 막지 않도록 예외를 삼키고 False."""
    if not text.strip():
        return False
    try:
        return worklog.record(WORKLOG_STORE, scope=scope, kind=kind, conversation_key=conversation_key,
                              locator=locator, text=text, author=author, sent_at=sent_at)["created"]
    except Exception:
        log.exception("원문 저장 실패: %s", locator)
        return False


# ── LLM 호출 (동기 — 분석은 별도 스레드에서 돈다) ─────────────────
def _llm_call(system_prompt: str, payload: Any, priority: int = llm_queue.BACKGROUND) -> tuple[Any, dict, str]:
    """JSON 응답 한 번. (응답 JSON, usage, "공급자:모델")을 돌려준다.

    백그라운드(순위 2·3)는 공급자를 차례로 시도한다(Groq → Cerebras, 키가 있는 것만). 대기열 한도에 막히거나
    429를 받으면 다음 공급자로 넘기고, 모두 막히면 llm_queue.Busy를 낸다(호출부는 그 주기를 멈춘다)."""
    body = json.dumps(payload, ensure_ascii=False)
    estimated = len(body) * 3 // 4 + len(system_prompt) // 2
    candidates = llm_queue.available(priority) or llm_queue.PROVIDERS[:1]
    busy: llm_queue.Busy | None = None
    for provider in candidates:
        try:
            llm_queue.check(provider, priority, estimated, WORKLOG_STORE)
        except llm_queue.Busy as error:
            busy = error
            continue
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                provider.url,
                headers={"Authorization": f"Bearer {provider.key}"},
                json={
                    "model": provider.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": body},
                    ],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
            )
        usage = usage_of(resp) or {}
        status = getattr(resp, "status_code", 200)
        metrics.count_groq(usage)
        llm_queue.record(WORKLOG_STORE, provider.name, priority, getattr(resp, "headers", None), status,
                         int(usage.get("total_tokens") or 0))
        if status == 429 and priority > llm_queue.USER:
            busy = llm_queue.Busy("minute", 60, provider.name)
            log.info("%s 한도 초과 → 다음 공급자", provider.name)
            continue
        resp.raise_for_status()
        return json.loads(resp.json()["choices"][0]["message"]["content"]), usage, f"{provider.name}:{provider.model}"
    raise busy or llm_queue.Busy("minute", 60)


def _groq_json(system_prompt: str, payload: Any) -> Any:
    return _llm_call(system_prompt, payload)[0]


def llm_ready() -> bool:
    """키가 있는 LLM 공급자가 하나라도 있는지."""
    return bool(llm_queue.available())


def usage_of(resp: httpx.Response) -> dict | None:
    """Groq 응답의 usage. 본문이 JSON이 아니면(오류 응답 등) None."""
    try:
        return resp.json().get("usage")
    except Exception:
        return None


EXTRACT_PROMPT = """너는 팀 대화와 회의록에서 Task와 Decision 후보를 뽑는 분석기야.
입력은 records 배열이고 각 항목에 source_id, text가 있다.
items 배열 하나만 담은 JSON 객체를 출력해: {"items": [...]}.
각 item 필드:
- kind: "task"(새로 할 일) | "decision"(정해진 방침) | "progress"(기존 일의 진행 보고) | "opinion"(의견) | "question"(질문)
- title: 짧은 이름. 같은 대상이면 매번 같은 이름을 써.
- statement: 무엇을 하거나 정했는지 한 문장
- certainty: decision일 때만. "decided"(확정 표현이 원문에 있음) | "proposed"(제안·논의 중)
- areas: decision일 때 해당하는 것만: "goal","scope","architecture","budget","schedule"
- status: task/progress일 때 "open","in_progress","done","cancelled" 중 하나
- assignee, due(YYYY-MM-DD): 원문에 있을 때만
- source_ids: 근거가 된 record의 source_id 목록 (입력에 있는 값만)
- quote: 근거 원문을 그대로 짧게 인용 (원문에 있는 문자열 그대로)
원문에 없는 내용을 지어내지 마. 확정 표현이 없으면 decision의 certainty는 "proposed"다.
단순 진행 보고는 decision이 아니라 progress다. 해당 사항이 없으면 {"items": []}."""

JUDGE_PROMPT = """너는 새 기록에서 나온 항목(item)을 기존 Task·Decision 목록(related)과 비교하는 판정기야.
JSON 객체 하나만 출력해: {"change_type", "target_id", "affected_task_ids", "rationale"}.
change_type:
- "unchanged": 기존 항목과 같은 내용
- "supplementary": 기존 항목에 세부 사항·진행 상태를 더할 뿐 의미를 바꾸지 않음
- "proposed": 기존 어떤 항목과도 다른 새 항목 (target_id는 null)
- "conflicting": 기존 항목과 모순되지만 교체한다는 말은 없음
- "superseding": 기존 결정을 명시적으로 대체함 (decision끼리만)
target_id: 비교 대상 기존 항목의 id (related 안의 값만, 같은 kind만). proposed면 null.
affected_task_ids: item이 decision일 때 영향을 받는 기존 task id (related 안의 값만).
rationale: 판단 근거 한두 문장. 확신이 없으면 conflicting으로 두고 이유를 적어."""


CLASSIFY_PROMPT = """너는 팀 Notion 페이지의 섹션이 어떤 기록인지 고르는 분류기야.
입력 sections 배열의 각 항목에 id, path(페이지 안 위치), title, container(담는 방식), sample(내용 앞부분)이 있다.
제목 단어만 보지 말고 sample 내용으로 판단해. 제목이 사람 이름이나 날짜·주차여도 내용이 업무 기록이면 그 역할이다.
role:
- "progress": 팀원별·주차별 진행, 한 일/할 일, 데일리·스프린트 기록
- "meeting": 회의·회고(KPT, Retro 등)·결정 사항 기록
- "reference": 멘토링·피드백, WBS·일정·로드맵, 참고 자료 (보관만 함)
- "ignore": 소개, 규칙·원칙, 양식·템플릿, 안내, 연락처 등 작업 기록이 아닌 것
JSON 하나만 출력해: {"sections": {"<id>": {"role": "...", "confidence": "high|medium|low", "reason": "한 줄"}}}.
confidence가 low인 판정은 분석에 쓰이지 않고 원문 보관만 된다. 판단할 근거가 없으면 그 id는 빼."""


def groq_classify_sections(items: list[dict]) -> dict[str, dict]:
    """새로 생기거나 이름이 바뀐 섹션만 판정한다. 판정은 바로 쓰이고, 확신이 low면 원문만 보관한다."""
    if not (GROQ_API_KEY or llm_ready()) or not items:
        return {}
    data = _groq_json(CLASSIFY_PROMPT, {"sections": items})
    found = data.get("sections") if isinstance(data, dict) else None
    return found if isinstance(found, dict) else {}


PART_KIND_PROMPT = """너는 팀 Notion 회의록·문서 페이지를 나눈 '부분'이 어떤 글인지 고르는 분류기야.
입력: page_path(페이지 위치), page(페이지 전체: title, chars 전체 길이, part_count 부분 수, outline 부분 제목 목차),
parts 배열. 각 part에 part_id, path, title, chars(길이), head(앞부분),
children(그 부분 안의 접힌 토글: child_path, chars)이 있다.
먼저 페이지 전체가 무엇인지 본다. 페이지가 회의록·멘토링 기록이 아니라 기획서·보고서·조사 자료·발표 준비·산출물
정리 같은 문서이면(긴 페이지, 제목으로 나뉜 목차, 페르소나·산출물·역할 분담 같은 내용), 그 안의 부분은
메모처럼 보여도 모두 "document"다. "meeting"은 회의·멘토링·피드백 자리를 적은 페이지의 부분만이다.
kind:
- "meeting": 회의·멘토링에서 나온 메모, 피드백, 결정, 할 일, 질문과 답
- "document": 기획서·제안서·조사 자료처럼 따로 쓴 긴 문서, 양식, 소개 글
- "link": 링크·첨부·녹음 노트 공유'만' 있는 부분. 메모·피드백·결론 문장이 한 줄이라도 섞여 있으면 meeting이다
회의 메모 안에 기획서 같은 긴 문서가 토글로 붙어 있으면, 그 부분은 meeting으로 두고
그 토글을 split에 넣어 document로 떼어 내. 메모 토글(기능, 멘토링 등)은 떼지 마.
document·link에는 summary(40자 이내 한 줄 설명: 무엇에 대한 문서인지, 작성 기준일이 있으면 함께)를 붙여.
JSON 하나만 출력해: {"parts": [{"part_id": "...", "kind": "...", "confidence": 0.0~1.0, "summary": "..." 또는 null,
"reason": "한 줄", "split": [{"child_path": "...", "kind": "document", "summary": "..."}]}]}.
판단할 근거가 없으면 그 part_id는 빼."""


def groq_classify_parts(page: dict, priority: int = llm_queue.BACKGROUND) -> dict:
    """한 페이지의 새로 생기거나 바뀐 부분만 종류를 판정한다(docs/v1.1/spec.md P1-3). LLM 키가 없으면 미룬다."""
    if not (GROQ_API_KEY or llm_ready()) or not page.get("parts"):
        return {}
    data = _llm_call(PART_KIND_PROMPT, page, priority)[0]
    return data if isinstance(data, dict) else {}


# 추출 지시문의 판. 지시문이나 항목 구성을 바꾸면 올린다(같은 내용도 새 판으로 다시 추출된다).
EXTRACT_PROMPT_VERSION = "part-extract-3"  # 2: 한국어 제목, 멘토 발언은 피드백, 아이디어 설명은 항목 아님 / 3: 질문지는 팀의 질문, 담당은 사람만

PART_EXTRACT_PROMPT = """너는 팀 회의·멘토링 메모에서 항목을 뽑는 분석기야. 입력: path(회의 위치)와 text(메모 원문).
뽑을 항목(kind):
- "decision": 정한 것, 확정된 방향
- "task": 누가 무엇을 해야 하는 일. owner(메모에 적힌 이름, 없으면 null), due(YYYY-MM-DD, 없으면 null),
  role_hint("plan" 기획 | "design" 디자인 | "dev" 개발 | null)
- "open": 아직 정하지 못했거나 논의가 필요한 것, 열린 질문
- "feedback": 멘토·외부 사람이 준 조언·지적·의견
- "insight": 고객·사용자 조사(VOC)에서 얻은 사실이나 발견
구분 규칙:
- 멘토링·피드백 회의에서 "멘토링", "피드백" 아래 적힌 줄과 끝의 결론 줄(→ …)은 멘토가 한 말이다. 조언·지적·의견은
  질문 형태라도 "feedback"이다. 팀이 그 자리에서 정했다고 적힌 것만 "decision"이다.
- "기능", "아이디어" 아래처럼 팀이 낸 아이디어·기능을 설명하는 줄은 항목으로 뽑지 마. 할 일·결정이 아니다.
- 원문에 정했다는 말이 없으면 decision을 만들지 마. 짧은 단어 하나("진동 제어")를 결정으로 늘리지 마.
- 질문지(멘토·고객에게 물어보려고 팀이 미리 적은 질문)는 팀의 질문이다. "feedback"이 아니라 "open"이다.
- owner는 사람 이름만 쓴다. 부서·팀·회사 이름이면 null이다.
각 항목: {"kind", "title"(한국어 한 줄, 40자 안팎), "detail"(필요하면 한국어 한두 문장, 없으면 null), "owner", "due",
"role_hint", "quote"(근거가 된 원문을 text에서 그대로 복사한 짧은 구절), "confidence"(0.0~1.0)}.
title·detail은 반드시 한국어로 쓴다. quote는 반드시 text 안에 글자 그대로 있어야 한다. 근거 없는 항목은 만들지 마.
JSON 하나만 출력해: {"items": [...]}. 뽑을 것이 없으면 {"items": []}."""


def groq_extract_part(payload: dict, priority: int = llm_queue.BACKGROUND) -> dict:
    """회의 메모 부분 하나에서 항목을 뽑는다(docs/v1.1/spec.md P1-4). {"items", "usage", "model"}.
    LLM 키가 없으면 빈 dict(추출을 미룬다). Groq가 막히면 Cerebras로 넘기고, 모두 막히면 llm_queue.Busy.
    model에는 "공급자:모델"을 남긴다(정답지 비교 때 어느 쪽 결과인지 구분)."""
    if not (GROQ_API_KEY or llm_ready()):
        return {}
    data, usage, label = _llm_call(PART_EXTRACT_PROMPT, payload, priority)
    items = data.get("items") if isinstance(data, dict) else None
    return {"items": items if isinstance(items, list) else [], "usage": usage, "model": label}


def groq_extract(records: list[dict]) -> list[dict]:
    payload = {"records": [{"source_id": r["source_id"], "text": r["text"], "author": r.get("author")} for r in records]}
    data = _groq_json(EXTRACT_PROMPT, payload)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"extract 응답 형식이 아님: {str(data)[:200]}")
    return items


STAGE_PROMPT = """너는 팀 프로젝트의 진행 단계를 판정하는 분석기야. 입력: stages(단계 목록), gates(단계별로 끝났다고 볼 조건),
items(회의록에서 사람이 확인해 반영한 항목: id, kind(decision 정한 것 | task 할 일 | open 미정 | feedback 멘토 의견 |
insight VOC 발견 | document 팀 문서(기획서 등)의 요약과 앞부분), title, statement).
document는 팀이 쓴 기획 문서다. "현재 상태", "후보" 같은 표현을 그대로 믿어라(후보면 아직 확정이 아니다).
할 일:
1. gates마다 items만 보고 조건이 충족됐는지 판정해. 충족이면 근거 항목 id를 evidence에 꼭 넣어. 근거가 없으면 met은 false다.
   후보·아이디어를 나열한 것은 "확정"이 아니다. decision이어야 확정이다. feedback(멘토 의견)은 팀이 정한 것이 아니다.
2. discussing: 최근 항목들이 주로 어느 단계의 내용을 다루는지 stage 하나와 근거 id. 알 수 없으면 stage null.
3. overturned: VOC(insight)가 팀이 세운 가정을 뒤집었으면 {"assumption": 가정 한 줄, "evidence": [id]}, 아니면 null.
JSON 하나만 출력해: {"gates": {"<gate_id>": {"met": true|false, "evidence": [id...]}}, "discussing": {"stage": "1-2"|null,
"evidence": [id...]}, "overturned": null}. id는 items에 있는 값만 써."""


def groq_stage_judge(payload: dict, priority: int = llm_queue.BACKGROUND) -> dict:
    """단계·게이트 판정(대기열 순위 3, 백그라운드). LLM 키가 없으면 빈 답(판정 안 함)."""
    if not (GROQ_API_KEY or llm_ready()):
        return {}
    data = _llm_call(STAGE_PROMPT, payload, priority)[0]
    return data if isinstance(data, dict) else {}


def groq_judge(item: dict, related: list[dict], priority: int = llm_queue.BACKGROUND) -> dict:
    return _llm_call(JUDGE_PROMPT, {"item": item, "related": related}, priority)[0]


def analyze_due() -> list[dict]:
    """정책에 걸린 묶음을 분석한다. Groq 키가 없으면 원문만 쌓이고 분석은 건너뛴다."""
    if not GROQ_API_KEY:
        return []
    results = worklog.analyze_due(WORKLOG_STORE, groq_extract, groq_judge, policy=POLICY)
    for result in results:
        if "error" in result:
            log.warning("분석 실패(다음 주기에 재시도): %s", result["error"])
        else:
            log.info("분석 완료: 후보 %d건, 제외 %d건", len(result["candidates"]), len(result["ignored"]))
    return results


def team_snapshot(scope: str, root_id: str | None = None, max_chars: int = 12000) -> dict | None:
    """저장된 팀 정보 페이지 원문(1번 메뉴 요약용). 아직 수집 전이면 None."""
    from worklog import sections

    return sections.snapshot(WORKLOG_STORE, scope, root_id=root_id, max_chars=max_chars)


def meeting_notes(scope: str, notes_id: str | None, max_chars: int = 8000) -> dict | None:
    """저장된 회의 기록(2·3번 메뉴용). 아직 수집 전이면 None."""
    from worklog import sections

    return sections.meeting_notes(WORKLOG_STORE, scope, notes_id, max_chars=max_chars)
