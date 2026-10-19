"""
회의록(Notion) 페이지에 테스트용 '팀 소개' 항목을 하나 만든다.
1) 먼저 대상 페이지/DB 구조를 확인해서 화면에 출력하고
2) 프로퍼티 스키마에 맞춰 자동으로 새 항목을 추가한다.

실행: (venv 활성화된 상태에서, echo-bot 폴더에서)
    python scripts/seed_meeting_notes.py
"""

import os
import sys
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
MEETING_NOTES_PAGE_ID = "3a611be97ce7804a93eee64fa0735e5f"

notion = Client(auth=NOTION_TOKEN)


def main() -> None:
    print(f"[1/3] 페이지/DB 정보 조회 중... ({MEETING_NOTES_PAGE_ID})")

    is_database = False
    try:
        db = notion.databases.retrieve(MEETING_NOTES_PAGE_ID)
        is_database = True
        props = db.get("properties", {})
        print("-> 이 페이지는 '풀페이지 데이터베이스'입니다. 속성 목록:")
        for name, meta in props.items():
            print(f"   - {name!r}: {meta.get('type')}")
    except Exception as e:
        print(f"   databases.retrieve 실패({e}), 일반 페이지로 시도합니다.")

    if not is_database:
        page = notion.pages.retrieve(MEETING_NOTES_PAGE_ID)
        print("-> 이 페이지는 일반 페이지입니다.")
        print(page)
        print("\n일반 페이지라 이 스크립트는 블록을 추가하지 않습니다. 담당자에게 알려주세요.")
        sys.exit(0)

    # ── 요약 텍스트 (플레이스홀더 팀 소개 + 테스트용 담당자 언급) ──
    summary_text = (
        "우리 팀은 B2B AI Agent 플랫폼을 만드는 팀입니다. "
        "이 회의록 항목은 3번(내 역할) 기능 테스트용으로 자동 생성된 샘플입니다. "
        "Alex Wilber 님이 API 연동 테스트와 웹훅 점검을 맡기로 했습니다. (#1)"
    )

    properties: dict = {}
    for name, meta in props.items():
        ptype = meta.get("type")
        if ptype == "title":
            properties[name] = {"title": [{"text": {"content": "팀 소개"}}]}
        elif ptype == "rich_text" and name == "Summary":
            properties[name] = {"rich_text": [{"text": {"content": summary_text}}]}
        elif ptype == "date":
            properties[name] = {"date": {"start": date.today().isoformat()}}

    print("\n[2/3] 아래 내용으로 새 항목을 생성합니다:")
    print(f"   제목: 팀 소개")
    print(f"   Summary: {summary_text}")
    print(f"   날짜: {date.today().isoformat()}")

    result = notion.pages.create(
        parent={"database_id": MEETING_NOTES_PAGE_ID},
        properties=properties,
    )

    print("\n[3/3] 완료!")
    print(f"   생성된 페이지 URL: {result.get('url')}")


if __name__ == "__main__":
    main()
