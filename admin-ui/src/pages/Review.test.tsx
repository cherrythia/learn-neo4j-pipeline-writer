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
  source_ref: "zorblax_invoice_INV-2025-0418.pdf",
  candidate: { kind: "entity", class_name: "Software", properties: { software_id: "SW-QDOC", name: "Quillstack Docs" } },
  evidence: [{ source_type: "pdf", source_ref: "zorblax_invoice_INV-2025-0418.pdf", locator: "page 1", quote: "Quillstack Docs" }],
  score: 0.7125,
  breakdown: {
    value: 0.7125,
    components: {
      grounding: { value: 1, weight: 0.25, contribution: 0.25 },
      back_translation: { value: 0.5, weight: 0.2, contribution: 0.1 },
    },
    decision: "review",
    reasons: ["score 0.7125 is between 0.6 and 0.85: needs review"],
    notes: ["no existing Software matched"],
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
  expect(screen.getByText("no existing Software matched")).toBeDefined();
  expect(screen.getByTestId("evidence").textContent).toContain("page 1");

  fireEvent.click(screen.getByRole("button", { name: "Approve" }));
  await waitFor(() => expect(calls.some((c) => c.path === "/api/review/7/approve")).toBe(true));
  const approve = calls.find((c) => c.path === "/api/review/7/approve")!;
  expect(approve.method).toBe("POST");
  expect(approve.body).toEqual({ merge: false });
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
    "GET /api/review": [{ ...ITEM, reason: "conflict", conflict_with: { message: "software_id SW-QDOC already has name X" } }],
  });
  fireEvent.click(await screen.findByTestId("review-row-7"));
  expect(screen.getByText("software_id SW-QDOC already has name X")).toBeDefined();
});

const DUPLICATE: ReviewItem = {
  ...ITEM,
  reason: "duplicate",
  candidate: { kind: "entity", class_name: "Software", properties: { software_id: "SW-QDX", name: "Quillstack Docs (Demo)" } },
  conflict_with: {
    message: "Software 'SW-QDX' is a possible duplicate of Software 'SW-QDOC' (name similarity 0.97): needs review",
    duplicate_of: {
      class_name: "Software",
      key_property: "software_id",
      candidate_key: "SW-QDX",
      key: "SW-QDOC",
      similarity: 0.97,
    },
  },
};

test("a possible duplicate shows the existing entity and can be merged into it", async () => {
  const { calls } = renderWithApi(<ReviewPage />, {
    "GET /api/review": [DUPLICATE],
    "POST /api/review/7/approve": { status: "approved" },
  });
  fireEvent.click(await screen.findByTestId("review-row-7"));
  const alert = screen.getByTestId("duplicate-of");
  expect(alert.textContent).toContain("Software software_id = SW-QDOC");
  expect(alert.textContent).toContain("0.97");
  fireEvent.click(screen.getByRole("button", { name: "Merge into SW-QDOC" }));
  await waitFor(() => expect(calls.find((c) => c.path === "/api/review/7/approve")?.body).toEqual({ merge: true }));
});

test("a possible duplicate can be kept as a new entity", async () => {
  const { calls } = renderWithApi(<ReviewPage />, {
    "GET /api/review": [DUPLICATE],
    "POST /api/review/7/approve": { status: "approved" },
  });
  fireEvent.click(await screen.findByTestId("review-row-7"));
  fireEvent.click(screen.getByRole("button", { name: "Keep as new" }));
  await waitFor(() => expect(calls.find((c) => c.path === "/api/review/7/approve")?.body).toEqual({ merge: false }));
});

test("a conflict that involves a possible duplicate shows both and can be merged", async () => {
  const { calls } = renderWithApi(<ReviewPage />, {
    "GET /api/review": [
      {
        ...DUPLICATE,
        reason: "conflict",
        conflict_with: { ...DUPLICATE.conflict_with, message: "LICENSES is many_to_one: already points to SW-QDOC" },
      },
    ],
    "POST /api/review/7/approve": { status: "approved" },
  });
  fireEvent.click(await screen.findByTestId("review-row-7"));
  expect(screen.getByText("LICENSES is many_to_one: already points to SW-QDOC")).toBeDefined();
  expect(screen.getByTestId("duplicate-of").textContent).toContain("SW-QDOC");
  expect(screen.getByRole("button", { name: "Approve (replace)" })).toBeDefined();
  fireEvent.click(screen.getByRole("button", { name: "Merge into SW-QDOC" }));
  await waitFor(() => expect(calls.find((c) => c.path === "/api/review/7/approve")?.body).toEqual({ merge: true }));
});

test("a refused merge is shown on the item", async () => {
  renderWithApi(<ReviewPage />, {
    "GET /api/review": [
      { ...DUPLICATE, conflict_with: { ...DUPLICATE.conflict_with, merge_conflict: "already points to SW-ORBT" } },
    ],
  });
  fireEvent.click(await screen.findByTestId("review-row-7"));
  expect(screen.getByTestId("merge-conflict").textContent).toContain("already points to SW-ORBT");
});

test("the duplicate filter asks for duplicate items", async () => {
  const { calls } = renderWithApi(<ReviewPage />, { "GET /api/review": [] });
  await screen.findByText("Nothing to review.");
  fireEvent.click(screen.getByText("Duplicate"));
  await waitFor(() => expect(calls.some((c) => c.search.includes("reason=duplicate"))).toBe(true));
});

test("reject sends the note", async () => {
  const { calls } = renderWithApi(<ReviewPage />, {
    "GET /api/review": [ITEM],
    "POST /api/review/7/reject": { status: "rejected" },
  });
  fireEvent.click(await screen.findByTestId("review-row-7"));
  fireEvent.click(screen.getByRole("button", { name: "Reject" }));
  fireEvent.change(await screen.findByLabelText("Note"), { target: { value: "wrong software" } });
  const dialog = screen.getByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Reject" }));
  await waitFor(() => expect(calls.find((c) => c.path === "/api/review/7/reject")?.body).toEqual({ note: "wrong software" }));
});

test("edit then approve sends the edited candidate", async () => {
  const { calls } = renderWithApi(<ReviewPage />, {
    "GET /api/review": [ITEM],
    "POST /api/review/7/approve": { status: "approved" },
  });
  fireEvent.click(await screen.findByTestId("review-row-7"));
  fireEvent.click(screen.getByRole("button", { name: "Edit then approve" }));
  fireEvent.change(await screen.findByLabelText("name"), { target: { value: "Quillstack Docs Suite" } });
  fireEvent.click(screen.getByRole("button", { name: "Approve edited" }));
  await waitFor(() =>
    expect(calls.find((c) => c.path === "/api/review/7/approve")?.body).toEqual({
      edited: { ...ITEM.candidate, properties: { software_id: "SW-QDOC", name: "Quillstack Docs Suite" } },
      merge: false,
    }),
  );
});

test("switching items resets the edit form and reject note (review round 1)", async () => {
  const other: ReviewItem = {
    ...ITEM,
    id: 8,
    candidate: { kind: "entity", class_name: "Software", properties: { software_id: "SW-ORBT", name: "Orbwyn Endpoint Guard" } },
    evidence: [{ source_type: "pdf", source_ref: "zorblax_invoice_INV-2025-0418.pdf", locator: "page 2", quote: "Orbwyn Endpoint Guard" }],
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
  expect(name.value).toBe("Orbwyn Endpoint Guard");
  fireEvent.click(screen.getByRole("button", { name: "Approve edited" }));
  await waitFor(() =>
    expect(calls.find((c) => c.path === "/api/review/8/approve")?.body).toEqual({
      edited: other.candidate,
      merge: false,
    }),
  );
});

test("edit keeps untouched values typed, and an emptied field becomes null (review round 2)", async () => {
  const typed: ReviewItem = {
    ...ITEM,
    id: 9,
    candidate: {
      kind: "entity",
      class_name: "SoftwareLicense",
      properties: { asset_tag: "SL-25-0201", name: null, seats: 5, perpetual: true },
    },
  };
  const { calls } = renderWithApi(<ReviewPage />, {
    "GET /api/review": [typed],
    "POST /api/review/9/approve": { status: "approved" },
  });
  fireEvent.click(await screen.findByTestId("review-row-9"));
  fireEvent.click(screen.getByRole("button", { name: "Edit then approve" }));
  expect(((await screen.findByLabelText("name")) as HTMLInputElement).value).toBe("");
  fireEvent.change(screen.getByLabelText("asset_tag"), { target: { value: "SL-25-0299" } });
  fireEvent.change(screen.getByLabelText("seats"), { target: { value: "" } });
  fireEvent.change(screen.getByLabelText("perpetual"), { target: { value: "False" } });
  fireEvent.click(screen.getByRole("button", { name: "Approve edited" }));
  await waitFor(() =>
    expect(calls.find((c) => c.path === "/api/review/9/approve")?.body).toEqual({
      edited: {
        ...typed.candidate,
        properties: { asset_tag: "SL-25-0299", name: null, seats: null, perpetual: false },
      },
      merge: false,
    }),
  );
});
