import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { json, renderWithApi } from "../testing";
import { OntologyPage } from "./Ontology";

afterEach(cleanup);

const version = (n: number, status: string) => ({
  version: n,
  status,
  notes: null,
  created_by: "alice",
  created_at: "2026-11-01T10:00:00Z",
  published_at: null,
});
const DETAIL = {
  ...version(2, "draft"),
  classes: [
    {
      name: "Department",
      identity_key: "dept_code",
      properties: [{ name: "dept_code", datatype: "string", required: true }],
      parent: null,
      namespace: "core",
      deprecated: false,
    },
  ],
  relations: [],
};

test("publish shows the gate's reasons on 422", async () => {
  renderWithApi(<OntologyPage role="admin" />, {
    "GET /api/ontology/versions": [version(1, "published"), version(2, "draft")],
    "GET /api/ontology/versions/2": DETAIL,
    "POST /api/ontology/versions/2/publish": () =>
      json({ detail: "v2 failed validation: class 'X' has no properties; relation 'R' has unknown range" }, 422),
  });
  fireEvent.click(await screen.findByRole("button", { name: "Publish v2" }));
  const alert = await screen.findByTestId("error-alert");
  expect(within(alert).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
    "class 'X' has no properties",
    "relation 'R' has unknown range",
  ]);
});

test("reviewers get no edit actions", async () => {
  renderWithApi(<OntologyPage role="reviewer" />, {
    "GET /api/ontology/versions": [version(1, "published"), version(2, "draft")],
    "GET /api/ontology/versions/2": DETAIL,
  });
  await screen.findByText("v2", { selector: "h3" });
  expect(screen.queryByRole("button", { name: "Create draft" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Publish v2" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Add class" })).toBeNull();
});

test("create draft posts and selects the new version", async () => {
  const { calls } = renderWithApi(<OntologyPage role="admin" />, {
    "GET /api/ontology/versions": [version(1, "published")],
    "GET /api/ontology/versions/1": { ...DETAIL, ...version(1, "published") },
    "GET /api/ontology/versions/2": DETAIL,
    "POST /api/ontology/drafts": { version: 2 },
  });
  fireEvent.click(await screen.findByRole("button", { name: "Create draft" }));
  await waitFor(() => expect(calls.some((c) => c.path === "/api/ontology/versions/2")).toBe(true));
});

test("switching versions clears the previous version's publish error (review round 1)", async () => {
  renderWithApi(<OntologyPage role="admin" />, {
    "GET /api/ontology/versions": [version(1, "published"), version(2, "draft")],
    "GET /api/ontology/versions/1": { ...DETAIL, ...version(1, "published") },
    "GET /api/ontology/versions/2": DETAIL,
    "POST /api/ontology/versions/2/publish": () => json({ detail: "v2 failed validation: bad" }, 422),
  });
  fireEvent.click(await screen.findByRole("button", { name: "Publish v2" }));
  await screen.findByTestId("error-alert");
  const versions = screen.getByRole("table", { name: "Ontology versions" });
  fireEvent.click(within(versions).getByText("v1"));
  await screen.findByText("v1", { selector: "h3" });
  expect(screen.queryByTestId("error-alert")).toBeNull();
});
