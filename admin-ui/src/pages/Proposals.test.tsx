import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { ProposalOut } from "../api/types";
import { renderWithApi } from "../testing";
import { ProposalsPage } from "./Proposals";

afterEach(cleanup);

// happy-dom has no document.fonts, which Mantine's autosize JsonInput listens to.
const doc = document as unknown as { fonts?: unknown };
doc.fonts ??= { addEventListener() {}, removeEventListener() {} };

const proposal = (id: number, status: string, decided_by: string | null = null): ProposalOut => ({
  id,
  kind: "relation",
  payload: { name: "AUDITED_BY", domain: "Issuer", range: "Auditor", cardinality: "many_to_one" },
  rationale: `rationale ${id}`,
  proposed_by: "bob",
  status,
  decided_by,
  created_at: "2026-11-01T10:00:00Z",
});

const rowOf = (table: HTMLElement, id: number) =>
  within(table).getAllByRole("row").find((r) => r.firstElementChild?.textContent === String(id))!;

test("submits a proposal, approves and rejects pending ones, and shows the decisions", async () => {
  let rows = [proposal(1, "pending"), proposal(2, "pending")];
  const { calls } = renderWithApi(<ProposalsPage role="admin" />, {
    "GET /api/proposals": () => rows,
    "POST /api/proposals": () => {
      rows = [...rows, { ...proposal(3, "pending"), kind: "class", rationale: "auditors matter" }];
      return { id: 3 };
    },
    "POST /api/proposals/1/approve": () => {
      rows = rows.map((p) => (p.id === 1 ? { ...p, status: "approved", decided_by: "alice" } : p));
      return { draft: 2 };
    },
    "POST /api/proposals/2/reject": () => {
      rows = rows.map((p) => (p.id === 2 ? { ...p, status: "rejected", decided_by: "alice" } : p));
      return { status: "rejected" };
    },
  });
  const table = await screen.findByRole("table", { name: "Proposals" });
  await within(table).findByText("rationale 1");
  // The list is filtered to pending by default.
  expect(calls[0]).toMatchObject({ method: "GET", path: "/api/proposals", search: "?status=pending" });

  fireEvent.change(screen.getByLabelText("Rationale"), { target: { value: "auditors matter" } });
  fireEvent.click(screen.getByRole("button", { name: "Submit proposal" }));
  await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/proposals")).toBe(true));
  expect(calls.find((c) => c.method === "POST" && c.path === "/api/proposals")!.body).toEqual({
    kind: "class",
    payload: { name: "Auditor", identity_key: "name", parent: null, properties: [{ name: "name", required: true }] },
    rationale: "auditors matter",
  });
  expect(await within(table).findByText("auditors matter")).toBeDefined();
  expect((screen.getByLabelText("Rationale") as HTMLInputElement).value).toBe("");

  fireEvent.click(within(rowOf(table, 1)).getByRole("button", { name: "Approve" }));
  await waitFor(() => expect(within(rowOf(table, 1)).getByText("approved")).toBeDefined());
  const approve = calls.find((c) => c.path === "/api/proposals/1/approve")!;
  expect(approve.method).toBe("POST");
  expect(approve.body).toBeUndefined();
  expect(within(rowOf(table, 1)).getByText("by alice")).toBeDefined();
  expect(within(rowOf(table, 1)).queryByRole("button", { name: "Approve" })).toBeNull();

  fireEvent.click(within(rowOf(table, 2)).getByRole("button", { name: "Reject" }));
  await waitFor(() => expect(within(rowOf(table, 2)).getByText("rejected")).toBeDefined());
  const reject = calls.find((c) => c.path === "/api/proposals/2/reject")!;
  expect(reject.method).toBe("POST");
  expect(reject.body).toBeUndefined();
});

test("reviewers can propose but not decide", async () => {
  renderWithApi(<ProposalsPage role="reviewer" />, { "GET /api/proposals": [proposal(1, "pending")] });
  await screen.findByText("rationale 1");
  expect(screen.getByRole("button", { name: "Submit proposal" })).toBeDefined();
  expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
});
