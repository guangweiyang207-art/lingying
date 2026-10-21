import { SseStreamError } from "./sse-stream";

/**
 * 以浏览器 `WebSocket` 消费会话 entry 流的客户端。
 *
 * WS 握手带不了 `Authorization` header，会话凭证也不落 URL（ADR 0071）：每次建连后
 * 由 `authToken` 取当前凭证、以 `{"type": "auth", "token": ...}` 首帧完成鉴权。这里同样
 * 自行承担断线后按指数退避自动重建，重建时经 `buildUrl` 重新取一遍 URL——调用方把最近
 * 一次 entry 的 seq 拼进 `?after=`，服务端从游标续传。
 */

export interface WsMessage {
  event: string;
  data: Record<string, unknown>;
  /** entry 事件的 seq，客户端据此推进续传游标。 */
  id?: string;
}

/** WS 关闭码到语义的映射与 {@link SseStreamError} 同形，上层 401 处理（跳登录）得以复用。 */
export class WsStreamError extends SseStreamError {}

export interface WsStreamOptions {
  /** 每次建连（含重连）前调用，返回完整 `ws(s)://` 或站内相对 URL（含游标参数）。 */
  buildUrl: () => string;
  /** 每次建连后作为鉴权首帧发送的 token；返回 null 时不发 auth 帧。 */
  authToken?: () => string | null;
  onMessage: (message: WsMessage) => void;
  /** 每次成功建立连接（含重连）后调用一次。 */
  onOpen?: () => void;
  /** 连接失败或中断时调用；`error.retryable` 为 true 时随后按退避重连。 */
  onError?: (error: WsStreamError) => void;
  /** 首次退避延迟（毫秒）。 */
  initialRetryDelayMs?: number;
  maxRetryDelayMs?: number;
}

export interface WsStreamHandle {
  /** 关闭连接并停止重连；幂等。 */
  close(): void;
  readonly closed: boolean;
  /** 最近一次收到的帧 id，重连时由调用方拼进 `?after=`。 */
  readonly lastEventId: string | null;
}

const DEFAULT_INITIAL_RETRY_DELAY_MS = 1000;
const DEFAULT_MAX_RETRY_DELAY_MS = 30_000;

/** 服务端明确拒绝的关闭码：重试不会改变结果。 */
const NON_RETRYABLE_CLOSE_CODES = new Set([4401, 4404]);

/** 服务端关闭码到 {@link SseStreamError.status} 的映射，其余关闭码不带 status。 */
const CLOSE_CODE_STATUSES = new Map([[4401, 401], [4404, 404]]);

function toWsUrl(url: string): string {
  if (url.startsWith("ws://") || url.startsWith("wss://")) return url;
  const protocol = globalThis.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${globalThis.location.host}${url}`;
}

export function openWsStream(options: WsStreamOptions): WsStreamHandle {
  const initialRetryDelayMs = options.initialRetryDelayMs ?? DEFAULT_INITIAL_RETRY_DELAY_MS;
  const maxRetryDelayMs = options.maxRetryDelayMs ?? DEFAULT_MAX_RETRY_DELAY_MS;

  let closed = false;
  let lastEventId: string | null = null;
  let retryDelayMs = initialRetryDelayMs;
  let attempt = 0;
  let socket: WebSocket | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  const connect = () => {
    if (closed) return;
    socket = new WebSocket(toWsUrl(options.buildUrl()));
    socket.onopen = () => {
      // 收到连接才算这条链路真的可用，退避从此归零。
      attempt = 0;
      const token = options.authToken?.() ?? null;
      if (token !== null) {
        socket?.send(JSON.stringify({ type: "auth", token }));
      }
      options.onOpen?.();
    };
    socket.onmessage = (event) => {
      let frame: unknown;
      try {
        frame = JSON.parse(typeof event.data === "string" ? event.data : "{}");
      } catch (err) {
        console.error("Failed to parse WS frame:", err, event.data);
        return;
      }
      if (!frame || typeof frame !== "object" || Array.isArray(frame)) return;
      const message = frame as WsMessage;
      if (typeof message.event !== "string" || typeof message.data !== "object" || message.data === null) return;
      if (typeof message.id === "string") {
        lastEventId = message.id;
      }
      options.onMessage(message);
    };
    socket.onclose = (event) => {
      if (closed) return;
      const code = event.code;
      const error = new WsStreamError(`WebSocket closed (${code})`, {
        status: CLOSE_CODE_STATUSES.get(code) ?? null,
        retryable: !NON_RETRYABLE_CLOSE_CODES.has(code),
      });
      options.onError?.(error);
      if (error.retryable) {
        scheduleRetry();
      } else {
        closed = true;
        stopWatchingVisibility();
      }
    };
    // onerror 总是紧跟着 onclose，语义在 onclose 里统一处理。
    socket.onerror = () => {};
  };

  const scheduleRetry = () => {
    if (closed) return;
    const delay = Math.min(retryDelayMs * 2 ** attempt, maxRetryDelayMs);
    attempt += 1;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      connect();
    }, delay);
  };

  /**
   * 与 `sse-stream` 同一策略：切回前台是「网络条件已变」的强信号，抢占剩余等待并把退避归零。
   */
  const handleVisibilityChange = () => {
    if (closed || retryTimer === null) return;
    if (document.visibilityState !== "visible") return;
    clearTimeout(retryTimer);
    retryTimer = null;
    attempt = 0;
    connect();
  };

  const visibilityHost = typeof document === "undefined" ? null : document;
  visibilityHost?.addEventListener("visibilitychange", handleVisibilityChange);

  const stopWatchingVisibility = () => {
    visibilityHost?.removeEventListener("visibilitychange", handleVisibilityChange);
  };

  connect();

  return {
    close() {
      if (closed) return;
      closed = true;
      if (retryTimer !== null) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      stopWatchingVisibility();
      socket?.close(1000);
      socket = null;
    },
    get closed() {
      return closed;
    },
    get lastEventId() {
      return lastEventId;
    },
  };
}
