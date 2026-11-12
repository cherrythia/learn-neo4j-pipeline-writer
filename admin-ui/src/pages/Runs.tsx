import { Alert, Button, Card, Code, Grid, Group, Modal, Stack, Table, Text, Title } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { unwrap } from "../api/client";
import { useApi } from "../api/context";
import type { Breakdown } from "../api/types";
import { ErrorAlert, Json, StatusBadge, when } from "../components/common";
import { summarize } from "./Review";

function Counts({ counts }: { counts: Record<string, unknown> }) {
  return (
    <Text size="xs">
      {Object.entries(counts)
        .map(([k, v]) => `${k} ${String(v)}`)
        .join(" · ")}
    </Text>
  );
}

export function RunsPage({ role }: { role: string }) {
  const api = useApi();
  const runs = useQuery({ queryKey: ["runs"], queryFn: () => unwrap(api.GET("/api/runs")) });
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <Stack>
      <Title order={2}>Ingestion runs</Title>
      <Grid>
        <Grid.Col span={{ base: 12, md: 6 }}>
          <Table highlightOnHover aria-label="Runs">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Run</Table.Th>
                <Table.Th>Source</Table.Th>
                <Table.Th>Status</Table.Th>
                <Table.Th>Counts</Table.Th>
                <Table.Th>Started</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {(runs.data ?? []).map((r) => (
                <Table.Tr
                  key={r.id}
                  onClick={() => setSelected(r.id)}
                  style={{ cursor: "pointer" }}
                  bg={r.id === selected ? "var(--mantine-color-blue-light)" : undefined}
                >
                  <Table.Td>
                    <Code>{r.id.slice(0, 8)}</Code>
                  </Table.Td>
                  <Table.Td>
                    {r.source_type}: {r.source_ref}
                  </Table.Td>
                  <Table.Td>
                    <StatusBadge status={r.status} />
                  </Table.Td>
                  <Table.Td>
                    <Counts counts={r.counts} />
                  </Table.Td>
                  <Table.Td>{when(r.started_at)}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 6 }}>
          {selected ? <RunView key={selected} id={selected} admin={role === "admin"} /> : <Text c="dimmed">Select a run.</Text>}
        </Grid.Col>
      </Grid>
    </Stack>
  );
}

function RunView({ id, admin }: { id: string; admin: boolean }) {
  const api = useApi();
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const run = useQuery({
    queryKey: ["runs", id],
    queryFn: () => unwrap(api.GET("/api/runs/{run_id}", { params: { path: { run_id: id } } })),
  });
  const rollback = useMutation({
    mutationFn: () => unwrap(api.POST("/api/runs/{run_id}/rollback", { params: { path: { run_id: id } } })),
    onSuccess: () => {
      setConfirm(false);
      void qc.invalidateQueries({ queryKey: ["runs"] });
      void qc.invalidateQueries({ queryKey: ["review"] });
    },
  });
  if (!run.data) return null;
  const r = run.data;
  return (
    <Card withBorder>
      <Stack>
        <Group justify="space-between">
          <Title order={4}>Run {r.id.slice(0, 8)}</Title>
          {admin && (
            <Button color="red" variant="light" onClick={() => setConfirm(true)}>
              Roll back run
            </Button>
          )}
        </Group>
        <Text size="sm">
          {r.source_type} {r.source_ref} · ontology v{r.ontology_version} · started by {r.started_by ?? "?"}
        </Text>
        <Counts counts={r.counts} />
        {r.error && <Text c="red">{r.error}</Text>}
        <ErrorAlert error={rollback.error} title="Rollback failed" />
        {rollback.data && ( // shown on the page: the confirmation closes on success
          <Alert color="green" title="Run rolled back" data-testid="rollback-result">
            <Json value={rollback.data} />
          </Alert>
        )}
        <Table aria-label="Candidates">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>#</Table.Th>
              <Table.Th>Fact</Table.Th>
              <Table.Th>Decision</Table.Th>
              <Table.Th>Score</Table.Th>
              <Table.Th>Why</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {r.candidates.map((c) => {
              const b = (c.breakdown ?? {}) as Breakdown;
              const failures = (c.gate_failures ?? []) as string[];
              return (
                <Table.Tr key={String(c.id)}>
                  <Table.Td>{String(c.id)}</Table.Td>
                  <Table.Td>{summarize(c.payload as Record<string, unknown>)}</Table.Td>
                  <Table.Td>
                    <StatusBadge status={String(c.decision)} />
                  </Table.Td>
                  <Table.Td>{typeof c.score === "number" ? c.score.toFixed(3) : ""}</Table.Td>
                  <Table.Td>
                    <Text size="xs">{[...failures, ...(b.reasons ?? [])].join("; ")}</Text>
                  </Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      </Stack>
      <Modal opened={confirm} onClose={() => setConfirm(false)} title="Roll back this run?">
        <Stack>
          <Text size="sm">
            Deletes what only this run wrote to Neo4j, repairs provenance and closes its open review items.
          </Text>
          <Button color="red" onClick={() => rollback.mutate()} loading={rollback.isPending}>
            Confirm rollback
          </Button>
        </Stack>
      </Modal>
    </Card>
  );
}
