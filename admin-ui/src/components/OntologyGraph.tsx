import { Background, Controls, MarkerType, ReactFlow } from "@xyflow/react";
import { useMemo } from "react";
import type { OntologySpec } from "../api/types";
import { buildGraph } from "./graphModel";

export function OntologyGraph({ spec }: { spec: OntologySpec }) {
  const { nodes, edges } = useMemo(() => {
    const g = buildGraph(spec);
    return {
      nodes: g.nodes,
      edges: g.edges.map((e) => ({ ...e, markerEnd: { type: MarkerType.ArrowClosed } })),
    };
  }, [spec]);
  return (
    <div style={{ height: 520, border: "1px solid var(--mantine-color-gray-3)" }} data-testid="ontology-graph">
      <ReactFlow nodes={nodes} edges={edges} fitView nodesConnectable={false}>
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
