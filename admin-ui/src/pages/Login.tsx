import { Alert, Button, Center, Paper, PasswordInput, Stack, TextInput, Title } from "@mantine/core";
import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { THROTTLED_MESSAGE } from "../api/client";
import { useApi } from "../api/context";
import { ME_KEY } from "../auth";

export const INVALID_CREDENTIALS = "Invalid username or password.";

export function LoginPage() {
  const api = useApi();
  const qc = useQueryClient();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { data, response } = await api.POST("/api/auth/login", { body: { username, password } });
      if (response.ok && data) {
        qc.setQueryData(ME_KEY, data);
        return;
      }
      if (response.status === 401) setError(INVALID_CREDENTIALS);
      else if (response.status === 429) setError(THROTTLED_MESSAGE);
      else setError(`Login failed (${response.status}).`);
    } catch {
      setError("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Center h="100vh">
      <Paper withBorder p="xl" w={360} component="form" onSubmit={submit}>
        <Stack>
          <Title order={2}>Writer Admin</Title>
          {error && (
            <Alert color="red" role="alert">
              {error}
            </Alert>
          )}
          <TextInput
            label="Username"
            value={username}
            onChange={(e) => setUsername(e.currentTarget.value)}
            required
            autoComplete="username"
          />
          <PasswordInput
            label="Password"
            value={password}
            onChange={(e) => setPassword(e.currentTarget.value)}
            required
            autoComplete="current-password"
          />
          <Button type="submit" loading={busy}>
            Log in
          </Button>
        </Stack>
      </Paper>
    </Center>
  );
}
