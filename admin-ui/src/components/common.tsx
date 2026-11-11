import { Alert, Badge, Code, List } from "@mantine/core";
import { ApiError } from "../api/client";

export function StatusBadge({ status }: { status: string }) {
  const color =
    {
      published: "green",
      draft: "yellow",
      retired: "gray",
      open: "blue",
      pending: "yellow",
      approved: "green",
      rejected: "red",
      commit: "green",
      review: "yellow",
      conflict: "orange",
      reject: "red",
      succeeded: "green",
      failed: "red",
      running: "blue",
      rolled_back: "gray",
      admin: "grape",
      reviewer: "blue",
    }[status] ?? "gray";
  return <Badge color={color}>{status}</Badge>;
}

export function Json({ value }: { value: unknown }) {
  return (
    <Code block style={{ maxHeight: 320, overflow: "auto" }}>
      {JSON.stringify(value, null, 2)}
    </Code>
  );
}

/** "v2 failed validation: a; b" -> ["a", "b"]: one line per gate/validation reason. */
export function reasonsOf(detail: string): string[] {
  const idx = detail.indexOf(": ");
  const body = idx >= 0 ? detail.slice(idx + 2) : detail;
  const parts = body.split("; ").filter(Boolean);
  return parts.length > 1 ? parts : [detail];
}

/** Inline display of a failed action, e.g. a 422 publish/approve with its reasons. */
export function ErrorAlert({ error, title }: { error: unknown; title: string }) {
  if (!error) return null;
  const detail = error instanceof ApiError ? error.detail : String(error);
  const reasons = reasonsOf(detail);
  return (
    <Alert color="red" title={title} data-testid="error-alert">
      {reasons.length > 1 ? (
        <List size="sm">
          {reasons.map((r) => (
            <List.Item key={r}>{r}</List.Item>
          ))}
        </List>
      ) : (
        detail
      )}
    </Alert>
  );
}

export function when(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : "";
}
