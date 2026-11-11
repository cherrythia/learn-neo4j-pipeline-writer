// Typed API client for the writer service (types generated from openapi.json, see gen:api).
//
// Every request:
//   - sends the session cookie (credentials: "include")
//   - sends X-Requested-With: writer-admin (the service's CSRF check; login needs it too)
// Errors are mapped once, here:
//   401 -> the app's "logged out" handler (routes to Login); login's own 401 is left to the form
//   403 -> permission notification
//   429 -> "login throttled" notification
//   other -> notification with the server's `detail`
import createClient, { type Client, type Middleware } from "openapi-fetch";
import type { paths } from "./schema";

export const CSRF_HEADER = "X-Requested-With";
export const CSRF_VALUE = "writer-admin";
export const LOGIN_PATH = "/api/auth/login";

export type Notice = { title: string; message: string; color: "red" | "yellow" | "orange" };

export interface ErrorSink {
  /** The session is gone (401): show the login screen. */
  onUnauthorized(): void;
  /** Show a notification to the user. */
  notify(notice: Notice): void;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
    this.name = "ApiError";
  }
}

export const THROTTLED_MESSAGE = "Login throttled: too many failed attempts. Try again later.";
export const FORBIDDEN_MESSAGE = "You don't have permission to do that (admin only).";

/** FastAPI's `detail` is a string, or a list of validation errors. */
export function formatDetail(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => {
          if (d && typeof d === "object" && "msg" in d) {
            const loc = Array.isArray((d as { loc?: unknown }).loc)
              ? (d as { loc: unknown[] }).loc.filter((p) => p !== "body").join(".")
              : "";
            return loc ? `${loc}: ${(d as { msg: string }).msg}` : (d as { msg: string }).msg;
          }
          return JSON.stringify(d);
        })
        .join("; ");
    }
    return JSON.stringify(detail);
  }
  return fallback;
}

async function readDetail(response: Response): Promise<string> {
  const fallback = `${response.status} ${response.statusText}`.trim();
  try {
    return formatDetail(await response.clone().json(), fallback);
  } catch {
    return fallback;
  }
}

export function errorMiddleware(sink: ErrorSink): Middleware {
  return {
    async onResponse({ request, response }) {
      if (response.ok) return undefined;
      const path = new URL(request.url).pathname;
      if (response.status === 401) {
        if (!path.endsWith(LOGIN_PATH)) sink.onUnauthorized();
        return undefined;
      }
      if (response.status === 403) {
        sink.notify({ title: "Permission denied", message: FORBIDDEN_MESSAGE, color: "orange" });
      } else if (response.status === 429) {
        sink.notify({ title: "Too many attempts", message: THROTTLED_MESSAGE, color: "yellow" });
      } else {
        sink.notify({
          title: `Request failed (${response.status})`,
          message: await readDetail(response),
          color: "red",
        });
      }
      return undefined;
    },
  };
}

export type Api = Client<paths>;

export interface ApiOptions {
  baseUrl?: string;
  fetch?: (input: Request) => Promise<Response>;
  sink?: ErrorSink;
}

/** Same origin unless the page sets <meta name="writer-api-base" content="http://..."> */
export function defaultBaseUrl(): string {
  const meta = globalThis.document?.querySelector<HTMLMetaElement>('meta[name="writer-api-base"]');
  return meta?.content || globalThis.location?.origin || "";
}

export function createApi(options: ApiOptions = {}): Api {
  const client = createClient<paths>({
    baseUrl: options.baseUrl ?? defaultBaseUrl(),
    credentials: "include",
    headers: { [CSRF_HEADER]: CSRF_VALUE },
    ...(options.fetch ? { fetch: options.fetch } : {}),
  });
  if (options.sink) client.use(errorMiddleware(options.sink));
  return client;
}

/** Turn openapi-fetch's {data, error, response} into data-or-throw (for TanStack Query). */
export async function unwrap<T>(
  result: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await result;
  if (!response.ok) {
    throw new ApiError(
      response.status,
      formatDetail(error, `${response.status} ${response.statusText}`.trim()),
    );
  }
  return data as T;
}
