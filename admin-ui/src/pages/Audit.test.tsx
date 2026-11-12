import { afterEach, expect, test } from "bun:test";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import type { AuditOut } from "../api/types";
import { renderWithApi } from "../testing";
import { AuditPage } from "./Audit";

afterEach(cleanup);

const ENTRIES: AuditOut[] = [
  {
    id: 2,
    at: "2026-11-01T10:05:00Z",
    actor: "alice",
    action: "run.rollback",
    entity_type: "ingestion_run",
    entity_id: "run-1",
    detail: { nodes: 1, review_items_closed: 1 },
  },
  {
    id: 1,
    at: "2026-11-01T10:00:00Z",
    actor: "bob",
    action: "proposal.approve",
    entity_type: "extension_proposal",
    entity_id: "3",
    detail: { draft: 2 },
  },
];

test("fetches the latest 200 entries and lists them newest first", async () => {
  const { calls } = renderWithApi(<AuditPage />, { "GET /api/audit": ENTRIES });
  const table = await screen.findByRole("table", { name: "Audit log" });
  await within(table).findByText("run.rollback");
  expect(calls).toHaveLength(1);
  expect(calls[0]).toMatchObject({ method: "GET", path: "/api/audit", search: "?limit=200", body: undefined });

  await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(3));
  const [, first, second] = within(table).getAllByRole("row");
  expect(first!.textContent).toContain("alice");
  expect(first!.textContent).toContain("ingestion_run run-1");
  expect(first!.textContent).toContain('{"nodes":1,"review_items_closed":1}');
  expect(second!.textContent).toContain("proposal.approve");
  expect(second!.textContent).toContain('{"draft":2}');
});
