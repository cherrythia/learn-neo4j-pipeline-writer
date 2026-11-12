// Test helpers: the real providers and API client over a scripted fetch.
import { expect } from "bun:test";
import { fireEvent, render, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import type { Notice } from "./api/client";
import { Providers } from "./App";
import { createRuntime } from "./runtime";

export const BASE = "http://writer.test";

export type Handler = (req: Request) => unknown | Response | Promise<unknown | Response>;

export interface Call {
  method: string;
  path: string;
  search: string;
  body: unknown;
  headers: Headers;
  credentials: RequestCredentials;
}

/** routes: { "GET /api/auth/me": handler-or-value }. Unknown routes return 404. */
export function scriptedFetch(routes: Record<string, unknown>) {
  const calls: Call[] = [];
  const fetch = async (req: Request): Promise<Response> => {
    const url = new URL(req.url);
    const text = req.method === "GET" ? "" : await req.text();
    calls.push({
      method: req.method,
      path: url.pathname,
      search: url.search,
      body: text ? JSON.parse(text) : undefined,
      headers: req.headers,
      credentials: req.credentials,
    });
    const route = routes[`${req.method} ${url.pathname}`];
    if (route === undefined) return json({ detail: "not found" }, 404);
    const out = typeof route === "function" ? await (route as Handler)(req) : route;
    return out instanceof Response ? out : json(out);
  };
  return { fetch, calls };
}

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

export function renderWithApi(ui: ReactNode, routes: Record<string, unknown>, path = "/") {
  const { fetch, calls } = scriptedFetch(routes);
  const notices: Notice[] = [];
  const runtime = createRuntime({ baseUrl: BASE, fetch, notify: (n) => notices.push(n) });
  const utils = render(
    <Providers runtime={runtime}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </Providers>,
  );
  return { ...utils, calls, notices, runtime };
}


/** Pick an option in a Mantine Select. Every Select keeps its dropdown in the page, so the
 * option is looked up only inside the listbox this input controls. */
export async function pick(input: HTMLElement, option: string) {
  fireEvent.click(input);
  await waitFor(() => expect(input.getAttribute("aria-expanded")).toBe("true"));
  const listbox = document.getElementById(input.getAttribute("aria-controls") ?? "")!;
  fireEvent.click(within(listbox).getByRole("option", { name: option, hidden: true }));
}
