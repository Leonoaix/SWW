/** Transport for the local service. Turns every failure into a readable line. */
import { object, strings } from "./wire";

const TIMEOUT_MS = 120_000;

export async function request(path: string, options: RequestInit = {}): Promise<unknown> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const headers = new Headers(options.headers);
    // The service rejects unsafe methods without this, so a page on another
    // localhost port cannot drive it with a drive-by fetch.
    if (options.method && options.method !== "GET") headers.set("X-SWW-Client", "1");
    const response = await fetch(`/matcher-api${path}`, { ...options, headers, signal: controller.signal });
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new Error("本地服务没有返回有效 JSON。请确认匹配服务正在运行且 /matcher-api 代理正确。");
    }
    if (!response.ok) {
      const data = object(payload);
      const detail = data.detail ?? data.error ?? data.message;
      throw new Error(typeof detail === "string" ? detail
        : strings(detail).join("；") || `请求失败（HTTP ${response.status}）`);
    }
    return payload;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("请求等待超时。请查看本地服务状态后重试。");
    }
    if (error instanceof TypeError) {
      throw new Error("无法连接本地服务，请确认已运行 ./scripts/start-matcher.sh。");
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

export const post = (path: string, data: unknown = {}): Promise<unknown> =>
  request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
