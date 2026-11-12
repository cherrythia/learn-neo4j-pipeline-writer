import {
  Alert,
  Button,
  Card,
  Grid,
  Group,
  JsonInput,
  Modal,
  SegmentedControl,
  Stack,
  Table,
  Tabs,
  Text,
  Textarea,
  TextInput,
  Title,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { unwrap } from "../api/client";
import { useApi } from "../api/context";
import type { Breakdown, EvidenceItem, ReviewItem } from "../api/types";
import { ErrorAlert, Json, StatusBadge } from "../components/common";
import { ScoreBreakdown } from "../components/ScoreBreakdown";

export function summarize(candidate: Record<string, unknown>): string {
  if (candidate.kind === "relation") {
    const s = candidate.source as { class_name?: string; key?: unknown } | undefined;
    const t = candidate.target as { class_name?: string; key?: unknown } | undefined;
    return `${s?.class_name ?? "?"} -[${String(candidate.relation)}]-> ${t?.class_name ?? "?"}`;
  }
  const props = (candidate.properties ?? {}) as Record<string, unknown>;
  const first = Object.values(props)[0];
  return `${String(candidate.class_name ?? "?")}${first !== undefined ? `: ${String(first)}` : ""}`;
}

type Filter = "all" | "score" | "conflict";

export function ReviewPage() {
  const api = useApi();
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState<number | null>(null);
  const items = useQuery({
    queryKey: ["review", filter],
    queryFn: () =>
      unwrap(
        api.GET("/api/review", {
          params: { query: { status: "open", ...(filter === "all" ? {} : { reason: filter }) } },
        }),
      ),
  });
  const current = items.data?.find((i) => i.id === selected) ?? null;

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>Review queue</Title>
        <SegmentedControl
          value={filter}
          onChange={(v) => setFilter(v as Filter)}
          data={[
            { label: "All", value: "all" },
            { label: "Score", value: "score" },
            { label: "Conflict", value: "conflict" },
          ]}
        />
      </Group>
      <Grid>
        <Grid.Col span={{ base: 12, md: 5 }}>
          {items.data?.length === 0 && <Text c="dimmed">Nothing to review.</Text>}
          <Table highlightOnHover aria-label="Review items">
            <Table.Tbody>
              {(items.data ?? []).map((item) => (
                <Table.Tr
                  key={item.id}
                  onClick={() => setSelected(item.id)}
                  style={{ cursor: "pointer" }}
                  bg={item.id === selected ? "var(--mantine-color-blue-light)" : undefined}
                  data-testid={`review-row-${item.id}`}
                >
                  <Table.Td>#{item.id}</Table.Td>
                  <Table.Td>
                    <StatusBadge status={item.reason} />
                  </Table.Td>
                  <Table.Td>{summarize(item.candidate)}</Table.Td>
                  <Table.Td>{item.score?.toFixed(3) ?? ""}</Table.Td>
                  <Table.Td>
                    <Text size="xs" c="dimmed">
                      {item.source_type}: {item.source_ref}
                    </Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 7 }}>
          {current ? (
            // keyed: switching items must not carry over edits, notes or errors
            <ReviewDetail key={current.id} item={current} onDone={() => setSelected(null)} />
          ) : (
            <Text c="dimmed">Select an item to see its evidence and score.</Text>
          )}
        </Grid.Col>
      </Grid>
    </Stack>
  );
}

export function ReviewDetail({ item, onDone }: { item: ReviewItem; onDone: () => void }) {
  const api = useApi();
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  const done = () => {
    void qc.invalidateQueries({ queryKey: ["review"] });
    onDone();
  };
  const approve = useMutation({
    mutationFn: (edited: Record<string, unknown> | null) =>
      unwrap(
        api.POST("/api/review/{item_id}/approve", {
          params: { path: { item_id: item.id } },
          body: edited ? { edited } : {},
        }),
      ),
    onSuccess: () => {
      setEditing(false);
      done();
    },
  });
  const reject = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/review/{item_id}/reject", {
          params: { path: { item_id: item.id } },
          body: { note: note || null },
        }),
      ),
    onSuccess: () => {
      setRejecting(false);
      done();
    },
  });
  const evidence = item.evidence as EvidenceItem[];

  return (
    <Card withBorder>
      <Stack>
        <Group justify="space-between">
          <Title order={4}>
            #{item.id} {summarize(item.candidate)}
          </Title>
          <StatusBadge status={item.reason} />
        </Group>
        {item.conflict_with && (
          <Alert color="orange" title="Conflict">
            {String((item.conflict_with as { message?: unknown }).message ?? JSON.stringify(item.conflict_with))}
          </Alert>
        )}
        <Text fw={600}>Candidate</Text>
        <Json value={item.candidate} />
        <Text fw={600}>Evidence</Text>
        {evidence.map((e, i) => (
          <Card key={i} withBorder p="xs" data-testid="evidence">
            <Text size="sm" fs="italic">
              “{e.quote}”
            </Text>
            <Text size="xs" c="dimmed">
              {e.source_type} {e.source_ref} · {e.locator}
            </Text>
          </Card>
        ))}
        <Text fw={600}>Score</Text>
        <ScoreBreakdown breakdown={item.breakdown as Breakdown} score={item.score} />
        <ErrorAlert error={approve.error} title="Approve failed" />
        <ErrorAlert error={reject.error} title="Reject failed" />
        <Group>
          <Button color="green" onClick={() => approve.mutate(null)} loading={approve.isPending}>
            Approve
          </Button>
          <Button variant="light" onClick={() => setEditing(true)}>
            Edit then approve
          </Button>
          <Button color="red" variant="light" onClick={() => setRejecting(true)}>
            Reject
          </Button>
        </Group>
      </Stack>
      {editing && ( // mounted only while open, so it starts from the current candidate
        <EditModal
          opened
          candidate={item.candidate}
          onClose={() => setEditing(false)}
          onSubmit={(edited) => approve.mutate(edited)}
          busy={approve.isPending}
        />
      )}
      <Modal opened={rejecting} onClose={() => setRejecting(false)} title="Reject candidate">
        <Stack>
          <Textarea label="Note" value={note} onChange={(e) => setNote(e.currentTarget.value)} />
          <Button color="red" onClick={() => reject.mutate()} loading={reject.isPending}>
            Reject
          </Button>
        </Stack>
      </Modal>
    </Card>
  );
}

const asText = (v: unknown) => (v === null || v === undefined ? "" : String(v));

/** Text from the form back to a property value. Untouched fields keep their original value
 * (and type, including null); an emptied field is null; numbers and booleans are parsed,
 * and anything unparseable is sent as typed so the server's schema gate reports it. */
export function coerce(original: unknown, text: string): unknown {
  if (text === asText(original)) return original;
  if (text.trim() === "") return null;
  if (typeof original === "number") {
    const n = Number(text);
    return Number.isNaN(n) ? text : n;
  }
  if (typeof original === "boolean") {
    const t = text.trim().toLowerCase();
    return t === "true" ? true : t === "false" ? false : text;
  }
  return text;
}

function EditModal({
  opened,
  candidate,
  onClose,
  onSubmit,
  busy,
}: {
  opened: boolean;
  candidate: Record<string, unknown>;
  onClose: () => void;
  onSubmit: (edited: Record<string, unknown>) => void;
  busy: boolean;
}) {
  const props = (candidate.properties ?? {}) as Record<string, unknown>;
  const [values, setValues] = useState<Record<string, string>>(
    Object.fromEntries(Object.entries(props).map(([k, v]) => [k, asText(v)])),
  );
  const [json, setJson] = useState(JSON.stringify(candidate, null, 2));
  const [tab, setTab] = useState<string | null>("properties");
  const [jsonError, setJsonError] = useState<string | null>(null);

  function submit() {
    if (tab === "json") {
      try {
        onSubmit(JSON.parse(json) as Record<string, unknown>);
      } catch {
        setJsonError("Not valid JSON");
      }
      return;
    }
    const properties = Object.fromEntries(
      Object.entries(values).map(([k, v]) => [k, coerce(props[k], v)]),
    );
    onSubmit({ ...candidate, properties });
  }

  return (
    <Modal opened={opened} onClose={onClose} title="Edit then approve" size="lg">
      <Tabs value={tab} onChange={setTab}>
        <Tabs.List>
          <Tabs.Tab value="properties">Properties</Tabs.Tab>
          <Tabs.Tab value="json">JSON</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="properties" pt="sm">
          <Stack>
            {Object.keys(values).map((k) => (
              <TextInput
                key={k}
                label={k}
                value={values[k]}
                onChange={(e) => {
                  const v = e.currentTarget.value;
                  setValues((s) => ({ ...s, [k]: v }));
                }}
              />
            ))}
          </Stack>
        </Tabs.Panel>
        <Tabs.Panel value="json" pt="sm">
          <JsonInput
            label="Candidate"
            value={json}
            onChange={setJson}
            autosize
            minRows={8}
            error={jsonError}
            validationError="Not valid JSON"
          />
        </Tabs.Panel>
      </Tabs>
      <Group justify="flex-end" mt="md">
        <Button color="green" onClick={submit} loading={busy}>
          Approve edited
        </Button>
      </Group>
    </Modal>
  );
}
