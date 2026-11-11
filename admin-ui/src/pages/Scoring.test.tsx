import { afterEach, describe, expect, test } from "bun:test";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import type { ScoringPolicy } from "../api/types";
import { renderWithApi } from "../testing";
import { ScoringPage } from "./Scoring";
import { validatePolicy } from "./scoringValidation";

afterEach(cleanup);

const POLICY: ScoringPolicy = {
  weights: {
    grounding: 0.25,
    back_translation: 0.2,
    stability: 0.15,
    source_reliability: 0.2,
    entity_match: 0.1,
    self_confidence: 0.1,
  },
  commit_threshold: 0.85,
  review_threshold: 0.6,
  source_reliability: { superset: 0.95, excel: 0.85, pdf: 0.75 },
  self_consistency_runs: 3,
};

describe("validatePolicy", () => {
  test("accepts the default policy", () => {
    expect(validatePolicy(POLICY)).toEqual([]);
  });
  test("weights must sum to 1 within 1e-6", () => {
    const ok = { ...POLICY, weights: { ...POLICY.weights, grounding: 0.25 + 5e-7 } };
    expect(validatePolicy(ok)).toEqual([]);
    const bad = { ...POLICY, weights: { ...POLICY.weights, grounding: 0.3 } };
    expect(validatePolicy(bad)).toEqual(["weights must sum to 1 (got 1.0500)"]);
  });
  test("review threshold must be below commit", () => {
    expect(validatePolicy({ ...POLICY, review_threshold: 0.85 })).toEqual([
      "review threshold must be lower than commit threshold",
    ]);
  });
  test("N and reliabilities are range-checked", () => {
    const errs = validatePolicy({ ...POLICY, self_consistency_runs: 11, source_reliability: { ...POLICY.source_reliability, pdf: 1.5 } });
    expect(errs).toHaveLength(2);
  });
});

test("admin: invalid weights block the PUT", async () => {
  const { calls } = renderWithApi(<ScoringPage role="admin" />, { "GET /api/scoring": POLICY, "PUT /api/scoring": POLICY });
  const grounding = await screen.findByLabelText("grounding");
  fireEvent.change(grounding, { target: { value: "0.5" } });
  fireEvent.click(screen.getByRole("button", { name: "Save policy" }));
  expect((await screen.findByTestId("policy-errors")).textContent).toContain("weights must sum to 1");
  expect(calls.some((c) => c.method === "PUT")).toBe(false);
});

test("admin: a valid policy is PUT", async () => {
  const { calls } = renderWithApi(<ScoringPage role="admin" />, { "GET /api/scoring": POLICY, "PUT /api/scoring": POLICY });
  fireEvent.click(await screen.findByRole("button", { name: "Save policy" }));
  await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual(POLICY));
});

test("reviewer: read only", async () => {
  renderWithApi(<ScoringPage role="reviewer" />, { "GET /api/scoring": POLICY });
  expect(((await screen.findByLabelText("grounding")) as HTMLInputElement).disabled).toBe(true);
  expect(screen.queryByRole("button", { name: "Save policy" })).toBeNull();
});
