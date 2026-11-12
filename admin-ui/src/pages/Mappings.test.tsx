import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { MappingOut } from "../api/types";
import { renderWithApi } from "../testing";
import { EXAMPLE_MAPPING, MappingsPage } from "./Mappings";

afterEach(cleanup);

// DatasetMapping in openapi.json: entities[].columns is property -> column.
const MAPPING = {
  entities: [{ class_name: "Issuer", columns: { ticker: "ticker", name: "issuer_name" } }],
  relations: [],
};
const mapping = (id: number, status: string, suggested_by: string, approved_by: string | null = null): MappingOut => ({
  id,
  dataset_id: 3,
  dataset_name: "issuers",
  mapping: MAPPING,
  status,
  suggested_by,
  approved_by,
  created_at: "2026-11-01T10:00:00Z",
});

const rowOf = (table: HTMLElement, id: number) =>
  within(table).getAllByRole("row").find((r) => r.firstElementChild?.textContent === String(id))!;

test("suggests, saves a manual draft, approves, and shows the refreshed list", async () => {
  let rows: MappingOut[] = [];
  const { calls } = renderWithApi(<MappingsPage />, {
    "GET /api/mappings": () => rows,
    "POST /api/mappings/suggest/3": () => {
      rows = [...rows, mapping(5, "draft", "llm")];
      return { id: 5, mapping: MAPPING };
    },
    "POST /api/mappings": () => {
      rows = [...rows, mapping(6, "draft", "user")];
      return { id: 6 };
    },
    "POST /api/mappings/5/approve": () => {
      rows = rows.map((m) => (m.id === 5 ? { ...m, status: "approved", approved_by: "alice" } : m));
      return { status: "approved" };
    },
  });
  const table = await screen.findByRole("table", { name: "Mappings" });
  await waitFor(() => expect(calls.some((c) => c.path === "/api/mappings")).toBe(true));

  fireEvent.change(screen.getByLabelText("Superset dataset id"), { target: { value: "3" } });
  fireEvent.click(screen.getByRole("button", { name: "Suggest mapping (LLM)" }));
  await waitFor(() => expect(rowOf(table, 5)).toBeDefined());
  const suggest = calls.find((c) => c.path === "/api/mappings/suggest/3")!;
  expect(suggest.method).toBe("POST");
  expect(suggest.body).toBeUndefined();
  expect(rowOf(table, 5).textContent).toContain("issuers");
  expect(rowOf(table, 5).textContent).toContain("llm");

  fireEvent.change(screen.getByLabelText("Manual mapping"), { target: { value: JSON.stringify(MAPPING) } });
  fireEvent.click(screen.getByRole("button", { name: "Save manual draft" }));
  await waitFor(() => expect(rowOf(table, 6)).toBeDefined());
  const create = calls.find((c) => c.method === "POST" && c.path === "/api/mappings")!;
  expect(create.body).toEqual({ dataset_id: 3, mapping: MAPPING });

  fireEvent.click(within(rowOf(table, 5)).getByRole("button", { name: "Approve" }));
  await waitFor(() => expect(within(rowOf(table, 5)).getByText("approved")).toBeDefined());
  const approve = calls.find((c) => c.path === "/api/mappings/5/approve")!;
  expect(approve.method).toBe("POST");
  expect(approve.body).toBeUndefined();
  expect(rowOf(table, 5).textContent).toContain("approved by alice");
  expect(within(rowOf(table, 5)).queryByRole("button", { name: "Approve" })).toBeNull();
  expect(within(rowOf(table, 6)).getByRole("button", { name: "Approve" })).toBeDefined();
});


test("the example mapping in the form is a valid DatasetMapping (review round 1)", () => {
  // columns must be a property -> column object, as openapi EntityMapping.columns says
  for (const e of EXAMPLE_MAPPING.entities) {
    expect(Array.isArray(e.columns)).toBe(false);
    for (const [prop, col] of Object.entries(e.columns)) {
      expect(typeof prop).toBe("string");
      expect(typeof col).toBe("string");
    }
  }
});
