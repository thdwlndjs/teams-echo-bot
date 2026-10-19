import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Header, HTTPException
from notion_client import Client
from pydantic import BaseModel
from dotenv import load_dotenv

from microsoft_teams.api import ConversationUpdateActivity, MessageActivity, TypingActivityInput
from microsoft_teams.api.activities.invoke.adaptive_card.action import AdaptiveCardInvokeActivity
from microsoft_teams.api.models.adaptive_card.adaptive_card_action_response import (
    AdaptiveCardActionMessageResponse,
)
from microsoft_teams.apps import ActivityContext, App, FastAPIAdapter
from microsoft_teams.cards import AdaptiveCard, ExecuteAction, SubmitAction, TextBlock

# ── 설정 ────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)  # SDK 내부 logger.exception()이 실제로 출력되도록
# ENV_FILE 안 정하면 기존과 똑같이 그냥 .env를 찾는다(Playground/로컬 기본값).
# 배포 환경에선 `ENV_FILE=.env.production python src/main.py`로 실 Teams 자격증명 프로필을 쓴다.
load_dotenv(os.environ.get("ENV_FILE"))
N8N_SHARED_SECRET = os.environ["N8N_SHARED_SECRET"]  # n8n↔봇 간 검증용
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")  # 회의록 → 태스크 요약용 (없으면 원문 그대로 표시)
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")  # llama-3.1-8b-instant는 Groq에서 단종(404)
# GitHub/Notion 토큰, 레포, 페이지는 봇 공용 값이 아니라 팀별 `설정` 마법사에서 받는다 (team_configs.json).

_ISSUE_REF_RE = re.compile(r"#(\d+)")

# ── 캐시 (메모리, 서버 프로세스 살아있는 동안만 유지 — 재시작하면 비워짐) ──────────
# GitHub 이슈/커밋처럼 실시간으로 바뀌어야 하는 값은 절대 여기 안 들어간다(self-claim 정확도 때문).
# 반대로 거의 안 바뀌는 구조 정보나, 비싼 호출(Groq)만 골라서 캐싱한다.

# (레포, 프로젝트 번호) -> (project_id, field_id, done_option_id) | None
# 팀이 GitHub Project 보드 구조(Status 필드/Done 옵션)를 바꾸지 않는 한 거의 안 변해서 사실상 영구 캐시.
_project_status_field_cache: dict[tuple[str, int], tuple[str, str, str] | None] = {}

# (project_id, 이슈번호) -> project item id | None
# 이슈가 Project에 한번 들어가면 아이템 ID도 거의 안 바뀐다. 캐시가 틀려도(이슈가 빠졌다 다시 들어간 경우)
# 기존 "건너뜀" 처리로 안전하게 실패하니 리스크가 낮다.
_project_item_id_cache: dict[tuple[str, int], str | None] = {}

# meeting_notes_page_id -> {"hash": 마지막으로 Groq에 넘긴 원문 해시, "result": 그때 분류 결과}
# 회의록 원문 자체는 매번 Notion에서 새로 읽어오되(직접 수정됐을 수 있으니), 내용이 지난번과
# 똑같으면(해시 동일) Groq 재호출 없이 저장된 분류 결과를 재사용한다.
_meeting_notes_classification_cache: dict[str, dict] = {}

# notion_page_id -> {"last_edited": 마지막으로 요약했을 때 페이지의 last_edited_time, "summary": 그때 요약 결과}
# 시간 기준 TTL이 아니라 "내용이 실제로 바뀌었는지"로 판단한다. 그것도 무거운 전체 조회로 확인하는 게
# 아니라 `pages.retrieve`(가벼운 메타데이터 호출 1번)의 last_edited_time만 보고 판단 — 안 바뀌었으면
# 재귀 블록 조회(N+1)도 Groq 호출도 둘 다 건너뛴다. 설정 마법사에서 이 페이지를 등록하는 순간
# 미리 한 번 채워두므로(`_prewarm_project_summary`), 보통은 '1'번을 처음 눌러도 이미 캐시가 있다.
_project_summary_cache: dict[str, dict] = {}

# ── 앱 초기화 (기존 FastAPI에 Teams 어댑터를 붙이는 구조) ──
fastapi_app = FastAPI()
adapter = FastAPIAdapter(app=fastapi_app)
app = App(http_server_adapter=adapter)

# ── (로컬 전용 우회책) M365 Agents Playground 카드 버튼 답장 404 ─────────────
# Playground가 일반 메시지/conversationUpdate 활동을 봇에 넘길 땐 serviceUrl에
# "http://localhost:PORT/_connector"처럼 "/_connector"가 붙어 있어서 ctx.send()의 답장이
# 그 주소로 정상 도착한다. 그런데 카드 버튼 클릭(Action.Execute → invoke 활동)을 넘길 때는
# 어째서인지 "/_connector"가 빠진 serviceUrl("http://localhost:PORT")이 실려 온다. 봇은 받은
# serviceUrl을 그대로 믿고 답장 주소를 만들기 때문에, 카드 버튼에서 시작된 ctx.send()만
# 404 Not Found로 죽는다 — Teams/Playground 클라이언트 화면엔 "Unable to reach app. Please
# try again."로 표시됨. 실제 Teams(프로덕션 Bot Framework 채널)는 serviceUrl이 처음부터
# 올바른 엔드포인트라 이 문제 자체가 없으므로, 인증을 안 하는 로컬 개발 모드
# (DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS=true)에서만 보정한다.
_UNAUTHENTICATED_LOCAL_DEV = os.environ.get("DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS", "").lower() == "true"


def _fix_playground_invoke_service_url(ctx: ActivityContext) -> None:
    """카드 invoke 활동의 conversation_ref.service_url에 빠진 "/_connector"를 보정한다.

    ctx.stream은 ActivityContext가 만들어질 때(=이 함수가 불리기도 전에) conversation_ref의
    service_url을 이미 문자열로 복사해서 내부 ApiClient를 만들어버린 상태다. 그래서 여기서
    conversation_ref.service_url만 고쳐서는 ctx.stream엔 반영이 안 되고, 스트림을 새로
    만들어야 고쳐진 주소로 나간다 — ctx.send()는 매번 self.conversation_ref를 다시 읽어서
    보내기 때문에 이 문제가 없다."""
    if not _UNAUTHENTICATED_LOCAL_DEV:
        return
    ref = ctx.conversation_ref
    if ref.service_url and "/_connector" not in ref.service_url:
        ref.service_url = ref.service_url.rstrip("/") + "/_connector"
        ctx.stream = ctx._activity_sender.create_stream(ref)  # noqa: SLF001 — 로컬 전용 우회책


# ── 팀/사용자별 설정 저장소 ────────────────────────────────────────
# 이 봇 하나를 여러 팀/개인이 같이 쓸 수 있도록, GitHub 레포·API 토큰·Notion 페이지 같은 값을
# 코드/'.env'에 고정하지 않고 대화 주체별로 저장해서 조회한다.
# key: 팀 채널이면 "team:<팀 ID>", 1:1 개인 채팅이면 "user:<그 사람의 aad_object_id>".
#      서로 다른 팀/사람이 같은 키를 공유하지 않아서, 설정을 저장한 사람만 자기 설정을 보고 쓴다.
# value: {
#   "github_repo": "owner/repo", "github_token": "...", "notion_token": "...",
#   "notion_page_id": "...", "meeting_notes_page_id": "...", "github_project_number": 4,
#   "admin_aad_id": "이 설정을 처음 완료한 사람 — 이후 재설정은 이 사람만 가능",
# }
TEAM_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "team_configs.json")
# 사람 단위(팀 무관)로 전역 저장. key: aad_object_id, value: {"github_id": "..."}
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
    return _load_member_profiles().get(aad_id, {}).get("github_id")


def _set_github_id(aad_id: str, github_id: str) -> None:
    profiles = _load_member_profiles()
    profiles.setdefault(aad_id, {})["github_id"] = github_id
    _save_member_profiles(profiles)


def _get_team_key(ctx: "ActivityContext") -> str:
    """이 대화의 설정을 저장/조회할 키를 반환한다.

    팀 채널이면 팀 단위로("team:<팀 ID>"), 팀 컨텍스트가 없는 1:1 개인 채팅이면
    그 사람 단위로("user:<aad_object_id>") 구분한다. 예전엔 개인 채팅을 전부
    "default" 하나로 묶어서, 서로 다른 사람이 1:1로 설정하면 덮어써지는 문제가 있었다.
    """
    team = ctx.activity.team
    if team and team.id:
        return f"team:{team.id}"
    return f"user:{ctx.activity.from_.aad_object_id}"


def _get_config(team_key: str) -> dict | None:
    """team_key로 저장된 설정을 반환한다. 없으면 None — 다른 사람/팀 설정으로 조용히 넘어가지 않는다."""
    return _load_team_configs().get(team_key)


def _notion_client_for(config: dict) -> Client:
    return Client(auth=config["notion_token"])


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
    # 공백 없이 + ASCII만 (실제 API 토큰은 항상 ASCII). 한글 등 non-ASCII가 섞이면
    # httpx가 헤더에 넣을 때 UnicodeEncodeError로 죽어버려서, 여기서 미리 걸러야 한다.
    if not text or re.search(r"\s", text) or not text.isascii():
        return None
    return text


# GitHub 아이디 형식: 영문/숫자/하이픈만, 하이픈으로 시작·끝 불가, 하이픈 연속 불가, 최대 39자.
# (실존 계정인지 API로 확인하진 않지만, "2"처럼 명백히 아이디가 아닌 값은 최소한 걸러낸다.)
_GITHUB_USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")


def _extract_github_id(text: str) -> str | None:
    text = text.strip()
    return text if _GITHUB_USERNAME_RE.fullmatch(text) else None


async def _github_token_valid(token: str) -> bool:
    """이 토큰으로 GitHub API 인증이 실제로 되는지 확인한다."""
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
    """주어진 Notion integration 토큰으로 이 페이지(또는 데이터베이스)를 실제로 읽을 수 있는지 확인한다.

    페이지에 사람이 게스트로 초대된 것과 별개로, Notion API는 그 페이지의
    'Connections'에 이 integration이 따로 추가돼 있어야만 접근을 허용한다.
    """
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


# ── 대화 참조 저장소 (프로토타입: 메모리 dict / 운영: DB로 교체 필요) ──
# key: github_id, value: teams conversation_id
conversation_store: dict[str, str] = {}


# ── 사용자가 봇에게 메시지를 보낼 때마다 conversation_id 갱신 ──
def _register_conversation(ctx: ActivityContext[MessageActivity]) -> None:
    aad_id = ctx.activity.from_.aad_object_id
    github_id = _get_github_id(aad_id)
    if github_id:
        conversation_store[github_id] = ctx.activity.conversation.id


@app.on_message_pattern(re.compile(r"hello|hi|greetings"))
async def handle_greeting(ctx: ActivityContext[MessageActivity]) -> None:
    _register_conversation(ctx)
    await ctx.send("Hello! How can I assist you today?")


WELCOME_TEXT = "안녕하세요! 아래에서 골라주세요\n\n⚙️ 연결 설정을 바꾸고 싶으면 '설정'이라고 입력하세요."

# (버튼에 실릴 값, 버튼 제목) — 버튼을 눌러도 되고, 그냥 숫자를 텍스트로 보내도 똑같이 동작한다.
_MENU_ITEMS = [
    ("1", "1️⃣ 팀/프로젝트 정보"),
    ("2", "2️⃣ 진행상황 조회"),
    ("3", "3️⃣ 내 역할 조회"),
    ("4", "4️⃣ 완료된 작업 공유하기"),
]


def _build_menu_card() -> AdaptiveCard:
    # (임시 실험) Action.Execute가 Playground에서 클릭 자체가 요청으로 안 나가는 문제 때문에,
    # 여기 버튼만 Action.Submit으로 바꿔서 그쪽은 되는지 테스트 중. Action.Submit은 Teams에서
    # invoke가 아니라 이 카드가 속한 대화의 message 활동으로 오고, data는 그 활동의 value에
    # 실려온다 — handle_message()에서 그걸 감지해서 처리한다.
    return AdaptiveCard(
        body=[TextBlock(text=WELCOME_TEXT, wrap=True)],
        actions=[
            SubmitAction(title=label, data={"action": "menu", "choice": choice})
            for choice, label in _MENU_ITEMS
        ],
    )


@app.on_card_action_execute("menu")
async def handle_menu_card_action(
    ctx: ActivityContext[AdaptiveCardInvokeActivity],
) -> AdaptiveCardActionMessageResponse:
    """웰컴 카드의 1~4번 버튼을 눌렀을 때 호출된다. 실제 결과는 `_run_action`이 대화에
    메시지를 보내고, 여기서는 클릭이 접수됐다는 짧은 토스트만 돌려준다."""
    _fix_playground_invoke_service_url(ctx)
    await ctx.reply(TypingActivityInput())  # 텍스트 메뉴 경로와 달리 카드 클릭은 이게 없어서 무반응처럼 보였음
    choice = ctx.activity.value.action.data["choice"]
    await _run_action(ctx, choice)
    return AdaptiveCardActionMessageResponse(value="처리했어요")


@app.on_conversation_update
async def handle_conversation_update(ctx: ActivityContext[ConversationUpdateActivity]) -> None:
    activity = ctx.activity
    members_added = activity.members_added or []
    bot_id = activity.recipient.id if activity.recipient else None

    # 봇 자기 자신이 추가된 이벤트(팀/채널에 앱이 설치된 경우 등)는 무시하고,
    # 실제 사용자가 새로 추가된 경우에만 처리한다.
    if not any(member.id != bot_id for member in members_added):
        return

    conversation_type = activity.conversation.conversation_type if activity.conversation else None

    # 1:1 개인 채팅에서만 메뉴를 자동 발신한다.
    # 채널/그룹 채팅은 n8n 파이프라인의 push 알림 전용이라 대화형 응답이 필요 없다.
    if conversation_type != "personal":
        return

    await ctx.send(_build_menu_card())


# ── 초기 설정 마법사 ────────────────────────────────────────
# key: aad_object_id, value: {"team_key": str, "step": int, "data": dict}
setup_sessions: dict[str, dict] = {}
# GitHub 아이디가 없어서 잠시 멈춰둔 원래 요청.
# key: aad_object_id, value: {"kind": "menu", "raw_text": str} 또는 {"kind": "claim", "issue_number": int}
github_onboarding_sessions: dict[str, dict] = {}

@dataclass
class SetupStep:
    key: str  # session["data"]에 저장될 키
    prompt: str  # "질문 한 줄 + (필요하면) 예시 한 줄" 구조로 통일. 부가 설명은 다 걷어냄.
    parser: Callable[[str], object | None]  # 파싱 실패 시 None을 돌려주는 함수
    error_message: str  # 파싱 실패 시 보여줄 안내 메시지


# 토큰을 레포 다음, 노션 페이지보다 먼저 받는 이유: 노션 페이지 접근 검증에 그 토큰이 바로 필요해서다.
SETUP_STEPS: list[SetupStep] = [
    SetupStep(
        "github_repo",
        "**1** 레포\n`thdwlndjs/n8nTest`",
        _extract_github_repo,
        "레포 형식이 아니에요.\n`owner/repo`로 다시 보내줄래요?",
    ),
    SetupStep(
        "github_token",
        "**2** GitHub 토큰\n(repo, project 권한 있는 Personal Access Token)\n\n모르면 '가이드'라고 보내주세요",
        _extract_token,
        "토큰 형식이 아니에요. 공백 없이 다시 보내줄래요?",
    ),
    SetupStep(
        # 관리자도 결국 3/4번 쓰려면 본인 GitHub 아이디가 필요한 사람이라, GitHub 토큰 넣는 김에
        # 바로 같이 받는다 — 나중에 3번 눌렀을 때 따로 끊고 다시 물어보지 않도록.
        "admin_github_id",
        "**3** 본인 GitHub 아이디\n`thdwlndjs`처럼 아이디만 보내주세요",
        _extract_github_id,
        "GitHub 아이디 형식이 아니에요. (예: thdwlndjs)",
    ),
    SetupStep(
        "notion_token",
        "**4** Notion 토큰\n(Integration Token)\n\n모르면 '가이드'라고 보내주세요",
        _extract_token,
        "토큰 형식이 아니에요. 공백 없이 다시 보내줄래요?",
    ),
    SetupStep(
        "notion_page_id",
        "**5** 팀 정보 노션 페이지 링크",
        _extract_notion_page_id,
        "페이지를 못 찾았어요.\n링크를 다시 보내줄래요?",
    ),
    SetupStep(
        "meeting_notes_page_id",
        "**6** 회의록 노션 페이지 링크",
        _extract_notion_page_id,
        "페이지를 못 찾았어요.\n링크를 다시 보내줄래요?",
    ),
    SetupStep(
        "github_project_number",
        "**7** GitHub 프로젝트 번호\nProjects 탭 URL 끝 숫자예요",
        _extract_project_number,
        "숫자만 보내줄래요?",
    ),
]

# 토큰 발급 방법을 몰라서 막히는 사람을 위한 단계별 가이드.
# 이 문구를 그대로 보내면(대소문자/공백 무시) 토큰 파싱을 시도하는 대신 가이드를 보여주고 같은 단계에 머문다.
_HELP_TRIGGERS = {"모름", "몰라", "모르겠어", "가이드", "도움말", "help"}
# GitHub 아이디가 숫자 한 글자처럼 흔치 않은 형태일 때 되묻는 확인(예/아니요) 응답 인식용.
_YES_WORDS = {"네", "예", "응", "어", "맞아", "맞아요", "yes", "y"}
_NO_WORDS = {"아니", "아니요", "아니오", "no", "n"}

_ASK_GITHUB_ID_TEXT = (
    "먼저 GitHub 아이디를 알려줄래요?\n`thdwlndjs`처럼 아이디만 보내주세요.\n\n"
    "(그만두려면 '취소' 또는 '나가기')"
)
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

# 완료 요약에서 값 그대로 보여주면 안 되는 항목(토큰) — 대신 "등록됨"만 표시
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


async def _start_setup(ctx: ActivityContext[MessageActivity]) -> None:
    aad_id = ctx.activity.from_.aad_object_id
    team_key = _get_team_key(ctx)

    existing = _get_config(team_key)
    if existing is not None and existing.get("admin_aad_id") != aad_id:
        await ctx.send("이미 설정돼 있어요.\n이 설정은 처음 등록한 관리자만 바꿀 수 있어요.")
        return

    setup_sessions[aad_id] = {"team_key": team_key, "step": 0, "data": {}}
    # 팀 채팅일 때만 "어느 팀 설정인지"가 새 정보라 표시하고, 개인 채팅에선 당연한 말이라 생략.
    header = f"**{ctx.activity.team.name}** 설정 시작할게요" if ctx.activity.team else "설정 시작할게요"
    await ctx.send(f"{header}\n\n{SETUP_STEPS[0].prompt}\n\n(중간에 그만두려면 '취소' 또는 '나가기')")


async def _handle_setup_message(ctx: ActivityContext[MessageActivity], raw_text: str) -> None:
    aad_id = ctx.activity.from_.aad_object_id
    session = setup_sessions[aad_id]
    step_index = session["step"]
    step = SETUP_STEPS[step_index]
    key = step.key

    if raw_text.strip().lower() in _HELP_TRIGGERS and key in _SETUP_GUIDES:
        await ctx.send(_SETUP_GUIDES[key])
        return

    value = step.parser(raw_text)
    if value is None:
        await ctx.send(f"⚠️ {step.error_message}")
        return

    if key == "github_token":
        if not await _github_token_valid(value):
            await ctx.send("⚠️ 이 토큰으로 GitHub 인증이 안 돼요.\n토큰이 맞는지 다시 확인해서 보내줄래요?")
            return

    if key == "notion_token" and value == session["data"].get("github_token"):
        # 방금 넣은 값이 바로 앞 단계(GitHub 토큰)랑 완전히 같으면, 아까 그 값을 실수로 또
        # 붙여넣은 것으로 본다. 서로 다른 서비스 토큰이 우연히 같을 수는 없기 때문.
        await ctx.send(
            "⚠️ 방금 보낸 값이 GitHub 토큰이랑 똑같아요.\n"
            "Notion 토큰 대신 GitHub 토큰을 잘못 다시 보낸 것 같은데, Notion 토큰을 다시 확인해서 보내줄래요?"
        )
        return

    if key in ("notion_page_id", "meeting_notes_page_id"):
        notion_client = Client(auth=session["data"]["notion_token"])
        accessible = await asyncio.to_thread(_notion_page_accessible, value, notion_client)
        if not accessible:
            await ctx.send(
                "⚠️ 이 페이지는 방금 넣은 Notion 토큰으로 불러올 수 없어요.\n"
                "게스트 권한만 있거나, 이 토큰이 아직 그 페이지의 연결(Connections)에 "
                "추가 안 됐을 수 있어요.\n"
                "페이지 주인에게 연결 추가를 부탁하거나, 다른 페이지 링크를 보내줄래요?"
            )
            return
        if key == "notion_page_id":
            # 팀/프로젝트 정보 페이지를 막 등록한 시점에 미리 요약해서 캐시를 채워둔다 —
            # 설정 마법사 응답은 안 기다리게 백그라운드로(실패해도 무시, '1'번 클릭 때 재시도됨).
            asyncio.create_task(_prewarm_project_summary(value, notion_client))

    session["data"][key] = value
    step_index += 1

    if step_index >= len(SETUP_STEPS):
        summary_lines = [
            f"- {_SETUP_LABELS[k]}: {'등록됨' if k in _SECRET_KEYS else v}"
            for k, v in session["data"].items()
            if k in _SETUP_LABELS
        ]
        summary = "\n".join(summary_lines)

        # 본인 GitHub 아이디는 팀 설정이 아니라 사람 단위 프로필(member_profiles.json)에 저장한다.
        admin_github_id = session["data"].pop("admin_github_id")
        _set_github_id(aad_id, admin_github_id)

        session["data"]["admin_aad_id"] = aad_id
        configs = _load_team_configs()
        configs[session["team_key"]] = session["data"]
        _save_team_configs(configs)
        del setup_sessions[aad_id]
        await ctx.send(f"**✅ 설정 완료**\n\n{summary}\n\n이제 1~4번 이 설정으로 동작해요.")
        return

    session["step"] = step_index
    # 방금 뭐가 저장됐고 다음에 뭘 물어보는지를 한 메시지에 같이 보여줘서, 이전 단계 값을
    # 착각해서 다음 단계에 잘못 또 보내는 실수(예: GitHub 토큰을 Notion 토큰 자리에 다시 붙여넣기)를 줄인다.
    await ctx.send(f"✅ {_SETUP_LABELS[key]} 완료!\n\n{SETUP_STEPS[step_index].prompt}")


async def _run_action(ctx: "ActivityContext", raw_text: str) -> None:
    """`설정`/클레임 라우팅이 끝난 뒤, 메뉴(1~4)를 실제로 처리한다.

    GitHub 아이디가 필요한 동작인데 아직 연결이 안 됐으면 온보딩으로 빠지고,
    온보딩이 끝나면 이 함수가 원래 요청을 그대로 다시 받아 이어서 처리한다.
    ctx는 텍스트 메시지일 수도, 메뉴 카드 버튼 클릭(invoke)일 수도 있다.
    """
    aad_id = ctx.activity.from_.aad_object_id
    team_key = _get_team_key(ctx)
    config = _get_config(team_key)

    if config is None:
        await ctx.send("아직 연결된 설정이 없어요.\n'설정'이라고 입력해서 먼저 연결해줄래요?")
        return

    needs_github_id = raw_text in ("3", "4")
    github_id = _get_github_id(aad_id)
    if needs_github_id and github_id is None:
        github_onboarding_sessions[aad_id] = {"kind": "menu", "raw_text": raw_text}
        await ctx.send(_ASK_GITHUB_ID_TEXT)
        return

    try:
        if raw_text == "1":
            # 1번만 진짜 토큰 스트리밍이 의미 있다(최종 결과가 곧 Groq가 생성하는 텍스트 그 자체라서).
            # on_chunk가 스트림에 다 채워주므로 여기선 ctx.send()를 따로 안 부르고 stream만 닫는다.
            await fetch_project_info(config, on_stage=ctx.stream.update, on_chunk=ctx.stream.emit)
            await ctx.stream.close()
            return
        elif raw_text == "2":
            ctx.stream.update("🔍 진행상황을 확인하는 중...")
            reply_text, unassigned_issues = await fetch_team_status(config)
            ctx.stream.emit(reply_text)
            await ctx.stream.close()
            if unassigned_issues:
                await ctx.send(_build_claim_card(unassigned_issues))
            return
        elif raw_text == "3":
            ctx.stream.update("🔍 내 역할을 확인하는 중...")
            reply_text = await fetch_my_role(github_id, ctx.activity.from_.name, config)
        elif raw_text == "4":
            ctx.stream.update("🔍 완료된 작업을 확인하는 중...")
            reply_text = await share_completed_work(config, github_id)
        else:
            reply_text = "올바른 값을 입력하세요. (1~4, 연결 설정 변경은 '설정')"
    except Exception as exc:  # 원인을 터미널에서 바로 볼 수 있게 트레이스백을 강제로 출력
        traceback.print_exc()
        reply_text = f"⚠️ 처리 중 오류가 발생했어요: {exc}"

    # ctx.stream.update()로 띄운 "확인하는 중..." 상태는 같은 스트림에 실제 내용을 emit하고
    # close해야만 진짜 답변으로 교체된다 — ctx.send()로 별도 전송하면 상태 표시가 그대로 남는다
    # (3/4번 및 예외 경로 공통. raw_text가 "1"~"4"가 아닌 경우는 애초에 이 함수까지 안 오므로
    # 여기 도달하는 모든 경로는 위에서 이미 ctx.stream.update()를 한 번 불렀다).
    ctx.stream.emit(reply_text)
    await ctx.stream.close()


async def _claim_and_reply(ctx: "ActivityContext", issue_number: int, github_id: str) -> None:
    """`claim_issue`를 실제로 호출하고 결과를 보낸다 (카드 버튼 클릭 직후, 또는 그때 GitHub 아이디
    온보딩이 끝난 직후). ctx는 메시지 활동일 수도, 카드 액션(invoke) 활동일 수도 있다 — 둘 다
    `activity.from_`/`activity.team`이 있어서 `_get_team_key`가 그대로 동작한다."""
    config = _get_config(_get_team_key(ctx))
    if config is None:
        await ctx.send("아직 연결된 설정이 없어요.\n'설정'이라고 입력해서 먼저 연결해줄래요?")
        return
    try:
        reply_text = await claim_issue(config, issue_number, github_id)
    except Exception as exc:
        traceback.print_exc()
        reply_text = f"⚠️ 처리 중 오류가 발생했어요: {exc}"
    await ctx.send(reply_text)


def _build_claim_card(issues: list[dict]) -> AdaptiveCard:
    """`2`번 결과에 딸려오는 "담당자 없는 이슈" 카드. 버튼 클릭이 곧 클레임 요청이라
    1~4 메뉴 숫자 입력이랑 겹칠 일이 아예 없다 (예전엔 다음 메시지의 숫자를 가로채는 방식이라
    메뉴 이동이랑 충돌했었음).

    (임시 실험) 메뉴 카드와 같은 이유로 Action.Submit을 쓴다 — Playground에서 Action.Execute
    클릭이 요청 자체가 안 나가는 문제 때문. handle_message()가 Submit 결과를 감지해서 처리한다.
    """
    return AdaptiveCard(
        body=[TextBlock(text="🙋 담당자 없는 이슈 — 맡고 싶은 걸 눌러주세요", wrap=True, weight="Bolder")],
        actions=[
            SubmitAction(
                title=f"#{i['number']} {i['title']}"[:40],
                data={"action": "claim_issue", "issue_number": i["number"]},
            )
            for i in issues
        ]
        + [SubmitAction(title="🚪 나가기", data={"action": "dismiss_claim_card"})],
    )


async def _handle_claim_click(ctx: "ActivityContext", issue_number: int) -> None:
    """클레임 카드의 "맡기" 버튼이 눌렸을 때 공통으로 처리한다 — Action.Execute(invoke)든
    Action.Submit(message)이든 이 함수로 모인다."""
    aad_id = ctx.activity.from_.aad_object_id
    config = _get_config(_get_team_key(ctx))
    if config is None:
        await ctx.send("아직 연결된 설정이 없어요.\n'설정'이라고 입력해서 먼저 연결해줄래요?")
        return

    github_id = _get_github_id(aad_id)
    if github_id is None:
        github_onboarding_sessions[aad_id] = {"kind": "claim", "issue_number": issue_number}
        await ctx.send(_ASK_GITHUB_ID_TEXT)
        return

    await _claim_and_reply(ctx, issue_number, github_id)


@app.on_card_action_execute("dismiss_claim_card")
async def handle_dismiss_claim_card(
    ctx: ActivityContext[AdaptiveCardInvokeActivity],
) -> AdaptiveCardActionMessageResponse:
    """클레임 카드의 "나가기" 버튼 — 아무것도 안 하고 그냥 닫는다.
    (실제 Teams에서 Action.Execute가 쓰일 경우를 위해 남겨둠 — Playground에서는 Action.Submit
    경로(handle_message)가 대신 처리한다.)"""
    return AdaptiveCardActionMessageResponse(value="알겠어요, 넘어갈게요.")


@app.on_card_action_execute("claim_issue")
async def handle_claim_card_action(
    ctx: ActivityContext[AdaptiveCardInvokeActivity],
) -> AdaptiveCardActionMessageResponse:
    """카드의 "맡기" 버튼을 눌렀을 때 호출된다(Action.Execute invoke 경로).
    (실제 Teams에서 Action.Execute가 쓰일 경우를 위해 남겨둠 — Playground에서는 Action.Submit
    경로(handle_message)가 대신 처리한다.) 실제 결과는 `_handle_claim_click`가 대화에
    메시지를 보내고, 여기서는 클릭 자체가 접수됐다는 짧은 토스트만 돌려준다."""
    _fix_playground_invoke_service_url(ctx)
    await ctx.reply(TypingActivityInput())
    issue_number = ctx.activity.value.action.data["issue_number"]
    await _handle_claim_click(ctx, issue_number)
    return AdaptiveCardActionMessageResponse(value="처리했어요")


async def _handle_github_onboarding_message(ctx: ActivityContext[MessageActivity], raw_text: str) -> None:
    aad_id = ctx.activity.from_.aad_object_id
    session = github_onboarding_sessions[aad_id]

    if "pending_confirm" in session:
        # 직전에 "숫자 한 글자 아이디 맞아요?"라고 확인 중이었으면, 이번 메시지는 그 답으로 처리한다.
        answer = raw_text.strip().lower()
        if answer in _YES_WORDS:
            github_id = session.pop("pending_confirm")
        elif answer in _NO_WORDS:
            del session["pending_confirm"]
            await ctx.send("그럼 정확한 GitHub 아이디를 다시 보내줄래요?")
            return
        else:
            await ctx.send("⚠️ '네' 또는 '아니요'로 답해줄래요?")
            return
    else:
        github_id = _extract_github_id(raw_text)
        if github_id is None:
            await ctx.send("⚠️ GitHub 아이디 형식이 아니에요. (예: thdwlndjs)")
            return

        if len(github_id) == 1 and github_id.isdigit():
            # 숫자 한 글자는 실존할 수도 있지만(GitHub는 1글자 아이디 허용), 메뉴 번호를 실수로
            # 잘못 보낸 경우가 훨씬 흔해서 존재 확인 대신 사람에게 한 번 되묻는다.
            session["pending_confirm"] = github_id
            await ctx.send(f"GitHub 아이디가 흔치 않은 형태인데, 정말 `{github_id}`가 맞으세요? (네/아니요)")
            return

    del github_onboarding_sessions[aad_id]
    _set_github_id(aad_id, github_id)
    await ctx.send(f"연결 완료! GitHub 아이디: `{github_id}`")

    if session["kind"] == "claim":
        await _claim_and_reply(ctx, session["issue_number"], github_id)
    else:
        await _run_action(ctx, session["raw_text"])


def _cancel_pending_sessions(aad_id: str) -> bool:
    """진행 중이던 상태(설정 마법사 / GitHub 아이디 온보딩)를 전부 지운다.
    뭔가 하나라도 지웠으면 True."""
    cancelled = False
    if aad_id in setup_sessions:
        del setup_sessions[aad_id]
        cancelled = True
    if aad_id in github_onboarding_sessions:
        del github_onboarding_sessions[aad_id]
        cancelled = True
    return cancelled


# Teams 채팅에선 Esc 키 같은 걸 감지할 수 없어서, 텍스트로 취소하는 명령어를 따로 둔다.
_CANCEL_TRIGGERS = {"나가기", "취소", "x", "9"}


@app.on_message
async def handle_message(ctx: ActivityContext[MessageActivity]):
    _register_conversation(ctx)
    await ctx.reply(TypingActivityInput())

    # (임시 실험) Action.Submit으로 바꾼 카드 버튼들(메뉴/클레임) 처리용. Action.Submit 클릭은
    # invoke가 아니라 이 메시지 활동으로 오고, 카드에 실었던 data가 그대로 activity.value에 담겨 온다.
    submit_value = ctx.activity.value
    if isinstance(submit_value, dict):
        submit_action = submit_value.get("action")
        if submit_action == "menu" and "choice" in submit_value:
            await _run_action(ctx, str(submit_value["choice"]))
            return
        if submit_action == "claim_issue" and "issue_number" in submit_value:
            await _handle_claim_click(ctx, submit_value["issue_number"])
            return
        if submit_action == "dismiss_claim_card":
            # Action.Execute 시절엔 invoke 응답 자체가 토스트("알겠어요, 넘어갈게요.")를 띄웠는데,
            # Action.Submit은 invoke가 아니라 토스트를 띄울 방법이 없다 — 짧은 메시지로 대신한다.
            await ctx.send("알겠어요, 넘어갈게요.")
            return

    raw_text = (ctx.activity.text or "").strip()
    aad_id = ctx.activity.from_.aad_object_id

    # 진행 중이던 게 있으면 취소하고 빠져나간다. (진행 중인 게 없으면 그냥 아래로 흘려서
    # 평소처럼 "올바른 값을 입력하세요" 등으로 처리되게 둔다.)
    if raw_text.strip().lower() in _CANCEL_TRIGGERS and _cancel_pending_sessions(aad_id):
        await ctx.send("취소했어요. 처음부터 다시 하려면 원하는 메뉴나 '설정'을 입력해주세요.")
        return

    # 설정 마법사 진행 중이면 그쪽으로 라우팅
    if aad_id in setup_sessions:
        await _handle_setup_message(ctx, raw_text)
        return

    # GitHub 아이디 온보딩 진행 중이면 그쪽으로 라우팅
    if aad_id in github_onboarding_sessions:
        await _handle_github_onboarding_message(ctx, raw_text)
        return

    if raw_text in ("설정", "0"):
        await _start_setup(ctx)
        return

    if not re.fullmatch(r"[1-4]", raw_text):
        await ctx.send("올바른 값을 입력하세요. (1~4, 연결 설정 변경은 '설정')")
        return

    await _run_action(ctx, raw_text)


# ── GitHub API 헬퍼 ────────────────────────────────────
# 예전엔 호출마다 httpx.AsyncClient()를 새로 만들어서(타임아웃도 없이) TLS 핸드셰이크가 매번
# 반복됐다. 공유 클라이언트 하나를 재사용하도록 바꾸고(커넥션 풀링), 타임아웃도 Groq 쪽과
# 맞춰 명시한다(GitHub가 응답을 안 주는 경우 무한 대기하던 것 방지). main()에서 서버 종료 시
# _close_github_http_client()로 정리한다.
_github_http_client: httpx.AsyncClient | None = None


def _get_github_http_client() -> httpx.AsyncClient:
    global _github_http_client
    if _github_http_client is None:
        _github_http_client = httpx.AsyncClient(timeout=15)
    return _github_http_client


async def _close_github_http_client() -> None:
    global _github_http_client
    if _github_http_client is not None:
        await _github_http_client.aclose()
        _github_http_client = None


async def _github_get(repo: str, path: str, token: str, params: dict | None = None) -> dict | list[dict]:
    url = f"https://api.github.com/repos/{repo}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    client = _get_github_http_client()
    resp = await client.get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


# (repo, path, params) -> {"data": ..., "expires_at": time.monotonic() 기준 만료 시각}
# 이슈/커밋 "목록" 조회는 캐시가 아예 없어서 버튼 누를 때마다 매번 새로 나갔다(회의 중 여러 명이
# 연달아 2/3/4번을 눌러도 매번 그대로 병목). 짧은 TTL로만 캐시해서 그 상황을 완화한다.
# 클레임 직전의 단일 이슈 조회(`claim_issue`)처럼 "방금 값"이 중요한 곳은 이 캐시를 안 거치고
# `_github_get`을 그대로 부른다 — 안 그러면 두 사람이 거의 동시에 같은 이슈를 맡으려 할 때
# 캐시된 "담당자 없음" 상태 때문에 중복 배정될 위험이 있다.
_github_get_cache: dict[tuple, dict] = {}
_GITHUB_GET_CACHE_TTL_SECONDS = 30


async def _github_get_cached(
    repo: str, path: str, token: str, params: dict | None = None, ttl: float = _GITHUB_GET_CACHE_TTL_SECONDS
) -> dict | list[dict]:
    key = (repo, path, tuple(sorted((params or {}).items())))
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
    client = _get_github_http_client()
    resp = await client.patch(url, headers=headers, json=json_body)
    resp.raise_for_status()
    return resp.json()


async def _github_graphql(query: str, token: str, variables: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    client = _get_github_http_client()
    resp = await client.post(
        "https://api.github.com/graphql",
        headers=headers,
        json={"query": query, "variables": variables or {}},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {data['errors']}")
    return data["data"]

# ── 노션에서 팀, 프로젝트 정보 가져오기 ───────────────────────────────
def _rich_text(block: dict) -> str:
    content = block.get("rich_text", [])
    return "".join(item.get("plain_text", "") for item in content)


def _map_parallel(fn: Callable[[dict], list[str]], items: list[dict]) -> list[list[str]]:
    """형제 블록/행끼리는 서로 독립이라, 있으면 스레드풀로 동시에 처리한다.

    재귀 호출마다 "자기 자식 수만큼만" 새 풀을 만들어서 map이 끝나면 바로 닫는다 — 하나의 공유
    풀에 재귀적으로 작업을 다시 밀어넣는 방식이 아니라서 데드락 위험이 없다(풀이 꽉 찬 채로 그
    안의 워커가 같은 풀에 또 작업을 넣고 기다리는 상황이 안 생김).
    `pool.map()`은 입력 순서 그대로 결과를 돌려주므로, 문서에 쓰여있는 순서(텍스트 출력 순서)는
    병렬화 전과 동일하게 보존된다.
    """
    if len(items) <= 1:
        return [fn(item) for item in items]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(items)) as pool:
        return list(pool.map(fn, items))

# ── 노션 페이지 블록 재귀 조회 ─────────────────────────────
def _list_child_blocks(block_id: str, notion_client: Client) -> list[dict]:
    blocks: list[dict] = []
    cursor: str | None = None
    while True:
        response = notion_client.blocks.children.list(block_id, start_cursor=cursor)
        blocks.extend(response["results"])
        if not response.get("has_more"):
            return blocks
        cursor = response.get("next_cursor")


# ── 데이터베이스(또는 인라인 데이터베이스 블록)의 행(row) 전체 조회 ─────────
def _query_database_rows(database_id: str, notion_client: Client) -> list[dict]:
    """Notion API 2025-09 버전부터 데이터베이스 자체가 아니라 그 안의 'data source'를
    대상으로 조회해야 한다 (`databases.query`는 더 이상 없고 `data_sources.query`를 씀).
    그래서 먼저 databases.retrieve로 data_source_id를 얻은 뒤 그걸로 조회한다.
    """
    db = notion_client.databases.retrieve(database_id)
    data_source_id = db["data_sources"][0]["id"]

    rows: list[dict] = []
    cursor: str | None = None
    while True:
        response = notion_client.data_sources.query(data_source_id, start_cursor=cursor)
        rows.extend(response["results"])
        if not response.get("has_more"):
            return rows
        cursor = response.get("next_cursor")

# ── 페이지 안에 박힌 인라인 데이터베이스 조회 ─────────────────────
# child_database 블록은 blocks.children.list로는 행(row)이 안 나와서 _query_database_rows로
# 따로 조회해야 하고, 각 행은 그 자체가 페이지라 본문까지 재귀로 읽어야 한다.
def _inline_database_lines(database_id: str, notion_client: Client, depth: int) -> list[str]:
    def _row_lines(row: dict) -> list[str]:
        row_lines: list[str] = []
        title = ""
        for prop in row.get("properties", {}).values():
            if prop.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                break
        if title:
            row_lines.append("  " * depth + title)
        for child in _list_child_blocks(row["id"], notion_client):
            row_lines.extend(_block_text(child, notion_client, depth + 1))
        return row_lines

    # 행(row)끼리는 서로 독립적인 페이지라 병렬 조회 가능 — 순서는 _map_parallel이 보존한다.
    rows = _query_database_rows(database_id, notion_client)
    lines: list[str] = []
    for row_lines in _map_parallel(_row_lines, rows):
        lines.extend(row_lines)
    return lines


# ── 노션 블록을 텍스트로 변환 ─────────────────────────────
def _block_text(block: dict, notion_client: Client, depth: int = 0) -> list[str]:
    block_type = block["type"]
    data = block.get(block_type, {})
    indent = "  " * depth

    if block_type == "child_database":
        # 이 블록 자체는 텍스트가 없고 has_children도 일반 블록처럼 동작 안 해서 별도 경로로 처리한다.
        return _inline_database_lines(block["id"], notion_client, depth)

    if block_type == "child_page":
        # "바로가기"에 걸린 다른 페이지로의 링크 — 그 페이지는 완전히 별개의 문서라, 안으로
        # 들어가서 내용을 끌어오면 안 된다(예: 팀 정보 페이지의 회의록 바로가기가 회의록 전체를
        # 끌고 오던 문제). 제목만 참고용으로 한 줄 보여주고 재귀는 하지 않는다.
        title = block.get("child_page", {}).get("title", "")
        return [indent + f"📄 {title}"] if title else []

    if block_type in {"paragraph", "heading_1", "heading_2", "heading_3", "quote", "callout", "toggle"}:
        text = _rich_text(data)
    elif block_type in {"bulleted_list_item", "numbered_list_item"}:
        text = _rich_text(data)
        prefix = "- " if block_type == "bulleted_list_item" else "1. "
        text = prefix + text
    elif block_type == "to_do":
        checked = "[x]" if data.get("checked") else "[ ]"
        text = f"{checked} {_rich_text(data)}"
    elif block_type == "code":
        text = _rich_text(data)
    elif block_type == "divider":
        text = "---"
    else:
        text = _rich_text(data)

    lines = [indent + text] if text else []
    if block.get("has_children"):
        # 형제 블록끼리는 서로 독립이라 병렬 조회 가능 — 순서는 _map_parallel이 보존한다.
        children = _list_child_blocks(block["id"], notion_client)
        for child_lines in _map_parallel(lambda c: _block_text(c, notion_client, depth + 1), children):
            lines.extend(child_lines)
    return lines

# ── 노션 페이지 전체 텍스트 가져오기 (page_id 지정) ─────────────────────
def _fetch_notion_page_by_id(page_id: str, notion_client: Client) -> str:
    page = notion_client.pages.retrieve(page_id)
    blocks = _list_child_blocks(page_id, notion_client)
    lines: list[str] = []
    for block_lines in _map_parallel(lambda b: _block_text(b, notion_client), blocks):
        lines.extend(block_lines)

    title = ""
    properties = page.get("properties", {})
    for property_data in properties.values():
        if property_data.get("type") == "title":
            title = "".join(item.get("plain_text", "") for item in property_data.get("title", []))
            break

    return "\n".join(([title] if title else []) + lines) or "Notion 페이지에 표시할 내용이 없습니다."

# ── 팀/프로젝트 정보 원문 요약 (Groq) ─────────────────────
async def _summarize_project_info(raw_text: str, on_chunk: Callable[[str], None] | None = None) -> str:
    """팀/프로젝트 정보 페이지 원문을 핵심만 간결하게 요약한다.

    GROQ_API_KEY가 없거나 호출에 실패하면 원문을 그대로 돌려준다(폴백).
    on_chunk가 있으면 Groq가 토큰을 생성하는 대로 그때그때 넘겨준다(스트리밍 UI용) — 없으면
    그냥 다 모아서 마지막에 한 번에 반환하는 것과 동작은 동일하다.
    """
    if not GROQ_API_KEY or not raw_text.strip():
        return raw_text

    system_prompt = (
        "너는 팀 소개/프로젝트 정보 페이지 원문을 읽고 핵심만 간결하게 요약하는 도우미야.\n"
        "팀 이름, 팀원과 역할, 프로젝트명과 목적, 현재 진행 단계 위주로 정리해.\n"
        "'[프로젝트명 입력]'처럼 아직 안 채워진 항목은 '미정'으로 짧게만 표시하거나 생략해.\n"
        "팀 규칙/협업 방식 같은 formality는 한두 줄로만 축약하고, 전체 5~10줄 이내로 정리해."
    )
    try:
        full_text = ""
        async with httpx.AsyncClient(timeout=15) as client:
            async with client.stream(
                "POST",
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": GROQ_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": raw_text},
                    ],
                    "temperature": 0.2,
                    "stream": True,
                },
            ) as resp:
                resp.raise_for_status()
                # OpenAI 호환 SSE 포맷: "data: {...}\n\n" 줄이 연속으로 오다가 "data: [DONE]"으로 끝남.
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):].strip()
                    if payload == "[DONE]":
                        break
                    delta = json.loads(payload)["choices"][0]["delta"].get("content", "")
                    if delta:
                        full_text += delta
                        if on_chunk:
                            on_chunk(delta)
        return full_text.strip()
    except Exception:
        traceback.print_exc()
        return raw_text  # 요약 실패 시 원문 그대로 폴백 (예전 동작과 동일)


# ── 노션 페이지 조회를 비동기적으로 수행 ─────────────────────────────
async def _get_project_summary(
    page_id: str,
    notion_client: Client,
    on_stage: Callable[[str], None] | None = None,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    """노션 페이지를 읽어 요약 텍스트를 돌려준다.

    "바뀐 게 있나"부터 가볍게 확인한다 — 페이지 메타데이터의 `last_edited_time`만 보는
    `pages.retrieve` 호출 1번(가벼움)으로 끝내고, 지난번 요약 때랑 그 값이 같으면 **무거운
    재귀 블록 조회(N+1)도, Groq 호출도 둘 다 건너뛰고** 캐시된 요약을 바로 돌려준다.
    실제로 내용이 바뀌었을 때만 전체 조회 → 요약을 새로 한다.

    설정 마법사에서 이 페이지를 등록하는 순간 `_prewarm_project_summary`가 미리 한 번 채워두므로,
    보통 실제 사용자가 '1'번을 처음 눌러도 이미 캐시가 있는 상태다.

    on_stage/on_chunk는 Teams 쪽(ctx.stream) 결합 없이 진행 상황만 알려주는 순수 콜백이다
    (호출부가 스트리밍 UI에 연결할지 말지는 여기서 신경 안 씀).
    """
    if on_stage:
        on_stage("🔍 변경 여부를 확인하는 중...")
    page_meta = await asyncio.to_thread(notion_client.pages.retrieve, page_id)
    last_edited = page_meta.get("last_edited_time")

    cached = _project_summary_cache.get(page_id)
    if cached and cached["last_edited"] == last_edited:
        if on_chunk:
            on_chunk(cached["summary"])  # 캐시 적중이라 토큰 단위론 못 흘리지만, 스트림엔 채워줘야 함
        return cached["summary"]

    if on_stage:
        on_stage("🔍 프로젝트 정보를 가져오는 중...")
    raw_text = await asyncio.to_thread(_fetch_notion_page_by_id, page_id, notion_client)

    if on_stage:
        on_stage("🧠 요약 정리하는 중...")
    text = await _summarize_project_info(raw_text, on_chunk=on_chunk)
    _project_summary_cache[page_id] = {"last_edited": last_edited, "summary": text}
    return text


async def _prewarm_project_summary(page_id: str, notion_client: Client) -> None:
    """설정 마법사에서 팀/프로젝트 정보 페이지를 막 등록한 직후 백그라운드로 미리 요약해서
    캐시를 채워둔다. 실패해도 무시 — 실제로 '1'번을 눌렀을 때 다시 시도되니 사용자에게 보여줄
    에러가 아니다."""
    try:
        await _get_project_summary(page_id, notion_client)
    except Exception:
        traceback.print_exc()


async def fetch_project_info(
    config: dict,
    on_stage: Callable[[str], None] | None = None,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    notion_client = _notion_client_for(config)
    return await _get_project_summary(config["notion_page_id"], notion_client, on_stage, on_chunk)


# ── 회의록 원문 → 확정 태스크 / 미정 사안 분류 (Groq, 무료 티어) ─────────────────────
async def _classify_meeting_notes(note_lines: list[str], cache_key: str) -> dict[str, list[str]]:
    """회의록 원문을 두 갈래로 분류한다.

    - confirmed: 담당자·할 일이 정해진 확정 태스크 ('담당자: 할 일' 형식, 이슈 번호(#123) 유지)
    - undecided: 아직 결정이 안 나고 논의만 된 사안 (담당/기한 미정 등)

    GROQ_API_KEY가 없거나 호출에 실패하면 둘 다 빈 리스트를 돌려주고,
    호출부에서 원문 그대로 보여주는 방식으로 폴백한다.

    cache_key(보통 meeting_notes_page_id)별로 마지막에 Groq에 보낸 원문 해시를 저장해두고,
    이번 원문이 그때랑 똑같으면(해시 동일) Groq를 다시 부르지 않고 저장된 결과를 재사용한다 —
    회의록이 실제로 안 바뀌었는데 `2`번을 여러 번 눌러도 무료 티어 요청 한도를 아낄 수 있다.
    """
    empty = {"confirmed": [], "undecided": []}
    if not GROQ_API_KEY or not note_lines:
        return empty

    raw_text = "\n".join(note_lines)
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    cached = _meeting_notes_classification_cache.get(cache_key)
    if cached and cached["hash"] == content_hash:
        return cached["result"]

    system_prompt = (
        "너는 회의록 원문을 읽고 두 갈래로 분류하는 도우미야.\n"
        "1) [확정] — 담당자와 할 일이 명확히 정해진 것. '담당자: 할 일' 형식으로 한 줄씩 적어. "
        "이슈 번호(#123) 같은 참조가 원문에 있으면 그대로 유지해.\n"
        "2) [미정] — 담당자나 방향이 아직 안 정해져서 논의만 되고 결론이 안 난 사안. 한 줄씩 적어.\n"
        "반드시 아래 형식 그대로만 출력하고, 다른 설명이나 인사말은 절대 붙이지 마:\n"
        "[확정]\n"
        "(항목들, 없으면 비워둠)\n"
        "[미정]\n"
        "(항목들, 없으면 비워둠)"
    )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": GROQ_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": raw_text},
                    ],
                    "temperature": 0.2,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        summary_text = data["choices"][0]["message"]["content"].strip()
    except Exception:
        traceback.print_exc()
        return empty

    # "[확정]"/"[미정]" 마커로 분리해서 파싱. 마커가 없으면 전부 확정으로 취급(폴백).
    confirmed_part, _, rest = summary_text.partition("[미정]")
    confirmed_part = confirmed_part.replace("[확정]", "")

    def _clean_lines(text: str) -> list[str]:
        lines = [line.strip("-• ").strip() for line in text.splitlines()]
        return [line for line in lines if line]

    result = {"confirmed": _clean_lines(confirmed_part), "undecided": _clean_lines(rest)}
    _meeting_notes_classification_cache[cache_key] = {"hash": content_hash, "result": result}
    return result


# ── GitHub 커밋에서 실제로 손댄 이슈 번호 뽑기 ─────────────────────
async def _get_commit_touched_issues(repo: str, token: str) -> set[int]:
    """최근 커밋 메시지에 언급된 이슈 번호 모음.

    이슈가 open이어도 관련 커밋이 하나도 없으면 "아직 착수 전"이고,
    커밋이 있으면 "실제로 진행중"이라는 걸 구분하는 데 쓴다.
    """
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
    """회의록에서 뽑힌 "확정" 태스크들을, 연결된 GitHub 이슈 상태 + 커밋 유무로
    완료/진행중/대기/미등록 넷으로 나눈다.

    외부 I/O가 전혀 없는 순수 함수라(Notion/GitHub/Groq 다 안 건드림) 이 분류 규칙만 따로
    테스트하기 쉽다 — `fetch_team_status`는 조회 결과를 이 함수에 넘기고 포맷팅만 한다.
    """
    done: list[str] = []
    in_progress: list[str] = []
    not_started: list[str] = []
    unlinked: list[str] = []

    for task in confirmed[:15]:  # 너무 길어지지 않게 최대 15개
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


# ── 2. 팀 진행상황 조회 ─────────────────────────────
async def fetch_team_status(config: dict) -> tuple[str, list[dict]]:
    """최근 회의록을 [확정 태스크] / [정해야 할 사안]으로 나누고,
    확정 태스크는 GitHub 이슈 상태 + 실제 커밋 유무까지 교차 확인해서
    "완료 / 진행중(커밋 있음) / 대기(이슈만 있고 착수 전)"까지 구분해 보여준다.

    반환값: (표시할 텍스트, 담당자 없는 open 이슈 목록 — GitHub 이슈 dict 그대로).
    호출부가 이 목록으로 "맡기" 버튼이 달린 카드를 따로 만들어 붙인다 (번호를 텍스트로
    다시 입력받는 방식은 1~4 메뉴 숫자랑 겹치는 문제가 있어서 카드 버튼으로 바꿨다).
    """
    notion_client = _notion_client_for(config)
    github_token = config["github_token"]
    repo = config["github_repo"]

    # 회의록 조회와 이슈 목록 조회는 서로 값을 안 주고받는 독립 호출이라 동시에 실행한다
    # (둘 다 아래 로직에서 무조건 쓰이므로 순서를 바꿔도 스킵되는 경우가 없어 동작은 그대로다).
    async def _fetch_notes() -> list[str]:
        try:
            return await asyncio.to_thread(
                _fetch_meeting_notes_lines, config["meeting_notes_page_id"], notion_client
            )
        except Exception:
            return []

    async def _fetch_issues() -> dict | list[dict]:
        # 반환 타입은 _github_get 시그니처 그대로 맞춘 것 — 이 엔드포인트는 실제로 항상 list를 주지만,
        # 그건 리팩터 전에도 마찬가지였던 기존 타입 느슨함이라 여기서 새로 좁히지 않는다.
        return await _github_get_cached(repo, "/issues", github_token, {"state": "all", "per_page": 30})

    note_lines, issues = await asyncio.gather(_fetch_notes(), _fetch_issues())
    note_lines = [line.strip() for line in note_lines if line.strip()]
    issue_state_by_number: dict[int, str] = {
        i["number"]: i["state"] for i in issues if "pull_request" not in i
    }

    unassigned_open = [
        i for i in issues
        if "pull_request" not in i and i["state"] == "open" and not (i.get("assignees") or [])
    ][:10]
    unassigned_hint = ["", "🙋 담당자 없는 이슈가 있어요 — 아래 카드에서 맡을 수 있어요"] if unassigned_open else []

    if not note_lines:
        # 회의록이 아직 없으면 GitHub 이슈 목록으로 폴백
        by_assignee: dict[str, list[str]] = {}
        for i in issues:
            if "pull_request" in i:
                continue
            assignees = i.get("assignees") or [{"login": "미배정"}]
            for a in assignees:
                by_assignee.setdefault(a["login"], []).append(f"{i['title']} [{i['state']}]")
        lines = [f"{name}: {', '.join(tasks)}" for name, tasks in by_assignee.items()]
        result = ["👥 팀 진행상황", "\n".join(lines) if lines else "(회의록도 없고 등록된 이슈도 없어요)"]
        return "\n".join(result + unassigned_hint), unassigned_open

    classified = await _classify_meeting_notes(note_lines, config["meeting_notes_page_id"])
    confirmed, undecided = classified["confirmed"], classified["undecided"]

    if not confirmed and not undecided:
        # 분류 실패(키 없음/API 오류) 시에는 회의록 원문으로 폴백
        text = "👥 팀 진행상황 (요약 실패, 회의록 원문)\n" + "\n".join(f"- {line}" for line in note_lines[-10:])
        return "\n".join([text] + unassigned_hint), unassigned_open

    commit_touched = await _get_commit_touched_issues(repo, github_token)
    classified_tasks = _classify_tasks_by_github_state(confirmed, issue_state_by_number, commit_touched)
    done = classified_tasks["done"]
    in_progress = classified_tasks["in_progress"]
    not_started = classified_tasks["not_started"]
    unlinked = classified_tasks["unlinked"]

    result = ["👥 팀 진행상황 (회의록 기준)"]
    if done:
        result += ["", "✅ 완료", *[f"- {t}" for t in done]]
    if in_progress:
        result += ["", "🔨 진행중 (커밋 있음)", *[f"- {t}" for t in in_progress]]
    if not_started:
        result += ["", "📋 대기 (이슈만 등록, 착수 전)", *[f"- {t}" for t in not_started]]
    if unlinked:
        result += ["", "🆕 이슈 미등록 (회의에서만 언급됨)", *[f"- {t}" for t in unlinked]]
    if undecided:
        result += ["", "🤔 아직 정해야 할 사안", *[f"- {t}" for t in undecided[:10]]]
    result += unassigned_hint

    return "\n".join(result), unassigned_open


def _page_property_text(properties: dict, prop_type: str) -> list[str]:
    """Notion 페이지 속성 dict에서 주어진 타입(title/rich_text)의 텍스트를 전부 뽑는다."""
    texts: list[str] = []
    for prop in properties.values():
        if prop.get("type") != prop_type:
            continue
        for item in prop.get(prop_type, []):
            plain = item.get("plain_text", "")
            if plain:
                texts.append(plain)
    return texts


def _fetch_meeting_notes_lines(meeting_notes_page_id: str, notion_client: Client) -> list[str]:
    """회의록 페이지에서 항목별 텍스트 줄을 가져온다.

    회의록 페이지가 풀페이지 데이터베이스(제목/Summary/날짜 컬럼)인 경우 각 row를,
    일반 페이지인 경우 블록 텍스트를 사용한다.
    """
    try:
        rows = _query_database_rows(meeting_notes_page_id, notion_client)
    except Exception:
        # 데이터베이스가 아니면(혹은 접근 불가) 일반 페이지 블록 방식으로 폴백
        try:
            return _fetch_notion_page_by_id(meeting_notes_page_id, notion_client).splitlines()
        except Exception:
            return []

    lines: list[str] = []
    for row in rows:
        properties = row.get("properties", {})
        title_parts = _page_property_text(properties, "title")
        summary_parts = _page_property_text(properties, "rich_text")
        row_text = " / ".join(p for p in (" ".join(title_parts), " ".join(summary_parts)) if p)
        if row_text:
            lines.append(row_text)
    return lines


# ── 3. 내 역할 조회 ─────────────────────────────
async def fetch_my_role(github_id: str | None, display_name: str | None, config: dict) -> str:
    """GitHub에서 나에게 배정된(open) 이슈 + 회의록에서 내 이름이 언급된 항목을 합쳐서 보여준다.

    회의록 줄에 이미 GitHub 목록에 있는 이슈 번호(#123)가 포함돼 있으면
    같은 작업으로 보고 회의록 쪽은 중복 표시하지 않는다.
    """
    github_token = config["github_token"]
    notion_client = _notion_client_for(config)

    # GitHub 이슈 조회와 회의록 조회는 서로 독립적이라(github_id/display_name 여부만으로
    # 각자 스킵 여부가 정해짐 — 서로의 결과에 의존하지 않음) 동시에 실행한다.
    async def _fetch_github_lines() -> tuple[list[str], set[int]]:
        if not github_id:
            return [], set()
        issues = await _github_get_cached(
            config["github_repo"], "/issues", github_token, {"assignee": github_id, "state": "open", "per_page": 30}
        )
        lines: list[str] = []
        numbers: set[int] = set()
        for issue in issues:
            if "pull_request" in issue:  # PR은 이슈 목록에 섞여 나오므로 제외
                continue
            lines.append(f"- {issue['title']} (#{issue['number']})")
            numbers.add(issue["number"])
        return lines, numbers

    async def _fetch_note_lines() -> list[str]:
        if not display_name:
            return []
        try:
            return await asyncio.to_thread(
                _fetch_meeting_notes_lines, config["meeting_notes_page_id"], notion_client
            )
        except Exception:
            return []

    (github_lines, github_issue_numbers), raw_note_lines = await asyncio.gather(
        _fetch_github_lines(), _fetch_note_lines()
    )
    if not github_lines:
        github_lines.append("- (배정된 GitHub 이슈가 없어요)")

    # display_name이 없으면 _fetch_note_lines()가 이미 빈 리스트를 돌려주므로 동작은 동일하다 —
    # 이 if는 순수하게 display_name의 None 배제를 되살려서 타입체커 내로잉을 복원하기 위함.
    meeting_lines: list[str] = []
    if display_name:
        for line in raw_note_lines:
            stripped = line.strip()
            if not stripped or display_name not in stripped:
                continue
            referenced = {int(m.group(1)) for m in _ISSUE_REF_RE.finditer(stripped)}
            if referenced & github_issue_numbers:
                continue  # GitHub 목록과 같은 작업이면 중복 제거
            meeting_lines.append(f"- {stripped}")

    result = ["👤 내 역할", "📌 GitHub 담당 이슈", *github_lines]
    if meeting_lines:
        result += ["📝 회의록 언급", *meeting_lines]
    return "\n".join(result)


# ── 4. 완료된 작업 공유 ─────────────────────────────
async def _find_project_status_field(
    owner: str, repo_name: str, project_number: int, token: str
) -> tuple[str, str, str] | None:
    """project_number로 지정된 Project에서 'Status'(또는 '상태') 필드와 'Done'(또는 '완료') 옵션을 찾는다.

    이 구성은 팀이 Project 보드를 바꾸지 않는 한 거의 안 변해서, (레포, 프로젝트 번호) 기준으로
    한번 찾은 결과를 계속 재사용한다 — GraphQL 호출을 매번 반복할 필요가 없다.
    """
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
    data = await _github_graphql(
        query, token, {"owner": owner, "repo": repo_name, "number": project_number}
    )
    project = data["repository"]["projectV2"]
    result: tuple[str, str, str] | None = None
    if project:
        for field in project["fields"]["nodes"]:
            if field.get("name") not in ("Status", "상태"):
                continue
            for option in field.get("options", []):
                if option["name"] in ("Done", "완료"):
                    result = (project["id"], field["id"], option["id"])
                    break
            if result:
                break

    _project_status_field_cache[cache_key] = result
    return result


async def _find_project_item_id(
    owner: str, repo_name: str, project_id: str, issue_number: int, token: str
) -> str | None:
    """이슈가 Project에 한번 들어가면 아이템 ID도 거의 안 바뀌어서 (project_id, 이슈번호) 기준으로 캐시한다.
    캐시가 틀려도(이슈가 Project에서 빠졌다 다시 들어간 경우) 호출부의 "건너뜀" 처리로 안전하게 실패한다.
    """
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
    data = await _github_graphql(
        query, token, {"owner": owner, "repo": repo_name, "number": issue_number}
    )
    issue = data["repository"]["issue"]
    result = None
    if issue:
        for item in issue["projectItems"]["nodes"]:
            if item["project"]["id"] == project_id:
                result = item["id"]
                break

    _project_item_id_cache[cache_key] = result
    return result


async def _set_project_item_status(
    project_id: str, field_id: str, option_id: str, item_id: str, token: str
) -> None:
    mutation = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
      updateProjectV2ItemFieldValue(
        input: { projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: { singleSelectOptionId: $optionId } }
      ) { projectV2Item { id } }
    }
    """
    await _github_graphql(
        mutation,
        token,
        {"projectId": project_id, "itemId": item_id, "fieldId": field_id, "optionId": option_id},
    )


async def share_completed_work(config: dict, github_id: str) -> str:
    """호출한 사람이 담당한 이슈 중, 최근 커밋에서 언급된 것만 찾아 GitHub Project 상태를 'Done'으로 바꾼다.

    (레포 전체가 아니라 호출자 본인 담당 이슈로만 범위를 좁힌다 — 이건 팀 전체가 아니라
    각자 자기 작업을 끝냈을 때 개인적으로 누르는 버튼이라서.)
    """
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

    lines = ["✅ 완료된 작업으로 반영됨"]
    lines.append(", ".join(updated) if updated else "(변경된 항목 없음)")
    if skipped:
        lines.append(f"⏭️ Project에 없어서 건너뜀: {', '.join(skipped)}")
    return "\n".join(lines)


# ── 담당자 없는 이슈 self-claim ─────────────────────────────
# `2`번(진행상황 조회) 결과에 딸려오는 카드의 "맡기" 버튼을 눌렀을 때 호출된다
# (handle_claim_card_action 참고 — 예전엔 텍스트로 번호를 다시 받는 방식이었는데 메뉴 숫자랑 겹쳐서 카드로 바꿨다).
async def claim_issue(config: dict, issue_number: int, github_id: str) -> str:
    """담당자가 없는 이슈에 호출한 사람을 GitHub assignee로 지정한다."""
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


# ── n8n → 봇: 개인 DM 발송 엔드포인트 ──────────────────────
class NotifyPayload(BaseModel):
    github_id: str
    message: str


@fastapi_app.post("/api/notify/user")
async def notify_user(payload: NotifyPayload, x_n8n_secret: str = Header(None)):
    if x_n8n_secret != N8N_SHARED_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")

    conversation_id = conversation_store.get(payload.github_id)
    if not conversation_id:
        # 아직 이 사용자가 봇과 대화를 시작한 적이 없어 proactive 발송 불가
        raise HTTPException(status_code=404, detail="conversation not found for user")

    await app.send(conversation_id, MessageActivity(text=payload.message))
    return {"status": "sent"}


async def main():
    await app.initialize()

    import uvicorn
    config = uvicorn.Config(app=fastapi_app, host="0.0.0.0", port=3978)
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await _close_github_http_client()


if __name__ == "__main__":
    asyncio.run(main())
