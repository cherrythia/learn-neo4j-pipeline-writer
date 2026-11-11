import { Button, Card, Group, PasswordInput, Select, Stack, Switch, Table, TextInput, Title } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { unwrap } from "../api/client";
import { useApi } from "../api/context";
import type { Me } from "../api/types";
import { ErrorAlert, when } from "../components/common";

type Role = "admin" | "reviewer";

export function UsersPage({ me }: { me: Me }) {
  const api = useApi();
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => unwrap(api.GET("/api/users")) });
  const refresh = () => qc.invalidateQueries({ queryKey: ["users"] });
  const [form, setForm] = useState({ username: "", password: "", role: "reviewer" as Role });
  const create = useMutation({
    mutationFn: () => unwrap(api.POST("/api/users", { body: form })),
    onSuccess: () => {
      setForm({ username: "", password: "", role: "reviewer" });
      void refresh();
    },
  });
  const patch = useMutation({
    mutationFn: (a: { username: string; role?: Role; active?: boolean }) =>
      unwrap(
        api.PATCH("/api/users/{username}", {
          params: { path: { username: a.username } },
          body: { role: a.role ?? null, active: a.active ?? null },
        }),
      ),
    onSuccess: refresh,
  });

  return (
    <Stack>
      <Title order={2}>Users</Title>
      <Card withBorder>
        <Group align="end">
          <TextInput label="Username" value={form.username} onChange={(e) => setForm({ ...form, username: e.currentTarget.value })} />
          <PasswordInput
            label="Password"
            description="At least 12 characters"
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.currentTarget.value })}
          />
          <Select label="Role" data={["reviewer", "admin"]} value={form.role} onChange={(v) => setForm({ ...form, role: (v ?? "reviewer") as Role })} w={140} />
          <Button onClick={() => create.mutate()} loading={create.isPending}>
            Create user
          </Button>
        </Group>
        <ErrorAlert error={create.error} title="Could not create the user" />
      </Card>
      <ErrorAlert error={patch.error} title="Update refused" />
      <Table aria-label="Users">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Username</Table.Th>
            <Table.Th>Role</Table.Th>
            <Table.Th>Active</Table.Th>
            <Table.Th>Created</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {(users.data ?? []).map((u) => {
            const self = u.username === me.username;
            return (
              <Table.Tr key={u.username}>
                <Table.Td>{u.username}</Table.Td>
                <Table.Td>
                  <Select
                    aria-label={`Role of ${u.username}`}
                    data={["reviewer", "admin"]}
                    value={u.role}
                    onChange={(v) => v && v !== u.role && patch.mutate({ username: u.username, role: v as Role })}
                    disabled={self}
                    w={140}
                  />
                </Table.Td>
                <Table.Td>
                  <Switch
                    aria-label={`${u.username} active`}
                    checked={u.active}
                    onChange={(e) => patch.mutate({ username: u.username, active: e.currentTarget.checked })}
                    disabled={self}
                  />
                </Table.Td>
                <Table.Td>{when(u.created_at)}</Table.Td>
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}
