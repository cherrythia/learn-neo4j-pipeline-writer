// Ontology spec -> React Flow nodes and edges. Pure, so it is unit-tested without a canvas.
import type { Edge, Node } from "@xyflow/react";
import type { OntologySpec } from "../api/types";

export const IS_A_DASH = "6 4";
const COL = 230;
const ROW = 150;

export interface ClassNodeData extends Record<string, unknown> {
  label: string;
  identityKey: string;
  deprecated: boolean;
}

function depthOf(name: string, parents: Map<string, string | null>): number {
  let depth = 0;
  const seen = new Set<string>();
  let current = parents.get(name) ?? null;
  while (current && !seen.has(current) && parents.has(current)) {
    seen.add(current);
    depth += 1;
    current = parents.get(current) ?? null;
  }
  return depth;
}

/** Classes as nodes (taxonomy depth -> row), relations as labelled edges, is-a edges dashed. */
export function buildGraph(spec: OntologySpec): { nodes: Node<ClassNodeData>[]; edges: Edge[] } {
  const parents = new Map(spec.classes.map((c) => [c.name, c.parent]));
  const perRow = new Map<number, number>();
  const nodes: Node<ClassNodeData>[] = spec.classes.map((c) => {
    const depth = depthOf(c.name, parents);
    const col = perRow.get(depth) ?? 0;
    perRow.set(depth, col + 1);
    return {
      id: c.name,
      position: { x: col * COL, y: depth * ROW },
      data: {
        label: c.deprecated ? `${c.name} (deprecated)` : c.name,
        identityKey: c.identity_key,
        deprecated: c.deprecated,
      },
      style: c.deprecated ? { opacity: 0.5 } : undefined,
    };
  });
  const known = new Set(spec.classes.map((c) => c.name));
  const isA: Edge[] = spec.classes
    .filter((c) => c.parent && known.has(c.parent))
    .map((c) => ({
      id: `is-a:${c.name}`,
      source: c.name,
      target: c.parent as string,
      label: "is a",
      style: { strokeDasharray: IS_A_DASH },
      data: { kind: "is-a" },
    }));
  const relations: Edge[] = spec.relations
    .filter((r) => known.has(r.domain) && known.has(r.range))
    .map((r) => ({
      id: `rel:${r.name}`,
      source: r.domain,
      target: r.range,
      label: `${r.name} (${r.cardinality})`,
      style: r.deprecated ? { stroke: "#adb5bd" } : undefined,
      data: { kind: "relation" },
    }));
  return { nodes, edges: [...isA, ...relations] };
}
