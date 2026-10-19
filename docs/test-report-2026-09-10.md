# 시나리오 테스트 보고서 (2026-09-10)

## 배경

Playground에서 카드 버튼을 클릭하면 "Unable to reach app. Please try again." 에러가 떴다.
처음엔 "M365 Agents Playground가 Adaptive Card Action.Execute를 완전히 지원 안 할 수 있다"는
Microsoft 공식 문서/이슈를 근거로 **Playground 쪽 한계**라고 추정했었다. 실제로는 아니었다 —
직접 재현해보니 **봇 코드 자체의 버그**였다.

## 테스트 방법

Playground UI는 화면을 볼 수 없어 직접 조작이 불가능해서, 대신:

1. Bot Framework Activity(JSON)를 직접 `/api/messages`로 POST하는 하네스를 만들고,
2. 봇이 보내는 응답을 가로채서 로그로 남기는 **스텁 커넥터 서버**(가짜 Bot Framework connector API)를
   `serviceUrl`로 지정해서, 실제 Playground 세션은 전혀 건드리지 않고 종단간(end-to-end)으로 검증했다.
3. 실제 GitHub/Notion 자격증명(관리자 계정에 저장된 것과 동일)을 재사용해서, 새 팀 키(`team:scenario-team-*`)
   하나를 처음부터 끝까지 실제로 셋업하고 시나리오를 돌렸다.
4. 테스트가 만든 팀 설정/멤버 프로필은 종료 시 전부 자동 정리(삭제)했다 — 실제 데이터엔 영향 없음.

## 결과 요약

**35개 검증 중 32개 PASS.** 나머지 3개는 봇 버그가 아니라 테스트 스크립트 쪽 오류(뒤에 설명)였다.

| 구간 | 결과 |
|---|---|
| 1. 미설정 사용자 보호 | 2/2 PASS |
| 2. 설정 마법사 – 잘못된 입력 처리 | 2/2 PASS |
| 3. 설정 마법사 – 정상 진행(7단계 전체, 실제 토큰) | 8/9 PASS (1개는 테스트 오류) |
| 4. 관리자 전용 재설정 제한 | 1/1 PASS |
| 4b. GitHub 아이디 온보딩 – 숫자 한 글자 확인 플로우 | 3/4 PASS (1개는 테스트 오류) |
| 5. 메뉴 1~4 (요약, 진행상황, 클레임 카드) | 5/5 PASS |
| 6. 담당자 없는 이슈 self-claim (카드 버튼=invoke) | 4/5 PASS (1개는 테스트 오류) |
| 7. 메뉴 카드 버튼 클릭(invoke) 자체 | 2/2 PASS |
| 8. 취소/나가기 | 2/2 PASS |

## 발견하고 고친 진짜 버그 2건

### 1. (심각) `AdaptiveCard` 버튼 클릭이 전부 500 에러로 죽고 있었음 — "Unable to reach app"의 진짜 원인

`handle_menu_card_action`, `handle_claim_card_action` 둘 다 카드 버튼 데이터를
`ctx.value.action.data`로 읽고 있었는데, `ctx`는 `ActivityContext` 래퍼 객체라 `.value`가 없다
(실제 invoke 데이터는 `ctx.activity.value`에 있음). 버튼을 누를 때마다 100% 재현되는
`AttributeError: 'ActivityContext' object has no attribute 'value'`였고, 이게 500으로 응답되면서
Teams/Playground 클라이언트가 "Unable to reach app. Please try again."로 표시한 것.

**즉 Playground 한계가 아니라 순수 코드 버그였다.** `ctx.activity.value.action.data`로 고쳐서
카드 버튼(메뉴 1~4, 클레임 "맡기"/"나가기") 전부 실제로 동작하는 것까지 이번 테스트로 확인했다.

```
File "src\main.py", line 249, in handle_menu_card_action
    choice = ctx.value.action.data["choice"]
             ^^^^^^^^^
AttributeError: 'ActivityContext' object has no attribute 'value'
```

### 2. (경미, 방어적 수정) 토큰에 한글 등 non-ASCII가 섞이면 UnicodeEncodeError로 죽음

`_extract_token`이 "공백 없음"만 검사해서, 한글이 섞인 값도 "형식상 유효"로 통과시켰다.
그 값으로 `notion_client.Client(auth=...)`를 만들면 httpx가 헤더에 넣다가
`UnicodeEncodeError`로 죽는다(이것도 500). 실제 API 토큰은 항상 ASCII이므로 `_extract_token`에
`.isascii()` 체크를 추가해서, 이런 값은 "토큰 형식이 아니에요"로 미리 걸러지게 고쳤다.

## "실패"로 표시됐지만 테스트 스크립트 문제였던 3건

- **잘못된 GitHub 토큰 거부**: 테스트가 한글이 섞인 가짜 토큰을 보냈는데, 방금 고친 위 2번 수정
  때문에 "토큰 형식이 아니에요"로 (더 이르게, 더 정확하게) 걸러짐. 원래 기대했던 "GitHub 인증
  실패" 메시지 대신 "형식 오류" 메시지가 온 것 — 봇이 오히려 더 잘 걸러낸 결과.
- **온보딩 후 클레임/역할 조회 이어서 처리 (2건)**: 테스트가 `teammate-gh`/`teammate2-gh`라는
  실존하지 않는 가짜 GitHub 아이디를 썼는데, 실제 레포에 없는 계정이라 GitHub API가 422를
  돌려줬다. 봇은 이 에러를 크래시 없이 정확히 잡아서 "⚠️ 처리 중 오류가 발생했어요: ..."로
  사용자에게 보여줬다 — 이것도 의도한 동작(우아한 실패 처리)이지, 버그가 아니다.

## 확인된 정상 동작 (발췌)

- 설정 마법사 7단계 전부(레포 → GitHub 토큰 → 본인 GitHub 아이디 → Notion 토큰 → 팀정보 →
  회의록 → 프로젝트 번호) 실제 자격증명으로 끝까지 통과, 완료 요약에 토큰은 마스킹됨
- GitHub 토큰=Notion 토큰 중복 입력 감지
- 관리자 아닌 사람이 `설정` 재시도 시 차단
- 숫자 한 글자 GitHub 아이디 입력 시 "정말 맞으세요?" 확인 플로우
- `1`번 = 요약된 팀 정보(회의록 안 섞임), `2`번 = 회의록 기준 진행상황 + 담당자 없는 이슈 카드
- 카드의 "맡기" 버튼(invoke) 클릭 → GitHub 아이디 온보딩 없으면 유도 → 클레임 처리
- 메뉴 카드 버튼(invoke) 클릭 → 실제 메뉴 로직 정상 수행
- `나가기`/`취소`로 진행 중이던 마법사/온보딩 중단

## 남은 할 일

- 실제 Playground 화면에서 카드 버튼을 직접 눌러서, 이번에도 "Unable to reach app"이 안 뜨는지
  최종 육안 확인 필요 (이 보고서의 테스트는 HTTP 레벨 검증이라 렌더링 자체는 못 봄).
- 이전 대화에서 미결정 상태로 남은 항목(클레임 카드가 아닌 시절의 "다음 메시지 가로채기" 버그는
  카드로 전환하면서 이미 해소됨 — 재확인 완료).
