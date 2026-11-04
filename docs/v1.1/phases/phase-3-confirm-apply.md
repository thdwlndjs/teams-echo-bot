# Phase 3 확인 반영과 worklog

브랜치 `feat/confirm-and-apply` · 앞선 조건 [Phase 2](phase-2-extracts.md) · 다음 [Phase 4](phase-4-meeting-inputs.md), [Phase 5](phase-5-stage-guide.md)

이 phase가 끝나면, 새 회의록은 추출 미리보기를 사람이 한 번 확인한 뒤 worklog에 반영된다. 확인된 회의록의 수정은 자동으로 반영되고, 결정 대체는 알림과 함께 되돌릴 수 있다. 설정 마법사에서 회의록 페이지 단계가 빠진다.

이 phase에는 아직 역할·권한(Phase 5)이 없다. "권한자"는 임시로 관리자(`team_configs.json`의 `admin_aad_id`)로 본다. Phase 5에서 권한 판정으로 바꾼다.

## 읽을 것

- [spec.md](../spec.md) P1-5 부분 상태와 반영 흐름, P1-6 worklog 반영, P1-7 회의록 입구(Notion 줄과 설정 변경), 데이터 모델(버전 7)

## 작업

- [x] `src/server/worklog/parts.py`: 상태 전이(`extracted → pending_confirm → applied / skipped`)와 2시간 규칙.
- [x] `src/server/worklog/db.py`: 마이그레이션 7(`extract_corrections`).
- [x] `src/server/worklog/core.py`
    - 확인된 항목을 `normalize_item` → `find_related` → 판정 → 반영하는 경로. 반영자는 확인한 사람.
    - 결정 대체 자동 반영(`change_candidates.status = 'auto_applied'`), 확신 미달은 `needs_discussion`.
    - `revert(store, candidate_id, actor)`.
    - 항목 종류 `feedback`, `insight`.
- [x] `src/server/main.py`
    - 메뉴 4번에 ② "Notion 변경 반영"(최근 수정 페이지 10개 → 선택 → 지금 처리 → 미리보기)과 ③ "최근 반영 내역"(되돌리기)을 더한다. 기존 "완료된 작업 공유하기"는 ①로 둔다.
    - 미리보기 카드 [반영] / [고치기] / [취소]. [고치기]는 항목별 수정·삭제·추가, 전후를 `extract_corrections`에 저장.
    - 메인 카드: 확인 대기, 반영 n건, 결정 대체 A → B [되돌리기], 삭제·소스 변경 확인.
    - `SETUP_STEPS`에서 `meeting_notes_page_id` 단계를 뺀다. 값이 있는 팀은 그대로 쓴다. 첫 수집 뒤 "회의록 n건을 찾았어요" / 0건 안내.
    - 메뉴 2번이 "(확인 전)" 추출 대신 반영된 `work_items`를 읽게 바꾼다(spec P1-9).
    - 직접 반영이 100초 안에 안 끝나면 안내하고 대기열에서 이어서 처리한다(spec P1-5 "직접 반영의 시간 제한").
    - [고치기]는 채팅으로 한다(spec P1-5 "[고치기]"). 메인 카드 알림의 모양은 spec P2-5를 따르되, 이 phase에서는 위 네 종류만 만든다.
- [x] `src/server/worklog/__main__.py`: `candidates`·`approve`·`reject` 명령을 없앤다(spec P1-0). `core.py`의 판정·반영 함수는 위 반영 경로에서 다시 쓴다.
- [x] `src/server/worklog/__main__.py`: `extract-eval --scope` 명령. 지금 추출 결과와 `extract_corrections`의 고친 뒤 목록을 비교해 빠진 항목·틀린 항목·남는 항목 수와 목록을 낸다.
- [x] spec P1-6의 수락 기준 4번 변경 사유를 README나 spec 변경 이력에 남긴다.
- [ ] 송지원이 [고치기]로 1~3차 멘토링 정답지를 만든다.

## 먼저 쓸 테스트

| ID | 테스트 |
| --- | --- |
| A7 | [반영]만 `work_items`를 만든다. [취소]는 만들지 않는다 |
| A8 | [고치기] 전후가 `extract_corrections`에 남는다 |
| A9 | 확인된 회의록이 수정되면 바뀐 항목만 반영된다 |
| A10 | 회의록 부분 삭제는 권한자 확인 전까지 항목을 지우지 않는다 |
| A11 | 결정 대체 후 `revert()`가 이전 상태로 되돌린다 |
| A23 | 자동 감지된 새 회의록은 마지막 수정 후 2시간 전에는 확인 대기로 올라가지 않는다 |
| A24 | 회의록 페이지가 이미 등록된 팀은 설정 변경 뒤에도 같은 회의록을 읽는다 |
| A31 | `extract-eval`이 빠진 항목·틀린 항목 수를 낸다 |
| A32 | 직접 반영이 100초 안에 안 끝나면 안내하고 대기열에서 이어서 처리한다 |
| A35 | `candidates`·`approve`·`reject` 명령이 없다 |

## 완료 조건

- [x] A7~A11, A23, A24, A31, A32, A35 통과, A18 통과
- [ ] A17: `extract-eval` 결과 빠진 항목·틀린 항목이 0. 차이가 있으면 프롬프트 버전을 올려 다시 돌리고, 결과를 PR에 남긴다
- [x] Playground(실제 저장소, 턴 경로): 미리보기 세 버튼, 되돌리기, 메인 카드 알림이 동작한다

## 구현 결과 (2026-11-04)

- 4번 메뉴 ①②③은 `src/server/confirm_flow.py`, 반영 계층은 `src/server/worklog/apply.py`.
- 실측(실제 저장소): 1차 멘토링 미리보기 5.3초, [반영] 39건 4.8초, 메뉴 2번 LLM 호출 0회, 되돌리기 동작, 메인 카드 "새 회의록 4건 확인 대기".
- 실측에서 고친 것
    - Phase 2에서 추출만 해 둔 부분이 상태 '수집'에 남아 확인 대기에 오지 않음 → 이미 추출이 있으면 추출됨으로 올림.
    - 한 회의록에서 39건이 나와 [삭제 n] 버튼(12개)으로 13번 뒤를 못 지움 → 채팅 `- n`으로도 지움.
    - 메인 카드 "반영 n건" 알림이 빠져 있었음 → "✍️ 확인된 회의록이 수정돼 n건을 반영했어요 (최근 24시간)" [최근 반영 내역] (2026-11-04 추가).
- 계획과 다른 것
    - 첫 수집 뒤 "회의록 n건을 찾았어요" 안내는 하지 않았다. 봇이 먼저 말을 걸 경로가 없어 Phase 4(회의록 입구)에서 메인 카드 알림으로 넣는다. 지금은 4번 ②에 0건 안내만 있다.
    - 소스 페이지(회의록 아닌 페이지) 변경·삭제 확인은 메인 카드에 없다. 소스 페이지는 지금도 추출·반영 대상이 아니라 반영된 항목이 없으므로, 확인할 대상이 생기는 Phase 5 단계 길잡이에서 넣는다.
    - 1차 멘토링 추출은 피드백 38건으로 많고 주제 이름만 적힌 줄처럼 항목이 아닌 것도 섞인다. A17(정답지 비교 0)은 송지원이 [고치기]로 정답지를 만든 뒤 `python -m worklog extract-eval --scope <팀>`으로 본다.
