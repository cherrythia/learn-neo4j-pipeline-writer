import {
  ActionIcon,
  Badge,
  Button,
  Checkbox,
  Grid,
  Group,
  Modal,
  Select,
  Stack,
  Table,
  Tabs,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { unwrap } from "../api/client";
import { useApi } from "../api/context";
import { specOf, type ClassSpec, type OntologySpec, type PropertyIn, type VersionDetail } from "../api/types";
import { ErrorAlert, StatusBadge, when } from "../components/common";
import { OntologyGraph } from "../components/OntologyGraph";

const DATATYPES = ["string", "int", "float", "date", "bool"] as const;
const CARDINALITIES = ["one_to_one", "many_to_one", "one_to_many", "many_to_many"] as const;

export function OntologyPage({ role }: { role: string }) {
  const api = useApi();
  const qc = useQueryClient();
  const admin = role === "admin";
  const versions = useQuery({
    queryKey: ["ontology", "versions"],
    queryFn: () => unwrap(api.GET("/api/ontology/versions")),
  });
  const [picked, setPicked] = useState<number | null>(null);
  const [confirmRollback, setConfirmRollback] = useState(false);
  const list = versions.data ?? [];
  const selected = picked ?? (list.length ? list[list.length - 1]!.version : null);

  const refresh = () => qc.invalidateQueries({ queryKey: ["ontology"] });
  const createDraft = useMutation({
    mutationFn: () => unwrap(api.POST("/api/ontology/drafts")),
    onSuccess: (r) => {
      setPicked(r.version);
      void refresh();
    },
  });
  const rollback = useMutation({
    mutationFn: () => unwrap(api.POST("/api/ontology/rollback")),
    onSuccess: (r) => {
      setConfirmRollback(false);
      setPicked(r.published);
      void refresh();
    },
  });

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>Ontology</Title>
        {admin && (
          <Group>
            <Button onClick={() => createDraft.mutate()} loading={createDraft.isPending}>
              Create draft
            </Button>
            <Button color="orange" variant="light" onClick={() => setConfirmRollback(true)}>
              Roll back
            </Button>
          </Group>
        )}
      </Group>
      <ErrorAlert error={createDraft.error} title="Could not create a draft" />
      <ErrorAlert error={rollback.error} title="Rollback failed" />
      <Grid>
        <Grid.Col span={{ base: 12, md: 3 }}>
          <Table highlightOnHover aria-label="Ontology versions">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Version</Table.Th>
                <Table.Th>Status</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {list.map((v) => (
                <Table.Tr
                  key={v.version}
                  onClick={() => setPicked(v.version)}
                  style={{ cursor: "pointer" }}
                  bg={v.version === selected ? "var(--mantine-color-blue-light)" : undefined}
                >
                  <Table.Td>v{v.version}</Table.Td>
                  <Table.Td>
                    <StatusBadge status={v.status} />
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 9 }}>
          {selected !== null && <VersionView key={selected} version={selected} admin={admin} />}
        </Grid.Col>
      </Grid>
      <Modal opened={confirmRollback} onClose={() => setConfirmRollback(false)} title="Roll back the ontology?">
        <Stack>
          <Text size="sm">Re-publishes the previous version and syncs the Neo4j constraints.</Text>
          <Button color="orange" onClick={() => rollback.mutate()} loading={rollback.isPending}>
            Confirm rollback
          </Button>
        </Stack>
      </Modal>
    </Stack>
  );
}

function VersionView({ version, admin }: { version: number; admin: boolean }) {
  const api = useApi();
  const qc = useQueryClient();
  const detail = useQuery({
    queryKey: ["ontology", "version", version],
    queryFn: () => unwrap(api.GET("/api/ontology/versions/{version}", { params: { path: { version } } })),
  });
  const publish = useMutation({
    mutationFn: () =>
      unwrap(api.POST("/api/ontology/versions/{version}/publish", { params: { path: { version } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ontology"] }),
  });
  if (!detail.data) return null;
  const v: VersionDetail = detail.data;
  const spec = specOf(v);
  const editable = admin && v.status === "draft";

  return (
    <Stack>
      <Group justify="space-between">
        <Group>
          <Title order={3}>v{v.version}</Title>
          <StatusBadge status={v.status} />
          <Text size="sm" c="dimmed">
            by {v.created_by}
            {v.published_at ? ` · published ${when(v.published_at)}` : ""}
          </Text>
        </Group>
        {editable && (
          <Button color="green" onClick={() => publish.mutate()} loading={publish.isPending}>
            Publish v{v.version}
          </Button>
        )}
      </Group>
      <ErrorAlert error={publish.error} title="Publish blocked" />
      {editable && <DraftActions version={v.version} spec={spec} />}
      <Tabs defaultValue="graph">
        <Tabs.List>
          <Tabs.Tab value="graph">Graph</Tabs.Tab>
          <Tabs.Tab value="classes">Classes ({spec.classes.length})</Tabs.Tab>
          <Tabs.Tab value="relations">Relations ({spec.relations.length})</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="graph" pt="sm">
          <OntologyGraph spec={spec} />
        </Tabs.Panel>
        <Tabs.Panel value="classes" pt="sm">
          <ClassesTable version={v.version} spec={spec} editable={editable} />
        </Tabs.Panel>
        <Tabs.Panel value="relations" pt="sm">
          <RelationsTable version={v.version} spec={spec} editable={editable} />
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}

function usePatch(version: number) {
  const api = useApi();
  const qc = useQueryClient();
  const onSuccess = () => qc.invalidateQueries({ queryKey: ["ontology", "version", version] });
  const patchClass = useMutation({
    mutationFn: (a: { name: string; body: { deprecated?: boolean; add_properties?: PropertyIn[] } }) =>
      unwrap(
        api.PATCH("/api/ontology/versions/{version}/classes/{name}", {
          params: { path: { version, name: a.name } },
          body: { add_properties: [], ...a.body },
        }),
      ),
    onSuccess,
  });
  const patchRelation = useMutation({
    mutationFn: (a: { name: string; deprecated: boolean }) =>
      unwrap(
        api.PATCH("/api/ontology/versions/{version}/relations/{name}", {
          params: { path: { version, name: a.name } },
          body: { deprecated: a.deprecated },
        }),
      ),
    onSuccess,
  });
  return { patchClass, patchRelation };
}

function ClassesTable({ version, spec, editable }: { version: number; spec: OntologySpec; editable: boolean }) {
  const { patchClass } = usePatch(version);
  const [addingTo, setAddingTo] = useState<ClassSpec | null>(null);
  return (
    <>
      <ErrorAlert error={patchClass.error} title="Update failed" />
      <Table aria-label="Classes">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Class</Table.Th>
            <Table.Th>Parent</Table.Th>
            <Table.Th>Identity key</Table.Th>
            <Table.Th>Properties</Table.Th>
            {editable && <Table.Th />}
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {spec.classes.map((c) => (
            <Table.Tr key={c.name} opacity={c.deprecated ? 0.5 : 1}>
              <Table.Td>
                {c.name} {c.namespace === "ext" && <Badge size="xs">ext</Badge>}{" "}
                {c.deprecated && <Badge size="xs" color="gray">deprecated</Badge>}
              </Table.Td>
              <Table.Td>{c.parent ?? ""}</Table.Td>
              <Table.Td>{c.identity_key}</Table.Td>
              <Table.Td>
                <Text size="xs">
                  {c.properties.map((p) => `${p.name}: ${p.datatype}${p.required ? "!" : ""}`).join(", ")}
                </Text>
              </Table.Td>
              {editable && (
                <Table.Td>
                  <Group gap={4} wrap="nowrap">
                    <Button size="compact-xs" variant="light" onClick={() => setAddingTo(c)}>
                      Add property
                    </Button>
                    <Button
                      size="compact-xs"
                      variant="light"
                      color="gray"
                      onClick={() => patchClass.mutate({ name: c.name, body: { deprecated: !c.deprecated } })}
                    >
                      {c.deprecated ? "Restore" : "Deprecate"}
                    </Button>
                  </Group>
                </Table.Td>
              )}
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <Modal opened={addingTo !== null} onClose={() => setAddingTo(null)} title={`Add property to ${addingTo?.name}`}>
        <PropertiesEditor
          initial={[emptyProp()]}
          submitLabel="Add"
          onSubmit={(props) =>
            addingTo &&
            patchClass.mutate(
              { name: addingTo.name, body: { add_properties: props } },
              { onSuccess: () => setAddingTo(null) },
            )
          }
        />
      </Modal>
    </>
  );
}

function RelationsTable({ version, spec, editable }: { version: number; spec: OntologySpec; editable: boolean }) {
  const { patchRelation } = usePatch(version);
  return (
    <>
      <ErrorAlert error={patchRelation.error} title="Update failed" />
      <Table aria-label="Relations">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Relation</Table.Th>
            <Table.Th>Domain → range</Table.Th>
            <Table.Th>Cardinality</Table.Th>
            {editable && <Table.Th />}
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {spec.relations.map((r) => (
            <Table.Tr key={r.name} opacity={r.deprecated ? 0.5 : 1}>
              <Table.Td>
                {r.name} {r.deprecated && <Badge size="xs" color="gray">deprecated</Badge>}
              </Table.Td>
              <Table.Td>
                {r.domain} → {r.range}
              </Table.Td>
              <Table.Td>{r.cardinality}</Table.Td>
              {editable && (
                <Table.Td>
                  <Button
                    size="compact-xs"
                    variant="light"
                    color="gray"
                    onClick={() => patchRelation.mutate({ name: r.name, deprecated: !r.deprecated })}
                  >
                    {r.deprecated ? "Restore" : "Deprecate"}
                  </Button>
                </Table.Td>
              )}
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </>
  );
}

const emptyProp = (): PropertyIn => ({ name: "", datatype: "string", required: false });

export function PropertiesEditor({
  initial,
  onSubmit,
  submitLabel,
}: {
  initial: PropertyIn[];
  onSubmit: (props: PropertyIn[]) => void;
  submitLabel: string;
}) {
  const [rows, setRows] = useState<PropertyIn[]>(initial);
  const set = (i: number, patch: Partial<PropertyIn>) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <Stack gap="xs">
      {rows.map((r, i) => (
        <Group key={i} align="end" wrap="nowrap">
          <TextInput label="Property" value={r.name} onChange={(e) => set(i, { name: e.currentTarget.value })} />
          <Select
            label="Type"
            data={[...DATATYPES]}
            value={r.datatype ?? "string"}
            onChange={(v) => set(i, { datatype: (v ?? "string") as PropertyIn["datatype"] })}
            w={110}
          />
          <Checkbox label="Required" checked={!!r.required} onChange={(e) => set(i, { required: e.currentTarget.checked })} mb={8} />
          <ActionIcon variant="subtle" color="red" aria-label="Remove property" onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))} mb={4}>
            ×
          </ActionIcon>
        </Group>
      ))}
      <Group justify="space-between">
        <Button variant="subtle" size="xs" onClick={() => setRows((rs) => [...rs, emptyProp()])}>
          + property
        </Button>
        <Button onClick={() => onSubmit(rows.filter((r) => r.name.trim()))}>{submitLabel}</Button>
      </Group>
    </Stack>
  );
}

function DraftActions({ version, spec }: { version: number; spec: OntologySpec }) {
  const api = useApi();
  const qc = useQueryClient();
  const [open, setOpen] = useState<"class" | "relation" | null>(null);
  const classNames = spec.classes.map((c) => c.name);
  const onSuccess = () => {
    setOpen(null);
    void qc.invalidateQueries({ queryKey: ["ontology", "version", version] });
  };
  const [cls, setCls] = useState({ name: "", identity_key: "", parent: null as string | null, description: "" });
  const [rel, setRel] = useState({
    name: "",
    domain: null as string | null,
    range: null as string | null,
    cardinality: "many_to_many" as (typeof CARDINALITIES)[number],
  });
  const addClass = useMutation({
    mutationFn: (properties: PropertyIn[]) =>
      unwrap(
        api.POST("/api/ontology/versions/{version}/classes", {
          params: { path: { version } },
          body: {
            name: cls.name,
            identity_key: cls.identity_key,
            parent: cls.parent,
            description: cls.description || null,
            namespace: "ext",
            properties,
          },
        }),
      ),
    onSuccess,
  });
  const addRelation = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/ontology/versions/{version}/relations", {
          params: { path: { version } },
          body: { name: rel.name, domain: rel.domain ?? "", range: rel.range ?? "", cardinality: rel.cardinality, namespace: "ext" },
        }),
      ),
    onSuccess,
  });
  return (
    <>
      <Group>
        <Button variant="light" onClick={() => setOpen("class")}>
          Add class
        </Button>
        <Button variant="light" onClick={() => setOpen("relation")}>
          Add relation
        </Button>
      </Group>
      <Modal opened={open === "class"} onClose={() => setOpen(null)} title={`Add class to v${version}`} size="lg">
        <Stack>
          <TextInput label="Name" value={cls.name} onChange={(e) => setCls({ ...cls, name: e.currentTarget.value })} />
          <TextInput
            label="Identity key"
            description="The property that identifies an instance (must be one of the properties)"
            value={cls.identity_key}
            onChange={(e) => setCls({ ...cls, identity_key: e.currentTarget.value })}
          />
          <Select label="Parent (taxonomy)" data={classNames} value={cls.parent} onChange={(v) => setCls({ ...cls, parent: v })} clearable />
          <TextInput label="Description" value={cls.description} onChange={(e) => setCls({ ...cls, description: e.currentTarget.value })} />
          <ErrorAlert error={addClass.error} title="Could not add the class" />
          <PropertiesEditor initial={[emptyProp()]} submitLabel="Add class" onSubmit={(props) => addClass.mutate(props)} />
        </Stack>
      </Modal>
      <Modal opened={open === "relation"} onClose={() => setOpen(null)} title={`Add relation to v${version}`}>
        <Stack>
          <TextInput label="Name" value={rel.name} onChange={(e) => setRel({ ...rel, name: e.currentTarget.value })} />
          <Select label="Domain" data={classNames} value={rel.domain} onChange={(v) => setRel({ ...rel, domain: v })} />
          <Select label="Range" data={classNames} value={rel.range} onChange={(v) => setRel({ ...rel, range: v })} />
          <Select
            label="Cardinality"
            data={[...CARDINALITIES]}
            value={rel.cardinality}
            onChange={(v) => setRel({ ...rel, cardinality: (v ?? "many_to_many") as (typeof CARDINALITIES)[number] })}
          />
          <ErrorAlert error={addRelation.error} title="Could not add the relation" />
          <Button onClick={() => addRelation.mutate()} loading={addRelation.isPending}>
            Add relation
          </Button>
        </Stack>
      </Modal>
    </>
  );
}
