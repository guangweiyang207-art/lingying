// 页面级套件的 test：每条用例自动回放录制的接口替身、固定时间并拦住外网请求。
import { AxeBuilder } from "@axe-core/playwright";
import { expect, test as base, type Page } from "@playwright/test";
import { inspectLayout } from "./layout-probe.ts";
import { FIXED_NOW, loadRecordedResponses, recordedKey, type RecordedResponse } from "./recorded.ts";

const RECORDED = loadRecordedResponses();

/**
 * 场景对某个接口的替换响应，用于长文本、多条目等压力变体；键形如 `GET /api/v1/projects`，带查询串的请求写全查询串。
 * `body` 是字符串时按 text/plain 原样返回，其余按 JSON 返回。
 */
export type ApiOverrides = Record<string, Pick<RecordedResponse, "status" | "body">>;

export interface ApiStub {
  override(overrides: ApiOverrides): void;
}

function isLoopback(url: URL) {
  return url.hostname === "127.0.0.1" || url.hostname === "localhost";
}

/** 把录制的 SSE 开场帧文本解析成会话 entry 流的 WS 帧序列（event/data/id 一一对应）。 */
function sseBodyToFrames(body: string): string[] {
  const frames: string[] = [];
  for (const block of body.split("\n\n")) {
    let event = "message";
    let data = "";
    let id: string | undefined;
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice("event:".length).trim();
      else if (line.startsWith("data:")) data += (data ? "\n" : "") + line.slice("data:".length).trim();
      else if (line.startsWith("id:")) id = line.slice("id:".length).trim();
    }
    if (!data) continue;
    try {
      frames.push(JSON.stringify({ event, data: JSON.parse(data), ...(id ? { id } : {}) }));
    } catch {
      // 非 JSON 的 data（如纯文本提示帧）不进入 WS 回放
    }
  }
  return frames;
}

export const test = base.extend<{ api: ApiStub }>({
  api: [
    async ({ page }, use) => {
      const overrides = new Map<string, Pick<RecordedResponse, "status" | "body">>();
      const unrecorded: string[] = [];

      await page.clock.setFixedTime(new Date(FIXED_NOW));
      // 外网资源（如在线字体）不进入断言，拦掉以免网络波动影响结果。
      await page.route(
        (url) => !isLoopback(url),
        (route) => route.abort(),
      );
      await page.route("**/api/**", async (route) => {
        const request = route.request();
        const key = recordedKey(request.method(), request.url());
        const response = overrides.get(key) ?? RECORDED.get(key);
        if (!response) {
          unrecorded.push(key);
          await route.fulfill({ status: 501, json: { detail: `没有录制 ${key}` } });
          return;
        }
        // 记忆文件正文等接口按 text/plain 返回原文：替换响应的 body 写成字符串
        if (typeof response.body === "string") {
          await route.fulfill({ status: response.status, body: response.body, contentType: "text/plain; charset=utf-8" });
          return;
        }
        await route.fulfill({ status: response.status, json: response.body });
      });
      // 会话 entry 流已 WS 化，不走 page.route：桥接到同键的录制/替换 SSE 开场帧
      // （WS 的 `.../entries/ws?after=N` 映射到录制的 `GET .../entries/stream?after=N`），
      // 按帧回放后保持连接不关流；没有对应录制的会话直接关闭，客户端按退避重连。
      await page.routeWebSocket(/\/entries\/ws(\?|$)/, (ws) => {
        const url = new URL(ws.url());
        const key = recordedKey("GET", url.pathname.replace("/entries/ws", "/entries/stream") + url.search);
        const response = overrides.get(key) ?? RECORDED.get(key);
        if (!response || typeof response.body !== "string") {
          ws.close();
          return;
        }
        for (const frame of sseBodyToFrames(response.body)) ws.send(frame);
      });

      await use({
        override(entries) {
          for (const [key, response] of Object.entries(entries)) {
            const [method, url] = key.split(" ", 2);
            overrides.set(recordedKey(method, url), response);
          }
        },
      });

      expect(unrecorded, "页面请求了没有录制的接口：运行 pnpm e2e:record 补录，或在场景里提供替换响应").toEqual(
        [],
      );
    },
    { auto: true },
  ],
});

export { expect };

/** 文档本身不滚动，没有被 overflow 裁切、用户够不到的内容，也没有未声明的横向滚动。 */
export async function expectReachableLayout(page: Page) {
  const report = await page.evaluate(inspectLayout);
  expect
    .soft(report.documentScrollHeight, "文档本身发生了滚动：外壳根节点或绝对定位元素撑高了文档")
    .toBe(report.viewportHeight);
  expect.soft(report.documentScrollWidth, "文档本身发生了横向滚动：有元素宽于视口").toBe(report.viewportWidth);
  expect
    .soft(
      report.strayScrollX,
      "纵向滚动区（含弹层正文）被宽内容撑出了横向滚动：内容应折行或限宽，确需横向滚动的区域写 overflow-x-auto",
    )
    .toEqual([]);
  expect(report.clipped, "有内容被 overflow 裁切且无法滚动到达").toEqual([]);
}

// axe 的 WCAG 2.2 规则（含 target-size）默认关闭，需要显式开启 wcag22aa。
const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

export async function expectAccessible(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(AXE_TAGS).analyze();
  const violations = results.violations.map((violation) => ({
    rule: violation.id,
    help: violation.help,
    targets: violation.nodes.map((node) => node.target.join(" ")),
  }));
  expect(violations, "axe 报告了可访问性问题").toEqual([]);
}
