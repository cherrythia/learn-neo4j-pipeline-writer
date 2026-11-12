import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { RunDetail, RunOut } from "../api/types";
import { renderWithApi } from "../testing";
import { RunsPage } from "./Runs";

afterEach(cleanup);

const RUN_ID = "0f3a9c12-4b5e-4d7f-8a90-1b2c3d4e5f60";
const RUN: RunOut = {
  id: RUN_ID,
  source_type: "pdf",
  source_ref: "a.pdf",
  ontology_version: 1,
  status: "completed",
  counts: { committed: 2, review: 1 },
  error: null,
  started_by: "alice",
  started_at: "2026-11-01T10:00:00Z",
  finished_at: "2026-11-01T10:01:00Z",
};
const detail = (status: string): RunDetail => ({
  ...RUN,
  status,
  candidates: [
    {
      id: 11,
      payload: { kind: "entity", class_name: "Department", properties: { dept_code: "FIN" } },
      decision: "review",
      score: 0.7125,
      breakdown: { reasons: ["score 0.7125 needs review"] },
      gate_failures: [],
    },
  ],
});

test("admin rolls back a run only after confirming, and sees the run rolled back", async () => {
  let status = "completed";
  const { calls } = renderWithApi(<RunsPage role="admin" />, {
    "GET /api/runs": () => [{ ...RUN, status }],
    [`GET /api/runs/${RUN_ID}`]: () => detail(status),
    [`POST /api/runs/${RUN_ID}/rollback`]: () => {
      status = "rolled_back";
      return { nodes: 1, relationships: 2, evidence: 3, review_items_closed: 1 };
    },
  });
  fireEvent.click(await screen.findByText(RUN_ID.slice(0, 8)));
  const candidates = await screen.findByRole("table", { name: "Candidates" });
  expect(within(candidates).getByText("0.713")).toBeDefined();
  expect(within(candidates).getByText("score 0.7125 needs review")).toBeDefined();

  fireEvent.click(screen.getByRole("button", { name: "Roll back run" }));
  const dialog = await screen.findByRole("dialog");
  expect(dialog.textContent).toContain("Roll back this run?");
  expect(calls.some((c) => c.path.endsWith("/rollback"))).toBe(false);

  fireEvent.click(within(dialog).getByRole("button", { name: "Confirm rollback" }));
  await waitFor(() => expect(calls.some((c) => c.path === `/api/runs/${RUN_ID}/rollback`)).toBe(true));
  const rollback = calls.find((c) => c.path === `/api/runs/${RUN_ID}/rollback`)!;
  expect(rollback.method).toBe("POST");
  expect(rollback.body).toBeUndefined();
  expect(rollback.headers.get("X-Requested-With")).toBe("writer-admin");

  // The list and the detail are refetched and show the new status.
  const runs = screen.getByRole("table", { name: "Runs" });
  await waitFor(() => expect(within(runs).getByText("rolled_back")).toBeDefined());
  // The confirmation closes once the rollback succeeds.
  await waitFor(() => expect(screen.queryAllByRole("dialog")).toHaveLength(0), { timeout: 2000 });
  // ...and the counts stay visible on the page (they used to vanish with the dialog)
  expect((await screen.findByTestId("rollback-result")).textContent).toContain("review_items_closed");
});

test("reviewers cannot roll back", async () => {
  renderWithApi(<RunsPage role="reviewer" />, {
    "GET /api/runs": [RUN],
    [`GET /api/runs/${RUN_ID}`]: detail("completed"),
  });
  fireEvent.click(await screen.findByText(RUN_ID.slice(0, 8)));
  await screen.findByRole("table", { name: "Candidates" });
  expect(screen.queryByRole("button", { name: "Roll back run" })).toBeNull();
});

test("switching runs after a rollback clears the previous run's result (review round 2)", async () => {
  const OTHER = { ...RUN, id: "22222222-0000-0000-0000-000000000000" };
  renderWithApi(<RunsPage role="admin" />, {
    "GET /api/runs": [RUN, OTHER],
    [`GET /api/runs/${RUN_ID}`]: detail("completed"),
    [`GET /api/runs/${OTHER.id}`]: { ...detail("completed"), id: OTHER.id },
    [`POST /api/runs/${RUN_ID}/rollback`]: { nodes: 1, relationships: 0, evidence: 0, review_items_closed: 0 },
  });
  fireEvent.click(await screen.findByText(RUN_ID.slice(0, 8)));
  fireEvent.click(await screen.findByRole("button", { name: "Roll back run" }));
  fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Confirm rollback" }));
  await screen.findByTestId("rollback-result");
  fireEvent.click(screen.getByText(OTHER.id.slice(0, 8)));
  await screen.findByText(`Run ${OTHER.id.slice(0, 8)}`);
  expect(screen.queryByTestId("rollback-result")).toBeNull();
});
