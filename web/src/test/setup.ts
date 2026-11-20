import "@testing-library/jest-dom/vitest";

// jsdom ships no WebGL: Sigma looks the constructor up when the module is evaluated.
class FakeWebGL2RenderingContext {
  readonly canvas = null;
}
if (!("WebGL2RenderingContext" in globalThis)) {
  Object.defineProperty(globalThis, "WebGL2RenderingContext", { value: FakeWebGL2RenderingContext });
}
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

/**
 * A fetch stub for the console's two doors.
 *
 * Give it either a queue (a test that makes exactly one call) or a route map keyed by URL prefix —
 * a page fires several requests at once, and a queue would hand the session response to whatever
 * query happened to be created first.
 */
export interface StubbedResponse {
  status?: number;
  body?: unknown;
}

export function mockFetch(responses: StubbedResponse[] | Record<string, StubbedResponse | StubbedResponse[]>) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const queue: StubbedResponse[] = Array.isArray(responses) ? responses.slice() : [];
  const routes = Array.isArray(responses) ? null : responses;

  const respond = (url: string): StubbedResponse => {
    if (routes) {
      // Longest match wins: /admin/api/databases would otherwise swallow /admin/api/databases/inspect.
      const key = Object.keys(routes)
        .filter((prefix) => prefix === "POST" || url.startsWith(prefix))
        .sort((left, right) => right.length - left.length)[0];
      if (!key) return { status: 200, body: {} };
      const match = routes[key];
      return Array.isArray(match) ? (match.shift() ?? { status: 200, body: {} }) : match;
    }
    return queue.shift() ?? { status: 200, body: {} };
  };

  const implementation = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    calls.push({ url, init });
    const next = respond(url);
    const status = next.status ?? 200;
    return new Response(next.body === undefined ? "" : JSON.stringify(next.body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  };
  vi.stubGlobal("fetch", vi.fn(implementation));
  return { calls };
}
