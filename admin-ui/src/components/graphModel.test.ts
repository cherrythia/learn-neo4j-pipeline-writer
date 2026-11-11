import { expect, test } from "bun:test";
import type { OntologySpec } from "../api/types";
import { buildGraph, IS_A_DASH } from "./graphModel";

const prop = (name: string) => ({ name, datatype: "string", required: true });
const SPEC: OntologySpec = {
  classes: [
    { name: "Party", identity_key: "id", properties: [prop("id")], parent: null, namespace: "core", deprecated: false },
    { name: "Issuer", identity_key: "ticker", properties: [prop("ticker")], parent: "Party", namespace: "core", deprecated: false },
    { name: "Filing", identity_key: "filing_id", properties: [prop("filing_id")], parent: null, namespace: "core", deprecated: true },
  ],
  relations: [
    { name: "FILED", domain: "Issuer", range: "Filing", cardinality: "one_to_many", namespace: "core", deprecated: false },
  ],
};

test("classes become nodes, laid out by taxonomy depth", () => {
  const { nodes } = buildGraph(SPEC);
  expect(nodes.map((n) => n.id)).toEqual(["Party", "Issuer", "Filing"]);
  const pos = Object.fromEntries(nodes.map((n) => [n.id, n.position]));
  expect(pos.Party!.y).toBe(0);
  expect(pos.Filing!.y).toBe(0);
  expect(pos.Issuer!.y).toBeGreaterThan(0);
  expect(pos.Party!.x).not.toBe(pos.Filing!.x);
  expect(nodes.find((n) => n.id === "Filing")!.data.label).toBe("Filing (deprecated)");
});

test("relations are labelled edges and is-a edges are dashed", () => {
  const { edges } = buildGraph(SPEC);
  const isA = edges.find((e) => e.id === "is-a:Issuer")!;
  expect([isA.source, isA.target, isA.label]).toEqual(["Issuer", "Party", "is a"]);
  expect(isA.style?.strokeDasharray).toBe(IS_A_DASH);
  const filed = edges.find((e) => e.id === "rel:FILED")!;
  expect([filed.source, filed.target, filed.label]).toEqual(["Issuer", "Filing", "FILED (one_to_many)"]);
  expect(filed.style?.strokeDasharray).toBeUndefined();
  expect(edges).toHaveLength(2);
});

test("edges to unknown classes are dropped", () => {
  const spec = { ...SPEC, relations: [{ ...SPEC.relations[0]!, range: "Nope" }] };
  expect(buildGraph(spec).edges.map((e) => e.id)).toEqual(["is-a:Issuer"]);
});
