# Phase 4 회의록 입구

브랜치 `feat/confirm-and-apply`(Phase 3~5를 한 브랜치에서, 머지는 Phase 5 뒤 한 번) · 앞선 조건 [Phase 3](phase-3-confirm-apply.md) · Phase 5와는 독립

이 phase가 끝나면, Notion 밖의 회의록을 Teams 채팅으로 받을 수 있다. 붙여넣은 텍스트, txt·docx 파일, 클로바노트에서 내려받은 파일이 모두 같은 미리보기 → 확인 흐름을 탄다.

## 읽을 것

- [spec.md](../spec.md) P1-7 회의록 입구, P1-5 반영 흐름

## 작업

- [x] `src/bot/main.py`: `_turn_payload`가 `attachments`(이름, `contentType`, 다운로드 URL)도 서버로 넘긴다. 지금은 `text`와 `value`만 넘긴다.
- [x] `src/server/main.py`: `TurnRequest`에 `attachments`를 더한다. 메뉴 4번 ④ "회의록 올리기"를 누른 뒤의 다음 메시지(텍스트나 파일)를 회의록으로 받는다. 파일 내려받기는 서버가 한다.
- [x] `pyproject.toml`: `python-docx` 의존성.
- [x] 새 `src/server/meeting_inputs.py`(가칭): 텍스트·txt·docx를 md로 바꾼다. docx는 문단·목록·표를 살린다.
- [x] 받은 내용을 `source_parts`(`origin = 'teams'` 또는 `'clova'`)로 만들고, 바로 직접 반영 경로(미리보기)로 보낸다.
- [x] 클로바노트 공유 링크(`clovanote.naver.com/s/…`)가 오면 파일이나 텍스트를 보내 달라고 답한다.
- [ ] Teams 파일 받기 확인
    - 개인 채팅: 앱 매니페스트([appPackage/](../../../appPackage/))의 파일 지원 설정.
    - 채널·그룹 채팅: 파일이 SharePoint 링크로 오는지, 추가 권한이 필요한지.
    - 안 되는 경로는 붙여넣기만 지원하고 안내 문구를 바꾼다.

## 먼저 쓸 테스트

| ID | 테스트 |
| --- | --- |
| A13 | 클로바노트 공유 링크를 받으면 파일을 보내 달라고 답한다 |
| A25 | 붙여넣은 텍스트가 `origin = 'teams'` 부분이 되고 미리보기 카드로 간다 |
| A26 | docx의 문단·목록·표가 md로 바뀐다 |

## 완료 조건

- [x] A13, A25, A26 통과, A18 통과
- [ ] Playground: 붙여넣기 하나와 파일 하나로 미리보기 → 반영까지 간다
- [ ] 파일이 안 되는 경로가 있으면 그 이유와 안내 문구를 PR에 남긴다

## 구현 결과 (2026-11-04)

- 4번 ④ "📤 회의록 올리기" → 다음 메시지 하나(텍스트 또는 txt·md·docx 파일)를 회의록으로 받는다. 세션 10분, '취소'로 그만둔다. 너무 짧은 글(20자 미만)·다른 형식 파일은 다시 보내 달라고 하고 세션을 남긴다.
- `src/server/meeting_inputs.py`: 붙여넣은 글은 빈 줄을 부분 경계로, 앞 공백 2칸을 한 단계 중첩으로. docx는 제목 #, 목록 - / 1.(단계는 탭, "List Bullet 2" 같은 스타일 이름도), 표는 | 표. txt는 UTF-8, 안 되면 CP949.
- origin: 파일 이름에 clova·클로바가 있거나 "참석자 1 00:01" 같은 줄이 두 번 이상이면 `clova`, 아니면 `teams`. 같은 내용을 다시 보내면 같은 페이지 ID.
- 받은 부분은 종류를 회의 메모로 정해 두고(판정 LLM 안 씀) 4번 ②와 같은 미리보기 경로로 보낸다.
- 첫 수집 뒤 메인 카드 안내(Phase 3에서 미룬 것): "Notion에서 회의록 n건을 찾았어요" / 0건이면 [📤 회의록 올리기]. 팀 설정의 `notes_found_noticed`로 한 번만.
- 실측(로컬 서버, 실제 LLM, 지어낸 회의록): 붙여넣기 → 미리보기 7.7초(결정·할 일·미정·피드백 각 1), docx(클로바노트 이름) → 클로바노트로 구분·표 행까지 할 일로, [반영] 4건, 공유 링크 안내. 확인 뒤 저장소는 확인 전 백업으로 되돌림.

### 확인하지 못한 것

- 실제 Teams에서의 파일 받기. 매니페스트 `supportsFiles: true`로 바꿨고 봇은 `application/vnd.microsoft.teams.file.download.info`의 `downloadUrl`(미리 인증된 주소)을 넘긴다. 개인 채팅은 이것으로 된다(Microsoft 문서 기준). 채널·그룹 채팅의 파일은 SharePoint 링크로 와서 Graph 권한이 필요해 지금은 받지 못하고 "파일을 받지 못했어요… 텍스트로 붙여넣어 주세요"로 안내된다. 앱 패키지(zip)를 다시 올려야 개인 채팅 파일 첨부 버튼이 생긴다.
- Agents Playground의 파일 첨부 UI. 턴 경로는 로컬 파일 서버 주소로 확인했다.
- 0건 안내의 [소스 추가] 버튼은 Phase 5(소스 추가)에서 붙인다.
