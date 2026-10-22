import { render, type RenderResult } from "@testing-library/react";
import { type Mock, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import { App } from "../App";

type Responder = (request: { url: string; body: unknown }) => unknown;

export interface FakeApi {
  fetch: Mock;
  /** Bodies POSTed to each path, in order. */
  posted: Record<string, unknown[]>;
}

/**
 * Stubs `fetch` with canned JSON per path (exact path with query, or path without it).
 * A function handler receives the request and returns a body, or a full `Response`.
 */
export function fakeApi(routes: Record<string, unknown>): FakeApi {
  const posted: Record<string, unknown[]> = {};
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const path = url.split("?")[0] ?? url;
    const body: unknown = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    if (init?.method === "POST") {
      (posted[path] ??= []).push(body);
    }
    const handler = url in routes ? routes[url] : routes[path];
    if (handler === undefined) {
      return Promise.resolve(json({ detail: `no fake for ${url}` }, 404));
    }
    const result = typeof handler === "function" ? (handler as Responder)({ url, body }) : handler;
    return Promise.resolve(result instanceof Response ? result : json(result));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetch: fetchMock, posted };
}

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}

/** Renders the whole app at `path`, with a probe showing the current location. */
export function renderApp(path: string): RenderResult {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
      <Routes>
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

export const META = {
  version: "0.1.0",
  project: "acme-maintenance",
  root: "/work/acme",
  user: "ada",
  alpha: 0.05,
  min_effect: 0.03,
  labels_file: "/work/acme/.hecklr/labels.jsonl",
  mine_dir: "/work/acme/.hecklr/mine",
  scenarios_dir: "/work/acme/scenarios/mined",
};

export function estimate(value: number, low: number, high: number, n = 10) {
  return { value, ci_low: low, ci_high: high, confidence: 0.95, n, method: "wilson" };
}
