// Dev server: serves the admin UI with HMR and proxies the API to FastAPI.
// The backend's routes include their prefix (/api/..., /ingest/...), so paths are forwarded
// unchanged.
import index from "./index.html";

const API = process.env.WRITER_API_URL ?? "http://localhost:8000";
export const PROXIED_PREFIXES = ["/api/", "/ingest/"];

export function proxyTarget(url: URL, api: string = API): string | null {
  if (!PROXIED_PREFIXES.some((p) => url.pathname.startsWith(p))) return null;
  return api + url.pathname + url.search;
}

if (import.meta.main) {
  Bun.serve({
    // Loopback only: the API proxy must not be reachable from the network.
    hostname: process.env.HOST ?? "127.0.0.1",
    port: Number(process.env.PORT ?? 3000),
    routes: { "/": index },
    async fetch(req) {
      const target = proxyTarget(new URL(req.url));
      if (target) return fetch(target, req);
      return new Response("Not found", { status: 404 });
    },
    development: { hmr: true, console: true },
  });
}
