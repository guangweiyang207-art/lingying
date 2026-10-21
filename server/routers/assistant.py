"""
Assistant session APIs.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

logger = logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, field_validator, model_validator

# pydantic 顶层不 re-export PydanticCustomError，只能直接依赖 pydantic-core。
from pydantic_core import PydanticCustomError

from lib import PROJECT_ROOT
from lib.infra.api_errors import BadRequestError, ConflictError, NotFoundError, ServiceUnavailableError
from server.agent_runtime.failure_observation import build_startup_failure_observation
from server.agent_runtime.models import SessionMeta, WithdrawalIntent
from server.agent_runtime.service import (
    AssistantService,
    InterruptSettleTimeoutError,
    PendingQuestionError,
    QueuedMessagesPendingError,
    RewriteAnchorError,
    RewriteUnavailableError,
    SessionSupersededError,
)
from server.agent_runtime.session_branch import SessionBranchError
from server.agent_runtime.session_manager import (
    AgentStartupError,
    QueuedMessageNotFoundError,
    QueuedMessageWithdrawalPendingError,
    SessionCapacityError,
    UnrecordedMessageError,
)
from server.auth import _verify_and_get_payload_async, is_auth_enabled
from server.i18n import Translator, get_locale

router = APIRouter()
# WS 不挂 router 级 get_current_user：浏览器 WebSocket 握手带不了 Authorization header，
# 鉴权在端点内以建连后的首帧承担（见 ws_router 路由的 docstring）。
ws_router = APIRouter()

assistant_service = AssistantService(project_root=PROJECT_ROOT)

MAX_IMAGE_SOURCE_BYTES = 5 * 1024 * 1024
# 前端把附图缩放重编码到这个预算之内；服务端合计上限按它折算，单张上限仍留 5 MB 作为
# 绕过前端时的兜底。合计按 5 × 1.5 MB 计，base64 约 10 MB，稳在 Messages API 的
# 32 MB 单请求上限（Bedrock 20 MB / Vertex 30 MB）之内。
MAX_RESIZED_IMAGE_SOURCE_BYTES = 3 * 1024 * 1024 // 2
# Base64 needs four characters for every three source bytes, rounded up to a full quartet.
MAX_IMAGE_BASE64_CHARS = 4 * ((MAX_IMAGE_SOURCE_BYTES + 2) // 3)
MAX_RESIZED_IMAGE_BASE64_CHARS = 4 * ((MAX_RESIZED_IMAGE_SOURCE_BYTES + 2) // 3)
MAX_IMAGES_PER_REQUEST = 5
MAX_IMAGES_TOTAL_BASE64_CHARS = MAX_IMAGES_PER_REQUEST * MAX_RESIZED_IMAGE_BASE64_CHARS


def get_assistant_service() -> AssistantService:
    return assistant_service


def agent_startup_failure_detail(
    exc: AgentStartupError,
    *,
    project_name: str,
    session_id: str | None,
    title: str,
) -> dict:
    """把 runtime 启动异常映射为两个 Agent API 共用的结构化错误。"""
    original = exc.__cause__ or exc
    failure = exc.failure_observation or build_startup_failure_observation(
        original,
        project_name=project_name,
        session_id=session_id,
        sdk_stderr=exc.sdk_stderr,
    )
    return {"code": "agent_startup_failed", "message": title, "failure": failure}


async def _validate_session_ownership(
    service: AssistantService, session_id: str, project_name: str, _t: Callable[..., str]
) -> "SessionMeta":
    """Validate session belongs to the specified project and return it."""
    session = await service.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=_t("session_not_found", session_id=session_id))
    if session.project_name != project_name:
        raise HTTPException(status_code=404, detail=_t("session_not_found", session_id=session_id))
    return session


class ImageAttachment(BaseModel):
    data: str = Field(max_length=MAX_IMAGE_BASE64_CHARS)
    media_type: str

    @field_validator("data", mode="before")
    @classmethod
    def validate_data_size(cls, value: object) -> object:
        if isinstance(value, str) and len(value) > MAX_IMAGE_BASE64_CHARS:
            raise PydanticCustomError(
                "assistant_image_too_large", "assistant image exceeds the base64 character budget"
            )
        return value


class ImageRequest(BaseModel):
    images: list[ImageAttachment] = Field(default_factory=list, max_length=MAX_IMAGES_PER_REQUEST)

    @model_validator(mode="before")
    @classmethod
    def validate_total_image_size(cls, value: object) -> object:
        images = value.get("images", []) if isinstance(value, dict) else []
        total_chars = sum(
            len(image.get("data", ""))
            for image in images
            if isinstance(image, dict) and isinstance(image.get("data", ""), str)
        )
        if total_chars > MAX_IMAGES_TOTAL_BASE64_CHARS:
            raise PydanticCustomError(
                "assistant_images_total_too_large",
                "assistant images exceed the total base64 character budget",
            )
        return value


class SendRequest(ImageRequest):
    content: str = ""
    session_id: str | None = None
    # 请求侧幂等键：同键重试返回既有权威条目，不产生重复。
    client_key: str | None = Field(default=None, max_length=128)


class RewriteRequest(ImageRequest):
    # 锚点：被改写的那条用户消息在事件日志里的条目 uuid。
    anchor_entry_uuid: str = Field(min_length=1, max_length=256)
    content: str = ""
    client_key: str | None = Field(default=None, max_length=128)


class AnswerQuestionRequest(BaseModel):
    answers: dict[str, str] = Field(default_factory=dict)


@router.post("/sessions/send")
async def send_message(
    project_name: str,
    req: SendRequest,
    request: Request,
    _t: Translator,
):
    try:
        service = get_assistant_service()
        return await service.send_or_create(
            project_name,
            req.content,
            session_id=req.session_id,
            images=req.images,
            locale=get_locale(request),
            client_key=req.client_key,
        )
    except SessionSupersededError as exc:
        raise ConflictError("session_already_superseded") from exc
    except UnrecordedMessageError as exc:
        raise ConflictError("message_accepted_but_unrecorded") from exc
    except SessionCapacityError as exc:
        raise ServiceUnavailableError("session_capacity_exceeded") from exc
    except FileNotFoundError as exc:
        raise NotFoundError("session_or_project_not_found") from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=_t("sdk_session_timeout")) from exc
    except ValueError as exc:
        # 空消息内容 / 非法项目名等坏请求，str(exc) 只进日志
        logger.warning("会话发送请求非法: %s", exc)
        raise BadRequestError("request_invalid") from exc
    except AgentStartupError as exc:
        raise HTTPException(
            status_code=502,
            detail=agent_startup_failure_detail(
                exc,
                project_name=project_name,
                session_id=req.session_id,
                title=_t("agent_startup_failed_title"),
            ),
        ) from exc
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.post("/sessions/{session_id}/rewrite")
async def rewrite_message(
    project_name: str,
    session_id: str,
    req: RewriteRequest,
    request: Request,
    _t: Translator,
):
    """改写会话中某条历史用户消息：分叉出新会话并在其上重跑。

    ``session_id`` 是被改写的原会话；响应里的 ``session_id`` 是承接改写的新会话，
    前端据此整体切换过去。运行中的会话由端点自动中断，调用方不必先停止。
    """
    try:
        service = get_assistant_service()
        return await service.rewrite_message(
            project_name,
            session_id,
            anchor_entry_uuid=req.anchor_entry_uuid,
            content=req.content,
            images=req.images,
            locale=get_locale(request),
            client_key=req.client_key,
        )
    except RewriteAnchorError as exc:
        raise BadRequestError("rewrite_anchor_invalid") from exc
    except PendingQuestionError as exc:
        raise ConflictError("rewrite_blocked_by_question") from exc
    except QueuedMessagesPendingError as exc:
        raise ConflictError("rewrite_blocked_by_queued_messages") from exc
    except SessionSupersededError as exc:
        raise ConflictError("session_already_superseded") from exc
    except RewriteUnavailableError as exc:
        raise ServiceUnavailableError("rewrite_unavailable") from exc
    except InterruptSettleTimeoutError as exc:
        raise HTTPException(status_code=504, detail=_t("rewrite_interrupt_timeout")) from exc
    except SessionBranchError as exc:
        logger.warning("分支会话建立失败: %s", exc)
        raise HTTPException(status_code=500, detail=_t("rewrite_failed")) from exc
    except SessionCapacityError as exc:
        raise ServiceUnavailableError("session_capacity_exceeded") from exc
    except FileNotFoundError as exc:
        raise NotFoundError("session_or_project_not_found") from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=_t("sdk_session_timeout")) from exc
    except ValueError as exc:
        logger.warning("会话改写请求非法: %s", exc)
        raise BadRequestError("request_invalid") from exc
    except AgentStartupError as exc:
        raise HTTPException(
            status_code=502,
            detail=agent_startup_failure_detail(
                exc,
                project_name=project_name,
                session_id=session_id,
                title=_t("agent_startup_failed_title"),
            ),
        ) from exc
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.get("/sessions")
async def list_sessions(
    project_name: str,
    _t: Translator,
    status: Literal["idle", "running", "completed", "error", "interrupted"] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    try:
        sessions = await get_assistant_service().list_sessions(
            project_name=project_name, status=status, limit=limit, offset=offset
        )
        return {"sessions": [s.model_dump() for s in sessions]}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.get("/sessions/{session_id}")
async def get_session(project_name: str, session_id: str, _t: Translator):
    try:
        service = get_assistant_service()
        session = await _validate_session_ownership(service, session_id, project_name, _t)
        return session.model_dump()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.delete("/sessions/{session_id}")
async def delete_session(project_name: str, session_id: str, _t: Translator):
    try:
        service = get_assistant_service()
        await _validate_session_ownership(service, session_id, project_name, _t)
        deleted = await service.delete_session(session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=_t("session_not_found", session_id=session_id))
        return {"success": True}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.get("/sessions/{session_id}/messages")
async def list_messages(project_name: str, session_id: str, _t: Translator):
    raise HTTPException(
        status_code=410,
        detail=_t("interface_offline"),
    )


@router.get("/sessions/{session_id}/snapshot")
async def get_snapshot(project_name: str, session_id: str, _t: Translator):
    raise HTTPException(
        status_code=410,
        detail=_t("interface_offline"),
    )


@router.get("/sessions/{session_id}/entries")
async def list_entries(
    project_name: str,
    session_id: str,
    _t: Translator,
    after: int = Query(default=-1, ge=-1),
):
    """冷读会话事件日志（历史回放；``after`` 为 seq 游标）。"""
    try:
        service = get_assistant_service()
        meta = await _validate_session_ownership(service, session_id, project_name, _t)
        return await service.list_session_entries(session_id, meta=meta, after_seq=after)
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise NotFoundError("session_not_found", session_id=session_id) from exc
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


# WS 鉴权帧等待上限：建连后客户端须在此窗口内完成首帧 auth，否则按未认证关闭。
WS_AUTH_TIMEOUT_SECONDS = 10

# 服务端主动关闭码：1000 = 流正常结束（订阅被溢出移除等，客户端应重连）；
# 4401 = 鉴权失败；4404 = 会话不存在或不属于该项目；1011 = 服务端内部错误。
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_SESSION_NOT_FOUND = 4404
WS_CLOSE_INTERNAL_ERROR = 1011


async def _ws_close(websocket: WebSocket, code: int) -> None:
    """关闭 WS；对端已断开时 close 会抛 RuntimeError/OSError，静默即可。"""
    with contextlib.suppress(RuntimeError, OSError):
        await websocket.close(code=code)


async def _ws_authenticate(websocket: WebSocket) -> bool:
    """WS 建连后的首帧鉴权。

    浏览器 WebSocket API 无法携带 Authorization header，且会话凭证不落 URL
    （ADR 0071 否决 query param 形态）：客户端建连后先发
    ``{"type": "auth", "token": "<JWT 或 arc- API Key>"}``，超时或校验失败以 4401 关闭。
    校验复用 REST Bearer 的 ``_verify_and_get_payload_async``，凭证能力与 REST 一致。
    ``AUTH_ENABLED=false`` 时跳过校验，协议保持一致。
    """
    if not is_auth_enabled():
        return True
    try:
        text = await asyncio.wait_for(websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_SECONDS)
    except (TimeoutError, WebSocketDisconnect):
        await _ws_close(websocket, WS_CLOSE_UNAUTHORIZED)
        return False
    try:
        frame = json.loads(text)
    except ValueError:
        frame = None
    token = frame.get("token") if isinstance(frame, dict) else None
    payload = None
    if isinstance(token, str) and token:
        try:
            payload = await _verify_and_get_payload_async(token)
        except HTTPException:
            payload = None
    if payload is None:
        await _ws_close(websocket, WS_CLOSE_UNAUTHORIZED)
        return False
    return True


async def _ws_resolve_session(
    websocket: WebSocket,
    service: AssistantService,
    project_name: str,
    session_id: str,
) -> SessionMeta | None:
    """WS 会话归属校验：不存在或不属于该项目都以 4404 关闭（与 REST 的 404 同形）。"""
    session = await service.get_session(session_id)
    if session is None or session.project_name != project_name:
        await _ws_close(websocket, WS_CLOSE_SESSION_NOT_FOUND)
        return None
    return session


@ws_router.websocket("/sessions/{session_id}/entries/ws")
async def stream_entries_ws(
    websocket: WebSocket,
    project_name: str,
    session_id: str,
    after: int = Query(default=-1, ge=-1),
) -> None:
    """WS entry 流：与 SSE 版共用 service 层事件，帧形如 ``{"event", "data", "id"?}``（``id`` 即 seq）。

    鉴权：建连后客户端先发 ``{"type": "auth", "token": ...}`` 首帧（凭证不落 URL）。
    游标：``?after=<seq>`` 冷订阅；断线重连由客户端携带最近一次 entry 的 seq 续传。
    客户端帧：``{"type": "ping"}`` 回 ``pong``；``{"type": "interrupt"}`` 转交会话中断，
    结果由 status 事件与 ``interrupt_result`` 帧承担。命令类操作（发送、问答、排队消息）
    继续走 REST，本通道只承担下行流与 keepalive。
    """
    service = get_assistant_service()
    await websocket.accept()
    if not await _ws_authenticate(websocket):
        return
    try:
        meta = await _ws_resolve_session(websocket, service, project_name, session_id)
    except Exception:
        logger.exception("WS entry 流会话校验失败 session=%s", session_id)
        await _ws_close(websocket, WS_CLOSE_INTERNAL_ERROR)
        return
    if meta is None:
        return

    send_lock = asyncio.Lock()

    async def send_frame(event: str, data: Any, seq: str | None = None) -> None:
        frame: dict[str, Any] = {"event": event, "data": data}
        if seq is not None:
            frame["id"] = seq
        async with send_lock:
            await websocket.send_json(frame)

    async def pump() -> None:
        """下发存量与直播事件；订阅被溢出移除时生成器结束（客户端重连信号）。"""
        try:
            async for sse in service.stream_entry_events(session_id, meta=meta, after_seq=after):
                data = sse.data if isinstance(sse.data, dict) else {}
                await send_frame(sse.event or "message", data, sse.id)
        except AgentStartupError as exc:
            # 即时发送冷恢复启动失败：落终态后补推 entry + status（与 REST 版一致）。
            original = exc.__cause__ or exc
            failure = exc.failure_observation or build_startup_failure_observation(
                original,
                project_name=project_name,
                session_id=session_id,
                sdk_stderr=exc.sdk_stderr,
            )
            async for sse in service.stream_startup_failure_events(session_id, failure):
                data = sse.data if isinstance(sse.data, dict) else {}
                await send_frame(sse.event or "message", data, sse.id)

    async def receive_loop() -> None:
        while True:
            text = await websocket.receive_text()
            try:
                frame = json.loads(text)
            except ValueError:
                logger.debug("忽略无法解析的 WS 客户端帧: %r", text)
                continue
            frame_type = frame.get("type") if isinstance(frame, dict) else None
            if frame_type == "ping":
                await send_frame("pong", {})
            elif frame_type == "interrupt":
                try:
                    result = await service.interrupt_session(session_id, meta=meta)
                except Exception:
                    logger.warning("WS interrupt 请求失败 session=%s", session_id, exc_info=True)
                else:
                    await send_frame("interrupt_result", result)

    receiver = asyncio.create_task(receive_loop())
    sender = asyncio.create_task(pump())
    close_code = 1000
    try:
        await asyncio.wait({receiver, sender}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (receiver, sender):
            task.cancel()
        results = await asyncio.gather(receiver, sender, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, (WebSocketDisconnect, RuntimeError, OSError)):
                logger.warning("WS entry 流任务异常 session=%s: %r", session_id, result)
                close_code = WS_CLOSE_INTERNAL_ERROR
    await _ws_close(websocket, close_code)


@router.post("/sessions/{session_id}/interrupt")
async def interrupt_session(project_name: str, session_id: str, _t: Translator):
    try:
        service = get_assistant_service()
        meta = await _validate_session_ownership(service, session_id, project_name, _t)
        return await service.interrupt_session(session_id, meta=meta)
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise NotFoundError("session_not_found", session_id=session_id) from exc
    except ValueError as exc:
        logger.warning("会话中断请求非法: %s", exc)
        raise BadRequestError("request_invalid") from exc
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


async def _act_on_queued_message(
    project_name: str, session_id: str, action: Callable[[AssistantService], Awaitable[dict]], _t: Translator
) -> dict:
    """排队消息操作（编辑、删除、重新发送、立即发送）共用的错误映射。

    重新发送与对「未发送」消息的立即发送会在 CLI 已退出时重建会话连接，可能遇到容量已满或启动失败。
    """
    try:
        return await action(get_assistant_service())
    except QueuedMessageNotFoundError as exc:
        raise NotFoundError("queued_message_not_found") from exc
    except QueuedMessageWithdrawalPendingError as exc:
        raise ConflictError("queued_message_withdrawal_pending") from exc
    except SessionCapacityError as exc:
        raise ServiceUnavailableError("session_capacity_exceeded") from exc
    except FileNotFoundError as exc:
        raise NotFoundError("session_not_found", session_id=session_id) from exc
    except AgentStartupError as exc:
        raise HTTPException(
            status_code=502,
            detail=agent_startup_failure_detail(
                exc,
                project_name=project_name,
                session_id=session_id,
                title=_t("agent_startup_failed_title"),
            ),
        ) from exc
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


async def _withdraw_queued_message(
    project_name: str, session_id: str, message_id: str, intent: WithdrawalIntent, _t: Translator
) -> dict:
    return await _act_on_queued_message(
        project_name,
        session_id,
        lambda service: service.withdraw_queued_message(project_name, session_id, message_id, intent=intent),
        _t,
    )


@router.post("/sessions/{session_id}/queued-messages/{message_id}/edit")
async def edit_queued_message(project_name: str, session_id: str, message_id: str, _t: Translator):
    """把一条排队消息退回输入框：撤回成功时响应带回它的内容（``message``）。

    ``outcome`` 为 ``withdrawn`` 时消息已移出排队；为 ``accepted`` 时 Agent 已接收它，消息照常进入对话。
    """
    return await _withdraw_queued_message(project_name, session_id, message_id, "edit", _t)


@router.delete("/sessions/{session_id}/queued-messages/{message_id}")
async def delete_queued_message(project_name: str, session_id: str, message_id: str, _t: Translator):
    """删除一条排队消息：撤回成功即丢弃。响应形状同编辑端点，``message`` 恒为 null。"""
    return await _withdraw_queued_message(project_name, session_id, message_id, "delete", _t)


@router.post("/sessions/{session_id}/queued-messages/{message_id}/resend")
async def resend_queued_message(project_name: str, session_id: str, message_id: str, request: Request, _t: Translator):
    """把一条「未发送」消息重新交给 Agent，会话已中断时先重建连接。响应带回这条排队消息（``queued_message``）。"""
    return await _act_on_queued_message(
        project_name,
        session_id,
        lambda service: service.resend_queued_message(project_name, session_id, message_id, locale=get_locale(request)),
        _t,
    )


@router.post("/sessions/{session_id}/queued-messages/{message_id}/send-now")
async def send_queued_message_now(project_name: str, session_id: str, message_id: str, _t: Translator):
    """立即发送一条排队消息：Agent 打断当前轮先处理它。

    ``outcome`` 为 ``sent`` 时已重新送入，消息仍留在托盘里直到被接纳；为 ``accepted`` 时 Agent 已接收它，不再重发。
    """
    return await _act_on_queued_message(
        project_name,
        session_id,
        lambda service: service.send_queued_message_now(project_name, session_id, message_id),
        _t,
    )


@router.post("/sessions/{session_id}/questions/{question_id}/answer")
async def answer_question(
    project_name: str,
    session_id: str,
    question_id: str,
    req: AnswerQuestionRequest,
    _t: Translator,
):
    if not req.answers:
        raise HTTPException(status_code=400, detail=_t("answers_required"))
    try:
        service = get_assistant_service()
        meta = await _validate_session_ownership(service, session_id, project_name, _t)
        return await service.answer_user_question(
            session_id=session_id,
            question_id=question_id,
            answers=req.answers,
            meta=meta,
        )
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise NotFoundError("session_not_found", session_id=session_id) from exc
    except ValueError as exc:
        # 会话未运行或无待回答问题
        logger.warning("会话回答请求非法: %s", exc)
        raise BadRequestError("session_question_unavailable") from exc
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc


@router.get("/sessions/{session_id}/stream")
async def stream_events(project_name: str, session_id: str, _t: Translator):
    raise HTTPException(
        status_code=410,
        detail=_t("interface_offline"),
    )


@router.get("/skills")
async def list_skills(project_name: str, _t: Translator):
    try:
        skills = get_assistant_service().list_available_skills(project_name=project_name)
        return {"skills": skills}
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=project_name) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("请求处理失败")
        raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc
