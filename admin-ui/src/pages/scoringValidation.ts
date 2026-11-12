import type { ScoringPolicy } from "../api/types";

export const WEIGHT_TOLERANCE = 1e-6;

/** Mirrors scoring/policy.py so the admin sees problems before the PUT. */
export function validatePolicy(p: ScoringPolicy): string[] {
  const errors: string[] = [];
  const weights = Object.entries(p.weights);
  for (const [name, w] of weights) {
    if (!(w >= 0 && w <= 1)) errors.push(`weight ${name} must be between 0 and 1`);
  }
  const total = weights.reduce((s, [, w]) => s + w, 0);
  if (Math.abs(total - 1) > WEIGHT_TOLERANCE) {
    errors.push(`weights must sum to 1 (got ${total.toFixed(4)})`);
  }
  for (const [name, t] of [
    ["commit_threshold", p.commit_threshold],
    ["review_threshold", p.review_threshold],
  ] as const) {
    if (!(t >= 0 && t <= 1)) errors.push(`${name} must be between 0 and 1`);
  }
  if (!(p.review_threshold < p.commit_threshold)) {
    errors.push("review threshold must be lower than commit threshold");
  }
  for (const [source, r] of Object.entries(p.source_reliability)) {
    if (!(r >= 0 && r <= 1)) errors.push(`source reliability for ${source} must be between 0 and 1`);
  }
  const n = p.self_consistency_runs;
  if (!Number.isInteger(n) || n < 1 || n > 10) errors.push("self-consistency runs (N) must be 1 to 10");
  return errors;
}
