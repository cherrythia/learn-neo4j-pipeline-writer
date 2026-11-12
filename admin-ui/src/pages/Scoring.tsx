import { Alert, Button, Card, Group, List, NumberInput, SimpleGrid, Stack, Text, Title } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { unwrap } from "../api/client";
import { useApi } from "../api/context";
import type { ScoringPolicy } from "../api/types";
import { ErrorAlert } from "../components/common";
import { validatePolicy } from "./scoringValidation";

const num = (v: number | string) => (typeof v === "number" ? v : Number.parseFloat(v));

export function ScoringPage({ role }: { role: string }) {
  const api = useApi();
  const qc = useQueryClient();
  const admin = role === "admin";
  const policy = useQuery({ queryKey: ["scoring"], queryFn: () => unwrap(api.GET("/api/scoring")) });
  const [draft, setDraft] = useState<ScoringPolicy | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  useEffect(() => {
    if (policy.data) setDraft(policy.data);
  }, [policy.data]);
  const save = useMutation({
    mutationFn: (body: ScoringPolicy) => unwrap(api.PUT("/api/scoring", { body })),
    onSuccess: (saved) => {
      qc.setQueryData(["scoring"], saved);
      notifications.show({ title: "Scoring policy saved", message: "New candidates use it from now on.", color: "green" });
    },
  });
  if (!draft) return null;

  const setWeight = (k: keyof ScoringPolicy["weights"], v: number | string) =>
    setDraft({ ...draft, weights: { ...draft.weights, [k]: num(v) } });
  const total = Object.values(draft.weights).reduce((s, w) => s + (Number.isFinite(w) ? w : 0), 0);

  function submit() {
    if (!draft) return;
    const problems = validatePolicy(draft);
    setErrors(problems);
    if (problems.length === 0) save.mutate(draft);
  }

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>Scoring policy</Title>
        {!admin && <Text c="dimmed">Read only (admins can edit)</Text>}
      </Group>
      <Card withBorder>
        <Text fw={600} mb="xs">
          Weights (sum {total.toFixed(4)})
        </Text>
        <SimpleGrid cols={{ base: 2, md: 3 }}>
          {(Object.keys(draft.weights) as (keyof ScoringPolicy["weights"])[]).map((k) => (
            <NumberInput
              key={k}
              label={k}
              value={draft.weights[k]}
              onChange={(v) => setWeight(k, v)}
              step={0.05}
              decimalScale={6}
              min={0}
              max={1}
              disabled={!admin}
            />
          ))}
        </SimpleGrid>
      </Card>
      <Card withBorder>
        <Text fw={600} mb="xs">
          Thresholds and runs
        </Text>
        <SimpleGrid cols={{ base: 2, md: 3 }}>
          <NumberInput label="Commit threshold" value={draft.commit_threshold} onChange={(v) => setDraft({ ...draft, commit_threshold: num(v) })} step={0.05} decimalScale={4} disabled={!admin} />
          <NumberInput label="Review threshold" value={draft.review_threshold} onChange={(v) => setDraft({ ...draft, review_threshold: num(v) })} step={0.05} decimalScale={4} disabled={!admin} />
          <NumberInput
            label="Self-consistency runs (N)"
            value={draft.self_consistency_runs}
            onChange={(v) => setDraft({ ...draft, self_consistency_runs: num(v) })}
            min={1}
            max={10}
            allowDecimal={false}
            disabled={!admin}
          />
        </SimpleGrid>
      </Card>
      <Card withBorder>
        <Text fw={600} mb="xs">
          Source reliability
        </Text>
        <SimpleGrid cols={{ base: 2, md: 3 }}>
          {Object.keys(draft.source_reliability).sort().map((k) => (
            <NumberInput
              key={k}
              label={k}
              value={draft.source_reliability[k]}
              onChange={(v) => setDraft({ ...draft, source_reliability: { ...draft.source_reliability, [k]: num(v) } })}
              step={0.05}
              decimalScale={4}
              disabled={!admin}
            />
          ))}
        </SimpleGrid>
      </Card>
      {errors.length > 0 && (
        <Alert color="red" title="Fix these before saving" data-testid="policy-errors">
          <List size="sm">
            {errors.map((e) => (
              <List.Item key={e}>{e}</List.Item>
            ))}
          </List>
        </Alert>
      )}
      <ErrorAlert error={save.error} title="Save refused" />
      {admin && (
        <Group>
          <Button onClick={submit} loading={save.isPending}>
            Save policy
          </Button>
        </Group>
      )}
    </Stack>
  );
}
