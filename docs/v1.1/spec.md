# PM봇 v1.1 Spec

2026-11-03 · 송지원

v1.1에서 만드는 동작·데이터·화면의 규격이다. 구현과 테스트는 이 문서를 기준으로 한다. 배경과 결정 이유는 [report-2026-11-03.md](report-2026-11-03.md), 순서는 [plan.md](plan.md), phase별 작업 지시는 [phases/](phases/)에 있다.

- 파일·함수 이름은 지금 코드(`src/server/…`) 기준이다. 새 모듈 이름은 가칭이고, 구현하면서 바뀌면 이 문서를 고친다.
- "권한자"는 승인 권한이 있는 사람(관리자, 개발자, 관리자가 권한을 준 사람)이다.
- **[가정]** 표시는 정답지 없이 정한 값이다. [plan.md](plan.md)의 "가정으로 진행하는 것의 수정 절차"로 고치고, 아래 변경 이력에 남긴다.

## 용어

| 용어 | 뜻 |
| --- | --- |
| 부분 (part) | 페이지·DB 행·업로드 하나를 나눈 단위. 기본은 최상위 블록 하나, 종류 판정이 떼어 낸 하위 항목도 부분이 된다 |
| 종류 (kind) | 부분의 성격. `meeting`(회의 메모·피드백) / `document`(문서) / `link`(링크 모음) |
| 추출 (extract) | `meeting` 부분에서 결정·할 일·미정·피드백·인사이트 항목을 뽑는 것 |
| 확인 (confirm) | 사람이 추출 미리보기를 보고 [반영]을 누르는 것. 새 회의록은 반드시 한 번 거친다 |
| 반영 (apply) | 확인된 항목을 worklog(`work_items`)에 쓰는 것. Notion에는 쓰지 않는다 |
| 소스 페이지 | 회의록이 아닌 원천 페이지(팀 정보 페이지, 문서) |
| 게이트 (gate) | 세부 단계를 끝났다고 보기 위한 조건 하나 |

## 공통 규칙

- Notion은 읽기만 한다. 모든 Notion 호출은 `notion_read` 관문(토큰별 초당 3회, 동시 3개, 백그라운드 초당 1회)을 지난다.
- 메뉴 응답은 저장본만 읽는다. 메뉴 응답 중 Groq 호출은 0회, Notion 호출은 감지용 수정 시각 확인(P2-6)만 허용한다.
- Notion ID는 `db.notion_id()`(하이픈 제거, 소문자)로 맞춘 뒤 비교한다.
- 같은 입력은 두 번 처리하지 않는다. 판정·추출은 내용 해시로, 질문은 `prompts_asked`로 막는다.
- 사람에게 보이는 시각은 `_display_tz()`(기본 KST)로 "MM/DD HH:mm"을 쓴다.

## P1 회의록 수집·정리

### P1-0 기존 경로 정리

2026-11-02 커밋 `190544a`로 들어간 두 기능을 없앤다. v1.1 시작 시점 기준으로 저장된 채팅 메시지, 변경 후보, 분석 실행은 모두 0건이라 옮길 데이터가 없다.

| 기능 | 지금 동작 | v1.1 | Phase |
| --- | --- | --- | --- |
| 채팅 메시지 저장 | 메뉴·인사·설정이 아닌 메시지를 `_record_turn_text`로 `sources`(`kind = 'teams_message'`)에 몰래 저장 | 저장하지 않는다. `handle_turn`의 `_is_team_record` 분기와 `_record_turn_text`를 지운다 | 0 |
| 1시간 분석(`analyze_due`) | 쌓인 메시지·Notion 기록을 묶어 Groq로 Task·결정 변경 후보를 만듦 | 돌리지 않는다. `_worklog_loop`에서 호출을 뺀다 | 0 |
| 터미널 승인 | `python -m worklog candidates / approve / reject`로 후보를 하나씩 반영 | 명령을 없앤다. 반영은 미리보기 확인(P1-5)으로만 한다 | 3 |

- `worklog/core.py`의 정규화(`normalize_item`), 관련 항목 찾기(`find_related`), 판정, 항목 쓰기 함수는 P1-6 반영에서 다시 쓴다. 지우는 것은 "묶음 → 후보 → 터미널 승인" 경로뿐이다.
- 이 기능들을 검사하던 기존 테스트는 지우거나 새 경로 기준으로 바꾼다. 바뀐 테스트 목록을 PR에 남긴다(A18의 기준 수가 바뀐다).

### P1-1 md 수집

`notion_md.fetch_markdown(client, page_id: str) -> MarkdownPage`

| 항목 | 규격 |
| --- | --- |
| 호출 | `GET /v1/pages/{page_id}/markdown`, 헤더 `Notion-Version: 2026-03-11`(이 호출에만). 다른 호출의 버전은 바꾸지 않는다. `notion_read.notion_client_for()`가 만든 클라이언트의 `page_markdown(page_id)`로 부른다(호출 관문을 지난다) |
| 응답 | `markdown`, `truncated`, `unknown_block_ids` |
| 잘린 응답 | `truncated = true`면 `unknown_block_ids`의 각 ID로 같은 엔드포인트를 불러 이어 붙인다. 페이지당 최대 20회 **[가정]**. 상한을 넘으면 그 페이지를 블록 읽기로 대체한다 |
| 실패 | 404·지원 안 되는 블록·상한 초과 → 원문(`sources`)은 예전 블록 읽기(`notion_read._block_text`)로 저장하고, 그 페이지의 부분은 만들지 않는다. `fallback_reason`을 기록하고 계측 `fallback`을 센다 |
| 반환 | `MarkdownPage(page_id, md, source = "md" \| "blocks", fallback_reason)` |

- 다시 받는 조건은 지금과 같다. `source_units.last_edited`가 바뀐 페이지·행만 받는다. 단, 부분이 생기기 전에 모은 행(부분이 하나도 없는 행)은 한 번 md로 받아 부분만 채운다(원문은 다시 저장하지 않는다).
- 원문(`sources`)은 메뉴가 계속 쓸 수 있게 그대로 저장한다. 글은 md에서 구조 태그를 뺀 것(`parts.md_to_text`)이다.
- link_to_page 블록은 대상 페이지를 하위 페이지처럼 지도에 넣는다. 다른 루트가 이미 덮은 페이지는 `_covered_ids`로 뺀다.

### P1-2 부분 분할과 블록 대조

분할은 두 단계다. md를 최상위 블록 단위로 자르고(1), 블록을 부분으로 묶는다(2).

**(1) 최상위 블록 자르기** `parts.top_level_blocks(md: str) -> list[RawBlock]`

실제 녹화본 8개(1·2·3차 멘토링, 3차 질문지, 팀 정보 페이지, 긴 WBS 문서 3개)로 확인한 규칙이다. 여덟 페이지 모두 최상위 블록 목록과 종류까지 1:1로 맞았다(`tests/test_fixtures.py`).

- 중첩은 **탭**으로만 표시된다. 탭으로 시작하지 않는 줄이 새 최상위 블록이다. 공백으로 시작하는 줄(` \[클로바노트공유\]`)도 최상위 단락이다.
- `<table>` … `</table>` 안의 `<tr>`·`<td>` 줄은 들여쓰기가 없어도 그 표에 속한다. 최상위 `<table>` 줄은 블록 하나다.
- 들여쓰기 없는 닫는 태그(`</callout>` 등)는 앞 블록에 속한다.
- 최상위 토글은 `<details>`와 다음 줄 `<summary>…</summary>`가 둘 다 들여쓰기 없이 나온다. 둘이 한 블록이고, 부분 제목은 summary의 글자다.
- 코드 블록(```)은 둘째 줄부터 들여쓰기가 없다. 닫는 ``` 까지 앞 블록에 속한다.
- 빈 단락은 `<empty-block/>`, 구분선은 `---`, 하위 페이지는 `<page url=…>제목</page>`, 인라인 DB는 `<database url=…>제목</database>`로 나온다.

**(2) 부분으로 묶기** `parts.group_parts(blocks: list[RawBlock]) -> list[RawPart]`

블록 하나를 부분 하나로 보면 너무 잘게 나뉜다. 1차 멘토링은 최상위 블록이 19개이고, 결론 `→` 줄 네 개도 각각 블록이다. 그래서 경계 블록 사이에 이어진 블록들을 한 부분으로 묶는다.

- 경계: 빈 단락(`<empty-block/>`), 구분선(`---`), 제목(`#`, `##`, `###`). 빈 단락과 구분선은 부분에 넣지 않고, 제목은 다음 부분의 첫 줄이 된다.
- 제목으로 시작한 부분은 빈 단락에서 끝나지 않고 다음 제목·구분선까지 간다. 문서는 제목 뒤에 빈 줄을 두고 본문을 쓰기 때문이다(그러지 않으면 제목만 있는 부분이 생겨 판정할 근거가 없다).
- 하위 페이지(`<page>`)와 인라인 DB(`<database>`)는 부분이 아니라 지도의 하위 항목이다(P1-1의 수집 대상).
- `RawPart(position, title, md, block_positions)`. `title`은 첫 블록 첫 줄에서 목록 기호(`1.`, `-`, `- [ ]`), 마크업, 블록 속성(`{color="…"}`)을 빼고 `<br>` 앞까지만 쓴 글자다. 토글이면 `<summary>`의 글자다.

`parts.align_blocks(blocks: list[RawBlock], top_blocks: list[dict]) -> list[str] | None`

- `notion_map.top_blocks(client, page_id)`의 결과와 개수·순서를 맞춰 블록마다 `block_id`를 붙인다. 부분은 자기 블록들의 ID 목록(`block_ids`)을 갖는다.
- 개수가 다르거나, 같은 위치가 어긋나면 `None`을 돌려준다(구분선 ↔ `---`, 빈 단락 ↔ `<empty-block/>`, 목록 ↔ `1.`·`-`, 제목 ↔ `#`, 표 ↔ `<table`, 하위 페이지 ↔ `<page`, DB ↔ `<database`). 이때 그 페이지의 부분은 블록 ID 없이(`block_ids_json = NULL`) 저장하고, 블록 위치 순번으로 ID를 만든다.
- 경로(`path`)는 `페이지 경로 > 부분 제목`이다. 예: `프로젝트 기본정보 > 1차 멘토링 > 스마트 옷장`.

**기대 결과**

| fixture | 부분 (종류는 P1-3 후 기대값) |
| --- | --- |
| 1차 멘토링 (실제, 최상위 블록 19개) | 식기세척기 · 스마트 옷장(`구체화` 토글은 document로 떼어 냄) · 선박 · 구독 관련 Agent(긍정적) · ThinQ Claw · 결론(`→` 4줄 + 클로바노트 공유 링크) → **6개**, 떼어 낸 뒤 7개 |
| 2차 멘토링 (실제, 16개) | 구독 · 식세기 · 스마트 옷장 · "3개 중에 어느걸 고를지?"와 그 아래 번호 3개 · 클로바노트 공유 → **5개** |
| `meeting_sample.md` (가짜, 19개) | 1차 멘토링과 같은 구조 → **6개**, 떼어 낸 뒤 7개 |

### P1-3 종류 판정

페이지 하나에 Groq 1회. 바뀐 부분이 있는 페이지만 부른다.

**입력**

```json
{
  "page_path": "프로젝트 기본정보 > 1차 멘토링",
  "parts": [
    {
      "part_id": "…",
      "path": "… > 스마트 옷장",
      "chars": 16500,
      "head": "앞 200자",
      "children": [
        {"child_path": "… > 스마트 옷장 > 구체화", "chars": 14500, "toggle": true}
      ]
    }
  ]
}
```

**출력**

```json
{
  "parts": [
    {
      "part_id": "…",
      "kind": "meeting",
      "confidence": 0.9,
      "summary": null,
      "split": [
        {"child_path": "… > 스마트 옷장 > 구체화", "kind": "document", "summary": "스마트 옷장 기획서, 9/28 기준"}
      ]
    }
  ]
}
```

- `split`에 든 하위 항목은 새 부분으로 떼어 내고, 원래 부분의 md에서는 뺀다.
- `confidence < 0.6` **[가정]**이면 `document`로 두고 `kind_note`에 이유를 남긴다. 추출하지 않고 원문만 보관한다.
- `document`·`link`는 `summary`(한 줄 설명, 40자 이내)가 반드시 있다.
- 내용 해시가 같은 부분은 다시 판정하지 않는다. `map-set`으로 사람이 정한 종류(`kind_source = 'human'`)는 같은 `block_id`면 해시가 바뀌어도 유지한다.
- Groq가 없거나 실패하면, 또는 답이 비어 있으면 `kind = NULL`로 두고 다음 동기화에서 다시 판정한다.
- 답에 판정이 있는데 어떤 부분만 빠졌으면(판단할 근거가 없다고 뺀 것) 그 부분은 `document`, `kind_note = 판정 근거 없음`으로 보관만 하고 다시 보내지 않는다.
- 동기화 한 번에 판정에 쓰는 토큰은 약 4,000(추정) **[가정]**이다. 최근 수정된 페이지부터, 한 호출이 그 몫을 넘지 않게 페이지의 부분을 나눠 보내고, 몫을 다 쓰면 멈춘다. 판정이 한 번 실패하면(429 등) 그 동기화에서는 멈춘다. 근거: 한 번에 전부 판정하자 분당 한도(8,000토큰)를 다 써서 바로 뒤 메뉴 2번 요약이 429로 실패했다(Phase 1 실측).

### P1-4 추출

`meeting` 부분 하나에 Groq 1회. 키는 `(content_hash, prompt_version)`.

**출력 항목**

```json
{
  "items": [
    {
      "kind": "decision | task | open | feedback | insight",
      "title": "한 줄 제목",
      "detail": "필요하면 한두 문장",
      "owner": "회의록에 적힌 이름 또는 null",
      "due": "YYYY-MM-DD 또는 null",
      "quote": "근거가 된 원문 그대로",
      "confidence": 0.0
    }
  ]
}
```

- `quote`는 부분 md 안에 그대로 있어야 한다. 없으면 그 항목을 버리고 로그에 남긴다.
- 같은 항목 합치기: `kind`와 `db.normalize_text(title)`이 같으면 하나로 합치고 근거를 모두 남긴다. LLM으로 합치지 않는다.
- 부분 하나의 입력이 분당 한도의 절반(약 4,000토큰) **[가정]**을 넘으면 하위 항목 단위로 나눠 보내고 결과를 이어 붙인다.
- 문서(`document`)는 추출하지 않는다.
- 응답의 `usage`(입력·출력 토큰)를 `part_extracts`에 저장한다.

### P1-5 부분 상태와 반영 흐름

```
(없음) ─수집→ extracted ─2시간 경과 또는 [반영] 요청→ pending_confirm ─[반영]→ applied
                                                          ├─[취소]→ skipped
                                                          └─[고치기]→ pending_confirm (고친 항목으로)
applied ─내용 수정→ (재추출) → applied (바뀐 항목만 반영, 알림)
applied ─삭제 감지→ 권한자에게 "거기서 나온 항목을 지울까요?" → 예: 항목 삭제 / 아니오: 항목 유지, 부분은 gone
```

| 경로 | 동작 |
| --- | --- |
| 직접 반영 | 4번 ② "Notion 변경 반영" → 최근 수정된 페이지 목록(최근 10개, 수정 시각순) → 선택 → 그 페이지를 지금 수집·판정·추출(대기열 최우선) → 미리보기 카드 |
| 자동 감지 | 새 `meeting` 부분을 미리 추출해 `extracted`로 둔다. 마지막 수정 후 2시간이 지나면 `pending_confirm`으로 올리고 메인 카드에 확인 대기를 띄운다 |
| 확인 | 팀원 누구나. `confirmed_by`에 Teams 사용자 ID를 남긴다 |
| [고치기] | 항목별 수정·삭제·추가. 고치기 전후를 `extract_corrections`에 저장한다 |
| 소스 페이지 변경 | 수정·삭제 모두 권한자에게 확인. 확인 전에는 저장본을 바꾸지 않는다 |

**직접 반영의 시간 제한**: 턴은 100초 시한(`TURN_DEADLINE_SECONDS`) 안에 끝나야 한다. 읽는 동안 "읽는 중… / 정리 중…" 상태를 보내고, 시한 안에 추출이 끝나지 않으면 "정리가 끝나면 메인 카드에 올려 둘게요"라고 답한 뒤 대기열(순위 1)에서 계속 처리한다. 끝나면 그 부분은 `pending_confirm`이 된다.

**[고치기]**: 지금 카드는 버튼만 보낼 수 있고 입력칸이 없다. 그래서 채팅으로 고친다.

1. [고치기]를 누르면 항목을 번호와 함께 다시 보여 주고, 항목마다 [삭제] 버튼을 단다.
2. 고치거나 더할 때는 채팅으로 보낸다. `3: 새 문장`은 3번 항목 제목 바꾸기, `+ 할 일: 문장`은 항목 추가(종류는 결정·할 일·미정·피드백·인사이트 중 하나).
3. [다 고쳤어요]를 누르면 고친 목록으로 미리보기를 다시 보여 준다. 고치기 세션은 사용자별로 서버 메모리에 두고, 10분 동안 입력이 없으면 끝낸다.

**미리보기 카드**

```
{페이지 제목} ({MM/DD HH:mm} 수정)
결정 n · 할 일 n · 미정 n · 피드백 n · 인사이트 n
- (결정) …
- (할 일) … · {담당} · {기한}
…
[반영] [고치기] [취소]
```

### P1-6 worklog 반영

- 확인된 항목은 `worklog/core.py`의 `normalize_item` → `find_related` → 판정을 거쳐 반영한다. 반영자(actor)는 확인한 사람이다.
- 판정 결과에 따라 처리한다.

| 판정 | 처리 | 알림 |
| --- | --- | --- |
| 새 항목 | `work_items` 생성 | 메인 카드 "반영 n건" |
| 기존 항목 보완 | 기존 항목 갱신, `revisions` 기록 | 메인 카드 "반영 n건" |
| 기존 결정 대체 (확신 ≥ 0.7 **[가정]**) | 기존 결정 `superseded`, 새 결정 생성, `change_candidates.status = 'auto_applied'` | 메인 카드 "결정이 바뀌었어요: A → B [되돌리기]" |
| 확신 < 0.7 또는 판정 실패 | 반영하지 않음, `work_items.status = 'needs_discussion'` | 2번 메뉴 "추가 논의 필요" |

- `revert(store, candidate_id, actor)`: 권한자만. `revisions`의 이전 스냅샷으로 항목을 되돌리고, 만든 링크를 지우고, `change_candidates.status = 'reverted'`로 둔다.
- `work_items.kind`에 `feedback`, `insight`를 더한다.
- 처음 수락 기준 4번("AI 판단은 후보일 뿐, 확정이 아니다")은 v1.1에서 "새 회의록은 사람이 한 번 확인하고, 확인된 항목은 자동 반영하며 출처·이력·되돌리기로 통제한다"로 바꾼다. 사유: 건별 승인은 실제로 이뤄지지 않아 기록이 멈췄다.

### P1-7 회의록 입구

| 입구 | 받는 형태 | 처리 |
| --- | --- | --- |
| Notion | 팀 정보 페이지 아래 전부, 소스 추가로 등록한 페이지·DB | P1-1~P1-5 |
| Teams 채팅 | 4번 ④ "회의록 올리기" 뒤에 보낸 텍스트 | md로 저장, `origin = 'teams'`, 제목은 첫 줄 |
| Teams 파일 | txt, docx | docx는 문단·목록·표를 md로 바꾼다. `origin = 'teams'` |
| 클로바노트 | 작성자가 내려받은 파일이나 복사한 텍스트를 Teams로 | `origin = 'clova'`(파일 이름이나 사용자 선택으로 구분). 공유 링크는 읽지 않는다 |

- Teams·클로바노트 입력은 받는 즉시 직접 반영 경로(미리보기 카드)로 간다.
- 파일을 받으려면 봇이 첨부를 서버로 넘겨야 한다. 지금 봇(`src/bot/main.py`의 `_turn_payload`)은 `text`와 `value`만 넘긴다. `attachments`(이름, `contentType`, `contentUrl` 또는 다운로드 URL)를 더하고, 서버 `TurnRequest`에도 같은 필드를 더한다. 파일 내려받기는 서버가 한다.
- docx 변환은 `python-docx`를 쓴다(`pyproject.toml` 의존성 추가).
- 설정 마법사에서 6단계(`meeting_notes_page_id`)를 뺀다. 값이 이미 있는 팀은 그 페이지를 소스로 계속 쓴다.
- 첫 수집이 끝나면 메인 카드에 "회의록 n건을 찾았어요"를 띄운다. 0건이면 "회의록이 다른 곳에 있나요? [소스 추가] [회의록 올리기]"를 띄운다.
- Teams 공유 링크(`clovanote.naver.com/s/…`)가 메시지에 오면 "공유 링크는 읽을 수 없어요. 노트에서 내려받은 파일이나 텍스트를 보내 주세요"라고 답한다.

### P1-8 Groq 대기열

| 순위 | 작업 | 순서 |
| --- | --- | --- |
| 1 | 사람이 누른 [반영]·회의록 올리기 | 받은 순서 |
| 2 | 자동 감지 추출, 종류 판정 | 최근 수정된 것부터 |
| 3 | 단계 판정 (P2-4) | 오래된 것부터 |

- 응답 헤더 `x-ratelimit-remaining-tokens`, `x-ratelimit-reset-tokens`로 다음 호출 시각을 정한다.
- 백그라운드(순위 2·3)는 하루 한도의 70% **[가정]**까지만 쓴다. 순위 1은 한도 끝까지 쓴다.
- 429 응답: 순위 1이면 `retry-after`만큼 기다렸다 한 번 더, 순위 2·3은 다음 주기로 넘긴다.
- 사용자 안내: 분당 한도 → "n초 뒤 이어서 정리해요", 하루 한도 → "내일 다시 정리돼요. 원문은 저장했어요".
- 대기 중인 부분 수(종류 판정 전 + 추출 전 회의 메모)를 "n건 정리 중"으로 보인다. Phase 2에서는 메뉴 2번 끝에, 메인 카드는 Phase 3부터.
- 하루 합계는 응답 헤더로 오지 않아서 저장소 `llm_usage`(UTC 날짜·공급자별)에 직접 센다(`GROQ_DAILY_TOKENS` 기본 200,000, `GROQ_BACKGROUND_SHARE` 기본 0.7). Groq의 하루 경계와 정확히 같지는 않다.
- 구현: `src/server/llm_queue.py`. 백그라운드 호출은 `check()`가 막으면 `Busy`를 내고, 판정·추출은 그 주기를 멈춘다. 사람이 기다리는 호출(메뉴 요약)은 막지 않고 `record()`로 쓴 양만 알린다.
- 백그라운드 루프는 주기마다(기본 5분) 판정·추출을 조금씩 이어 한다(Notion은 부르지 않음). Notion 동기화는 지금처럼 1시간마다.
- **예비 공급자(Cerebras)**: `CEREBRAS_API_KEY`가 있으면 백그라운드 호출(순위 2·3)은 Groq가 막히거나(분당·하루 몫) 429를 주면 같은 모델(`gpt-oss-120b`)의 Cerebras로 넘긴다. Cerebras는 한도 헤더가 없어 분당 호출 수(기본 5)와 하루 합계(기본 100만의 90%)를 직접 센다. 추출 결과의 `model`에 `공급자:모델`을 남긴다. 사람이 기다리는 호출(메뉴 요약)은 Groq만 쓴다. Gemini는 무료 등급이 입력을 학습·사람 검토에 쓰고 모델이 달라 넣지 않았다.
- 모델은 `GROQ_MODEL`(기본 `openai/gpt-oss-120b`). 순위 2용 모델을 따로 둘 수 있게 `GROQ_BACKGROUND_MODEL`을 둔다. 기본값은 같은 모델이고, gpt-oss-20b로 바꿀지는 정답지 비교로 정한다.

### P1-9 메뉴 1·2번 (P1 범위)

- 2번: 반영된 `work_items`와 확인 대기 수만 읽는다. 화면 구성은 P2-7 전까지 지금과 같다.
    - Phase 2에는 아직 반영(확인)이 없다. 이 기간에는 `part_extracts`를 직접 읽고 제목 옆에 "(확인 전)"을 붙인다. Phase 3에서 반영된 `work_items`를 읽도록 바꾼다.
    - Phase 2 화면: 할 일(담당 있음)은 지금처럼 GitHub 이슈 상태로 나누고, "📌 정한 것"(결정), "🤔 아직 정해야 할 사안"(미정 + 담당 없는 할 일), "💬 멘토링 피드백"(최근 5개)을 보인다. 추출 전이면 원문 요약을 "회의록 정리 중"으로 보인다.
    - 같은 항목(종류 + 정규화한 제목)은 회의가 달라도 하나로 합치고 근거를 모두 남긴다. 최근 회의의 항목이 먼저.
- 1번: 지금 내용에 "주요 문서"를 더한다. `document` 부분의 제목, 한 줄 설명, Notion 링크를 최근 수정순으로 최대 5개.
- 기준 시각 "(Notion 기준 MM/DD HH:mm)"은 `source_pages.checked_at`의 최댓값이다. `checked_at`은 내용이 안 바뀌어도 확인할 때마다 갱신한다.

### P1-10 다시 수집과 이전

- `src/server`에서 `python reindex.py --scope <team_key>`(worklog 패키지는 Notion·LLM에 의존하지 않으므로 서버 폴더에 둔다): 저장소를 `worklog.sqlite3.bak-YYYYMMDD-HHMMSS`로 복사하고, 그 팀의 페이지·행 수정 시각, 구조 지문, DB 조회 커서를 지운 뒤 모든 페이지를 md로 다시 받아 부분을 만든다. 기존 `sources`는 지우지 않는다.
- 동기화 몫을 넘는 나머지 부분도 끝까지 판정한다. 분당 6,000토큰을 넘지 않게 쉬어 가고, 429가 나면 1분 기다렸다 최대 3번 다시 한다.
- 다시 수집한 부분 중 이미 반영된 내용과 같은 해시는 `applied`로 둔다. 처음 보는 부분은 `extracted`부터 시작한다.
- `role_source = 'none'`("판정 못 함") 섹션은 다음 동기화에서 다시 판정한다.

## P2 단계 길잡이

### P2-1 사람과 권한

| 동작 | 팀원 | 개발자 | 권한을 받은 사람 | 관리자 |
| --- | --- | --- | --- | --- |
| 메뉴 1~3 보기, 알림 내용 보기 | O | O | O | O |
| Notion 변경 반영, 미리보기 확인·고치기, 회의록 올리기 | O | O | O | O |
| 단계 전환 승인, 게이트 답, 되돌리기, 삭제·소스 변경 확인, 게이트 편집 | X | O | O | O |
| 설정, 권한 부여·회수, 역할 고치기, 관리자 추가·이전 | X | X | X | O |

- 관리자는 설정 마법사를 끝까지 마친 첫 사람이다(지금 `team_configs.json`의 `admin_aad_id`).
- 개발자는 처음 등록에서 역할로 "개발"을 고른 사람이다.
- 권한 판정: `members.can_approve`가 참이거나, 역할이 `dev`이거나, 관리자인 경우. `can_approve`는 관리자만 바꾼다.

### P2-2 처음 등록

처음 메시지를 보낸 팀원에게 순서대로 묻는다.

1. "역할을 골라 주세요" [기획] [디자인] [개발] [기타] (필수)
2. "GitHub 아이디를 알려 주세요" (건너뛰기 가능)
3. "회의록에 이름이 어떻게 적히나요? 여러 개면 쉼표로" (건너뛰기 가능)

- 건너뛴 항목은 3번 메뉴를 처음 누를 때 다시 묻는다.
- 저장 위치는 `members`(사람 단위, 팀이 달라도 같이 쓴다). 지금 `member_profiles.json`의 `github_id`는 처음 실행할 때 `members.github_login`으로 옮긴다.
- "별칭" 명령으로 별칭을 고친다.

### P2-3 단계와 게이트

공통 기본값(`stage_gates.scope = '*'`) **[가정]**. 권한자가 팀별로 더하거나 빼거나 고치면 그 팀의 행이 생긴다.

| 세부 단계 | gate_id | 조건 | 충족 판정 |
| --- | --- | --- | --- |
| 1-1 문제 탐색 | `candidates` | 후보 주제가 1개 이상이고, 후보마다 해결할 문제가 있다 | 반영된 항목에 후보 주제가 있고 문제 설명이 붙어 있음 |
| 1-2 방향 확정 | `topic_decided` | 주제 확정 결정이 있다 | `decision` 중 주제 확정이 있음 |
| 1-2 방향 확정 | `core_problem` | 해결할 핵심 문제가 정해졌다 | `decision` 중 핵심 문제가 있음 |
| 1-3 구체화·타당성 | `assumptions` | 검증할 핵심 가정이 정해졌다 | `decision`·`open` 중 가정 목록이 있음 |
| 1-3 구체화·타당성 | `target_customer` | 타깃 고객이 정해졌다 | `decision` 중 타깃 고객이 있음 |
| 2-1 VOC 설계 | `voc_method` | 검증 방법과 대상이 정해졌다 | `decision` 중 인터뷰·설문 방법과 대상 |
| 2-2 수집 | `voc_collected` | 수집 결과가 기록됐다 | `insight`가 1개 이상 |
| 2-3 분석·반영 | `voc_result` | 가정 검증 결과와 기능 우선순위가 정해졌다 | 가정별 결과와 우선순위 `decision` |
| 3-1 백로그 | `backlog` | GitHub에 백로그 이슈가 있다 | 연결된 저장소에 열린 이슈가 1개 이상 |
| 3-2 설계 | `architecture` | 아키텍처 결정이 있다 | `decision` 중 `areas`에 `architecture` |
| 3-3 스프린트 반복 | `milestones` | 마일스톤별 진행이 기록된다 | GitHub 마일스톤이 있고 닫힌 이슈가 있음 |
| 3-4 릴리스·검증 | `release` | 릴리스 기준과 충족 여부가 기록됐다 | 릴리스 기준 `decision`과 결과 기록 |

- "주제 확정", "핵심 문제" 같은 의미 판정은 추출 항목의 `title`·`detail`을 보고 Groq가 게이트별로 참/거짓과 근거 항목 ID를 낸다(대기열 순위 3). 근거 없는 참은 받지 않는다.
- VOC 단계(2-x)는 권한자가 건너뛸 수 있다. 스프린트는 GitHub 마일스톤으로 본다.
- 이 팀의 기대값: `candidates` 충족, `topic_decided` 미충족(기획서 "현재 상태: 팀 주제 후보") → 현재 단계 1-2, 빈 게이트 `topic_decided`.

### P2-4 단계 제안

현재 단계는 `team_stages.stage`다. 승인 없이는 바뀌지 않는다. 처음 값은 1-1이고, 이전한 팀은 권한자가 첫 제안에서 정한다.

| 상황 | 카드 | 답과 결과 |
| --- | --- | --- |
| 현재 단계의 게이트가 비어 있음 | "현재 {단계}예요. {빈 게이트}만 확정하고 넘어갈까요?" | [정할게요] → 게이트 `open` 유지, 2번 "지금 진행 중"에 표시 / [나중에] → `later` |
| 현재 단계의 게이트가 모두 충족 | "{단계}에서 정할 사안을 완료했어요. 다음 단계로 넘어갈까요?" | [넘어갈게요] → `team_stages` 갱신, `history_json`에 승인자·시각 / [아직이요] → 그대로 |
| 논의 중인 단계가 현재 단계보다 앞서는데 게이트가 비어 있음 | "이미 {논의 중 단계} 내용을 논의하고 있는데 {빈 게이트}가 없어요." | [정할게요] → `open` / [나중에 할게요] → `later`, 2번 "이전 단계에서 남은 것" / [건너뛰기] → `skipped`, 목록에서 제외 |
| VOC 결과가 가정을 뒤집음 | "{가정}이 VOC에서 뒤집혔어요. 기획 단계로 돌아갈까요?" | [돌아갈게요] → 1-3으로, 이력 기록 / [아니요] → 그대로 |

- "논의 중인 단계"는 최근 반영된 항목을 Groq로 읽어 세부 단계 하나와 근거 항목 ID로 낸다.
- 같은 (팀, 질문 종류, 단계, 게이트) 질문은 `prompts_asked`에 답이 있으면 다시 하지 않는다. 게이트 상태가 바뀌면 새 질문으로 본다.
- 버튼은 권한자에게만 보이고, 다른 팀원에게는 같은 카드가 "확인 대기 중"으로 보인다.

### P2-5 메인 카드

메인 카드(챗봇을 열 때 보이는 카드) 위쪽에 알림을 최신순으로 최대 5개 보여 준다.

| 알림 | 보이는 사람 | 버튼 |
| --- | --- | --- |
| 새 회의록 n건 확인 대기 | 모두 | [확인하기] (모두) |
| 반영 n건 / 결정 대체 A → B | 모두 | [되돌리기] (권한자) |
| 단계 제안·게이트 질문 | 모두 | 답 버튼 (권한자) |
| 회의록 삭제·소스 페이지 변경 확인 | 모두 | [반영] [무시] (권한자) |
| n건 정리 중 | 모두 | 없음 |
| 회의록 n건을 찾았어요 / 못 찾았어요 | 모두 | [소스 추가] [회의록 올리기] |

- 확인 대기가 3일 **[가정]** 넘게 쌓이면 관리자에게 한 번 더 알린다.

### P2-6 변경 감지

| 시점 | 동작 | Notion 호출 |
| --- | --- | --- |
| 챗봇을 열 때 (그 팀 사용자의 턴: welcome 또는 메시지·버튼) | 마지막 확인이 10분보다 오래됐으면 등록된 페이지·DB의 수정 시각만 확인. 턴 응답을 기다리게 하지 않도록 백그라운드로 돌린다 | 페이지·DB당 1회 |
| 1시간마다 | 같은 수정 시각 확인(백그라운드 관문) | 페이지·DB당 1회 |
| 수정 시각이 바뀜 | 그 페이지만 P1-1부터 처리(백그라운드 대기열) | 필요한 만큼 |

- 지금의 1시간 주기 전체 동기화(`sync_roots`)는 수정 시각 확인으로 바꾼다. 구조 지문 비교는 수정 시각이 바뀐 페이지에서만 한다.

### P2-7 메뉴

| 메뉴 | 내용 |
| --- | --- |
| 1번 팀/프로젝트 정보 | 지금 요약 + 주요 문서(P1-9) |
| 2번 진행상황 | 현재 단계 / 확정된 흐름(대체 이력 포함) / 지금 진행 중 / 이전 단계에서 남은 것 / 추가 논의 필요 / 천천히 정해도 되는 것, 끝에 "(MM/DD 기준)" |
| 3번 내 역할 | 내가 맡은 일(GitHub 배정 이슈 + 별칭이 `owner`에 맞는 할 일), 그 아래 `owner`가 없는 할 일 중 내 역할에 맞는 것(Groq가 추출할 때 붙인 역할 태그 기준) + [내가 맡기] |
| 4번 작업 공유·반영 | ① 완료한 작업 공유(GitHub Project → Done) ② Notion 변경 반영 ③ 최근 반영 내역(되돌리기, 권한자) ④ 회의록 올리기 ⑤ 소스 추가 |

- [내가 맡기]: 할 일의 `owner`를 누른 사람으로 바꾸고 `revisions`에 남긴다. GitHub 이슈가 연결돼 있으면 배정도 바꾼다.
- 추출 항목의 역할 태그를 위해 P1-4 출력의 `task`에 `role_hint`(`plan` / `design` / `dev` / `null`)를 더한다.

## 데이터 모델

이미 적용된 마이그레이션 1~4는 고치지 않고 뒤에 더한다. `work_items.kind`·`status`, `change_candidates.status`는 TEXT라 새 값에 마이그레이션이 필요 없다.

| 버전 | Phase | 내용 |
| --- | --- | --- |
| 5 | 1 | `source_parts` |
| 6 | 2 | `part_extracts` |
| 7 | 3 | `extract_corrections` |
| 8 | 5 | `members`, `team_members`, `team_stages`, `stage_gates`, `prompts_asked` |

```sql
-- 5
CREATE TABLE source_parts (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    origin TEXT NOT NULL,            -- notion | teams | clova
    page_id TEXT,                    -- notion만
    section_id TEXT,                 -- 지도의 섹션
    block_ids_json TEXT,             -- 대조 성공 시 이 부분의 최상위 블록 ID 목록
    parent_part_id TEXT,             -- split으로 떼어 낸 경우
    path TEXT NOT NULL,
    position INTEGER NOT NULL,
    kind TEXT,                       -- meeting | document | link | NULL(미판정)
    kind_source TEXT,                -- llm | human | fallback
    kind_note TEXT,
    summary TEXT,
    md TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    chars INTEGER NOT NULL,
    state TEXT NOT NULL,             -- collected | extracted | pending_confirm | applied | skipped
    last_edited TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT,
    status TEXT NOT NULL,            -- active | gone
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX source_parts_scope ON source_parts(scope, state, status);
CREATE INDEX source_parts_page ON source_parts(page_id, position);

-- 6
CREATE TABLE part_extracts (
    content_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    items_json TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (content_hash, prompt_version)
);

-- 7
CREATE TABLE extract_corrections (
    id TEXT PRIMARY KEY,
    part_id TEXT NOT NULL REFERENCES source_parts(id),
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 8
CREATE TABLE members (
    teams_user_id TEXT PRIMARY KEY,  -- aad_object_id
    role TEXT,                       -- plan | design | dev | other
    github_login TEXT,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE TABLE team_members (
    scope TEXT NOT NULL,
    teams_user_id TEXT NOT NULL REFERENCES members(teams_user_id),
    can_approve INTEGER NOT NULL DEFAULT 0,
    is_admin INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, teams_user_id)
);
CREATE TABLE team_stages (
    scope TEXT PRIMARY KEY,
    stage TEXT NOT NULL,             -- '1-1' … '3-4'
    approved_by TEXT,
    approved_at TEXT,
    history_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE stage_gates (
    scope TEXT NOT NULL,             -- '*' = 공통 기본값
    stage TEXT NOT NULL,
    gate_id TEXT NOT NULL,
    text TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',  -- open | met | later | skipped
    evidence_json TEXT,
    edited_by TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, stage, gate_id)
);
CREATE TABLE prompts_asked (
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,              -- stage_advance | gate_missing | skipped_ahead | pivot | delete_confirm | source_change
    key TEXT NOT NULL,
    answer TEXT,
    answered_by TEXT,
    asked_at TEXT NOT NULL,
    answered_at TEXT,
    PRIMARY KEY (scope, kind, key)
);
```

- 역할·별칭은 사람 단위(`members`), 권한은 팀 단위(`team_members`)로 둔다. 보고서의 `members.can_approve`를 팀마다 다를 수 있게 나눈 것이다.
- 추출 항목의 `quote`는 반영할 때 기존 `evidence`(`evidence_kind = 'quote'`)로 저장한다.

## 계측

메뉴 응답·반영 요청·백그라운드 작업마다 서버 로그에 한 줄을 남긴다. 테스트와 가정 수정의 근거로 쓴다.

```
metric kind=<menu|apply|background> team=<scope> menu=<1-4|-> notion_calls=<n> groq_calls=<n> groq_tokens=<n> parts=<n> fallback=<n> elapsed_ms=<n>
```

- 단계 판정은 입력 항목 ID 목록과 결과(단계, 게이트별 참/거짓, 근거 ID)를 `stage_judge` 로그로 남긴다.
- 토큰·비밀값은 로그에 남기지 않는다.

## 수락 기준

테스트 이름은 가칭이다. "고치기 전 실패"는 새 테스트를 지금 코드에 돌리면 실패해야 한다는 뜻이다.

| ID | 기준 | 확인 | Phase | 성공 기준 |
| --- | --- | --- | --- | --- |
| A1 | `meeting_sample.md`가 부분 6개로 나뉜다(실제 녹화본이 있으면 1차 6개, 2차 5개) | `test_group_parts_mentoring` | 1 | 2 |
| A2 | `구체화` 토글이 `document` 부분으로 떼어지고, `스마트 옷장` 부분 md에는 없다 | `test_split_document_child` (가짜 Groq) | 1 | 2 |
| A3 | fixture의 표 셀 내용이 모두 부분 md에 있다(`meeting_sample.md` 표 3개, 실제 1차 멘토링 표 6개) | `test_md_keeps_tables` | 1 | - |
| A4 | 블록 수가 다르면 블록 읽기로 대체한다 | `test_align_mismatch_falls_back` | 1 | - |
| A5 | 같은 (내용 해시, 프롬프트 버전)은 재시작해도 다시 추출하지 않는다 | `test_extract_cached_by_hash` | 2 | 4 |
| A6 | 메뉴 2번 응답의 `groq_calls = 0` | `test_menu2_no_groq` + 계측 로그 | 2 | 4 |
| A7 | 미리보기 [반영]만 `work_items`를 만든다. [취소]는 만들지 않는다 | `test_confirm_flow` | 3 | 1 |
| A8 | [고치기] 전후가 `extract_corrections`에 남는다 | `test_corrections_saved` | 3 | 1 |
| A9 | 확인된 회의록이 수정되면 바뀐 항목만 반영된다 | `test_reapply_changed_items_only` | 3 | - |
| A10 | 회의록 부분 삭제는 권한자 확인 전까지 항목을 지우지 않는다 | `test_delete_needs_approval` | 3 | - |
| A11 | 결정 대체 후 `revert()`가 이전 상태로 되돌린다 | `test_revert_supersede` | 3 | - |
| A12 | `quote`가 원문에 없는 항목은 버린다 | `test_quote_must_exist` | 2 | 1 |
| A13 | 클로바노트 공유 링크를 받으면 파일을 보내 달라고 답한다 | `test_clova_link_reply` | 4 | - |
| A14 | 권한이 없는 팀원에게 승인 버튼이 없다 | `test_buttons_by_permission` | 5 | - |
| A15 | 이 팀 fixture로 현재 단계 1-2, 빈 게이트 `topic_decided` | `test_stage_this_team` | 5 | 3 |
| A16 | 같은 질문을 두 번 하지 않는다. 게이트 상태가 바뀌면 새 질문으로 본다 | `test_prompt_not_repeated` | 5 | - |
| A17 | 정답지와 비교해 빠진 항목·틀린 항목이 0 | 실제 Notion 확인 | 3 | 1 |
| A18 | 그 전 phase까지의 테스트 전부 통과(v1.1 시작 시 77개) | 전체 테스트 | 전부 | - |
| A31 | 정답지 비교 명령이 빠진 항목·틀린 항목 수를 낸다 | `test_extract_eval` | 3 | 1 |
| A32 | 직접 반영이 100초 안에 안 끝나면 안내하고 대기열에서 이어서 처리한다 | `test_apply_over_deadline` | 3 | - |
| A33 | 메뉴가 아닌 채팅 메시지를 보내도 `sources`에 아무것도 저장되지 않는다 | `test_chat_not_recorded` | 0 | - |
| A34 | 백그라운드 루프가 `analyze_due`를 부르지 않는다 | `test_loop_skips_analyze_due` | 0 | - |
| A35 | `candidates`·`approve`·`reject` 명령이 없다 | `test_cli_has_no_candidate_commands` | 3 | - |
| A19 | `truncated` 응답을 이어 받고, 20회를 넘으면 블록 읽기로 대체한다 | `test_md_truncated_follow_up` | 1 | - |
| A20 | 같은 내용 해시는 다시 판정하지 않고, `kind_source = 'human'`은 해시가 바뀌어도 유지한다 | `test_kind_cached_and_human_kept` | 1 | - |
| A21 | 대기열 우선순위·백그라운드 하루 몫·429 처리 | `test_groq_queue_priority` | 2 | 4 |
| A22 | `document` 부분은 추출하지 않는다 | `test_document_not_extracted` | 2 | 2 |
| A23 | 자동 감지된 새 회의록은 마지막 수정 후 2시간 전에 확인 대기로 올라가지 않는다 | `test_pending_after_two_hours` | 3 | - |
| A24 | 회의록 페이지가 이미 등록된 팀은 설정 변경 뒤에도 같은 회의록을 읽는다 | `test_existing_notes_page_kept` | 3 | - |
| A25 | 붙여넣은 텍스트가 `origin = 'teams'` 부분이 되고 미리보기로 간다 | `test_paste_to_preview` | 4 | - |
| A26 | docx의 문단·목록·표가 md로 바뀐다 | `test_docx_to_md` | 4 | - |
| A27 | 처음 등록이 역할 → GitHub → 별칭 순서이고, 역할 없이는 넘어가지 않는다 | `test_onboarding_order` | 5 | - |
| A28 | 단계는 승인 없이 바뀌지 않는다 | `test_stage_needs_approval` | 5 | 3 |
| A29 | 열 때 감지는 10분 안에 다시 열면 Notion을 부르지 않는다 | `test_open_check_cached` | 5 | - |
| A30 | [내가 맡기]가 할 일의 담당을 바꾸고 `revisions`에 남긴다 | `test_take_task` | 5 | - |

## 변경 이력

| 날짜 | 바뀐 것 | 근거 |
| --- | --- | --- |
| 2026-11-04 | Phase 5 구현: 권한 판정 하나(`people.can_approve`), 처음 등록, 단계 판정 입력에 문서 부분 포함(약 5,000토큰), 첫 제안에 논의 중인 단계와 [그 단계로] 답, 지나온 단계의 빈 게이트도 질문, 3번 GitHub는 한 번만 다시 묻기, 4번 ⑤ 소스 추가는 권한자. 데이터 모델 추가 열은 phase-5 문서 | 실측: 문서 없이 판정하면 후보 주제 근거가 없음, 입력이 크면 Groq 413. 성공 기준 3은 실제 데이터로 나오지 않음(팀이 보고서 이후 더 진행) |
| 2026-11-04 | Phase 4 구현: 4번 ④ 회의록 올리기(텍스트·txt·md·docx), origin 구분 규칙(파일 이름·'참석자 n 시각' 줄), 받은 부분은 종류 판정 없이 회의 메모, 첫 수집 안내는 한 번만(`notes_found_noticed`). 채널·그룹 채팅 파일은 받지 못하고 붙여넣기로 안내 | 채널 파일은 SharePoint 링크라 Graph 권한이 필요 |
| 2026-11-04 | Phase 3 구현: 4번 메뉴 ① 공유 ② Notion 변경 반영 ③ 최근 반영 내역, 고치기에 채팅 `- n` 지우기 추가, 이미 추출된 부분은 추출됨으로 올림, 메뉴 4번 이름 "작업 공유·회의록 반영", `extract-eval` 명령. 첫 수집 안내는 Phase 4, 소스 페이지 변경 확인은 Phase 5로 미룸 | 실측: 1차 멘토링 한 건에서 39건(삭제 버튼 12개로 부족), Phase 2 추출분 13개가 확인 대기에 오지 않음 |
| 2026-11-03 | 종류 판정에 페이지 전체(제목·길이·부분 수·목차)를 함께 보냄, `reindex.py --rejudge-parts`. 추출 지시문 3판(한국어 제목, 멘토 발언은 피드백, 아이디어·기능 설명과 근거 없는 결정은 뽑지 않음, 질문지는 open, 담당은 사람만) | 실측: 큰 기획 문서의 제목 부분 15개가 회의 메모로 판정돼 메뉴 2번을 채움(33→19개로 바로잡음). 1차 초안에서 영어 제목, "식상한거…"가 미정으로, 기능 설명이 할 일로, 질문지가 피드백으로 분류됨 |
| 2026-11-03 | 예비 공급자 Cerebras(같은 모델), 대기열을 공급자별로(`llm_queue`, `llm_usage`) | Groq 하루 20만 토큰을 실측 하루 만에 거의 다 씀. Cerebras 무료 체험 하루 100만·분당 5회(공식 문서) |
| 2026-11-03 | Phase 2 구현: 추출 키는 떼어 낸 뒤 md 해시, 하루 합계를 `groq_usage`에 직접 셈, 메뉴 2번 섹션(정한 것·미정·피드백), 추출 초안 도구 `extract_draft.py` | Groq 응답 헤더에 하루 남은 양이 없음 |
| 2026-11-03 | 제목 부분은 빈 단락에서 끝나지 않음, 제목 정리, 근거 없음 부분은 보관만 | reindex 실측: 제목만 있는 부분 11개가 판정되지 않고 남음, 큰 문서 한 페이지가 부분 57개(→43개), 판정에 Groq 73,104토큰 |
| 2026-11-03 | Phase 1 구현·실측 반영: md 실패 시 원문만 블록 읽기(부분 없음), 대조 실패 시 ID 없이 저장, 부분 없는 행 한 번 채우기, 토글·코드 블록 규칙, 판정 예산(동기화당 ~4,000토큰, 최근 수정 먼저), reindex는 서버 폴더 `reindex.py`(커서까지 지우고 쉬어 가며 끝까지 판정), state `collected` 추가 | 실제 녹화본 8개 대조 성공, 처음 수집 Notion 418→42회·143→20초, 한 번에 판정 시 Groq 429로 메뉴 2번 실패 |
| 2026-11-03 | P1-2 분할 규칙: 블록 하나 = 부분 하나 → 경계(빈 단락·구분선·제목) 사이 블록 묶음. md 최상위 줄 규칙(탭만 중첩, 표 안 줄·닫는 태그는 앞 블록). 1차 멘토링 기대값 7개 → 6개, `block_id` → `block_ids_json` | Phase 0 실제 녹화본 5개: 1차 멘토링 최상위 블록 19개(빈 단락 7, 구분선 1, `→` 단락 4 포함) |
| 2026-11-03 | P1-0 추가: 채팅 메시지 저장과 변경 후보·터미널 승인 경로를 없앤다 | 송지원 결정(숨은 기능이고 쌓인 데이터 0건) |
| 2026-11-03 | 첫 작성. **[가정]** 값: 종류 판정 확신 0.6, 대체 확신 0.7, md 이어 받기 20회, 추출 분할 4,000토큰, 백그라운드 70%, 확인 대기 알림 3일, 게이트 기본값 12개(세부 단계 10개) | [report-2026-11-03.md](report-2026-11-03.md) |
