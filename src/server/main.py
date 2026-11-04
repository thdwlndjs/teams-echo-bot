"""팀봇 서버 — 에코봇(Teams 입력단)이 넘겨준 대화 한 턴을 처리한다.

에코봇은 Teams에서 받은 메시지를 아래 JSON으로 묶어 `POST /api/turn`으로 보내고,
서버는 처리 과정을 NDJSON(한 줄에 JSON 하나) 이벤트로 흘려보낸다.

요청 (TurnRequest)
    {"kind": "message" | "welcome",
     "user": {"aad_id": "...", "name": "..."},
     "team": {"id": "...", "name": "..."} | null,      # 1:1 채팅이면 null
     "text": "2",                                       # 사용자가 친 텍스트
     "value": {"action": "menu", "choice": "2"} | null} # 카드 버튼 data

응답 이벤트 (한 줄씩)
    {"type": "status",  "text": "🔍 확인하는 중..."}   → 에코봇: ctx.stream.update
    {"type": "chunk",   "text": "요약 조각"}           → 에코봇: ctx.stream.emit
    {"type": "message", "text": "최종 답"}             → 스트림이 열려 있으면 emit+close, 아니면 ctx.send
    {"type": "buttons", "title": "...", "emphasis": bool,
     "buttons": [{"title": "...", "data": {...}}]}       → 에코봇: 버튼 카드로 만들어 전송
    {"type": "done"}                                     → 열린 스트림 닫기
    {"type": "ping"}                                     → 아직 작업 중 (화면 표시 없음, KEEPALIVE_SECONDS마다)

한 요청은 TURN_DEADLINE_SECONDS(100초) 안에 끝난다. 넘으면 안내 메시지를 보내고 그만둔다.

Teams SDK에 전혀 의존하지 않는다. 카드 모양도 "제목 + 버튼 목록"이라는 의미만 보내고,
실제 Adaptive Card는 에코봇이 만든다.
"""

import asyncio
from datetime import datetime, timedelta, timezone, tzinfo
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from notion_client import Client
from pydantic import BaseModel

# .env는 아래 모듈들보다 먼저 읽는다. worklog_bridge·notion_read는 불러오는 순간 환경변수(GROQ_API_KEY,
# NOTION_RATE 등)를 읽어 두기 때문에, 순서가 바뀌면 .env 값이 그쪽에 전달되지 않는다.
load_dotenv(os.environ.get("ENV_FILE"))

import confirm_flow  # noqa: E402
import member_flow  # noqa: E402
import meeting_inputs  # noqa: E402
import llm_queue  # noqa: E402
import metrics  # noqa: E402
import notion_map  # noqa: E402
import worklog_bridge  # noqa: E402
from worklog import apply as wapply  # noqa: E402
from worklog import parts as wparts  # noqa: E402
from worklog import people  # noqa: E402
from worklog import stages as wstages  # noqa: E402
from worklog_bridge import usage_of as _usage_of  # noqa: E402

# ── 설정 ────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
BOT_SERVER_SECRET = os.environ["BOT_SERVER_SECRET"]  # 에코봇↔서버 간 검증용 (양쪽 .env에 같은 값)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8000"))
DISPLAY_TZ = os.environ.get("DISPLAY_TZ", "Asia/Seoul")  # 사용자에게 보여주는 시각의 시간대
# Groq 한 번에 보내는 원문 상한(추정 토큰). 무료 등급은 분당 8,000토큰이라, 이보다 크면 413(요청이 너무 큼)으로 거절된다.
GROQ_INPUT_TOKENS = int(os.environ.get("GROQ_INPUT_TOKENS", "4000"))
# 위 토큰 예산을 한국어 글자 수로 바꾼 값(한글 1자 = UTF-8 3바이트, 4바이트 ≈ 1토큰으로 어림)
_GROQ_INPUT_CHARS = GROQ_INPUT_TOKENS * 4 // 3
# 한 요청의 최대 처리 시간과, 그동안 에코봇에 "아직 작업 중" 신호를 보내는 간격.
TURN_DEADLINE_SECONDS = float(os.environ.get("TURN_DEADLINE_SECONDS", "100"))
KEEPALIVE_SECONDS = float(os.environ.get("KEEPALIVE_SECONDS", "15"))
_TURN_TIMEOUT_TEXT = "⏳ Notion·GitHub 응답이 늦어 이번 요청을 마치지 못했어요.\n잠시 후 다시 시도해 줄래요?"

_ISSUE_REF_RE = re.compile(r"#(\d+)")

# create_task로 띄운 작업은 참조를 안 잡아두면 끝나기 전에 GC될 수 있어서 여기 모아둔다.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ── 캐시 (메모리, 서버 프로세스 살아있는 동안만 유지) ──────────
_project_status_field_cache: dict[tuple[str, int], tuple[str, str, str] | None] = {}
_project_item_id_cache: dict[tuple[str, int], str | None] = {}
# team_key -> {"hash": 요약한 원문의 해시, "summary": 요약} — 저장된 팀 정보 원문이 같으면 Groq를 다시 부르지 않는다.
_project_summary_cache: dict[str, dict] = {}

app = FastAPI()


# ── 대화 한 턴 (에코봇 요청 1건) ────────────────────────────
class TurnUser(BaseModel):
    aad_id: str
    name: str | None = None


class TurnTeam(BaseModel):
    id: str
    name: str | None = None


class TurnConversation(BaseModel):
    id: str
    type: str | None = None  # "personal" | "channel" | "groupChat"


class TurnAttachment(BaseModel):
    name: str | None = None
    content_type: str | None = None
    url: str | None = None  # Teams 파일은 content.downloadUrl, 그 밖은 contentUrl


class TurnRequest(BaseModel):
    kind: str = "message"  # "message" | "welcome"
    user: TurnUser
    team: TurnTeam | None = None
    text: str = ""
    value: dict | None = None
    # 원문 기록용 (예전 에코봇은 안 보낸다 — 없으면 기록 키를 사용자 기준으로 만든다)
    conversation: TurnConversation | None = None
    message_id: str | None = None
    sent_at: str | None = None
    attachments: list[TurnAttachment] = []


class Turn:
    """예전 코드의 ctx(ActivityContext) 자리를 대신한다.

    ctx.send → turn.send, ctx.stream.update → turn.status, ctx.stream.emit(조각) → turn.chunk.
    실제로 Teams에 보내는 건 에코봇이고, 여기서는 이벤트를 큐에 넣기만 한다.
    """

    def __init__(self, req: TurnRequest):
        self.req = req
        self.aad_id = req.user.aad_id
        self.user_name = req.user.name
        # 팀 채널이면 팀 단위, 1:1 채팅이면 사람 단위 — 예전 _get_team_key와 같은 규칙.
        self.team_key = f"team:{req.team.id}" if req.team else f"user:{req.user.aad_id}"
        self.team_name = req.team.name if req.team else None
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue()

    def _put(self, event: dict) -> None:
        self.queue.put_nowait(event)

    def status(self, text: str) -> None:
        self._put({"type": "status", "text": text})

    def chunk(self, text: str) -> None:
        self._put({"type": "chunk", "text": text})

    def done(self) -> None:
        self._put({"type": "done"})

    async def send(self, text: str) -> None:
        self._put({"type": "message", "text": text})

    async def send_buttons(self, title: str, buttons: list[dict], emphasis: bool = False) -> None:
        self._put({"type": "buttons", "title": title, "emphasis": emphasis, "buttons": buttons})

    def finish(self) -> None:
        self.queue.put_nowait(None)


@app.post("/api/turn")
async def turn_endpoint(req: TurnRequest, x_bot_secret: str | None = Header(None)):
    if not x_bot_secret or not secrets.compare_digest(x_bot_secret, BOT_SERVER_SECRET):
        raise HTTPException(status_code=401, detail="invalid secret")

    turn = Turn(req)

    async def run() -> None:
        try:
            # 외부 API가 아무리 느려도 한 요청은 이 시간 안에 끝낸다. 넘으면 사용자에게 알리고 그만둔다.
            await asyncio.wait_for(handle_turn(turn), timeout=TURN_DEADLINE_SECONDS)
        except asyncio.TimeoutError:
            await turn.send(_TURN_TIMEOUT_TEXT)
        except Exception as exc:
            traceback.print_exc()
            await turn.send(f"⚠️ 처리 중 오류가 발생했어요: {exc}")
        finally:
            turn.finish()

    # 처리는 별도 작업으로 돌린다. 에코봇 연결이 중간에 끊겨도 설정 저장 같은 일은 끝까지 간다.
    _spawn(run())

    async def events():
        while True:
            try:
                event = await asyncio.wait_for(turn.queue.get(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                # 아직 작업 중이라는 신호. 에코봇은 이걸로 "끊김"과 "느림"을 구분한다(화면엔 안 보임).
                yield '{"type": "ping"}\n'
                continue
            if event is None:
                break
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── 팀/사용자별 설정 저장소 ────────────────────────────────────────
TEAM_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "team_configs.json")
MEMBER_PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "member_profiles.json")


def _load_team_configs() -> dict:
    try:
        with open(TEAM_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_team_configs(data: dict) -> None:
    with open(TEAM_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_member_profiles() -> dict:
    try:
        with open(MEMBER_PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_member_profiles(data: dict) -> None:
    with open(MEMBER_PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_github_id(aad_id: str) -> str | None:
    """GitHub 아이디: worklog의 members(v1.1)가 먼저, 없으면 예전 member_profiles.json."""
    member = people.get(worklog_bridge.WORKLOG_STORE, aad_id)
    if member and member["github_login"]:
        return member["github_login"]
    return _load_member_profiles().get(aad_id, {}).get("github_id")


def _set_github_id(aad_id: str, github_id: str) -> None:
    people.set_github(worklog_bridge.WORKLOG_STORE, aad_id, github_id)
    profiles = _load_member_profiles()
    profiles.setdefault(aad_id, {})["github_id"] = github_id
    _save_member_profiles(profiles)


def _sync_member(turn: "Turn", config: dict) -> None:
    """말을 건 사람을 팀원으로 기록하고, 예전 파일의 값(관리자, GitHub 아이디)을 이 사람 몫만 옮긴다(spec P2-2)."""
    store = worklog_bridge.WORKLOG_STORE
    member = people.touch(store, turn.team_key, turn.aad_id, turn.user_name)
    if config.get("admin_aad_id") == turn.aad_id and not people.is_admin(store, turn.team_key, turn.aad_id):
        people.set_admin(store, turn.team_key, turn.aad_id)
    legacy = _load_member_profiles().get(turn.aad_id, {}).get("github_id")
    if legacy and not member["github_login"]:
        people.set_github(store, turn.aad_id, legacy)


def _get_config(team_key: str) -> dict | None:
    """team_key로 저장된 설정을 반환한다. 없으면 None — 다른 사람/팀 설정으로 조용히 넘어가지 않는다."""
    return _load_team_configs().get(team_key)


def _notion_client_for(config: dict, background: bool = False):
    """Notion 클라이언트는 항상 토큰별 관문을 지나는 것으로 만든다(notion_read.notion_client_for 참고)."""
    return notion_client_for(config["notion_token"], background=background)


# ── 설정 마법사 입력값 파싱 ──────────────────────────────
def _extract_github_repo(text: str) -> str | None:
    text = text.strip()
    m = re.search(r"github\.com/([^/\s]+/[^/\s]+)", text)
    if m:
        return m.group(1).removesuffix(".git").rstrip("/")
    if re.fullmatch(r"[\w.-]+/[\w.-]+", text):
        return text
    return None


def _extract_notion_page_id(text: str) -> str | None:
    stripped = text.strip().replace("-", "")
    m = re.search(r"([0-9a-fA-F]{32})", stripped)
    return m.group(1) if m else None


def _extract_project_number(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.isdigit() else None


def _extract_token(text: str) -> str | None:
    text = text.strip()
    # 공백 없이 + ASCII만. non-ASCII가 섞이면 httpx가 헤더에 넣다가 UnicodeEncodeError로 죽는다.
    if not text or re.search(r"\s", text) or not text.isascii():
        return None
    return text


_GITHUB_USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")


def _extract_github_id(text: str) -> str | None:
    text = text.strip()
    return text if _GITHUB_USERNAME_RE.fullmatch(text) else None


async def _github_token_valid(token: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            )
        return resp.status_code == 200
    except Exception:
        return False


def _notion_page_accessible(page_id: str, notion_client: Client) -> bool:
    """페이지 'Connections'에 이 integration이 추가돼 있어야만 API로 읽을 수 있다."""
    try:
        notion_client.pages.retrieve(page_id)
        return True
    except Exception:
        pass
    try:
        notion_client.databases.retrieve(page_id)
        return True
    except Exception:
        return False


# ── 메뉴 / 문구 ────────────────────────────────────────
WELCOME_TEXT = "안녕하세요! 아래에서 골라주세요\n\n⚙️ 연결 설정을 바꾸고 싶으면 '설정'이라고 입력하세요."

_MENU_ITEMS = [
    ("1", "1️⃣ 팀/프로젝트 정보"),
    ("2", "2️⃣ 진행상황 조회"),
    ("3", "3️⃣ 내 역할 조회"),
    ("4", "4️⃣ 작업 공유·회의록 반영"),
]
_MENU_BUTTONS = [{"title": label, "data": {"action": "menu", "choice": choice}} for choice, label in _MENU_ITEMS]

# 예전엔 on_message_pattern("hello|hi|greetings")가 부분 일치라 "github" 같은 입력에도 걸릴 수 있었다.
# 정확히 그 단어만 보냈을 때만 인사한다.
_GREETING_RE = re.compile(r"(hello|hi|greetings)", re.IGNORECASE)


# ── 대화 상태 (서버 메모리) ────────────────────────────────
# key: aad_object_id, value: {"team_key": str, "step": int, "data": dict}
setup_sessions: dict[str, dict] = {}
# key: aad_object_id, value: {"kind": "menu", "raw_text": str} 또는 {"kind": "claim", "issue_number": int}
github_onboarding_sessions: dict[str, dict] = {}


@dataclass
class SetupStep:
    key: str
    prompt: str
    parser: Callable[[str], object | None]
    error_message: str


SETUP_STEPS: list[SetupStep] = [
    SetupStep("github_repo", "**1** 레포\n`thdwlndjs/n8nTest`", _extract_github_repo,
              "레포 형식이 아니에요.\n`owner/repo`로 다시 보내줄래요?"),
    SetupStep("github_token",
              "**2** GitHub 토큰\n(repo, project 권한 있는 Personal Access Token)\n\n모르면 '가이드'라고 보내주세요",
              _extract_token, "토큰 형식이 아니에요. 공백 없이 다시 보내줄래요?"),
    SetupStep("admin_github_id", "**3** 본인 GitHub 아이디\n`thdwlndjs`처럼 아이디만 보내주세요",
              _extract_github_id, "GitHub 아이디 형식이 아니에요. (예: thdwlndjs)"),
    SetupStep("notion_token", "**4** Notion 토큰\n(Integration Token)\n\n모르면 '가이드'라고 보내주세요",
              _extract_token, "토큰 형식이 아니에요. 공백 없이 다시 보내줄래요?"),
    SetupStep("notion_page_id", "**5** 팀 정보 노션 페이지 링크", _extract_notion_page_id,
              "페이지를 못 찾았어요.\n링크를 다시 보내줄래요?"),
    # 회의록 페이지는 따로 묻지 않는다: 팀 정보 페이지 안에서 부분 종류 판정으로 찾는다(docs/v1.1/spec.md P1-7).
    # 이미 meeting_notes_page_id가 저장된 팀은 그 페이지를 계속 함께 읽는다(_team_roots).
    SetupStep("github_project_number", "**6** GitHub 프로젝트 번호\nProjects 탭 URL 끝 숫자예요",
              _extract_project_number, "숫자만 보내줄래요?"),
]

_HELP_TRIGGERS = {"모름", "몰라", "모르겠어", "가이드", "도움말", "help"}
_YES_WORDS = {"네", "예", "응", "어", "맞아", "맞아요", "yes", "y"}
_NO_WORDS = {"아니", "아니요", "아니오", "no", "n"}
_CANCEL_TRIGGERS = {"나가기", "취소", "x", "9"}

_ASK_GITHUB_ID_TEXT = (
    "먼저 GitHub 아이디를 알려줄래요?\n`thdwlndjs`처럼 아이디만 보내주세요.\n\n"
    "(그만두려면 '취소' 또는 '나가기')"
)
_NO_CONFIG_TEXT = "아직 연결된 설정이 없어요.\n'설정'이라고 입력해서 먼저 연결해줄래요?"
_SETUP_GUIDES: dict[str, str] = {
    "github_token": (
        "**GitHub 토큰 발급 가이드**\n\n"
        "1. https://github.com/settings/tokens 접속\n"
        "(GitHub 우측 상단 프로필 → Settings → 왼쪽 메뉴 맨 아래 Developer settings → Personal access tokens)\n\n"
        "2. **Generate new token (classic)** 선택 — Fine-grained 말고 꼭 Classic\n\n"
        "3. Note에 이름 아무거나(예: teamsAgent), 만료일 설정\n\n"
        "4. Select scopes에서 최상위 체크박스 딱 2개만 체크: **`repo`**, **`project`**\n\n"
        "5. 맨 아래 Generate token → 나오는 `ghp_...` 토큰을 복사해서 여기에 보내주세요\n\n"
        "토큰 다시 보내줄래요?"
    ),
    "notion_token": (
        "**Notion 토큰 발급 가이드**\n\n"
        "1. https://app.notion.com/developers/connections 접속\n\n"
        "2. **신규 연결** 선택\n\n"
        "3. 이름 아무거나 입력하고, 아래 항목 체크\n"
        "- 콘텐츠 기능: 읽기, 업데이트, 삽입 (셋 다 체크)\n"
        "- 이메일 주소를 포함한 사용자 정보 읽기\n\n"
        "4. 만들어진 **액세스 토큰** 복사\n\n"
        "5. 연결하려는 Notion 페이지로 가서 우측 상단 `···` → **Connections** → 방금 만든 integration 추가\n"
        "(이 연결을 안 해두면 페이지 검증 단계에서 계속 실패해요)\n\n"
        "토큰 다시 보내줄래요?"
    ),
}
_SECRET_KEYS = {"github_token", "notion_token"}
_SETUP_LABELS = {
    "github_repo": "레포",
    "github_token": "GitHub 토큰",
    "admin_github_id": "내 GitHub 아이디",
    "notion_token": "Notion 토큰",
    "notion_page_id": "팀 정보",
    "meeting_notes_page_id": "회의록",
    "github_project_number": "프로젝트 번호",
}


# ── 턴 라우팅 (예전 handle_message의 판단 부분) ─────────────────
async def handle_turn(turn: Turn) -> None:
    if turn.req.kind == "welcome":
        await _send_welcome(turn)
        return

    config = _get_config(turn.team_key)
    if config is not None:
        await asyncio.to_thread(_sync_member, turn, config)
        _check_on_open(turn.team_key, config)

    # 카드 버튼(Action.Submit / Action.Execute) — 에코봇이 data를 value에 그대로 실어 보낸다.
    value = turn.req.value
    if isinstance(value, dict):
        action = value.get("action")
        if action == "set_role" and value.get("role") in people.ROLES:
            await member_flow.handle_role(turn, str(value["role"]), lambda resume: _resume(turn, resume))
            return
        if action == "onboard_skip":
            await member_flow.handle_skip(turn, lambda resume: _resume(turn, resume))
            return
        if action in ("perm_toggle", "perm_admin"):
            await member_flow.toggle_permission(turn, str(value.get("user") or ""), action == "perm_admin")
            return
        if await _onboarding_gate(turn, config, {"value": value}):
            return
        if await _dispatch_value(turn, value):
            return

    raw_text = turn.req.text.strip()
    aad_id = turn.aad_id
    await _handle_text(turn, config, raw_text, aad_id)


async def _onboarding_gate(turn: "Turn", config: dict | None, resume: dict) -> bool:
    """처음 쓰는 팀원은 역할부터 고른다(필수, spec P2-2). 등록을 시작했으면 True."""
    if config is None or not member_flow.enabled() or turn.aad_id in setup_sessions:
        return False
    if member_flow.has_session(turn.aad_id) or not member_flow.needs_onboarding(turn.aad_id):
        return False
    await member_flow.start(turn, resume)
    return True


async def _resume(turn: "Turn", resume: dict) -> None:
    """처음 등록 뒤 처음 하려던 동작을 이어서 한다."""
    if "value" in resume:
        await _dispatch_value(turn, resume["value"])
    elif resume.get("menu"):
        await _run_action(turn, resume["menu"])


async def _dispatch_value(turn: "Turn", value: dict) -> bool:
    """카드 버튼 하나를 처리한다. 처리했으면 True."""
    action = value.get("action")
    if action == "menu" and "choice" in value:
        await _run_action(turn, str(value["choice"]))
    elif action == "claim_issue" and "issue_number" in value:
        await _handle_claim_click(turn, int(value["issue_number"]))
    elif action == "dismiss_claim_card":
        await turn.send("알겠어요, 넘어갈게요.")
    elif action == "take_task" and value.get("item_id"):
        await _handle_take_task(turn, str(value["item_id"]))
    elif action in _CONFIRM_ACTIONS:
        await _handle_confirm_action(turn, action, value)
    else:
        return False
    return True


async def _handle_text(turn: "Turn", config: dict | None, raw_text: str, aad_id: str) -> None:
    if raw_text.lower() not in _CANCEL_TRIGGERS and member_flow.has_session(aad_id):
        await member_flow.handle_text(turn, raw_text, lambda resume: _resume(turn, resume))  # 처음 등록·별칭
        return

    # 채팅 메시지는 저장하지 않는다. 예전엔 메뉴가 아닌 메시지를 몰래 저장해 변경 후보를 만들었다(v1.1에서 없앰,
    # docs/v1.1/spec.md P1-0). 회의록은 Notion이나 '회의록 올리기'로만 받는다.
    if raw_text.lower() not in _CANCEL_TRIGGERS and confirm_flow.has_edit_session(aad_id):
        await confirm_flow.handle_edit_text(turn, raw_text)  # [고치기] 중인 채팅
        return

    if raw_text.lower() in _CANCEL_TRIGGERS and _cancel_pending_sessions(aad_id):
        await turn.send("취소했어요. 처음부터 다시 하려면 원하는 메뉴나 '설정'을 입력해주세요.")
        return

    if aad_id in source_add_sessions and config is not None:  # 4번 ⑤ 소스 추가 다음 메시지
        await _handle_source_add(turn, config, raw_text)
        return

    if confirm_flow.has_upload_session(aad_id):  # 4번 ④ 회의록 올리기 다음 메시지
        await confirm_flow.handle_upload(turn, turn.req.text, [a.model_dump() for a in turn.req.attachments])
        return

    if meeting_inputs.has_clova_link(raw_text):
        await turn.send(meeting_inputs.CLOVA_LINK_REPLY)
        return

    if aad_id in setup_sessions:
        await _handle_setup_message(turn, raw_text)
        return

    if aad_id in github_onboarding_sessions:
        await _handle_github_onboarding_message(turn, raw_text)
        return

    if raw_text in ("설정", "0"):
        await _start_setup(turn)
        return

    if raw_text == "별칭" and config is not None:
        await member_flow.start_aliases(turn)
        return

    if raw_text == "권한" and config is not None:
        await member_flow.show_team_card(turn)
        return

    if re.fullmatch(r"[1-4]", raw_text) and await _onboarding_gate(turn, config, {"menu": raw_text}):
        return

    if _GREETING_RE.fullmatch(raw_text):
        await turn.send("Hello! How can I assist you today?")
        return

    if not re.fullmatch(r"[1-4]", raw_text):
        await turn.send("올바른 값을 입력하세요. (1~4, 연결 설정 변경은 '설정')")
        return

    await _run_action(turn, raw_text)


def _cancel_pending_sessions(aad_id: str) -> bool:
    cancelled = False
    if aad_id in setup_sessions:
        del setup_sessions[aad_id]
        cancelled = True
    if aad_id in github_onboarding_sessions:
        del github_onboarding_sessions[aad_id]
        cancelled = True
    if confirm_flow.edit_sessions.pop(aad_id, None) is not None:
        cancelled = True
    if confirm_flow.upload_sessions.pop(aad_id, None) is not None:
        cancelled = True
    if member_flow.sessions.pop(aad_id, None) is not None:
        cancelled = True
    if source_add_sessions.pop(aad_id, None) is not None:
        cancelled = True
    return cancelled


# ── 초기 설정 마법사 ────────────────────────────────────────
async def _start_setup(turn: Turn) -> None:
    existing = _get_config(turn.team_key)
    if existing is not None and existing.get("admin_aad_id") != turn.aad_id:
        await turn.send("이미 설정돼 있어요.\n이 설정은 처음 등록한 관리자만 바꿀 수 있어요.")
        return

    setup_sessions[turn.aad_id] = {"team_key": turn.team_key, "step": 0, "data": {}}
    header = f"**{turn.team_name}** 설정 시작할게요" if turn.team_name else "설정 시작할게요"
    await turn.send(f"{header}\n\n{SETUP_STEPS[0].prompt}\n\n(중간에 그만두려면 '취소' 또는 '나가기')")


async def _handle_setup_message(turn: Turn, raw_text: str) -> None:
    aad_id = turn.aad_id
    session = setup_sessions[aad_id]
    step_index = session["step"]
    step = SETUP_STEPS[step_index]
    key = step.key

    if raw_text.strip().lower() in _HELP_TRIGGERS and key in _SETUP_GUIDES:
        await turn.send(_SETUP_GUIDES[key])
        return

    value = step.parser(raw_text)
    if value is None:
        await turn.send(f"⚠️ {step.error_message}")
        return

    if key == "github_token" and not await _github_token_valid(value):
        await turn.send("⚠️ 이 토큰으로 GitHub 인증이 안 돼요.\n토큰이 맞는지 다시 확인해서 보내줄래요?")
        return

    if key == "notion_token" and value == session["data"].get("github_token"):
        await turn.send(
            "⚠️ 방금 보낸 값이 GitHub 토큰이랑 똑같아요.\n"
            "Notion 토큰 대신 GitHub 토큰을 잘못 다시 보낸 것 같은데, Notion 토큰을 다시 확인해서 보내줄래요?"
        )
        return

    if key in ("notion_page_id", "meeting_notes_page_id"):
        notion_client = notion_client_for(session["data"]["notion_token"])
        accessible = await asyncio.to_thread(_notion_page_accessible, value, notion_client)
        if not accessible:
            await turn.send(
                "⚠️ 이 페이지는 방금 넣은 Notion 토큰으로 불러올 수 없어요.\n"
                "게스트 권한만 있거나, 이 토큰이 아직 그 페이지의 연결(Connections)에 "
                "추가 안 됐을 수 있어요.\n"
                "페이지 주인에게 연결 추가를 부탁하거나, 다른 페이지 링크를 보내줄래요?"
            )
            return
        if key == "notion_page_id":
            # 팀 정보 페이지를 등록한 순간 구조·원문을 읽어 저장해 둔다 → 1번은 이걸로 바로 답한다.
            _spawn(_register_team_map(session["team_key"], [value], session["data"]["notion_token"]))
        else:
            # 회의록 페이지도 등록한 순간 읽어 둔다(팀 정보 안에 있으면 다시 읽지 않는다) → 2·3번이 바로 답한다.
            _spawn(_register_team_map(session["team_key"], [session["data"]["notion_page_id"], value],
                                      session["data"]["notion_token"]))

    session["data"][key] = value
    step_index += 1

    if step_index >= len(SETUP_STEPS):
        summary = "\n".join(
            f"- {_SETUP_LABELS[k]}: {'등록됨' if k in _SECRET_KEYS else v}"
            for k, v in session["data"].items()
            if k in _SETUP_LABELS
        )
        admin_github_id = session["data"].pop("admin_github_id")
        _set_github_id(aad_id, admin_github_id)

        session["data"]["admin_aad_id"] = aad_id
        configs = _load_team_configs()
        configs[session["team_key"]] = session["data"]
        _save_team_configs(configs)
        people.touch(worklog_bridge.WORKLOG_STORE, session["team_key"], aad_id, turn.user_name)
        people.set_admin(worklog_bridge.WORKLOG_STORE, session["team_key"], aad_id)
        del setup_sessions[aad_id]
        await turn.send(f"**✅ 설정 완료**\n\n{summary}\n\n이제 1~4번 이 설정으로 동작해요.")
        return

    session["step"] = step_index
    await turn.send(f"✅ {_SETUP_LABELS[key]} 완료!\n\n{SETUP_STEPS[step_index].prompt}")


# ── 메뉴 실행 ────────────────────────────────────────
async def _run_action(turn: Turn, raw_text: str) -> None:
    # 메뉴 응답 한 번 = metric 한 줄(Notion·Groq 호출 수, 걸린 시간). 문서: docs/v1.1/spec.md "계측"
    with metrics.measure("menu", turn.team_key, raw_text):
        await _run_menu(turn, raw_text)


async def _run_menu(turn: Turn, raw_text: str) -> None:
    config = _get_config(turn.team_key)
    if config is None:
        await turn.send(_NO_CONFIG_TEXT)
        return

    github_id = _get_github_id(turn.aad_id)
    if raw_text == "4":  # 작업 공유·반영 하위 메뉴(① 완료 공유 ② Notion 변경 반영 ③ 최근 반영 내역)
        await confirm_flow.show_submenu(turn)
        return
    if raw_text in ("3", "4-1") and github_id is None and _ask_github_now(turn.aad_id, raw_text):
        github_onboarding_sessions[turn.aad_id] = {"kind": "menu", "raw_text": raw_text}
        await turn.send(_ASK_GITHUB_ID_TEXT)
        return

    try:
        if raw_text == "1":
            # 요약 글자가 chunk 이벤트로 흘러가고, done으로 스트림을 닫는다.
            await fetch_project_info(config, turn.team_key, on_stage=turn.status, on_chunk=turn.chunk)
            turn.done()
            return
        elif raw_text == "2":
            turn.status("🔍 진행상황을 확인하는 중...")
            reply_text, unassigned_issues = await fetch_team_status(config, turn.team_key, on_stage=turn.status)
            await turn.send(reply_text)
            if unassigned_issues:
                await turn.send_buttons(*_claim_buttons(unassigned_issues), emphasis=True)
            return
        elif raw_text == "3":
            turn.status("🔍 내 역할을 확인하는 중...")
            member = people.get(worklog_bridge.WORKLOG_STORE, turn.aad_id) or {"aliases": [], "role": None}
            reply_text = await fetch_my_role(github_id, turn.user_name, config, turn.team_key, on_stage=turn.status,
                                             aliases=member["aliases"])
            await turn.send(reply_text)
            card = await asyncio.to_thread(_take_card, turn.team_key, member.get("role"))
            if card:
                await turn.send_buttons(*card)
            return
        elif raw_text == "4-1":
            turn.status("🔍 완료된 작업을 확인하는 중...")
            reply_text = await share_completed_work(config, github_id)
        else:
            reply_text = "올바른 값을 입력하세요. (1~4, 연결 설정 변경은 '설정')"
    except Exception as exc:
        traceback.print_exc()
        reply_text = f"⚠️ 처리 중 오류가 발생했어요: {exc}"

    # status를 띄운 뒤의 message는 에코봇이 같은 스트림에 emit+close해서 "확인하는 중..."을 교체한다.
    await turn.send(reply_text)


def _ask_github_now(aad_id: str, raw_text: str) -> bool:
    """GitHub 아이디를 물을지. 4번 ①(완료 공유)은 꼭 필요해서 매번, 3번은 처음 한 번만 다시 묻는다(spec P2-2).
    한 번 물은 뒤의 3번은 GitHub 없이(회의록에서 맡은 일만) 보여 준다."""
    if raw_text == "4-1":
        return True
    store = worklog_bridge.WORKLOG_STORE
    member = people.get(store, aad_id)
    if member and "github_menu3" in member["asked"]:
        return False
    people.mark_asked(store, aad_id, "github_menu3")
    return True


def _take_card(team_key: str, role: str | None) -> tuple[str, list[dict]] | None:
    """3번 아래: 담당이 없는 할 일 중 내 역할에 맞는 것(추출할 때 붙인 role_hint) + [내가 맡기] (spec P2-7)."""
    if not role:
        return None
    tasks = wapply.live_items(worklog_bridge.WORKLOG_STORE, team_key).get("task", [])
    fits = [t for t in tasks if not t.get("assignee") and t["attributes"].get("role_hint") == role][:8]
    if not fits:
        return None
    lines = [f"🧩 담당이 없는 {people.ROLES.get(role, role)} 할 일 — 맡을 것을 눌러 주세요", *[f"- {t['title']}" for t in fits]]
    title = "\n".join(lines)
    return title, [{"title": f"🙋 맡기: {t['title']}"[:40], "data": {"action": "take_task", "item_id": t["id"]}} for t in fits]


async def _handle_take_task(turn: Turn, item_id: str) -> None:
    """[내가 맡기]: 담당을 누른 사람으로 바꾸고 이력을 남긴다. 연결된 GitHub 이슈가 있으면 배정도 바꾼다."""
    config = _get_config(turn.team_key)
    if config is None:
        await turn.send(_NO_CONFIG_TEXT)
        return
    store = worklog_bridge.WORKLOG_STORE
    member = people.get(store, turn.aad_id) or {"aliases": []}
    name = (member["aliases"] or [turn.user_name or turn.aad_id])[0]
    try:
        taken = await asyncio.to_thread(wapply.take_task, store, turn.team_key, item_id, name, turn.user_name or turn.aad_id)
    except Exception as error:
        await turn.send(f"맡지 못했어요: {error}")
        return
    lines = [f"🙋 '{taken['title']}'을(를) 맡았어요 (담당: {name})"]
    github_id = _get_github_id(turn.aad_id)
    for number in taken["issue_refs"][:1]:
        if github_id:
            try:
                lines.append(await claim_issue(config, number, github_id))
            except Exception as error:
                lines.append(f"⚠️ GitHub #{number} 배정은 못 했어요: {error}")
        else:
            lines.append(f"GitHub #{number}에도 배정하려면 4번 ① 완료 공유나 담당 없는 이슈 맡기에서 GitHub 아이디를 알려 주세요.")
    await turn.send("\n".join(lines))


def _claim_buttons(issues: list[dict]) -> tuple[str, list[dict]]:
    title = "🙋 담당자 없는 이슈 — 맡고 싶은 걸 눌러주세요"
    buttons = [
        {"title": f"#{i['number']} {i['title']}"[:40], "data": {"action": "claim_issue", "issue_number": i["number"]}}
        for i in issues
    ]
    buttons.append({"title": "🚪 나가기", "data": {"action": "dismiss_claim_card"}})
    return title, buttons


async def _handle_claim_click(turn: Turn, issue_number: int) -> None:
    if _get_config(turn.team_key) is None:
        await turn.send(_NO_CONFIG_TEXT)
        return

    github_id = _get_github_id(turn.aad_id)
    if github_id is None:
        github_onboarding_sessions[turn.aad_id] = {"kind": "claim", "issue_number": issue_number}
        await turn.send(_ASK_GITHUB_ID_TEXT)
        return

    await _claim_and_reply(turn, issue_number, github_id)


async def _claim_and_reply(turn: Turn, issue_number: int, github_id: str) -> None:
    config = _get_config(turn.team_key)
    if config is None:
        await turn.send(_NO_CONFIG_TEXT)
        return
    try:
        reply_text = await claim_issue(config, issue_number, github_id)
    except Exception as exc:
        traceback.print_exc()
        reply_text = f"⚠️ 처리 중 오류가 발생했어요: {exc}"
    await turn.send(reply_text)


async def _handle_github_onboarding_message(turn: Turn, raw_text: str) -> None:
    aad_id = turn.aad_id
    session = github_onboarding_sessions[aad_id]

    if "pending_confirm" in session:
        answer = raw_text.strip().lower()
        if answer in _YES_WORDS:
            github_id = session.pop("pending_confirm")
        elif answer in _NO_WORDS:
            del session["pending_confirm"]
            await turn.send("그럼 정확한 GitHub 아이디를 다시 보내줄래요?")
            return
        else:
            await turn.send("⚠️ '네' 또는 '아니요'로 답해줄래요?")
            return
    else:
        github_id = _extract_github_id(raw_text)
        if github_id is None:
            await turn.send("⚠️ GitHub 아이디 형식이 아니에요. (예: thdwlndjs)")
            return
        if len(github_id) == 1 and github_id.isdigit():
            session["pending_confirm"] = github_id
            await turn.send(f"GitHub 아이디가 흔치 않은 형태인데, 정말 `{github_id}`가 맞으세요? (네/아니요)")
            return

    del github_onboarding_sessions[aad_id]
    _set_github_id(aad_id, github_id)
    await turn.send(f"연결 완료! GitHub 아이디: `{github_id}`")

    if session["kind"] == "claim":
        await _claim_and_reply(turn, session["issue_number"], github_id)
    else:
        await _run_action(turn, session["raw_text"])


# ── GitHub API 헬퍼 ────────────────────────────────────
_github_http_client: httpx.AsyncClient | None = None


def _get_github_http_client() -> httpx.AsyncClient:
    global _github_http_client
    if _github_http_client is None:
        _github_http_client = httpx.AsyncClient(timeout=15)
    return _github_http_client


@app.on_event("shutdown")
async def _close_github_http_client() -> None:
    global _github_http_client
    if _github_http_client is not None:
        await _github_http_client.aclose()
        _github_http_client = None


async def _github_get(repo: str, path: str, token: str, params: dict | None = None) -> dict | list[dict]:
    url = f"https://api.github.com/repos/{repo}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    resp = await _get_github_http_client().get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


# (token 지문, repo, path, params) -> {"data", "expires_at"}
# 같은 레포를 권한이 다른 토큰 두 개가 쓸 때 결과가 섞이지 않게 토큰 지문도 키에 넣는다.
_github_get_cache: dict[tuple, dict] = {}
_GITHUB_GET_CACHE_TTL_SECONDS = 30


async def _github_get_cached(
    repo: str, path: str, token: str, params: dict | None = None, ttl: float = _GITHUB_GET_CACHE_TTL_SECONDS
) -> dict | list[dict]:
    token_fp = hashlib.sha256(token.encode()).hexdigest()[:16]
    key = (token_fp, repo, path, tuple(sorted((params or {}).items())))
    now = time.monotonic()
    cached = _github_get_cache.get(key)
    if cached and cached["expires_at"] > now:
        return cached["data"]
    data = await _github_get(repo, path, token, params)
    _github_get_cache[key] = {"data": data, "expires_at": now + ttl}
    return data


async def _github_patch(repo: str, path: str, token: str, json_body: dict) -> dict:
    url = f"https://api.github.com/repos/{repo}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    resp = await _get_github_http_client().patch(url, headers=headers, json=json_body)
    resp.raise_for_status()
    return resp.json()


async def _github_graphql(query: str, token: str, variables: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = await _get_github_http_client().post(
        "https://api.github.com/graphql",
        headers=headers,
        json={"query": query, "variables": variables or {}},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {data['errors']}")
    return data["data"]


# ── 노션 페이지 읽기 (notion_read.py) ───────────────────────────────
from notion_read import (  # noqa: E402
    notion_client_for, _block_text, _fetch_notion_page_by_id, _inline_database_lines, _list_child_blocks, _map_parallel,
    _query_database_rows, _rich_text,
)


# ── 1. 팀/프로젝트 정보 (Groq 스트리밍 요약) ─────────────────────
def _fit_for_groq(text: str, keep: str = "head") -> str:
    """Groq 입력 예산(GROQ_INPUT_TOKENS)에 맞게 자른다. keep="tail"이면 뒤쪽(최근 것)을 남긴다."""
    budget = GROQ_INPUT_TOKENS * 4  # 추정: 4바이트 ≈ 1토큰
    data = text.encode("utf-8")
    if len(data) <= budget:
        return text
    if keep == "tail":
        return "(앞부분 생략)\n" + data[-budget:].decode("utf-8", errors="ignore")
    return data[:budget].decode("utf-8", errors="ignore") + "\n(이하 생략)"


async def _summarize_project_info(raw_text: str, on_chunk: Callable[[str], None] | None = None) -> str | None:
    """요약을 돌려준다. LLM 키가 없거나 모두 실패하면 None — 호출부가 원문 앞부분을 보여주고 캐시하지 않는다.
    Groq가 한도(429)에 걸렸거나 쉬는 중이면 같은 모델의 Cerebras로 넘긴다(docs/v1.1/spec.md P1-8)."""
    candidates = [(p, p.key) for p in llm_queue.available(llm_queue.USER)]
    if not candidates and GROQ_API_KEY:
        candidates = [(llm_queue.PROVIDERS[0], GROQ_API_KEY)]
    if not candidates or not raw_text.strip():
        return None
    raw_text = _fit_for_groq(raw_text)

    system_prompt = (
        "너는 팀 소개/프로젝트 정보 페이지 원문을 읽고 핵심만 간결하게 요약하는 도우미야.\n"
        "팀 이름, 팀원과 역할, 프로젝트명과 목적, 현재 진행 단계 위주로 정리해.\n"
        "'[프로젝트명 입력]'처럼 아직 안 채워진 항목은 '미정'으로 짧게만 표시하거나 생략해.\n"
        "팀 규칙/협업 방식 같은 formality는 한두 줄로만 축약하고, 전체 5~10줄 이내로 정리해."
    )
    ready = [c for c in candidates if not llm_queue.cooling_down(c[0])] or candidates
    for provider, key in ready:
        full_text = ""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                async with client.stream(
                    "POST",
                    provider.url,
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": GROQ_MODEL if provider.name == "groq" else provider.model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": raw_text},
                        ],
                        "temperature": 0.2,
                        "stream": True,
                    },
                ) as resp:
                    usage: dict | None = None
                    try:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            payload = line[len("data: "):].strip()
                            if payload == "[DONE]":
                                break
                            event = json.loads(payload)
                            # Groq는 마지막 조각의 x_groq.usage에, Cerebras는 usage에 토큰 수를 싣는다.
                            usage = (event.get("x_groq") or {}).get("usage") or event.get("usage") or usage
                            choices = event.get("choices") or [{}]
                            delta = (choices[0].get("delta") or {}).get("content", "")
                            if delta:
                                full_text += delta
                                if on_chunk:
                                    on_chunk(delta)
                    finally:
                        metrics.count_groq(usage)
                        # 사람이 기다리는 호출(순위 1)은 막지 않고, 쓴 양만 대기열에 알린다(백그라운드가 몫을 맞춘다).
                        llm_queue.record(worklog_bridge.WORKLOG_STORE, provider.name, llm_queue.USER, resp.headers,
                                         resp.status_code, int((usage or {}).get("total_tokens") or 0))
            return full_text.strip() or None
        except Exception:
            traceback.print_exc()
            if full_text:
                return full_text.strip()  # 이미 보낸 글이 있으면 다른 공급자로 처음부터 다시 보내지 않는다
    return None


def _team_roots(config: dict) -> list[str]:
    """지도에 담을 루트: 팀 정보 페이지(기준)와 회의록 페이지. 회의록이 팀 정보 안에 있으면 따로 읽지 않는다."""
    extra = [r for r in config.get("extra_source_ids") or [] if r]  # 4번 ⑤ 소스 추가로 등록한 페이지·DB
    return [r for r in (config.get("notion_page_id"), config.get("meeting_notes_page_id"), *extra) if r]


_team_sync_locks: dict[str, threading.Lock] = {}
_team_sync_locks_guard = threading.Lock()


def _sync_team_map(team_key: str, roots: list[str], token: str, background: bool) -> dict:
    """팀 정보·회의록 페이지의 구조·원문을 worklog 저장소에 맞춘다(바뀐 곳만 읽는다).
    같은 팀을 동시에 두 번 맞추지 않는다(등록 직후·첫 메뉴·백그라운드 수집이 겹칠 때). 뒤에 온 쪽은 앞의 결과를
    이어받아 바뀐 곳만 본다."""
    with _team_sync_locks_guard:
        lock = _team_sync_locks.setdefault(team_key, threading.Lock())
    with lock:
        return notion_map.sync_roots(worklog_bridge.WORKLOG_STORE, team_key, roots,
                                     notion_client_for(token, background=background),
                                     classify=worklog_bridge.groq_classify_sections,
                                     classify_parts=worklog_bridge.groq_classify_parts)


def _format_as_of(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_display_tz())
        return when.strftime("%m/%d %H:%M")
    except Exception:
        return iso


def _display_tz() -> tzinfo:
    """표시용 시간대. Windows처럼 시간대 데이터(tzdata)가 없는 곳에서도 한국 시간은 나오게 한다."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(DISPLAY_TZ)
    except Exception:
        return timezone(timedelta(hours=9), "KST") if DISPLAY_TZ == "Asia/Seoul" else timezone.utc


async def _summarize_snapshot(team_key: str, snapshot: dict, on_chunk: Callable[[str], None] | None = None) -> str:
    """저장된 팀 정보 원문을 요약한다. 원문이 지난번과 같으면(해시 동일) Groq를 다시 부르지 않는다."""
    content_hash = hashlib.sha256(snapshot["text"].encode("utf-8")).hexdigest()
    cached = _project_summary_cache.get(team_key)
    if cached and cached["hash"] == content_hash:
        if on_chunk:
            on_chunk(cached["summary"])
        return cached["summary"]
    streamed = False

    def _track(delta: str) -> None:
        nonlocal streamed
        streamed = True
        if on_chunk:
            on_chunk(delta)

    text = await _summarize_project_info(snapshot["text"], on_chunk=_track)
    if text is None:
        # 요약 실패(키 없음·Groq 오류): 원문 앞부분만 보여주고, 캐시하지 않아 다음에 다시 요약을 시도한다.
        preview = snapshot["text"][:1500] + ("\n…" if len(snapshot["text"]) > 1500 else "")
        fallback = f"⚠️ 요약을 만들지 못해 저장된 원문 앞부분을 보여줘요.\n\n{preview}"
        if on_chunk and not streamed:
            on_chunk(fallback)
        return fallback
    _project_summary_cache[team_key] = {"hash": content_hash, "summary": text}
    return text


async def _register_team_map(team_key: str, roots: list[str], token: str) -> None:
    """설정에서 팀 정보·회의록 페이지를 등록한 순간, 구조와 원문을 한 번 읽어 저장하고 요약까지 만들어 둔다.
    설정 마법사는 기다리지 않는다(백그라운드). 실패해도 메뉴를 처음 누를 때 다시 시도한다."""
    try:
        await asyncio.to_thread(_sync_team_map, team_key, roots, token, False)
        snapshot = await asyncio.to_thread(worklog_bridge.team_snapshot, team_key, roots[0], _GROQ_INPUT_CHARS)
        if snapshot:
            await _summarize_snapshot(team_key, snapshot)
    except Exception:
        traceback.print_exc()


async def _first_read(config: dict, team_key: str, on_stage: Callable[[str], None] | None) -> None:
    """등록 때 읽기가 아직 안 끝났거나 실패했을 때만: 지금 한 번 읽는다(사용자 요청 우선순위)."""
    if on_stage:
        on_stage("🔍 Notion 페이지를 처음 읽는 중이에요 (한 번만 걸려요)...")
    await asyncio.to_thread(_sync_team_map, team_key, _team_roots(config), config["notion_token"], False)


async def _stored_notes(config: dict, team_key: str, on_stage: Callable[[str], None] | None = None) -> dict:
    """2·3번용 회의 기록. Notion을 다시 읽지 않고 저장본을 쓴다(처음 한 번만 읽음)."""
    notes_id = config.get("meeting_notes_page_id")
    notes = await asyncio.to_thread(worklog_bridge.meeting_notes, team_key, notes_id, _GROQ_INPUT_CHARS)
    if notes is None:
        await _first_read(config, team_key, on_stage)
        notes = await asyncio.to_thread(worklog_bridge.meeting_notes, team_key, notes_id, _GROQ_INPUT_CHARS)
    return notes or {"texts": [], "as_of": None, "omitted": 0}


def _note_footer(notes: dict) -> list[str]:
    as_of = _format_as_of(notes.get("as_of"))
    return ["", f"(Notion 기준 {as_of})"] if as_of else []


def _brief(text: str, limit: int = 120) -> str:
    """저장된 원문 한 건을 한 줄로: 머리줄([역할] 경로)을 빼고 내용 줄들을 이어 붙인다."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    body = " · ".join(lines[1:] or lines)
    return body if len(body) <= limit else body[: limit - 1] + "…"


async def fetch_project_info(
    config: dict,
    team_key: str,
    on_stage: Callable[[str], None] | None = None,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    """팀/프로젝트 정보 — 저장해 둔 구조·원문으로 답한다(Notion을 다시 읽지 않음).
    저장분은 등록할 때 처음 만들어지고, 이후 백그라운드 수집이 바뀐 곳만 갱신한다."""
    root = config["notion_page_id"]
    snapshot = await asyncio.to_thread(worklog_bridge.team_snapshot, team_key, root, _GROQ_INPUT_CHARS)
    if snapshot is None:
        await _first_read(config, team_key, on_stage)
        snapshot = await asyncio.to_thread(worklog_bridge.team_snapshot, team_key, root, _GROQ_INPUT_CHARS)
    if snapshot is None:
        text = "팀 정보 페이지에서 읽을 내용을 찾지 못했어요."
        if on_chunk:
            on_chunk(text)
        return text
    if on_stage:
        on_stage("🧠 요약 정리하는 중...")
    summary = await _summarize_snapshot(team_key, snapshot, on_chunk)
    docs = _document_lines(await asyncio.to_thread(wparts.documents, worklog_bridge.WORKLOG_STORE, team_key))
    if docs and on_chunk:
        on_chunk("\n\n" + "\n".join(docs))
    as_of = _format_as_of(snapshot.get("as_of"))
    if as_of and on_chunk:
        on_chunk(f"\n\n(Notion 기준 {as_of})")
    return "\n\n".join(filter(None, [summary, "\n".join(docs)]))


def _document_lines(docs: list[dict]) -> list[str]:
    """1번 메뉴의 주요 문서: 제목 — 한 줄 설명 (Notion 링크). 문서: docs/v1.1/spec.md P1-9."""
    if not docs:
        return []
    lines = ["📄 주요 문서"]
    for doc in docs:
        title = doc["path"].rsplit(" > ", 1)[-1]
        link = f"https://www.notion.so/{(doc['page_id'] or '').replace('-', '')}" if doc.get("page_id") else ""
        lines.append(f"- {title} — {doc['summary']}" + (f" ({link})" if link else ""))
    return lines


async def _get_commit_touched_issues(repo: str, token: str) -> set[int]:
    try:
        commits = await _github_get_cached(repo, "/commits", token, {"per_page": 30})
    except Exception:
        return set()
    touched: set[int] = set()
    for commit in commits:
        message = commit.get("commit", {}).get("message", "")
        touched |= {int(m.group(1)) for m in _ISSUE_REF_RE.finditer(message)}
    return touched


def _classify_tasks_by_github_state(
    confirmed: list[str],
    issue_state_by_number: dict[int, str],
    commit_touched: set[int],
) -> dict[str, list[str]]:
    """외부 I/O 없는 순수 함수 — 완료/진행중/대기/미등록 분류 규칙만 담는다."""
    done: list[str] = []
    in_progress: list[str] = []
    not_started: list[str] = []
    unlinked: list[str] = []

    for task in confirmed[:15]:
        referenced = [int(m.group(1)) for m in _ISSUE_REF_RE.finditer(task)]
        matched = next((n for n in referenced if n in issue_state_by_number), None)
        if matched is None:
            unlinked.append(task)
        elif issue_state_by_number[matched] == "closed":
            done.append(task)
        elif matched in commit_touched:
            in_progress.append(task)
        else:
            not_started.append(task)

    return {"done": done, "in_progress": in_progress, "not_started": not_started, "unlinked": unlinked}


# ── 팀 기록 수집·분석 (worklog) ─────────────────────────────
def _sync_notion_maps() -> dict[str, dict]:
    """설정된 모든 팀의 '팀 정보 페이지'를 루트로, 구조 지문 확인 → 바뀐 곳만 재스캔 → 지도대로 원문 수집.
    Notion은 읽기만 한다."""
    results: dict[str, dict] = {}
    for team_key, config in _load_team_configs().items():
        root = config.get("notion_page_id")
        if not root or not config.get("notion_token"):
            continue
        try:
            _last_team_check[team_key] = time.monotonic()
            results[team_key] = _sync_team_map(team_key, _team_roots(config), config["notion_token"], background=True)
        except Exception:
            traceback.print_exc()
    return results


def _organize_parts() -> dict[str, dict[str, int]]:
    """모든 팀: 종류 판정 전인 부분을 판정하고, 회의 메모 부분을 추출한다(Notion은 부르지 않음).
    Groq 대기열 몫(분당·하루)을 넘으면 멈추고 다음 주기에 이어 한다. 문서: docs/v1.1/spec.md P1-4, P1-8."""
    results: dict[str, dict[str, int]] = {}
    store = worklog_bridge.WORKLOG_STORE
    for team_key in _load_team_configs():
        try:
            judged = wparts.judge_pending(store, team_key, worklog_bridge.groq_classify_parts)
            extracted = wparts.extract_pending(store, team_key, worklog_bridge.groq_extract_part,
                                               worklog_bridge.EXTRACT_PROMPT_VERSION)
            # 새 회의록: 마지막 수정 후 2시간이 지나면 확인 대기로. 확인된 회의록의 수정: 새 항목만 자동 반영.
            promoted = wapply.promote_due(store, team_key)
            edits = wapply.apply_edits(store, team_key, worklog_bridge.EXTRACT_PROMPT_VERSION, worklog_bridge.groq_judge)
            results[team_key] = {"judged": judged, "extracted": extracted, "promoted": promoted, "edited": len(edits)}
        except Exception:
            traceback.print_exc()
    return results


async def _worklog_loop() -> None:
    # 서버가 켜지자마자 수집을 시작하면 첫 사용자 요청과 겹친다. 잠시 기다렸다 시작한다.
    await asyncio.sleep(worklog_bridge.START_DELAY_SECONDS)
    last_collect = 0.0
    while True:
        try:
            # 1시간 분석(analyze_due → 변경 후보)은 돌리지 않는다(v1.1, docs/v1.1/spec.md P1-0).
            with metrics.measure("background"):
                if time.monotonic() - last_collect >= worklog_bridge.NOTION_COLLECT_SECONDS:
                    last_collect = time.monotonic()
                    await asyncio.to_thread(_sync_notion_maps)
                # 주기마다(기본 5분) 부분 판정·추출을 조금씩 이어 한다. 메뉴는 그 결과만 읽는다.
                await asyncio.to_thread(_organize_parts)
                # 반영된 항목이 바뀐 팀만 단계·게이트를 다시 판정한다(순위 3, spec P2-3).
                await _assess_stages()
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(worklog_bridge.ANALYZE_TICK_SECONDS)


OPEN_CHECK_SECONDS = 600  # 챗봇을 열 때 Notion 수정 시각 확인은 팀마다 10분에 한 번(spec P2-6)
_last_team_check: dict[str, float] = {}


def _check_on_open(team_key: str, config: dict) -> None:
    """그 팀 사용자의 턴마다: 마지막 확인이 10분보다 오래됐으면 수정 시각 확인(바뀐 페이지만 다시 읽기)을
    백그라운드로 돌린다. 턴 응답은 기다리지 않는다. 1시간 주기 확인도 같은 시각을 쓴다."""
    if os.environ.get("WORKLOG_LOOP", "1") == "0":  # 백그라운드 작업을 끈 실행(테스트)
        return
    if not config.get("notion_page_id") or not config.get("notion_token"):
        return
    now = time.monotonic()
    if now - _last_team_check.get(team_key, -OPEN_CHECK_SECONDS) < OPEN_CHECK_SECONDS:
        return
    _last_team_check[team_key] = now

    async def run() -> None:
        try:
            await asyncio.to_thread(_sync_team_map, team_key, _team_roots(config), config["notion_token"], True)
        except Exception:
            traceback.print_exc()

    _spawn(run())


async def _github_stage_facts(config: dict) -> dict:
    """코드로 보는 게이트(백로그·마일스톤)에 쓰는 GitHub 사실. 못 읽으면 빈 값."""
    try:
        issues = await _github_get_cached(config["github_repo"], "/issues", config["github_token"],
                                          {"state": "all", "per_page": 100})
    except Exception:
        return {}
    issues = [i for i in issues if isinstance(i, dict) and "pull_request" not in i]
    with_milestone = [i for i in issues if i.get("milestone")]
    return {"open_issues": sum(1 for i in issues if i.get("state") == "open"),
            "milestones": len({i["milestone"]["number"] for i in with_milestone}),
            "closed_issues": sum(1 for i in with_milestone if i.get("state") == "closed")}


async def _assess_stages() -> None:
    if not worklog_bridge.llm_ready():
        return
    for team_key, config in _load_team_configs().items():
        try:
            github = await _github_stage_facts(config)
            await asyncio.to_thread(wstages.assess, worklog_bridge.WORKLOG_STORE, team_key, github,
                                    worklog_bridge.groq_stage_judge)
        except llm_queue.Busy:
            return  # 한도: 다음 주기에
        except Exception:
            traceback.print_exc()


@app.on_event("startup")
async def _start_worklog() -> None:
    if os.environ.get("WORKLOG_LOOP", "1") != "0":
        _spawn(_worklog_loop())


# ── 2. 팀 진행상황 조회 ─────────────────────────────
async def fetch_team_status(config: dict, team_key: str,
                            on_stage: Callable[[str], None] | None = None) -> tuple[str, list[dict]]:
    """회의 기록은 저장본(worklog)에서, 이슈·커밋은 GitHub에서 읽는다. Notion은 다시 읽지 않는다."""
    github_token = config["github_token"]
    repo = config["github_repo"]

    async def _fetch_issues() -> dict | list[dict]:
        return await _github_get_cached(repo, "/issues", github_token, {"state": "all", "per_page": 30})

    notes, issues = await asyncio.gather(_stored_notes(config, team_key, on_stage), _fetch_issues())
    note_lines = [text.strip() for text in notes["texts"] if text.strip()]
    footer = _note_footer(notes)
    issue_state_by_number: dict[int, str] = {i["number"]: i["state"] for i in issues if "pull_request" not in i}

    unassigned_open = [
        i for i in issues
        if "pull_request" not in i and i["state"] == "open" and not (i.get("assignees") or [])
    ][:10]
    unassigned_hint = ["", "🙋 담당자 없는 이슈가 있어요 — 아래 카드에서 맡을 수 있어요"] if unassigned_open else []

    if not note_lines:
        by_assignee: dict[str, list[str]] = {}
        for i in issues:
            if "pull_request" in i:
                continue
            for a in i.get("assignees") or [{"login": "미배정"}]:
                by_assignee.setdefault(a["login"], []).append(f"{i['title']} [{i['state']}]")
        lines = [f"{name}: {', '.join(tasks)}" for name, tasks in by_assignee.items()]
        result = ["👥 팀 진행상황", "\n".join(lines) if lines else "(회의록도 없고 등록된 이슈도 없어요)"]
        return "\n".join(result + unassigned_hint + footer), unassigned_open

    # 회의 메모는 사람이 확인해 worklog에 반영된 항목만 보인다(docs/v1.1/spec.md P1-9). 메뉴를 누를 때 LLM을 부르지 않는다.
    stored = await asyncio.to_thread(_stored_items, team_key)
    confirmed, undecided = stored["confirmed"], stored["undecided"]
    pending_hint = []
    if stored["to_confirm"]:
        pending_hint.append(f"📥 확인 대기 회의록 {stored['to_confirm']}건 — 메인 카드나 4번 ②에서 확인해 주세요")
    if stored["pending"]:
        pending_hint.append(f"⏳ 회의록 {stored['pending']}건 정리 중")

    shown_any = confirmed or undecided or stored["decisions"] or stored["feedback"] or stored["discussion"]
    if not shown_any and not stored["board"]["shown"]:
        header = "👥 팀 진행상황 (아직 확인된 회의록이 없어요, 원문)"
        text = header + "\n" + "\n".join(f"- {_brief(t)}" for t in note_lines[-10:])
        return "\n".join([text] + unassigned_hint + ([""] + pending_hint if pending_hint else []) + footer), unassigned_open

    commit_touched = await _get_commit_touched_issues(repo, github_token)
    tasks = _classify_tasks_by_github_state(confirmed, issue_state_by_number, commit_touched)

    # 6개 분류(spec P2-7): 현재 단계 / 확정된 흐름 / 지금 진행 중 / 이전 단계에서 남은 것 / 추가 논의 필요 / 천천히 정해도 되는 것
    board = stored["board"]
    result = ["👥 팀 진행상황 (회의록 기준)"]
    if board["shown"]:
        state = "" if board["approved"] else " (제안, 권한자 승인 전)"
        met = f" · 끝낸 것: {', '.join(g['text'] for g in board['met'])}" if board["met"] else ""
        result += ["", f"🧭 현재 단계: {wstages.label(board['shown'])}{state}{met}"]
    if stored["decisions"]:
        result += ["", "📌 확정된 흐름", *[f"- {t}" for t in stored["decisions"]]]
    doing = [f"- 정할 것: {g['text']}" for g in board["doing"]]
    doing += [f"- ✅ {t}" for t in tasks["done"]] + [f"- 🔨 {t} (커밋 있음)" for t in tasks["in_progress"]]
    doing += [f"- 📋 {t} (이슈만 등록)" for t in tasks["not_started"]] + [f"- 🆕 {t} (이슈 미등록)" for t in tasks["unlinked"]]
    if doing:
        result += ["", "🔨 지금 진행 중", *doing]
    if board["earlier"]:
        result += ["", "⏪ 이전 단계에서 남은 것", *[f"- {g['text']} ({wstages.label(g['stage'])})" for g in board["earlier"]]]
    if stored["discussion"]:
        result += ["", "🗣 추가 논의 필요", *[f"- {t}" for t in stored["discussion"]]]
    if undecided:
        result += ["", "🐢 천천히 정해도 되는 것", *[f"- {t}" for t in undecided[:10]]]
    if stored["feedback"]:
        result += ["", "💬 멘토링 피드백", *[f"- {t}" for t in stored["feedback"]]]
    result += unassigned_hint + ([""] + pending_hint if pending_hint else []) + footer

    return "\n".join(result), unassigned_open


def _stored_items(team_key: str) -> dict:
    """메뉴 2번에 쓸 반영된 항목(worklog). 사람이 확인한 회의록에서 나온 것만 보인다(docs/v1.1/spec.md P1-9).
    Notion·LLM을 부르지 않는다."""
    store = worklog_bridge.WORKLOG_STORE
    live = wapply.live_items(store, team_key)
    tasks = live.get("task", [])
    return {
        "confirmed": [f"{t['assignee']}: {t['title']}" for t in tasks if t.get("assignee")],
        "undecided": [i["title"] for i in live.get("open", [])]
                     + [f"{t['title']} – 담당자 미정" for t in tasks if not t.get("assignee")],
        "discussion": [i["title"] for i in live.get("needs_discussion", [])][:5],
        "decisions": _decision_flow(store, team_key, live.get("decision", []))[:6],
        "feedback": [f["title"] for f in live.get("feedback", [])][:5],
        "pending": wparts.pending_count(store, team_key, worklog_bridge.EXTRACT_PROMPT_VERSION),
        "to_confirm": len(wapply.pending_pages(store, team_key)),
        "board": wstages.board(store, team_key),
    }


def _decision_flow(store, team_key: str, decisions: list[dict]) -> list[str]:
    """확정된 결정. 결정이 대체됐으면 "A → B"로 이력을 같이 보인다."""
    replaced = {r["title"]: r["target_title"] for r in wapply.recent_applied(store, team_key, 50)
                if r["change_type"] == "superseding" and r["target_title"]}
    return [f"{replaced[d['title']]} → {d['title']}" if d["title"] in replaced else d["title"] for d in decisions]


# ── 4번 반영 흐름·메인 카드 알림 (confirm_flow) ─────────────
_CONFIRM_ACTIONS = {"apply_list", "apply_pick", "apply_pick_ready", "part_apply", "part_edit", "part_cancel",
                    "edit_delete", "edit_done", "applied_list", "revert", "gone_remove", "gone_keep",
                    "upload_notes", "stage_answer", "source_add"}
source_add_sessions: dict[str, dict] = {}


async def _handle_source_add(turn: Turn, config: dict, raw_text: str) -> None:
    """소스 추가: 링크의 페이지 ID를 팀 설정의 extra_source_ids에 더하고 바로 읽기 시작한다(spec P1-7, P2-7)."""
    page_id = _extract_notion_page_id(raw_text)
    if page_id is None:
        await turn.send("Notion 링크에서 페이지 ID를 못 찾았어요. 페이지 링크를 그대로 보내 주세요. (그만두려면 '취소')")
        return
    client = _notion_client_for(config)
    if not await asyncio.to_thread(_notion_page_accessible, page_id, client):
        await turn.send("그 페이지를 읽을 수 없어요. 페이지 오른쪽 위 ··· → 연결(Connections)에서 이 봇의 integration을 "
                        "추가한 뒤 다시 보내 주세요.")
        return
    source_add_sessions.pop(turn.aad_id, None)
    if page_id in _team_roots(config):
        await turn.send("이미 읽고 있는 페이지예요.")
        return
    configs = _load_team_configs()
    stored = configs.setdefault(turn.team_key, config)
    stored["extra_source_ids"] = [*(stored.get("extra_source_ids") or []), page_id]
    _save_team_configs(configs)
    await _register_team_map(turn.team_key, _team_roots(stored), stored["notion_token"])
    await turn.send("➕ 소스를 추가했어요. 읽고 정리하는 동안 메인 카드에 '정리 중'으로 보여요.")


def _is_approver(turn: Turn, config: dict) -> bool:
    """승인·되돌리기·게이트 답·삭제 확인을 할 수 있는 사람(spec P2-1). 판정은 people.can_approve 하나로."""
    if config.get("admin_aad_id") and config.get("admin_aad_id") == turn.aad_id:
        return True
    return people.can_approve(worklog_bridge.WORKLOG_STORE, turn.team_key, turn.aad_id)


async def _handle_confirm_action(turn: Turn, action: str, value: dict) -> None:
    config = _get_config(turn.team_key)
    if config is None:
        await turn.send(_NO_CONFIG_TEXT)
        return
    team, page_id = turn.team_key, str(value.get("page_id") or "")

    async def sync_team() -> None:
        await asyncio.to_thread(_sync_team_map, team, _team_roots(config), config["notion_token"], False)

    if action == "apply_list":
        await confirm_flow.show_apply_list(turn, team)
    elif action == "apply_pick":
        await confirm_flow.pick_page(turn, team, page_id, sync_team)
    elif action == "apply_pick_ready":  # 메인 카드의 확인 대기: 이미 정리돼 있으니 바로 미리보기
        await confirm_flow.show_preview(turn, team, page_id)
    elif action == "part_apply":
        await confirm_flow.apply_page(turn, team, page_id, _is_approver(turn, config))
    elif action == "part_edit":
        await confirm_flow.start_edit(turn, team, page_id)
    elif action == "part_cancel":
        await confirm_flow.cancel_page(turn, team, page_id)
    elif action == "edit_delete":
        await confirm_flow.delete_item(turn, int(value.get("n") or 0))
    elif action == "edit_done":
        await confirm_flow.finish_edit(turn)
    elif action == "applied_list":
        await confirm_flow.show_applied_list(turn, team, _is_approver(turn, config))
    elif action == "revert":
        await confirm_flow.revert(turn, team, str(value.get("candidate_id") or ""), _is_approver(turn, config))
    elif action == "stage_answer":
        await confirm_flow.answer_stage(turn, team, value, _is_approver(turn, config))
    elif action == "upload_notes":
        await confirm_flow.start_upload(turn, team)
    elif action == "source_add":
        if not _is_approver(turn, config):
            await turn.send("소스 추가는 권한자(관리자·개발·권한을 받은 사람)가 해요.")
            return
        source_add_sessions[turn.aad_id] = {"team_key": team}
        await turn.send("➕ 추가할 Notion 페이지나 DB 링크를 보내 주세요. 그 페이지에도 이 봇의 integration을 연결해 둬야 읽을 수 "
                        "있어요. (그만두려면 '취소')")
    elif action in ("gone_remove", "gone_keep"):
        await confirm_flow.resolve_gone(turn, team, str(value.get("part_id") or ""), action == "gone_remove",
                                        _is_approver(turn, config))


async def _send_welcome(turn: Turn) -> None:
    """메인 카드: 알림(확인 대기·결정 대체·삭제 확인·정리 중)을 먼저, 그다음 메뉴."""
    config = _get_config(turn.team_key)
    if config is not None:
        try:
            admin = config.get("admin_aad_id") == turn.aad_id or people.is_admin(
                worklog_bridge.WORKLOG_STORE, turn.team_key, turn.aad_id)
            lines, buttons = await asyncio.to_thread(confirm_flow.main_card_alerts, turn.team_key,
                                                     _is_approver(turn, config), admin)
            if not config.get("notes_found_noticed"):
                notice = await asyncio.to_thread(confirm_flow.first_scan_notice, turn.team_key)
                if notice is not None:
                    lines, buttons = [*lines, notice[0]], [*buttons, *notice[1]]
                    configs = _load_team_configs()
                    configs.setdefault(turn.team_key, config)["notes_found_noticed"] = True
                    _save_team_configs(configs)
        except Exception:
            traceback.print_exc()
            lines, buttons = [], []
        if lines and buttons:
            await turn.send_buttons("\n".join(["🔔 알림", *lines]), buttons)
        elif lines:
            await turn.send("\n".join(["🔔 알림", *lines]))
    await turn.send_buttons(WELCOME_TEXT, _MENU_BUTTONS)


# ── 3. 내 역할 조회 ─────────────────────────────
async def fetch_my_role(github_id: str | None, display_name: str | None, config: dict, team_key: str,
                        on_stage: Callable[[str], None] | None = None, aliases: list[str] | None = None) -> str:
    """배정된 이슈는 GitHub에서, 회의록 속 내 이름은 저장본(worklog)에서 찾는다. Notion은 다시 읽지 않는다."""
    github_token = config["github_token"]

    async def _fetch_github_lines() -> tuple[list[str], set[int]]:
        if not github_id:
            return [], set()
        issues = await _github_get_cached(
            config["github_repo"], "/issues", github_token, {"assignee": github_id, "state": "open", "per_page": 30}
        )
        lines: list[str] = []
        numbers: set[int] = set()
        for issue in issues:
            if "pull_request" in issue:
                continue
            lines.append(f"- {issue['title']} (#{issue['number']})")
            numbers.add(issue["number"])
        return lines, numbers

    async def _fetch_notes() -> dict:
        if not display_name:
            return {"texts": [], "as_of": None}
        return await _stored_notes(config, team_key, on_stage)

    (github_lines, github_issue_numbers), notes = await asyncio.gather(_fetch_github_lines(), _fetch_notes())
    # 원문 한 건의 첫 줄은 "[역할] 경로" 머리줄이라 빼고, 내용 줄 단위로 내 이름을 찾는다.
    raw_note_lines = [line for text in notes["texts"] for line in text.splitlines()[1:]]
    if not github_lines:
        github_lines.append("- (배정된 GitHub 이슈가 없어요)")

    meeting_lines: list[str] = []
    if display_name:
        for line in raw_note_lines:
            stripped = line.strip()
            if not stripped or display_name not in stripped:
                continue
            referenced = {int(m.group(1)) for m in _ISSUE_REF_RE.finditer(stripped)}
            if referenced & github_issue_numbers:
                continue
            meeting_lines.append(f"- {stripped}")

    meeting_lines = list(dict.fromkeys(meeting_lines))  # 같은 줄이 여러 기록에 있으면 한 번만
    names = {n for n in [display_name, *(aliases or [])] if n}
    live = await asyncio.to_thread(wapply.live_items, worklog_bridge.WORKLOG_STORE, team_key)
    mine = [t for t in live.get("task", []) if t.get("assignee") in names]
    result = ["👤 내 역할", "📌 GitHub 담당 이슈", *github_lines]
    if mine:
        result += ["📝 회의록에서 맡은 일", *[f"- {t['title']}" + (f" (~{t['due']})" if t.get("due") else "") for t in mine]]
    if meeting_lines:
        result += ["📝 회의록 언급", *meeting_lines]
    return "\n".join(result + _note_footer(notes))


# ── 4. 완료된 작업 공유 ─────────────────────────────
async def _find_project_status_field(
    owner: str, repo_name: str, project_number: int, token: str
) -> tuple[str, str, str] | None:
    cache_key = (f"{owner}/{repo_name}", project_number)
    if cache_key in _project_status_field_cache:
        return _project_status_field_cache[cache_key]

    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        projectV2(number: $number) {
          id
          fields(first: 20) {
            nodes {
              ... on ProjectV2SingleSelectField { id name options { id name } }
            }
          }
        }
      }
    }
    """
    data = await _github_graphql(query, token, {"owner": owner, "repo": repo_name, "number": project_number})
    project = data["repository"]["projectV2"]
    result: tuple[str, str, str] | None = None
    if project:
        for fld in project["fields"]["nodes"]:
            if fld.get("name") not in ("Status", "상태"):
                continue
            for option in fld.get("options", []):
                if option["name"] in ("Done", "완료"):
                    result = (project["id"], fld["id"], option["id"])
                    break
            if result:
                break

    _project_status_field_cache[cache_key] = result
    return result


async def _find_project_item_id(
    owner: str, repo_name: str, project_id: str, issue_number: int, token: str
) -> str | None:
    cache_key = (project_id, issue_number)
    if cache_key in _project_item_id_cache:
        return _project_item_id_cache[cache_key]

    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        issue(number: $number) {
          projectItems(first: 10) {
            nodes { id project { id } }
          }
        }
      }
    }
    """
    data = await _github_graphql(query, token, {"owner": owner, "repo": repo_name, "number": issue_number})
    issue = data["repository"]["issue"]
    result = None
    if issue:
        for item in issue["projectItems"]["nodes"]:
            if item["project"]["id"] == project_id:
                result = item["id"]
                break

    _project_item_id_cache[cache_key] = result
    return result


async def _set_project_item_status(project_id: str, field_id: str, option_id: str, item_id: str, token: str) -> None:
    mutation = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
      updateProjectV2ItemFieldValue(
        input: { projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: { singleSelectOptionId: $optionId } }
      ) { projectV2Item { id } }
    }
    """
    await _github_graphql(
        mutation, token, {"projectId": project_id, "itemId": item_id, "fieldId": field_id, "optionId": option_id}
    )


async def share_completed_work(config: dict, github_id: str) -> str:
    repo = config["github_repo"]
    owner, repo_name = repo.split("/", 1)
    project_number = config["github_project_number"]
    token = config["github_token"]

    my_issues = await _github_get_cached(repo, "/issues", token, {"assignee": github_id, "state": "all", "per_page": 30})
    my_issue_numbers = {i["number"] for i in my_issues if "pull_request" not in i}
    if not my_issue_numbers:
        return "✅ 완료된 작업\n담당된 GitHub 이슈가 없어요."

    commits = await _github_get_cached(repo, "/commits", token, {"per_page": 20})
    touched_numbers: set[int] = set()
    for commit in commits:
        message = commit.get("commit", {}).get("message", "")
        touched_numbers |= {int(m.group(1)) for m in _ISSUE_REF_RE.finditer(message)}

    issue_numbers = sorted(my_issue_numbers & touched_numbers)
    if not issue_numbers:
        return "✅ 완료된 작업\n담당 이슈 중 최근 커밋에서 언급된 게 없어요."

    try:
        status_field = await _find_project_status_field(owner, repo_name, project_number, token)
    except Exception as exc:
        return (
            "⚠️ GitHub Project 정보를 가져오지 못했어요.\n"
            "토큰에 read:project / project 권한이 있는지 확인해주세요.\n"
            f"(에러: {exc})"
        )

    if status_field is None:
        return (
            f"⚠️ 연결된 GitHub Project(#{project_number})에서 'Status' 필드(Done/완료 옵션 포함)를 찾지 못했어요.\n"
            "Project에 Status 필드와 Done 옵션이 있는지 확인해주세요."
        )
    project_id, field_id, done_option_id = status_field

    updated: list[str] = []
    skipped: list[str] = []
    for number in issue_numbers:
        item_id = await _find_project_item_id(owner, repo_name, project_id, number, token)
        if item_id is None:
            skipped.append(f"#{number}")
            continue
        await _set_project_item_status(project_id, field_id, done_option_id, item_id, token)
        updated.append(f"#{number}")

    lines = ["✅ 완료된 작업으로 반영됨", ", ".join(updated) if updated else "(변경된 항목 없음)"]
    if skipped:
        lines.append(f"⏭️ Project에 없어서 건너뜀: {', '.join(skipped)}")
    return "\n".join(lines)


# ── 담당자 없는 이슈 self-claim ─────────────────────────────
async def claim_issue(config: dict, issue_number: int, github_id: str) -> str:
    """직전에 캐시 없이 다시 조회한다 — 두 사람이 동시에 눌러도 중복 배정되지 않게."""
    repo = config["github_repo"]
    token = config["github_token"]

    try:
        issue = await _github_get(repo, f"/issues/{issue_number}", token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"⚠️ #{issue_number} 이슈를 찾을 수 없어요."
        raise

    existing_assignees = issue.get("assignees") or []
    if existing_assignees:
        names = ", ".join(a["login"] for a in existing_assignees)
        return f"⚠️ #{issue_number}는 이미 담당자가 있어요: {names}"

    await _github_patch(repo, f"/issues/{issue_number}", token, {"assignees": [github_id]})
    return f"🙋 #{issue_number} '{issue['title']}' 담당자로 등록됐어요!"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
