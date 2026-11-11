import { describe, expect, mock, test } from "bun:test";
import {
  ApiError,
  createApi,
  FORBIDDEN_MESSAGE,
  formatDetail,
  type Notice,
  THROTTLED_MESSAGE,
  unwrap,
} from "./client";

const BASE = "http://writer.test";

function harness(status: number, body: unknown = {}) {
  const requests: Request[] = [];
  const notices: Notice[] = [];
  const onUnauthorized = mock(() => {});
  const api = createApi({
    baseUrl: BASE,
    fetch: async (req: Request) => {
      requests.push(req);
      return new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      });
    },
    sink: { onUnauthorized, notify: (n) => notices.push(n) },
  });
  return { api, requests, notices, onUnauthorized };
}

describe("api client", () => {
  test("sends the CSRF header and includes credentials on every request, login too", async () => {
    const h = harness(200, { username: "rita", role: "reviewer" });
    await h.api.POST("/api/auth/login", { body: { username: "rita", password: "x" } });
    await h.api.GET("/api/review", { params: { query: { status: "open" } } });
    expect(h.requests).toHaveLength(2);
    for (const req of h.requests) {
      expect(req.headers.get("X-Requested-With")).toBe("writer-admin");
      expect(req.credentials).toBe("include");
    }
    expect(h.requests[0]!.url).toBe(`${BASE}/api/auth/login`);
    expect(h.requests[1]!.url).toBe(`${BASE}/api/review?status=open`);
  });

  test("401 routes to login (and is not a notification)", async () => {
    const h = harness(401, { detail: "not logged in" });
    await h.api.GET("/api/auth/me");
    expect(h.onUnauthorized).toHaveBeenCalledTimes(1);
    expect(h.notices).toHaveLength(0);
  });

  test("a failed login is left to the login form", async () => {
    const h = harness(401, { detail: "invalid username or password" });
    await h.api.POST("/api/auth/login", { body: { username: "a", password: "b" } });
    expect(h.onUnauthorized).not.toHaveBeenCalled();
    expect(h.notices).toHaveLength(0);
  });

  test("403 shows a permission message", async () => {
    const h = harness(403, { detail: "requires role admin" });
    await h.api.POST("/api/ontology/drafts");
    expect(h.notices).toEqual([
      { title: "Permission denied", message: FORBIDDEN_MESSAGE, color: "orange" },
    ]);
    expect(h.onUnauthorized).not.toHaveBeenCalled();
  });

  test("429 shows the login-throttled message", async () => {
    const h = harness(429, { detail: "too many failed logins; try again later" });
    await h.api.POST("/api/auth/login", { body: { username: "a", password: "b" } });
    expect(h.notices[0]!.message).toBe(THROTTLED_MESSAGE);
  });

  test("other errors show the server's detail", async () => {
    const h = harness(422, { detail: "publish gate: class 'X' has no identity key" });
    await h.api.POST("/api/ontology/versions/{version}/publish", {
      params: { path: { version: 2 } },
    });
    expect(h.notices[0]).toEqual({
      title: "Request failed (422)",
      message: "publish gate: class 'X' has no identity key",
      color: "red",
    });
  });

  test("unwrap throws ApiError with the readable detail", async () => {
    const h = harness(409, { detail: "v1 is published; only drafts can be edited" });
    const err = await unwrap(
      h.api.POST("/api/ontology/versions/{version}/classes", {
        params: { path: { version: 1 } },
        body: { name: "X", identity_key: "id", properties: [{ name: "id", datatype: "string", required: true }], namespace: "ext" },
      }),
    ).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).detail).toBe("v1 is published; only drafts can be edited");
  });

  test("validation error lists are readable", () => {
    const body = {
      detail: [{ loc: ["body", "password"], msg: "String should have at least 12 characters" }],
    };
    expect(formatDetail(body, "x")).toBe("password: String should have at least 12 characters");
    expect(formatDetail(null, "fallback")).toBe("fallback");
  });
});
