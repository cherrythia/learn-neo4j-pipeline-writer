import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { ReviewItem } from "../api/types";
import { renderWithApi } from "../testing";
import { ReviewPage } from "./Review";

afterEach(cleanup);

export const ITEM: ReviewItem = {
  id: 7,
  reason: "score",
  status: "open",
  conflict_with: null,
  reviewer: null,
  note: null,
  created_at: "2026-11-01T10:00:00Z",
  run_id: "run-1",
  source_type: "pdf",
  source_ref: "a.pdf",
  candidate: { kind: "entity", class_name: "Issuer", properties: { ticker: "APH", name: "APH Co" } },
  evidence: [{ source_type: "pdf", source_ref: "a.pdf", locator: "page 1", quote: "APH Co" }],
  score: 0.7125,
  breakdown: {
    value: 0.7125,
    components: {
      grounding: { value: 1, weight: 0.25, contribution: 0.25 },
      back_translation: { value: 0.5, weight: 0.2, contribution: 0.1 },
    },
    decision: "review",
    reasons: ["score 0.7125 is between 0.6 and 0.85: needs review"],
    notes: ["no existing Issuer matched"],
  },
};

test("shows the score breakdown, evidence and reasons, and approves", async () => {
  const { calls } = renderWithApi(<ReviewPage />, {
    "GET /api/review": [ITEM],
    "POST /api/review/7/approve": { status: "approved" },
  });
  fireEvent.click(await screen.findByTestId("review-row-7"));

  const table = screen.getByRole("table", { name: "Score breakdown" });
  const rows = within(table).getAllByRole("row");
  expect(rows.map((r) => r.textContent)).toEqual([
    "ComponentValueWeightContribution",
    "grounding1.0000.2500.250",
    "back_translation0.5000.2000.100",
    "Total0.713",
  ]);
  expect(screen.getByText(/needs review/)).toBeDefined();
  expect(screen.getByText("no existing Issuer matched")).toBeDefined();
  expect(screen.getByTestId("evidence").textContent).toContain("page 1");

  fireEvent.click(screen.getByRole("button", { name: "Approve" }));
  await waitFor(() => expect(calls.some((c) => c.path === "/api/review/7/approve")).toBe(true));
  const approve = calls.find((c) => c.path === "/api/review/7/approve")!;
  expect(approve.method).toBe("POST");
  expect(approve.body).toEqual({});
  expect(approve.headers.get("X-Requested-With")).toBe("writer-admin");
});

test("filters by reason", async () => {
  const { calls } = renderWithApi(<ReviewPage />, { "GET /api/review": [] });
  await screen.findByText("Nothing to review.");
  fireEvent.click(screen.getByText("Conflict"));
  await waitFor(() => expect(calls.some((c) => c.search.includes("reason=conflict"))).toBe(true));
});

test("conflicts show their message", async () => {
  renderWithApi(<ReviewPage />, {
    "GET /api/review": [{ ...ITEM, reason: "conflict", conflict_with: { message: "ticker APH already has name X" } }],
  });
  fireEvent.click(await screen.findByTestId("review-row-7"));
  expect(screen.getByText("ticker APH already has name X")).toBeDefined();
});

test("reject sends the note", async () => {
  const { calls } = renderWithApi(<ReviewPage />, {
    "GET /api/review": [ITEM],
    "POST /api/review/7/reject": { status: "rejected" },
  });
  fireEvent.click(await screen.findByTestId("review-row-7"));
  fireEvent.click(screen.getByRole("button", { name: "Reject" }));
  fireEvent.change(await screen.findByLabelText("Note"), { target: { value: "wrong issuer" } });
  const dialog = screen.getByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Reject" }));
  await waitFor(() => expect(calls.find((c) => c.path === "/api/review/7/reject")?.body).toEqual({ note: "wrong issuer" }));
});

test("edit then approve sends the edited candidate", async () => {
  const { calls } = renderWithApi(<ReviewPage />, {
    "GET /api/review": [ITEM],
    "POST /api/review/7/approve": { status: "approved" },
  });
  fireEvent.click(await screen.findByTestId("review-row-7"));
  fireEvent.click(screen.getByRole("button", { name: "Edit then approve" }));
  fireEvent.change(await screen.findByLabelText("name"), { target: { value: "APH Corporation" } });
  fireEvent.click(screen.getByRole("button", { name: "Approve edited" }));
  await waitFor(() =>
    expect(calls.find((c) => c.path === "/api/review/7/approve")?.body).toEqual({
      edited: { ...ITEM.candidate, properties: { ticker: "APH", name: "APH Corporation" } },
    }),
  );
});

test("switching items resets the edit form and reject note (review round 1)", async () => {
  const other: ReviewItem = {
    ...ITEM,
    id: 8,
    candidate: { kind: "entity", class_name: "Issuer", properties: { ticker: "KEP", name: "Keppel" } },
    evidence: [{ source_type: "pdf", source_ref: "a.pdf", locator: "page 2", quote: "Keppel" }],
  };
  const { calls } = renderWithApi(<ReviewPage />, {
    "GET /api/review": [ITEM, other],
    "POST /api/review/8/approve": { status: "approved" },
  });
  fireEvent.click(await screen.findByTestId("review-row-7"));
  fireEvent.click(screen.getByRole("button", { name: "Edit then approve" }));
  fireEvent.change(await screen.findByLabelText("name"), { target: { value: "edited for 7" } });
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  fireEvent.click(screen.getByTestId("review-row-8"));
  fireEvent.click(screen.getByRole("button", { name: "Edit then approve" }));
  const name = (await screen.findByLabelText("name")) as HTMLInputElement;
  expect(name.value).toBe("Keppel");
  fireEvent.click(screen.getByRole("button", { name: "Approve edited" }));
  await waitFor(() =>
    expect(calls.find((c) => c.path === "/api/review/8/approve")?.body).toEqual({
      edited: other.candidate,
    }),
  );
});

test("edit keeps untouched values typed, and an emptied field becomes null (review round 2)", async () => {
  const typed: ReviewItem = {
    ...ITEM,
    id: 9,
    candidate: {
      kind: "entity",
      class_name: "Issuer",
      properties: { ticker: "APH", name: null, shares: 5, listed: true },
    },
  };
  const { calls } = renderWithApi(<ReviewPage />, {
    "GET /api/review": [typed],
    "POST /api/review/9/approve": { status: "approved" },
  });
  fireEvent.click(await screen.findByTestId("review-row-9"));
  fireEvent.click(screen.getByRole("button", { name: "Edit then approve" }));
  expect(((await screen.findByLabelText("name")) as HTMLInputElement).value).toBe("");
  fireEvent.change(screen.getByLabelText("ticker"), { target: { value: "APHX" } });
  fireEvent.change(screen.getByLabelText("shares"), { target: { value: "" } });
  fireEvent.change(screen.getByLabelText("listed"), { target: { value: "False" } });
  fireEvent.click(screen.getByRole("button", { name: "Approve edited" }));
  await waitFor(() =>
    expect(calls.find((c) => c.path === "/api/review/9/approve")?.body).toEqual({
      edited: {
        ...typed.candidate,
        properties: { ticker: "APHX", name: null, shares: null, listed: false },
      },
    }),
  );
});
