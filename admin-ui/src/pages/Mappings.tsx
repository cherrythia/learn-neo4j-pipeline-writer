import { Button, Card, Group, JsonInput, NumberInput, Stack, Table, Text, Title } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { unwrap } from "../api/client";
import { useApi } from "../api/context";
import { ErrorAlert, Json, StatusBadge, when } from "../components/common";

export const EXAMPLE_MAPPING = {
  entities: [
    {
      class_name: "Issuer",
      columns: { ticker: "ticker", name: "name" }, // ontology property -> dataset column
    },
  ],
  relations: [],
};

export function MappingsPage() {
  const api = useApi();
  const qc = useQueryClient();
  const mappings = useQuery({ queryKey: ["mappings"], queryFn: () => unwrap(api.GET("/api/mappings")) });
  const refresh = () => qc.invalidateQueries({ queryKey: ["mappings"] });
  const [datasetId, setDatasetId] = useState<number | string>(1);
  const [mapping, setMapping] = useState(JSON.stringify(EXAMPLE_MAPPING, null, 2));
  const id = Number(datasetId);

  const suggest = useMutation({
    mutationFn: () => unwrap(api.POST("/api/mappings/suggest/{dataset_id}", { params: { path: { dataset_id: id } } })),
    onSuccess: refresh,
  });
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/mappings", {
          body: { dataset_id: id, mapping: JSON.parse(mapping) as never },
        }),
      ),
    onSuccess: refresh,
  });
  const approve = useMutation({
    mutationFn: (mappingId: number) =>
      unwrap(api.POST("/api/mappings/{mapping_id}/approve", { params: { path: { mapping_id: mappingId } } })),
    onSuccess: refresh,
  });

  return (
    <Stack>
      <Title order={2}>Column mappings</Title>
      <Card withBorder>
        <Stack gap="xs">
          <NumberInput label="Superset dataset id" value={datasetId} onChange={setDatasetId} min={1} w={200} />
          <Group>
            <Button onClick={() => suggest.mutate()} loading={suggest.isPending}>
              Suggest mapping (LLM)
            </Button>
          </Group>
          <ErrorAlert error={suggest.error} title="Suggestion failed" />
          <JsonInput label="Manual mapping" value={mapping} onChange={setMapping} autosize minRows={6} validationError="Not valid JSON" />
          <Group>
            <Button variant="light" onClick={() => create.mutate()} loading={create.isPending}>
              Save manual draft
            </Button>
          </Group>
          <ErrorAlert error={create.error} title="Could not save the draft" />
        </Stack>
      </Card>
      <ErrorAlert error={approve.error} title="Approval refused" />
      <Table aria-label="Mappings">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>#</Table.Th>
            <Table.Th>Dataset</Table.Th>
            <Table.Th>Mapping</Table.Th>
            <Table.Th>Status</Table.Th>
            <Table.Th>Created</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {(mappings.data ?? []).map((m) => (
            <Table.Tr key={m.id}>
              <Table.Td>{m.id}</Table.Td>
              <Table.Td>
                {m.dataset_name} <Text size="xs">id {m.dataset_id}</Text>
              </Table.Td>
              <Table.Td maw={420}>
                <Json value={m.mapping} />
              </Table.Td>
              <Table.Td>
                <StatusBadge status={m.status} />
                <Text size="xs">
                  {m.suggested_by}
                  {m.approved_by ? ` · approved by ${m.approved_by}` : ""}
                </Text>
              </Table.Td>
              <Table.Td>{when(m.created_at)}</Table.Td>
              <Table.Td>
                {m.status === "draft" && (
                  <Button size="compact-xs" color="green" onClick={() => approve.mutate(m.id)}>
                    Approve
                  </Button>
                )}
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}
