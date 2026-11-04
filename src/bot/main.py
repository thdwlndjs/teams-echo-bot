"""에코봇 — Teams 입력단.

하는 일은 세 가지뿐이다.
1. Teams에서 메시지/카드 버튼을 받는다.
2. 누가(aad_id, 이름), 어디서(팀), 무엇을(text, 버튼 data) 보냈는지 JSON으로 묶어 서버 `/api/turn`에 보낸다.
3. 서버가 흘려보내는 이벤트(status/chunk/message/buttons/done)를 Teams 화면으로 옮긴다.

설정 마법사, 메뉴 판단, GitHub/Notion/Groq 호출, 저장소, 캐시는 전부 서버에 있다.
그래서 이 봇은 상태를 하나도 들고 있지 않다 — 재시작해도 잃는 게 없다.
"""

import json
import logging
import os
import time

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI

from microsoft_teams.api import ConversationUpdateActivity, MessageActivity, TypingActivityInput
from microsoft_teams.api.activities.invoke.adaptive_card.action import AdaptiveCardInvokeActivity
from microsoft_teams.api.models.adaptive_card.adaptive_card_action_response import (
    AdaptiveCardActionMessageResponse,
)
from microsoft_teams.apps import ActivityContext, App, FastAPIAdapter
from microsoft_teams.cards import AdaptiveCard, SubmitAction, TextBlock

# ── 설정 ────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
load_dotenv(os.environ.get("ENV_FILE"))
BOT_SERVER_URL = os.environ.get("BOT_SERVER_URL", "http://localhost:8000").rstrip("/")
BOT_SERVER_SECRET = os.environ["BOT_SERVER_SECRET"]  # 서버 .env와 같은 값
BOT_PORT = int(os.environ.get("BOT_PORT", "3978"))

_SERVER_DOWN_TEXT = "⚠️ 서버에 연결하지 못했어요. 잠시 후 다시 시도해줄래요?"
# 서버는 작업 중이면 15초마다 ping을 보내고, 한 요청을 100초 안에 끝낸다. 그보다 오래 걸리면 서버가 멈춘 것으로 본다.
BOT_TURN_DEADLINE_SECONDS = float(os.environ.get("BOT_TURN_DEADLINE_SECONDS", "150"))

fastapi_app = FastAPI()
adapter = FastAPIAdapter(app=fastapi_app)
app = App(http_server_adapter=adapter)


# ── (로컬 전용 우회책) Playground 카드 invoke의 serviceUrl에 "/_connector"가 빠지는 문제 ──
_UNAUTHENTICATED_LOCAL_DEV = os.environ.get("DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS", "").lower() == "true"


def _fix_playground_invoke_service_url(ctx: ActivityContext) -> None:
    if not _UNAUTHENTICATED_LOCAL_DEV:
        return
    ref = ctx.conversation_ref
    if ref.service_url and "/_connector" not in ref.service_url:
        ref.service_url = ref.service_url.rstrip("/") + "/_connector"
        ctx.stream = ctx._activity_sender.create_stream(ref)  # noqa: SLF001 — 로컬 전용 우회책


# ── 서버 연결 ────────────────────────────────────────────
_server_client: httpx.AsyncClient | None = None


def _get_server_client() -> httpx.AsyncClient:
    global _server_client
    if _server_client is None:
        # read 타임아웃은 "다음 이벤트가 올 때까지" 기다리는 시간이다. 서버가 작업 중이면 15초마다 ping을
        # 보내므로, 이 시간 동안 아무것도 안 오면 서버가 멈췄거나 연결이 끊긴 것이다.
        _server_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=45.0))
    return _server_client


FILE_DOWNLOAD_INFO = "application/vnd.microsoft.teams.file.download.info"


def _attachments(activity) -> list[dict]:
    """메시지에 붙은 파일(회의록 올리기용). Teams 개인 채팅 파일은 content.downloadUrl(미리 인증된 주소)을 넘긴다.
    Teams가 메시지마다 붙이는 text/html 사본은 뺀다. 내려받기는 서버가 한다."""
    out = []
    for item in getattr(activity, "attachments", None) or []:
        content_type = getattr(item, "content_type", None) or ""
        if content_type.startswith("text/html"):
            continue
        content = getattr(item, "content", None)
        url = None
        if content_type == FILE_DOWNLOAD_INFO:
            url = content.get("downloadUrl") if isinstance(content, dict) else getattr(content, "download_url", None)
        url = url or getattr(item, "content_url", None)
        if url:
            out.append({"name": getattr(item, "name", None), "content_type": content_type, "url": url})
    return out


def _turn_payload(ctx: ActivityContext, kind: str = "message", text: str = "", value: dict | None = None) -> dict:
    activity = ctx.activity
    team = activity.team
    conversation = activity.conversation
    timestamp = getattr(activity, "timestamp", None)
    return {
        "kind": kind,
        "user": {"aad_id": activity.from_.aad_object_id, "name": activity.from_.name},
        "team": {"id": team.id, "name": team.name} if team and team.id else None,
        "text": text,
        "value": value,
        # 서버가 원문을 기록할 때 쓰는 위치 정보 (같은 메시지가 두 번 와도 한 번만 저장되게)
        "conversation": {"id": conversation.id, "type": conversation.conversation_type} if conversation else None,
        "message_id": getattr(activity, "id", None),
        "sent_at": timestamp.isoformat() if hasattr(timestamp, "isoformat") else timestamp,
        "attachments": _attachments(activity) if kind == "message" else [],
    }


def _build_buttons_card(event: dict) -> AdaptiveCard:
    title_kwargs = {"weight": "Bolder"} if event.get("emphasis") else {}
    return AdaptiveCard(
        body=[TextBlock(text=event["title"], wrap=True, **title_kwargs)],
        # Playground에서 Action.Execute 클릭이 안 나가는 문제 때문에 Action.Submit을 쓴다.
        # Submit 클릭은 message 활동으로 오고 data는 activity.value에 실린다 → handle_message가 서버로 전달.
        actions=[SubmitAction(title=b["title"][:40], data=b["data"]) for b in event["buttons"]],
    )


async def relay(ctx: ActivityContext, payload: dict) -> None:
    """서버에 한 턴을 보내고, 돌아오는 이벤트를 Teams 화면으로 옮긴다."""
    stream_open = False
    started = time.monotonic()

    async def close_stream() -> None:
        nonlocal stream_open
        if stream_open:
            await ctx.stream.close()
            stream_open = False

    async def tell_failure() -> None:
        """실패를 사용자에게 꼭 알린다. 열린 스트림에 못 쓰면(만료 등) 새 메시지로 보낸다."""
        nonlocal stream_open
        if stream_open:
            try:
                ctx.stream.emit(_SERVER_DOWN_TEXT)
                await ctx.stream.close()
                stream_open = False
                return
            except Exception:
                logging.exception("스트림으로 실패 안내를 못 보냄 — 새 메시지로 보낸다")
                stream_open = False
        try:
            await ctx.send(_SERVER_DOWN_TEXT)
        except Exception:
            logging.exception("실패 안내 메시지도 보내지 못함")

    try:
        async with _get_server_client().stream(
            "POST", f"{BOT_SERVER_URL}/api/turn", json=payload, headers={"X-Bot-Secret": BOT_SERVER_SECRET}
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                kind = event.get("type")
                if time.monotonic() - started > BOT_TURN_DEADLINE_SECONDS:
                    raise TimeoutError("server kept the turn open too long")
                if kind == "ping":
                    continue  # 서버가 아직 작업 중
                if kind == "status":
                    ctx.stream.update(event["text"])
                    stream_open = True
                elif kind == "chunk":
                    ctx.stream.emit(event["text"])
                    stream_open = True
                elif kind == "message":
                    if stream_open:
                        # "확인하는 중..."을 띄웠던 같은 스트림에 결과를 채워 넣어야 상태 표시가 교체된다.
                        ctx.stream.emit(event["text"])
                        await close_stream()
                    else:
                        await ctx.send(event["text"])
                elif kind == "buttons":
                    await close_stream()
                    await ctx.send(_build_buttons_card(event))
                elif kind == "done":
                    await close_stream()
    except Exception:
        logging.exception("서버 전달 실패")
        await tell_failure()
        return

    await close_stream()


# ── Teams 이벤트 → 서버 전달 ─────────────────────────────
@app.on_conversation_update
async def handle_conversation_update(ctx: ActivityContext[ConversationUpdateActivity]) -> None:
    activity = ctx.activity
    members_added = activity.members_added or []
    bot_id = activity.recipient.id if activity.recipient else None

    # 봇 자신이 추가된 이벤트는 무시하고, 실제 사용자가 추가됐을 때만.
    if not any(member.id != bot_id for member in members_added):
        return
    # 1:1 개인 채팅에서만 메뉴를 보낸다.
    conversation_type = activity.conversation.conversation_type if activity.conversation else None
    if conversation_type != "personal":
        return

    await relay(ctx, _turn_payload(ctx, kind="welcome"))


@app.on_message
async def handle_message(ctx: ActivityContext[MessageActivity]) -> None:
    await ctx.reply(TypingActivityInput())
    value = ctx.activity.value if isinstance(ctx.activity.value, dict) else None
    await relay(ctx, _turn_payload(ctx, text=(ctx.activity.text or "").strip(), value=value))


async def _relay_card_action(ctx: ActivityContext[AdaptiveCardInvokeActivity]) -> AdaptiveCardActionMessageResponse:
    """Action.Execute(invoke) 경로 — 실제 Teams에서 Execute로 돌아갈 때를 위해 남겨둔다."""
    _fix_playground_invoke_service_url(ctx)
    await ctx.reply(TypingActivityInput())
    await relay(ctx, _turn_payload(ctx, value=dict(ctx.activity.value.action.data)))
    return AdaptiveCardActionMessageResponse(value="처리했어요")


@app.on_card_action_execute("menu")
async def handle_menu_card_action(ctx: ActivityContext[AdaptiveCardInvokeActivity]):
    return await _relay_card_action(ctx)


@app.on_card_action_execute("claim_issue")
async def handle_claim_card_action(ctx: ActivityContext[AdaptiveCardInvokeActivity]):
    return await _relay_card_action(ctx)


@app.on_card_action_execute("dismiss_claim_card")
async def handle_dismiss_claim_card(ctx: ActivityContext[AdaptiveCardInvokeActivity]):
    return AdaptiveCardActionMessageResponse(value="알겠어요, 넘어갈게요.")


async def main():
    await app.initialize()

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app=fastapi_app, host="0.0.0.0", port=BOT_PORT))
    try:
        await server.serve()
    finally:
        if _server_client is not None:
            await _server_client.aclose()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
