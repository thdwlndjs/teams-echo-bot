"""테스트가 실제 저장소(src/worklog.sqlite3)와 실제 키를 쓰지 않게 한다. 모든 테스트 모듈이 맨 먼저 불러온다.

예전에 한 테스트가 저장소 경로를 바꿔 끼우지 않은 채 LLM 호출 함수를 불러, 실제 저장소의 스키마를
올려 버린 일이 있었다(돌고 있던 서버가 "저장소가 코드보다 새 버전"으로 멈출 뻔했다).
서버 모듈(worklog_bridge 등)은 불러오는 순간 WORKLOG_STORE를 읽으므로, 그보다 먼저 임시 경로를 넣는다.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

TEST_STORE = Path(tempfile.mkdtemp(prefix="pm-bot-tests-")) / "worklog.sqlite3"
REAL_STORE = Path(__file__).resolve().parents[2] / "worklog.sqlite3"

os.environ["WORKLOG_STORE"] = str(TEST_STORE)
os.environ.setdefault("BOT_SERVER_SECRET", "test-secret")
os.environ["WORKLOG_LOOP"] = "0"
# 테스트는 실제 LLM을 부르지 않는다(필요한 테스트가 patch.dict로 가짜 키를 넣는다). 지우기만 하면 서버 모듈이
# 불러올 때 .env를 읽어 다시 채우므로(load_dotenv는 이미 있는 값만 덮어쓰지 않는다) 빈 값으로 넣어 둔다.
for name in ("GROQ_API_KEY", "CEREBRAS_API_KEY"):
    os.environ[name] = ""
os.environ["MEMBER_ONBOARDING"] = "0"  # 처음 등록(역할 고르기)은 그 흐름을 보는 테스트만 켠다
