import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "@xyflow/react/dist/style.css";

import { Center, Loader, MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { HashRouter, Navigate, Route, Routes } from "react-router";
import { ApiProvider } from "./api/context";
import { useMe } from "./auth";
import { AdminOnly, Shell } from "./components/Shell";
import { AuditPage } from "./pages/Audit";
import { LoginPage } from "./pages/Login";
import { MappingsPage } from "./pages/Mappings";
import { OntologyPage } from "./pages/Ontology";
import { ProposalsPage } from "./pages/Proposals";
import { ReviewPage } from "./pages/Review";
import { RunsPage } from "./pages/Runs";
import { ScoringPage } from "./pages/Scoring";
import { UsersPage } from "./pages/Users";
import type { Runtime } from "./runtime";

export function Providers({ runtime, children }: { runtime: Runtime; children: ReactNode }) {
  return (
    <MantineProvider>
      <Notifications position="top-right" />
      <QueryClientProvider client={runtime.queryClient}>
        <ApiProvider api={runtime.api}>{children}</ApiProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}

/** Logged out -> Login, whatever the route; logged in -> the role-aware shell. */
export function AppRoutes() {
  const me = useMe();
  if (me.isPending) {
    return (
      <Center h="100vh">
        <Loader />
      </Center>
    );
  }
  if (!me.data) return <LoginPage />;
  const user = me.data;
  return (
    <Shell me={user}>
      <Routes>
        <Route path="/" element={<Navigate to="/review" replace />} />
        <Route path="/review" element={<ReviewPage />} />
        <Route path="/ontology" element={<OntologyPage role={user.role} />} />
        <Route path="/proposals" element={<ProposalsPage role={user.role} />} />
        <Route path="/scoring" element={<ScoringPage role={user.role} />} />
        <Route path="/runs" element={<RunsPage role={user.role} />} />
        <Route path="/mappings" element={<AdminOnly role={user.role}><MappingsPage /></AdminOnly>} />
        <Route path="/audit" element={<AdminOnly role={user.role}><AuditPage /></AdminOnly>} />
        <Route path="/users" element={<AdminOnly role={user.role}><UsersPage me={user} /></AdminOnly>} />
        <Route path="*" element={<Navigate to="/review" replace />} />
      </Routes>
    </Shell>
  );
}

export function App({ runtime }: { runtime: Runtime }) {
  // Hash routing: FastAPI serves the static build under /admin/ with no server-side fallback.
  return (
    <Providers runtime={runtime}>
      <HashRouter>
        <AppRoutes />
      </HashRouter>
    </Providers>
  );
}
