import { Alert, AppShell, Badge, Button, Group, NavLink, Text, Title } from "@mantine/core";
import type { ReactNode } from "react";
import { NavLink as RouterLink, useLocation } from "react-router";
import type { Me } from "../api/types";
import { useLogout } from "../auth";
import { navFor } from "../nav";

export function Shell({ me, children }: { me: Me; children: ReactNode }) {
  const location = useLocation();
  const logout = useLogout();
  return (
    <AppShell header={{ height: 56 }} navbar={{ width: 210, breakpoint: "sm" }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Title order={3}>Writer Admin</Title>
          <Group gap="xs">
            <Text size="sm" data-testid="current-user">
              {me.username}
            </Text>
            <Badge color={me.role === "admin" ? "grape" : "blue"}>{me.role}</Badge>
            <Button size="xs" variant="subtle" onClick={() => void logout()}>
              Log out
            </Button>
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="xs" aria-label="Main navigation">
        {navFor(me.role).map((item) => (
          <NavLink
            key={item.path}
            component={RouterLink}
            to={item.path}
            label={item.label}
            active={location.pathname.startsWith(item.path)}
          />
        ))}
      </AppShell.Navbar>
      <AppShell.Main>{children}</AppShell.Main>
    </AppShell>
  );
}

export function AdminOnly({ role, children }: { role: string; children: ReactNode }) {
  if (role !== "admin") {
    return (
      <Alert color="orange" title="Permission denied">
        This page is for admins only.
      </Alert>
    );
  }
  return <>{children}</>;
}
