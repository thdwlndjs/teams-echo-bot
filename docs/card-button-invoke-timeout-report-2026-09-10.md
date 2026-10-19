# 카드 버튼 전환 후 Playground 오류 원인 리포트 (2026-09-10)

## 배경

같은 날 앞선 조사([test-report-2026-09-10.md](./test-report-2026-09-10.md))에서 카드 버튼 클릭이
500으로 죽는 버그 2건(`ctx.value` → `ctx.activity.value`, invoke의 `serviceUrl`에 `_connector`
누락)을 찾아 고쳤다. 그런데 사용자가 실제로 다시 눌러봐도 **"Unable to reach app. Please try
again."이 여전히 뜬다**고 확인해줬다. 서버 로그를 다시 보니 — **이번엔 진짜로 서버가 에러 없이
다 성공했다.** 404도 500도 없고, 모든 Notion/GitHub/Groq 호출과 봇의 답장(`_connector` POST)까지
전부 200/201로 끝났다. 그런데도 화면엔 에러가 뜬다.

**결론부터: 버그가 남은 게 아니라, 처리 시간이 Teams/Playground의 invoke 응답 타임아웃을 넘겨서
클라이언트가 먼저 포기하는 것.** 아래는 코드 리뷰 + 공식 문서/커뮤니티 레퍼런스 대조 결과.

## 왜 "카드 버튼으로 바꾼 후"에만 문제로 드러났나

| | 예전: 텍스트 메뉴 (`"1"`, `"2"` 입력) | 지금: 카드 버튼 (Action.Execute) |
|---|---|---|
| 활동 종류 | `message` | `invoke` (`adaptiveCard/action`) |
| 클라이언트가 답장을 기다리는 방식 | 안 기다림 — 그냥 메시지 하나 보내고 끝. 봇의 실제 답장은 `ctx.send()`로 **별도** 아웃바운드 POST | **기다림** — invoke는 요청-응답이 한 세트. 봇이 돌려주는 HTTP 응답 본문(`AdaptiveCardActionMessageResponse`) 자체가 결과 |
| 처리가 오래 걸리면? | 상관없음. 늦게 와도 그냥 늦게 채팅에 뜸 | **클라이언트가 일정 시간 안에 응답을 못 받으면 자체적으로 포기하고 에러 배너를 띄움** — 그 뒤에 봇이 실제로 성공 응답을 보내도 이미 늦음 |

즉, Notion/GitHub/Groq 체인 자체의 속도는 카드로 바꾸며 하나도 안 바뀌었다. 바뀐 건 **"느려도
괜찮은 전송 방식(message)"에서 "느리면 실패하는 전송 방식(invoke)"으로 옮겨간 것**뿐이다. 원래도
느렸던 로직이 카드 전환으로 인해 겉으로 드러난 것.

### 공식 문서 대조

MS Learn의 [Adaptive Card actions in Teams SDK](https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/cards-actions)
공식 Python 샘플(`handle_card_action`)을 봐도, 핸들러는 **입력받은 즉시 처리하고 바로
`AdaptiveCardActionMessageResponse`를 반환**하는 걸 전제로 짜여 있다. "먼저 짧게 응답하고 나중에
결과를 보낸다" 같은 패턴은 문서 어디에도 없다 — 플랫폼 차원에서 "이 안에서 오래 걸리는 일을 하지
말라"는 암묵적 전제로 보인다.

정확한 타임아웃 초 수는 MS 공식 문서에 명시돼 있지 않았다(검색 범위 내에서는). 다만 실제 사례들:

- Microsoft Q&A/GitHub 이슈 다수가 "Unable to reach app" / "Something went wrong. Please try
  again."이 **응답 지연** 때문에 뜬다고 보고하며, 체감상 5~15초 사이에서 실패한다고 언급한다.
  ([msteams-docs#6559](https://github.com/MicrosoftDocs/msteams-docs/issues/6559),
  [msteams-docs#6581](https://github.com/MicrosoftDocs/msteams-docs/issues/6581))
- 권장되는 회피 패턴은 "invoke 응답을 즉시(200 OK) 반환하고, 실제 긴 작업은 비동기로 처리한 뒤
  결과를 proactive 메시지나 카드 업데이트로 나중에 전달"하는 것.

우리가 재현한 현상(수 초~십수 초짜리 체인 → 정확히 이 에러)과 완전히 들어맞는다.

## 코드 리뷰: 왜 체인이 이렇게 느린가

**정정**: 처음 리포트에서 "Notion 블록 재귀 조회(N+1)"를 공통 원인처럼 썼는데, 틀렸다. **4번(완료된
작업 공유)은 Notion을 아예 안 부른다** — 4번이 느린/실패하는 이유는 Notion이 아니라 **GitHub
GraphQL 쪽의 별도 N+1**이다. 메뉴별로 실제 호출 체인이 다르므로 표로 정리한다.

| 메뉴 | 실제 호출 체인 (전부 순차 await) | 캐시 여부 |
|---|---|---|
| **1** 팀/프로젝트 정보 | Notion 페이지 재귀 조회(N+1) → Groq 요약 | 5분 TTL 캐시 있음(`_project_info_cache`) |
| **2** 진행상황 조회 | Notion 회의록 재귀 조회(N+1, 데이터베이스 아니면 폴백) → GitHub `/issues` → Groq 분류 → GitHub `/commits` | Notion 텍스트/Groq 분류만 캐시, GitHub 호출은 매번 새로 나감 |
| **3** 내 역할 조회 | GitHub `/issues`(가벼움) → Notion 회의록 재귀 조회(2번과 같은 N+1 경로) | 캐시 없음(Notion 캐시는 1번 전용, 회의록엔 안 씀) |
| **4** 완료된 작업 공유 | GitHub `/issues` → GitHub `/commits` → GitHub GraphQL(Status 필드, 첫 호출 이후 캐시) → **일치하는 이슈 하나마다** GraphQL(item id 조회, 캐시) + GraphQL(상태 변경 mutation, **캐시 불가**) | Status 필드/item id만 캐시, mutation은 매번 실행 |
| **클레임("맡기")** | GitHub `/issues/{n}` GET → GitHub `/issues/{n}` PATCH | 캐시 대상 아님 — 원래도 가벼움(호출 2개) |

즉 진짜 공통 원인은 "Notion"이 아니라: **메뉴마다 내용은 달라도, 외부 API(Notion/GitHub
REST·GraphQL/Groq) 호출을 여러 번 순차로 기다리는 구조이고, 그 체인 전체가 끝나야 invoke 응답을
반환한다**는 것. Notion 재귀 조회는 1/2/3번에서 그 체인의 무거운 한 축일 뿐이고, 4번에서는 같은
문제가 **GitHub GraphQL 쪽 N+1**(일치하는 이슈 수만큼 순차로 2번씩 왕복)로 나타난다. 클레임처럼
호출이 2개뿐인 경로는 원래 빠르므로, 만약 클레임까지 실패한다면 그건 이 리포트의 "느려서 타임아웃"
설명이 아니라 **다른 원인**(네트워크 지연, GitHub API 자체 지연/레이트리밋 등)일 가능성이 커서 별도
확인이 필요하다.

**세부 근거**:

**1) Notion 블록 재귀 조회가 사실상 N+1 쿼리** (`_list_child_blocks` / `_block_text`,
`src/main.py:735-822`): 블록 하나에 자식이 있으면 그 자식마다 `blocks.children.list`를 또 부르는
구조라, 페이지 하나 읽는 데 API 호출이 (관찰상) 10번 넘게 나간다. 전부 한 스레드
(`asyncio.to_thread`) 안에서 **한 번에 하나씩 순서대로** 실행되고 병렬화가 전혀 없다. 1/2/3번
메뉴가 이 경로를 탄다(`fetch_project_info`, `fetch_team_status`/`fetch_my_role`의
`_fetch_meeting_notes_lines` 폴백).

**2) 4번(`share_completed_work`, `src/main.py:1254-1310`)의 이슈별 순차 GraphQL 루프**:
```python
for number in issue_numbers:
    item_id = await _find_project_item_id(...)   # GraphQL, 캐시됨
    ...
    await _set_project_item_status(...)           # GraphQL mutation, 캐시 불가 — 매번 실행
```
일치하는 이슈가 N개면 GraphQL 왕복이 최소 N번(캐시 미스 시 2N번) 순차로 발생한다.
`asyncio.gather`로 묶을 수 있는데 안 쓰고 있다.

**3) `fetch_team_status`(2번 메뉴, `src/main.py:980-`)의 호출 순서**가 전부 직렬:
```
Notion 회의록 재귀 조회 (위 1번 패턴)
  → GitHub 이슈 목록(/issues)
    → Groq 분류(_classify_meeting_notes)
      → GitHub 커밋 목록(/commits, _get_commit_touched_issues)
```
Notion 조회와 GitHub 이슈 조회는 서로 값을 주고받지 않는데도 순서대로 기다린다.

**4) 캐시가 GitHub 호출 대부분엔 없음**: Notion 원문(`_project_info_cache`, 5분 TTL)과 Groq 분류
결과(`_meeting_notes_classification_cache`, 내용 해시 기준)는 캐시되지만, GitHub `/issues`,
`/commits`, mutation 호출은 캐시가 없어서 **버튼을 누를 때마다 매번 새로 나간다.**

**5) 응답 반환 시점 자체의 구조적 문제** (`src/main.py:270-277`, `581-587`): `_run_action`/
`_claim_and_reply` 안에서 `ctx.send()`로 실제 결과 메시지를 보내는 것과, 핸들러가
`AdaptiveCardActionMessageResponse`를 반환하는 것이 **같은 실행 흐름 안에 순서대로** 있다. 즉
invoke 응답 자체가 위 체인이 전부 끝날 때까지 미뤄진다 — 이건 메뉴 종류와 무관하게 1~4번, 클레임
전부에 공통으로 적용되는 구조적 원인.

## 실측 계측 결과 (텍스트 입력 "1"~"4" 기준)

카드 버튼은 클릭 전달 자체가 자동화로 불안정해서(앞서 확인), 텍스트로 "1"~"4"를 직접 보내 같은
로직을 태우고 구간별 소요시간을 `[TIMING]` 로그로 찍었다(`_timed()` 헬퍼, `src/main.py`에 임시로
추가 — 병목 위치 확인용이라 나중에 지울 것). 실제 팀 데이터(Notion 페이지, GitHub 레포)로 측정한
값이다.

### 최초 호출(캐시 없는 상태, cold)

| 메뉴 | 구간 | 소요시간 | 비중 |
|---|---|---|---|
| **1번** | notion_project_page | 7734ms | 77% |
| | groq_summarize | 2172ms | 22% |
| | **TOTAL** | **10063ms** | |
| **2번** | notion_meeting_notes | 1437ms | 28% |
| | github_list_issues | 594ms | 11% |
| | groq_classify | 2469ms | 47% |
| | github_commits | 546ms | 10% |
| | **TOTAL** | **5203ms** | |
| **3번** | github_my_issues | 578ms | 18% |
| | notion_meeting_notes | 2516ms | 77% |
| | **TOTAL** | **3250ms** | |
| **4번** | github_my_issues | 562ms | 48% |
| | github_commits | 610ms | 52% |
| | **TOTAL** | **1172ms** ⚠️ | |

⚠️ **4번은 이번 측정에서 "담당 이슈 중 최근 커밋에서 언급된 게 없어요"로 일찍 리턴됐다** — 실제
GitHub Project GraphQL 루프(`_find_project_status_field` → 이슈당
`_find_project_item_id`+`_set_project_item_status`)는 이번엔 안 탔다. 즉 **4번의 진짜 병목(이슈별
GraphQL 왕복)은 이번 수치에 안 잡혀 있고, 매칭되는 이슈가 있는 케이스는 여기 찍힌 1172ms보다
훨씬 오래 걸릴 것**으로 추정된다(다른 GitHub GraphQL 호출들이 관찰상 회당 0.5~0.8초대인 걸
감안하면, 이슈 N개당 최소 N번의 추가 왕복).

### 재호출(캐시 일부 적용된 상태)

| 메뉴 | 구간 | 소요시간 | 비고 |
|---|---|---|---|
| **1번** | TOTAL | **0ms** | 5분 TTL 캐시 적중, 완전히 스킵됨 |
| **2번** | notion_meeting_notes | 1578ms | 캐시 없음 — 매번 재조회 |
| | github_list_issues | 641ms | 캐시 없음 |
| | groq_classify | **0ms** | 내용 해시 캐시 적중 |
| | github_commits | 609ms | 캐시 없음 |
| | **TOTAL** | **3093ms** | Groq만 아꼈고 나머지 그대로 |

### 핵심 병목 요약

1. **1번의 Notion 페이지 조회(7.7초)가 전체 메뉴 중 단일 구간 최대치.** 재귀 블록 조회(N+1) 탓 —
   페이지 구조가 깊거나 자식 블록이 많을수록 그대로 늘어난다. 5분 캐시가 있어서 반복 호출엔 안
   걸리지만, 캐시가 비어 있는 첫 호출·5분 지난 후 호출마다 그대로 재현된다.
2. **2번의 Groq 분류(2.5초)가 비중 1위(47%).** GROQ_MODEL이 `openai/gpt-oss-120b`(큰 모델)라
   요약(1번)도 Groq만 2.2초 걸린다 — 모델 자체 지연이 도메인 로직과 무관하게 항상 붙는다.
3. **GitHub REST 호출(issues/commits)은 회당 0.5~0.65초로 비교적 가볍지만, 캐시가 전혀 없어서**
   2/3/4번을 누를 때마다 매번 그대로 다시 나간다.
4. **4번의 진짜 병목(이슈별 GraphQL 왕복)은 이번 측정 케이스에 안 걸려서 수치가 없다** — 별도로
   매칭되는 이슈가 있는 상태를 만들어서 다시 재보든지, 코드 구조상 이슈 수에 비례해 늘어난다는 점만
   우선 기록해둔다.

### 병렬화 가능 지점 (데이터 의존관계 기준)

| 메뉴 | 지금 순서 | 병렬화 가능? | 근거 |
|---|---|---|---|
| 1번 | notion → groq | ❌ 불가 | groq_summarize의 입력이 notion 조회 결과(raw_text) 그 자체라 순서 강제 |
| 2번 | notion → github_issues → groq → github_commits | ✅ 가능 | `github_list_issues`/`github_commits`는 `note_lines`나 서로에게 의존하지 않음 — `notion_meeting_notes`·`github_list_issues`·`github_commits`를 `asyncio.gather`로 동시 시작하고, `groq_classify`만 `notion_meeting_notes` 끝난 뒤 이어 붙이면 이론상 총 시간이 max(1437, 594, 546) + 2469 ≈ **3906ms**로 줄어듦(현재 5203ms 대비 **~25% 단축**) |
| 3번 | github_my_issues → notion_meeting_notes | ✅ 가능 | 두 결과(github_lines, meeting_lines)는 마지막에 문자열만 합쳐질 뿐 서로 안 씀 — `asyncio.gather`로 동시 실행하면 총 시간이 max(578, 2516) ≈ **2516ms**로 줄어듦(현재 3250ms 대비 **~22% 단축**) |
| 4번 | github_my_issues → github_commits | ✅ 가능 | `my_issue_numbers`/`touched_numbers`는 서로 독립적으로 계산 후 교집합만 냄 — 동시 실행 시 max(562, 610) ≈ **610ms**(현재 1172ms 대비 **~48% 단축**) |
| 4번(이슈 있을 때) | 이슈마다 순차 `find_item_id`→`set_status` | ✅ 가능(단, 이슈 간에만) | 서로 다른 이슈의 (find_item_id, set_status) 쌍끼리는 독립적 — 이슈별로 `asyncio.gather`. 단, 같은 이슈 안에서 `set_status`는 그 이슈의 `item_id`가 나온 뒤에만 가능(그 안에서는 순차 유지) |

지금은 계측만 했고 코드는 안 고쳤다. 다음 액션 정하면 알려달라 — 위 병렬화부터 넣을지, 아니면
지난 리포트의 "invoke 응답 먼저 반환 + 백그라운드 처리"부터 넣을지(둘 다 하는 게 제일 효과적).

## 권장 조치 (지금은 리뷰만 — 코드는 안 건드림)

1. **invoke 응답을 먼저 즉시 반환**하고, `_run_action`/`_claim_and_reply`의 실제 작업은
   `asyncio.create_task`로 백그라운드에 던진 뒤 결과를 `ctx.send()`로 나중에 보내기. `ctx.send()`는
   invoke 응답과 별개의 아웃바운드 POST라 텍스트 메뉴 때처럼 타임아웃 대상이 아니다. (1~4번, 클레임
   전부에 적용되는 구조적 조치 — 메뉴별 원인이 뭐든 이거 하나로 다 우회된다.)
2. `_list_child_blocks`/`_block_text` 재귀 조회를 형제 블록 단위로 `asyncio.gather` 병렬화
   (1/2/3번 메뉴에 적용).
3. `fetch_team_status`의 Notion 조회 / GitHub 이슈 조회를 `asyncio.gather`로 동시 실행 (2번 메뉴).
4. `share_completed_work`의 이슈별 GraphQL 루프(`_find_project_item_id` +
   `_set_project_item_status`)를 `asyncio.gather`로 동시 실행 (4번 메뉴).
5. GitHub API 응답에도 짧은 TTL(예: 30~60초) 캐시 도입 검토.

이 중 아무거나 먼저 고쳐볼까요, 아니면 전체를 한 번에 반영할까요?

## 참고 자료

- [Adaptive Card actions in Teams SDK — Microsoft Learn](https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/cards-actions)
- ["Unable to reach app" 이슈 — msteams-docs#6559](https://github.com/MicrosoftDocs/msteams-docs/issues/6559)
- ["Unable to reach app" 이슈 — msteams-docs#6581](https://github.com/MicrosoftDocs/msteams-docs/issues/6581)
- [Microsoft Q&A: Adaptive card 응답 지연](https://learn.microsoft.com/en-us/answers/questions/2126435/high-delay-in-response-time-for-adaptive-card-in-m)
