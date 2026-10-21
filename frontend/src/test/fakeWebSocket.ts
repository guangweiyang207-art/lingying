import { vi } from "vitest";

/**
 * 可控的 `WebSocket` 替身：把全局 `WebSocket` 打桩为由测试驱动的实例，供 `openWsStream`
 * 及其上层封装的测试推送帧、模拟服务端关闭。
 *
 * 与 `fakeSseFetch` 同构：`connections` 按建连顺序记录每次实例化，用于断言 URL（续传游标）
 * 与重连次数；`sent` 记录客户端发出的帧（首帧 auth）。事件回调由测试同步触发，无需冲洗
 * 微任务；重连走退避定时器，配合 vitest 假时钟推进。
 */
const connections: FakeWebSocket[] = [];

export class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readonly url: string;
  readonly sent: string[] = [];
  readyState = FakeWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;

  constructor(url: string | URL) {
    this.url = String(url);
    connections.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = FakeWebSocket.CLOSED;
  }

  /** 模拟服务端完成握手。 */
  serverOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  /** 推送一帧 JSON 消息。 */
  serverSend(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  /** 模拟服务端（或网络）关闭，``code`` 用服务端关闭码（4401 鉴权失败等）。 */
  serverClose(code: number): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code, reason: "" });
  }
}

export interface FakeWsStub {
  connections: FakeWebSocket[];
  /** 最近一次建立的连接。 */
  readonly latest: FakeWebSocket;
}

/** 用 `vi.stubGlobal("WebSocket", …)` 安装替身；由 vitest 的 `unstubAllGlobals` 或用例自行恢复。 */
export function stubWebSocket(): FakeWsStub {
  connections.length = 0;
  vi.stubGlobal("WebSocket", FakeWebSocket);
  return {
    connections,
    get latest() {
      return connections[connections.length - 1];
    },
  };
}
