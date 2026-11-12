import { expect, test } from "bun:test";
import type { OntologySpec } from "../api/types";
import { buildGraph, IS_A_DASH } from "./graphModel";

const prop = (name: string) => ({ name, datatype: "string", required: true });
const SPEC: OntologySpec = {
  classes: [
    { name: "Asset", identity_key: "asset_tag", properties: [prop("asset_tag")], parent: null, namespace: "core", deprecated: false },
    { name: "HardwareAsset", identity_key: "asset_tag", properties: [prop("asset_tag")], parent: "Asset", namespace: "core", deprecated: false },
    { name: "Invoice", identity_key: "invoice_number", properties: [prop("invoice_number")], parent: null, namespace: "core", deprecated: true },
  ],
  relations: [
    { name: "BILLED_ON", domain: "HardwareAsset", range: "Invoice", cardinality: "many_to_one", namespace: "core", deprecated: false },
  ],
};

test("classes become nodes, laid out by taxonomy depth", () => {
  const { nodes } = buildGraph(SPEC);
  expect(nodes.map((n) => n.id)).toEqual(["Asset", "HardwareAsset", "Invoice"]);
  const pos = Object.fromEntries(nodes.map((n) => [n.id, n.position]));
  expect(pos.Asset!.y).toBe(0);
  expect(pos.Invoice!.y).toBe(0);
  expect(pos.HardwareAsset!.y).toBeGreaterThan(0);
  expect(pos.Asset!.x).not.toBe(pos.Invoice!.x);
  expect(nodes.find((n) => n.id === "Invoice")!.data.label).toBe("Invoice (deprecated)");
});

test("relations are labelled edges and is-a edges are dashed", () => {
  const { edges } = buildGraph(SPEC);
  const isA = edges.find((e) => e.id === "is-a:HardwareAsset")!;
  expect([isA.source, isA.target, isA.label]).toEqual(["HardwareAsset", "Asset", "is a"]);
  expect(isA.style?.strokeDasharray).toBe(IS_A_DASH);
  const billed = edges.find((e) => e.id === "rel:BILLED_ON")!;
  expect([billed.source, billed.target, billed.label]).toEqual(["HardwareAsset", "Invoice", "BILLED_ON (many_to_one)"]);
  expect(billed.style?.strokeDasharray).toBeUndefined();
  expect(edges).toHaveLength(2);
});

test("edges to unknown classes are dropped", () => {
  const spec = { ...SPEC, relations: [{ ...SPEC.relations[0]!, range: "Nope" }] };
  expect(buildGraph(spec).edges.map((e) => e.id)).toEqual(["is-a:HardwareAsset"]);
});
