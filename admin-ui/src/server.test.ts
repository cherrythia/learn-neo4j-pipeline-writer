import { expect, test } from "bun:test";
import { proxyTarget } from "../server";

const API = "http://localhost:8000";

test("dev proxy forwards /api and /ingest paths unchanged", () => {
  expect(proxyTarget(new URL("http://127.0.0.1:3000/api/review?status=open"), API)).toBe(
    "http://localhost:8000/api/review?status=open",
  );
  expect(proxyTarget(new URL("http://127.0.0.1:3000/ingest/documents"), API)).toBe(
    "http://localhost:8000/ingest/documents",
  );
  expect(proxyTarget(new URL("http://127.0.0.1:3000/apix"), API)).toBeNull();
  expect(proxyTarget(new URL("http://127.0.0.1:3000/"), API)).toBeNull();
});
