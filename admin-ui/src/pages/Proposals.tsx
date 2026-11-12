import { Button, Card, Group, JsonInput, SegmentedControl, Select, Stack, Table, Text, TextInput, Title } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { unwrap } from "../api/client";
import { useApi } from "../api/context";
import { ErrorAlert, Json, StatusBadge, when } from "../components/common";

const EXAMPLE = {
  class: { name: "Location", identity_key: "name", parent: null, properties: [{ name: "name", required: true }] },
  relation: { name: "LOCATED_AT", domain: "HardwareAsset", range: "Location", cardinality: "many_to_one" },
};

export function ProposalsPage({ role }: { role: string }) {
  const api = useApi();
  const qc = useQueryClient();
  const admin = role === "admin";
  const [status, setStatus] = useState("pending");
  const proposals = useQuery({
    queryKey: ["proposals", status],
    queryFn: () =>
      unwrap(api.GET("/api/proposals", { params: { query: status === "all" ? {} : { status } } })),
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["proposals"] });
  const decide = useMutation({
    mutationFn: async ({ id, action }: { id: number; action: "approve" | "reject" }): Promise<unknown> =>
      action === "approve"
        ? unwrap(api.POST("/api/proposals/{proposal_id}/approve", { params: { path: { proposal_id: id } } }))
        : unwrap(api.POST("/api/proposals/{proposal_id}/reject", { params: { path: { proposal_id: id } } })),
    onSuccess: () => {
      void refresh();
      void qc.invalidateQueries({ queryKey: ["ontology"] });
    },
  });

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>Extension proposals</Title>
        <SegmentedControl
          value={status}
          onChange={setStatus}
          data={["pending", "approved", "rejected", "all"]}
        />
      </Group>
      <NewProposal onCreated={() => void refresh()} />
      <ErrorAlert error={decide.error} title="Decision failed" />
      <Table aria-label="Proposals">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>#</Table.Th>
            <Table.Th>Kind</Table.Th>
            <Table.Th>Payload</Table.Th>
            <Table.Th>Rationale</Table.Th>
            <Table.Th>Status</Table.Th>
            <Table.Th>Created</Table.Th>
            {admin && <Table.Th />}
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {(proposals.data ?? []).map((p) => (
            <Table.Tr key={p.id}>
              <Table.Td>{p.id}</Table.Td>
              <Table.Td>{p.kind}</Table.Td>
              <Table.Td maw={360}>
                <Json value={p.payload} />
              </Table.Td>
              <Table.Td>{p.rationale}</Table.Td>
              <Table.Td>
                <StatusBadge status={p.status} />
                {p.decided_by && <Text size="xs">by {p.decided_by}</Text>}
              </Table.Td>
              <Table.Td>{when(p.created_at)}</Table.Td>
              {admin && (
                <Table.Td>
                  {p.status === "pending" && (
                    <Group gap={4} wrap="nowrap">
                      <Button size="compact-xs" color="green" onClick={() => decide.mutate({ id: p.id, action: "approve" })}>
                        Approve
                      </Button>
                      <Button size="compact-xs" color="red" variant="light" onClick={() => decide.mutate({ id: p.id, action: "reject" })}>
                        Reject
                      </Button>
                    </Group>
                  )}
                </Table.Td>
              )}
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}

function NewProposal({ onCreated }: { onCreated: () => void }) {
  const api = useApi();
  const [kind, setKind] = useState<"class" | "relation">("class");
  const [payload, setPayload] = useState(JSON.stringify(EXAMPLE.class, null, 2));
  const [rationale, setRationale] = useState("");
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/proposals", {
          body: { kind, payload: JSON.parse(payload) as Record<string, unknown>, rationale: rationale || null },
        }),
      ),
    onSuccess: () => {
      setRationale("");
      onCreated();
    },
  });
  return (
    <Card withBorder>
      <Stack gap="xs">
        <Text fw={600}>Propose an extension</Text>
        <Select
          label="Kind"
          data={["class", "relation"]}
          value={kind}
          onChange={(v) => {
            const k = (v ?? "class") as "class" | "relation";
            setKind(k);
            setPayload(JSON.stringify(EXAMPLE[k], null, 2));
          }}
          w={160}
        />
        <JsonInput label="Payload" value={payload} onChange={setPayload} autosize minRows={4} validationError="Not valid JSON" />
        <TextInput label="Rationale" value={rationale} onChange={(e) => setRationale(e.currentTarget.value)} />
        <ErrorAlert error={create.error} title="Proposal refused" />
        <Group>
          <Button onClick={() => create.mutate()} loading={create.isPending}>
            Submit proposal
          </Button>
        </Group>
      </Stack>
    </Card>
  );
}
