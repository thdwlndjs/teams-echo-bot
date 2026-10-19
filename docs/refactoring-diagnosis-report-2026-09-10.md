# Codebase Refactoring 진단 리포트 (2026-09-10)

`codebase-refactoring` 스킬 + `Teams Bot Context` 문서 기준. Rule 1에 따라 **코드는 전혀 안 건드렸고**,
컨텍스트 문서의 15개 가설을 실제 코드로 하나씩 재검증했다(추가로, `[TIMING]` 계측이 아직 코드에 남아
있어서 텍스트 "1"~"4"로 다시 실측해 최신 수치를 확보했다).

## 1. 현재 아키텍처

- 엔트리포인트: `microsoft_teams` SDK가 FastAPI에 붙인 `/api/messages` 하나(+ n8n용 `/api/notify/user`).
- 모든 로직이 `src/main.py` 한 파일(~1400줄, 함수 58개)에 있음.
- 핸들러(`handle_message`, `handle_menu_card_action`, `handle_claim_card_action`,
  `handle_conversation_update`)가 매직 스트링("1"~"4", "설정", 취소어)으로 세 갈래(설정 마법사 /
  GitHub 온보딩 / 메뉴 액션)로 라우팅.
- 메뉴 액션 함수(`fetch_project_info`/`fetch_team_status`/`fetch_my_role`/`share_completed_work`)
  각각이 **I/O + 비즈니스 규칙 + Teams 표시 포맷팅**을 한 함수 안에서 다 함.
- 외부 연동: Notion(`notion_client.Client`), GitHub REST/GraphQL(생 `httpx`), Groq(생 `httpx`), n8n(수신
  전용 웹훅).
- 상태: 세션 2종(`setup_sessions`, `github_onboarding_sessions`) + 캐시 4종, 전부 모듈 전역 dict.
  설정은 JSON 파일 2개(`team_configs.json`, `member_profiles.json`)로 영속화, 락 없음.

## 2. 현재 의존 구조

```
Teams(Playground) → /api/messages(FastAPIAdapter)
  → handle_message / handle_menu_card_action / handle_claim_card_action
    → _run_action / _claim_and_reply / _handle_setup_message / _handle_github_onboarding_message
      → fetch_project_info   → _fetch_notion_page_by_id(재귀) → _summarize_project_info → Groq
      → fetch_team_status    → _fetch_meeting_notes_lines → _github_get(/issues)
                             → _classify_meeting_notes → Groq → _get_commit_touched_issues → _github_get(/commits)
      → fetch_my_role        → _github_get(/issues) , _fetch_meeting_notes_lines
      → share_completed_work → _github_get(/issues,/commits) , _github_graphql(상태필드/아이템/변경)
      → claim_issue          → _github_get(/issues/{n}) , _github_patch
    → ctx.send() → activity_sender → Bot Framework connector
```
거의 모든 경로가 `team_configs.json`/`member_profiles.json`/캐시 4종을 직접 건드림 — 공용 상태
경계가 없음.

## 3. Top Problems (검증됨 — 가설 그대로 반복 안 함)

### [P0] Notion 조회 전략이 함수마다 다르고, 1번 메뉴는 항상 제일 비싼 경로만 탐
**현재**: `fetch_project_info`는 항상 `_fetch_notion_page_by_id`(전체 블록 재귀, N+1)로 직행. 반면
`_fetch_meeting_notes_lines`는 `_query_database_rows`(가벼움)를 먼저 시도하고 실패해야만 같은 재귀
경로로 폴백.
**왜 중요한가**: 두 개가 개념적으로 "노션 소스 하나 읽어서 텍스트 뽑기"로 동일한 일을 하는데, 1번만
싼 경로를 시도할 기회조차 없음. 실측상 1번의 Notion 조회(6.9~7.7초)가 전체 메뉴 중 압도적 1위인 이유.
**근거**: `src/main.py:888` 부근 `fetch_project_info` vs `src/main.py:1081` `_fetch_meeting_notes_lines`.
**영향**: 성능(P0).
**권장 seam**: `fetch_project_info`도 `_query_database_rows` 먼저 시도 → 실패 시 폴백, 로 통일. 단
`notion_page_id`가 실제로 database 형태인지 먼저 확인 필요(아래 5번 시퀀스 참고) — 아니면 효과 없음.

### [P1] 독립적인 외부 호출들이 전부 순차 await — `asyncio.gather` 사용 0건
**현재**: `fetch_team_status`(notion → github_issues → groq → github_commits),
`fetch_my_role`(github_issues → notion), `share_completed_work`(github_issues → github_commits) 모두
서로 값을 안 주고받는 호출인데 순서대로 기다림.
**근거**: `src/main.py:1005-1046`, `1136-1153`, `1284-1293`. 코드 전체에 `gather` 0건(grep 확인).
**영향**: 성능(P1) — 실측상 2/3/4번 각각 최대 ~25~48% 단축 가능(아래 4번 표).
**권장 seam**: 세 함수 각각에서 독립 호출만 `asyncio.gather`로 묶기. 최종적으로 쓰는 데이터·순서는
그대로라 동작 변화 없음.

### [P1] GitHub 클라이언트가 호출마다 새 `httpx.AsyncClient()`, 타임아웃도 없음
**현재**: `_github_get`/`_github_patch`/`_github_graphql` 3곳 전부 `async with httpx.AsyncClient()
as client:` — 커넥션 재사용 없음, 타임아웃 미지정. Groq 쪽(`_summarize_project_info`,
`_classify_meeting_notes`)은 `timeout=15`로 명시돼 있는 것과 대조적.
**근거**: `src/main.py:715,724,732`(타임아웃 없음) vs `870,945`(15초).
**영향**: 성능(매 호출 TLS 핸드셰이크 재발생 — GitHub REST 호출이 550~650ms씩 걸리는 데 일조) +
안정성(GitHub가 응답 안 하면 무한 대기 가능, invoke 타임아웃 이슈를 더 악화시킴).
**권장 seam**: 앱 시작 시 만든 공용 `httpx.AsyncClient(timeout=...)` 하나를 세 함수가 공유. 인터페이스
추상화 없이 모듈 전역 클라이언트 하나 + 명시적 타임아웃만 추가.

### [P1] GitHub REST 호출(이슈/커밋)에 캐시가 전혀 없음
**현재**: Notion 원문(5분 TTL)·Groq 분류(해시 캐시)는 캐시되는데, `/issues`·`/commits`는 캐시 자체가
없어서 버튼 누를 때마다 항상 새로 나감.
**근거**: 재측정 확인 — 2번 재호출 시 Groq는 0ms(캐시 적중)인데 `github_list_issues`(657ms),
`github_commits`(547ms)는 최초 호출과 거의 동일하게 매번 걸림.
**영향**: 성능(P1) — 회의 중 여러 명이 연달아 2번을 누르는 상황에서 매번 병목.
**권장 seam**: 기존 `_project_info_cache`(TTL 캐시)와 똑같은 패턴으로 `/issues`·`/commits`에 짧은
TTL(30~60초) 캐시만 추가. 새 캐시 프레임워크 도입 안 함(스킬 anti-pattern 위반 방지).

### [P1] `fetch_team_status`가 조회+분류+포맷팅을 한 함수에서 다 함
**현재**: Notion/GitHub/Groq 호출, "완료/진행중/대기" 분류 로직, 이모지 포함 표시 문자열 생성이
~90줄짜리 함수 하나 안에 섞여 있음.
**근거**: `src/main.py:995-1067`.
**영향**: 유지보수성 — 분류 로직만 단위 테스트하려 해도 Notion+GitHub+Groq를 다 목(mock)해야 함.
**권장 seam**: 분류 루프(`for task in confirmed[:15]: ...` 부분, `1046-1060`)만 순수 함수로 추출
(`_classify_tasks_by_github_state(confirmed, issue_state_by_number, commit_touched)`). 나머지는 그대로.

### [P2] `except Exception` 15곳 — 그 중 사용자 최다 접점(`_run_action`)이 전부 뭉뚱그림
**현재**: 네트워크 에러/인증 에러/버그가 전부 "⚠️ 처리 중 오류가 발생했어요: {exc}"로 동일하게 감춰짐.
**근거**: `src/main.py:543-545`(가장 트래픽 많은 지점).
**영향**: 관측성/디버깅 속도.
**권장 seam**: 15곳 전부 손대지 말고, 이 한 곳만 `httpx.HTTPStatusError`(토큰 만료·권한 문제)와
나머지를 구분. (스킬이 "broad exception 무차별 세분화 금지"라고 명시 — 최다 접점 1곳만.)

### [P2] `SETUP_STEPS`가 위치 기반 4-tuple, 3곳에서 각각 언패킹
**현재**: `(key, prompt, parser, error_msg)` 튜플을 `SETUP_STEPS[0]`, `SETUP_STEPS[step_index]` 두
군데(3회) 언패킹 — 필드 추가/순서 변경 시 조용히 깨질 수 있음.
**근거**: 정의 `src/main.py:322`, 언패킹 `431`, `439`, `500`.
**영향**: 유지보수성(낮은 빈도지만 실수하기 쉬움).
**권장 seam**: `@dataclass SetupStep(key, prompt, parser, error_msg)`로 교체 — 동작 변화 없는 순수
리네임/래핑.

### 기각/재판정한 가설
- **"메뉴 매직 스트링"(컨텍스트 문서 #10)**: 실제로 "1"~"4"가 쓰이는 곳은 `_run_action`의 if/elif
  체인 한 곳뿐이고, 카드 버튼 데이터도 어차피 JSON으로 "1"~"4" 문자열을 그대로 보내야 함 — enum으로
  바꿔도 제거되는 중복이 없음. **문제 아님으로 기각.**
- **"JSON 전체쓰기 레이스"(컨텍스트 문서 #14)**: 패턴 자체(로드→수정→저장, 락 없음)는 존재 확인.
  다만 실제로 확인한 경로(`_set_github_id`, 설정 마법사 완료 저장)엔 로드-저장 사이에 `await`가 없어서
  **같은 프로세스 안에서는** asyncio 특성상 끼어들 틈이 없음. 서로 다른 사람이 동시에 설정을 완료하는
  경합은 이론적으로 가능하지만 재현 확인은 못 함 — **P2, MEDIUM 신뢰도**로 하향(사실로 단정 안 함).

## 4. 성능 병목 (실측, 재측정 완료)

텍스트 "1"~"4"로 재측정(HTTP invoke 아닌 message 활동 — 클릭 불안정성 배제):

| 순위 | 구간 | 소요 | 등급 |
|---|---|---|---|
| 1 | 1번 Notion 프로젝트 페이지 | 6875~7734ms (2회 측정 일관) | **P0** |
| 2 | Groq 요약/분류 | 2172~2531ms(콜드), 0ms(캐시 적중) | P1 |
| 3 | GitHub REST(issues/commits) | 546~657ms/건, 캐시 없음 | P1 |
| 4 | 순차 처리로 인한 손실분 | 2번 2813~5203ms 폭 — gather 시 이론상 ~25~48%↓ | P1(구조적) |

4번 메뉴는 이번에도 매칭 이슈가 없어 조기 리턴(github_issues 562ms + commits 579ms = 1141ms) —
`_find_project_item_id`/`_set_project_item_status` 이슈별 루프는 이번 실측에도 안 잡힘(다른 GraphQL
호출이 회당 0.5~0.8초인 걸 감안한 추정치로만 남김).

## 5. 리팩터링 시퀀스

**Step 1** — `fetch_team_status`/`fetch_my_role`/`share_completed_work`의 독립 호출을
`asyncio.gather`로 병렬화.
왜 지금: 동작 변화 없이 즉시 검증 가능(이미 있는 `[TIMING]` 로그로 전/후 비교), 리스크 최저.
영향 파일: `src/main.py` 세 함수. 리스크: 낮음. 기대 효과: 2/3/4번 각 ~25~48% 단축.

**Step 2** — `notion_page_id`(1번용)가 실제로 database 형태인지 먼저 확인 → 맞으면
`fetch_project_info`에 database-first 폴백 전략 적용.
왜 지금: 최대 병목(P0)이지만 데이터 구조 확인이 먼저 필요해서 Step 1보다 뒤. 영향 파일:
`fetch_project_info`만. 리스크: 낮음(기존에 검증된 폴백 패턴 재사용). 기대 효과: 확인되면 ~5~7초 절감,
아니면 스킵.

**Step 3** — GitHub용 공유 `httpx.AsyncClient`(타임아웃 명시) 도입.
영향 파일: `_github_get`/`_github_patch`/`_github_graphql` + 앱 startup/shutdown 훅. 리스크:
낮음~중간(클라이언트 라이프사이클 관리 필요). 기대 효과: 커넥션 재사용 + 무한 대기 방지.

**Step 4** — GitHub `/issues`·`/commits`에 짧은 TTL 캐시(기존 `_project_info_cache` 패턴 재사용).
영향 파일: 해당 호출부. 리스크: 낮음. 기대 효과: 연속 클릭 시나리오 병목 해소.

**Step 5** — `fetch_team_status`에서 분류 로직만 순수 함수로 추출.
영향 파일: `fetch_team_status`만. 리스크: 매우 낮음. 기대 효과: 테스트 가능성(성능과 무관).

**Step 6** — `SETUP_STEPS`를 dataclass로 교체.
영향 파일: 정의부 + 3개 언패킹 지점. 리스크: 매우 낮음.

**(별도 트랙, 이 시퀀스에 안 넣음)** — 지난 리포트의 "invoke 응답 먼저 반환 + 백그라운드 처리"는
성능 최적화가 아니라 **응답 프로토콜 자체를 바꾸는 결정**이라 이 성능 시퀀스와 결이 다름. 원한다면
별도 스텝으로 논의.

## 6. 지금 손대면 안 되는 것

- `app/handlers/services/integrations/...` 전체 디렉터리 구조 — 지금 규모(팀 소수, 파일 1개)에서
  마이그레이션 비용 대비 이득 없음.
- `_extract_github_repo`/`_extract_token`/`_extract_github_id`/`_extract_project_number` — 이미 작고
  단일 책임, 문제 없음.
- `_build_menu_card`/`_build_claim_card` — 이미 비즈니스 로직과 잘 분리돼 있음.
- 세션/캐시 4~6종을 DB나 Redis로 교체 — 지금 스케일(팀 소수, 프로세스 1개)에 안 맞음. 멀티 프로세스로
  스케일할 때 다시 검토.
- `_list_child_blocks`/`_block_text`/`_query_database_rows` 재귀 로직 자체 — 성능 이슈지 설계 결함
  아님(위에서 캐시/전략으로 대응). 로직 자체는 명확하고 정확함.
- `except Exception` 15곳 전부 세분화 — 트래픽 제일 많은 1곳만 우선.
- `notify_user`(n8n 웹훅) — 느린 경로 아니고 문제 근거 없음. 범위 밖.

## 7. 목표 아키텍처 (현실적 수준)

컨텍스트 문서의 7-레이어 구조는 지금 안 함. 대신 2단 분리 정도가 적정:

```
src/
  main.py        # Teams 핸들러 + 라우팅만 (얇게)
  actions.py     # fetch_project_info/fetch_team_status/fetch_my_role/share_completed_work/claim_issue
  integrations/
    notion.py    # 노션 클라이언트·재귀조회·데이터베이스조회
    github.py    # REST/GraphQL 클라이언트(공유 AsyncClient 포함)
    groq.py      # 요약/분류 호출
  storage.py     # team_configs/member_profiles 로드·저장, 캐시
```
domain/presentation/repository 레이어는 지금 어떤 결합 문제도 안 풀어줘서 도입 안 함.

## 8. 첫 리팩터 (딱 1개)

**Step 1 — `fetch_team_status`/`fetch_my_role`/`share_completed_work`의 독립 호출 3곳을
`asyncio.gather`로 병렬화.**

가장 레버리지 높은 이유:
- 동작 변화 없음(최소 seam의 정석) — 순서만 바꾸고 최종 결과는 동일.
- 검증이 공짜: 이미 코드에 있는 `[TIMING]` 계측으로 전/후 숫자를 바로 비교 가능.
- 원래 이 조사를 시작한 "Unable to reach app" 문제(invoke 타임아웃)도 응답이 빨라지는 만큼 같이
  완화됨 — 성능 개선과 원래 버그 완화가 겹침.
- 함수 3개, 각각 독립적으로 리뷰/롤백 가능한 작은 diff 3개로 나뉨.

여기서 멈춥니다(스킬 지침).
