import { Alert, List, Stack, Table, Text } from "@mantine/core";
import type { Breakdown } from "../api/types";

const fmt = (n: number | undefined) => (n === undefined ? "" : n.toFixed(3));

/** Score derivation: one row per signal, plus the routing reasons and matcher notes. */
export function ScoreBreakdown({ breakdown, score }: { breakdown: Breakdown; score?: number | null }) {
  const rows = Object.entries(breakdown.components ?? {});
  const total = score ?? breakdown.value;
  return (
    <Stack gap="xs">
      <Table striped withTableBorder aria-label="Score breakdown">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Component</Table.Th>
            <Table.Th>Value</Table.Th>
            <Table.Th>Weight</Table.Th>
            <Table.Th>Contribution</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map(([name, c]) => (
            <Table.Tr key={name}>
              <Table.Td>{name}</Table.Td>
              <Table.Td>{fmt(c.value)}</Table.Td>
              <Table.Td>{fmt(c.weight)}</Table.Td>
              <Table.Td>{fmt(c.contribution)}</Table.Td>
            </Table.Tr>
          ))}
          <Table.Tr>
            <Table.Td fw={700}>Total</Table.Td>
            <Table.Td />
            <Table.Td />
            <Table.Td fw={700} data-testid="score-total">
              {fmt(total ?? undefined)}
            </Table.Td>
          </Table.Tr>
        </Table.Tbody>
      </Table>
      {(breakdown.reasons ?? []).length > 0 && (
        <div>
          <Text fw={600} size="sm">
            Reasons
          </Text>
          <List size="sm">
            {breakdown.reasons!.map((r) => (
              <List.Item key={r}>{r}</List.Item>
            ))}
          </List>
        </div>
      )}
      {(breakdown.notes ?? []).length > 0 && (
        <Alert color="gray" title="Notes">
          <List size="sm">
            {breakdown.notes!.map((n) => (
              <List.Item key={n}>{n}</List.Item>
            ))}
          </List>
        </Alert>
      )}
    </Stack>
  );
}
