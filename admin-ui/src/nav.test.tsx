import { afterEach, expect, test } from "bun:test";
import { cleanup, screen, within } from "@testing-library/react";
import { AppRoutes } from "./App";
import { json, renderWithApi } from "./testing";
import { navFor } from "./nav";

afterEach(cleanup);

test("reviewers don't see admin-only pages", () => {
  expect(navFor("reviewer").map((n) => n.label)).toEqual(["Review queue", "Ontology", "Proposals", "Scoring", "Runs"]);
  expect(navFor("admin").map((n) => n.label)).toContain("Users");
});

async function navLabels() {
  const nav = await screen.findByLabelText("Main navigation");
  return within(nav)
    .getAllByRole("link")
    .map((a) => a.textContent);
}

test("nav follows the current user's role", async () => {
  renderWithApi(<AppRoutes />, { "GET /api/auth/me": { username: "rita", role: "reviewer" }, "GET /api/review": [] });
  expect(await navLabels()).toEqual(["Review queue", "Ontology", "Proposals", "Scoring", "Runs"]);
  cleanup();
  renderWithApi(<AppRoutes />, { "GET /api/auth/me": { username: "alice", role: "admin" }, "GET /api/review": [] });
  expect(await navLabels()).toEqual(["Review queue", "Ontology", "Proposals", "Mappings", "Scoring", "Runs", "Audit", "Users"]);
});

test("a reviewer opening an admin page gets a permission message", async () => {
  const { calls } = renderWithApi(<AppRoutes />, { "GET /api/auth/me": { username: "rita", role: "reviewer" } }, "/users");
  expect(await screen.findByText("This page is for admins only.")).toBeDefined();
  expect(calls.some((c) => c.path === "/api/users")).toBe(false);
});

test("a 401 mid-session returns to the login screen", async () => {
  renderWithApi(<AppRoutes />, {
    "GET /api/auth/me": { username: "rita", role: "reviewer" },
    "GET /api/review": () => json({ detail: "session expired" }, 401),
  });
  expect(await screen.findByRole("button", { name: "Log in" })).toBeDefined();
});

test("a 403 shows a permission notification", async () => {
  const { notices } = renderWithApi(
    <AppRoutes />,
    {
      "GET /api/auth/me": { username: "alice", role: "admin" },
      "GET /api/audit": () => json({ detail: "requires role admin" }, 403),
    },
    "/audit",
  );
  await screen.findByText("Audit log");
  await new Promise((r) => setTimeout(r, 50));
  expect(notices.map((n) => n.title)).toContain("Permission denied");
});

test("log out ends the session and shows the login screen", async () => {
  const { calls } = renderWithApi(<AppRoutes />, {
    "GET /api/auth/me": { username: "rita", role: "reviewer" },
    "GET /api/review": [],
    "POST /api/auth/logout": () => new Response(null, { status: 204 }),
  });
  (await screen.findByRole("button", { name: "Log out" })).click();
  expect(await screen.findByRole("button", { name: "Log in" })).toBeDefined();
  expect(calls.find((c) => c.path === "/api/auth/logout")?.headers.get("X-Requested-With")).toBe("writer-admin");
});
