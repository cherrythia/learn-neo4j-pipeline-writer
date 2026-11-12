import { Code, Stack, Table, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { unwrap } from "../api/client";
import { useApi } from "../api/context";
import { when } from "../components/common";

export function AuditPage() {
  const api = useApi();
  // The API returns newest first.
  const audit = useQuery({
    queryKey: ["audit"],
    queryFn: () => unwrap(api.GET("/api/audit", { params: { query: { limit: 200 } } })),
  });
  return (
    <Stack>
      <Title order={2}>Audit log</Title>
      <Table striped aria-label="Audit log">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>When</Table.Th>
            <Table.Th>Actor</Table.Th>
            <Table.Th>Action</Table.Th>
            <Table.Th>Entity</Table.Th>
            <Table.Th>Detail</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {(audit.data ?? []).map((a) => (
            <Table.Tr key={a.id}>
              <Table.Td>{when(a.at)}</Table.Td>
              <Table.Td>{a.actor}</Table.Td>
              <Table.Td>{a.action}</Table.Td>
              <Table.Td>
                {a.entity_type} {a.entity_id}
              </Table.Td>
              <Table.Td>
                <Code>{JSON.stringify(a.detail)}</Code>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}
