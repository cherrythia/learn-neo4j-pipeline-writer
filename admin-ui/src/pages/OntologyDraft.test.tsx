// Draft editing on the Ontology screen: add class / relation / property, deprecate and
// restore, and rolling back the published version.
import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { ClassSpec, RelationSpec, VersionDetail, VersionOut } from "../api/types";
import { pick, renderWithApi } from "../testing";
import { OntologyPage } from "./Ontology";

afterEach(cleanup);

const version = (n: number, status: string): VersionOut => ({
  version: n,
  status,
  notes: null,
  created_by: "alice",
  created_at: "2026-11-01T10:00:00Z",
  published_at: status === "published" ? "2026-11-01T11:00:00Z" : null,
});
const ASSET: ClassSpec = {
  name: "Asset",
  identity_key: "asset_tag",
  properties: [{ name: "asset_tag", datatype: "string", required: true }],
  parent: null,
  namespace: "core",
  deprecated: false,
};
const SOFTWARE: ClassSpec = { ...ASSET, name: "Software", identity_key: "software_id", properties: [{ name: "software_id", datatype: "string", required: true }] };
const INSTALLED_ON: RelationSpec = {
  name: "INSTALLED_ON",
  domain: "Software",
  range: "Asset",
  cardinality: "many_to_many",
  namespace: "core",
  deprecated: false,
};

/** A draft v2 whose detail the test mutates to play the server's part after each write. */
function draft() {
  const state = { classes: [ASSET, SOFTWARE], relations: [INSTALLED_ON] };
  const detail = (): VersionDetail => ({
    ...version(2, "draft"),
    classes: state.classes as unknown as VersionDetail["classes"],
    relations: state.relations as unknown as VersionDetail["relations"],
  });
  const routes = {
    "GET /api/ontology/versions": [version(1, "published"), version(2, "draft")],
    "GET /api/ontology/versions/2": detail,
  };
  return { state, routes };
}

/** Opens a Mantine Select by its input and clicks the named option. */
const select = (scope: HTMLElement, name: string) => within(scope).getByRole("combobox", { name });

async function openTab(name: RegExp) {
  fireEvent.click(await screen.findByRole("tab", { name }));
}

test("add class posts the class and its properties, and the new class is listed", async () => {
  const { state, routes } = draft();
  const { calls } = renderWithApi(<OntologyPage role="admin" />, {
    ...routes,
    "POST /api/ontology/versions/2/classes": () => {
      state.classes = [
        ...state.classes,
        {
          name: "SoftwareLicense",
          identity_key: "asset_tag",
          parent: "Asset",
          namespace: "ext",
          deprecated: false,
          properties: [{ name: "asset_tag", datatype: "string", required: true }, { name: "seats", datatype: "int", required: false }],
        },
      ];
      return { name: "SoftwareLicense" };
    },
  });
  fireEvent.click(await screen.findByRole("button", { name: "Add class" }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "SoftwareLicense" } });
  fireEvent.change(within(dialog).getByLabelText("Identity key"), { target: { value: "asset_tag" } });
  await pick(select(dialog, "Parent (taxonomy)"), "Asset");
  fireEvent.change(within(dialog).getByLabelText("Description"), { target: { value: "Licensed software seats" } });
  fireEvent.change(within(dialog).getByLabelText("Property"), { target: { value: "asset_tag" } });
  fireEvent.click(within(dialog).getByLabelText("Required"));
  fireEvent.click(within(dialog).getByRole("button", { name: "+ property" }));
  fireEvent.change(within(dialog).getAllByLabelText("Property")[1]!, { target: { value: "seats" } });
  await pick(within(dialog).getAllByRole("combobox", { name: "Type" })[1]!, "int");
  // A blank third row is dropped on submit.
  fireEvent.click(within(dialog).getByRole("button", { name: "+ property" }));
  fireEvent.click(within(dialog).getByRole("button", { name: "Add class" }));

  await waitFor(() => expect(calls.some((c) => c.path === "/api/ontology/versions/2/classes")).toBe(true));
  const add = calls.find((c) => c.path === "/api/ontology/versions/2/classes")!;
  expect(add.method).toBe("POST");
  expect(add.body).toEqual({
    name: "SoftwareLicense",
    identity_key: "asset_tag",
    parent: "Asset",
    description: "Licensed software seats",
    namespace: "ext",
    properties: [
      { name: "asset_tag", datatype: "string", required: true },
      { name: "seats", datatype: "int", required: false },
    ],
  });

  await openTab(/^Classes \(3\)/);
  const table = await screen.findByRole("table", { name: "Classes" });
  expect(within(table).getByText("asset_tag: string!, seats: int")).toBeDefined();
});

test("add relation posts domain, range and cardinality, and the new relation is listed", async () => {
  const { state, routes } = draft();
  const { calls } = renderWithApi(<OntologyPage role="admin" />, {
    ...routes,
    "POST /api/ontology/versions/2/relations": () => {
      state.relations = [
        ...state.relations,
        { name: "BUNDLED_WITH", domain: "Software", range: "Asset", cardinality: "many_to_one", namespace: "ext", deprecated: false },
      ];
      return { name: "BUNDLED_WITH" };
    },
  });
  fireEvent.click(await screen.findByRole("button", { name: "Add relation" }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "BUNDLED_WITH" } });
  await pick(select(dialog, "Domain"), "Software");
  await pick(select(dialog, "Range"), "Asset");
  await pick(select(dialog, "Cardinality"), "many_to_one");
  fireEvent.click(within(dialog).getByRole("button", { name: "Add relation" }));

  await waitFor(() => expect(calls.some((c) => c.path === "/api/ontology/versions/2/relations")).toBe(true));
  const add = calls.find((c) => c.path === "/api/ontology/versions/2/relations")!;
  expect(add.method).toBe("POST");
  expect(add.body).toEqual({ name: "BUNDLED_WITH", domain: "Software", range: "Asset", cardinality: "many_to_one", namespace: "ext" });

  await openTab(/^Relations \(2\)/);
  const table = await screen.findByRole("table", { name: "Relations" });
  const row = within(table).getByText(/BUNDLED_WITH/).closest("tr")!;
  expect(row.textContent).toContain("Software → Asset");
  expect(row.textContent).toContain("many_to_one");
});

test("add property patches the class with add_properties, and the property is listed", async () => {
  const { state, routes } = draft();
  const { calls } = renderWithApi(<OntologyPage role="admin" />, {
    ...routes,
    "PATCH /api/ontology/versions/2/classes/Asset": () => {
      state.classes = state.classes.map((c) =>
        c.name === "Asset" ? { ...c, properties: [...c.properties, { name: "status", datatype: "string", required: false }] } : c,
      );
      return { name: "Asset" };
    },
  });
  await openTab(/^Classes/);
  const table = await screen.findByRole("table", { name: "Classes" });
  const asset = within(table).getByText("Asset").closest("tr")!;
  fireEvent.click(within(asset).getByRole("button", { name: "Add property" }));
  const dialog = await screen.findByRole("dialog");
  expect(dialog.textContent).toContain("Add property to Asset");
  fireEvent.change(within(dialog).getByLabelText("Property"), { target: { value: "status" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Add" }));

  await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
  expect(calls.find((c) => c.method === "PATCH")!).toMatchObject({
    path: "/api/ontology/versions/2/classes/Asset",
    body: { add_properties: [{ name: "status", datatype: "string", required: false }] },
  });
  await waitFor(() =>
    expect(within(screen.getByRole("table", { name: "Classes" })).getByText("asset_tag: string!, status: string")).toBeDefined(),
  );
});

test("deprecate and restore patch classes and relations, and the rows follow", async () => {
  const { state, routes } = draft();
  const flip = <T extends { name: string; deprecated: boolean }>(list: T[], name: string) =>
    list.map((x) => (x.name === name ? { ...x, deprecated: !x.deprecated } : x));
  const { calls } = renderWithApi(<OntologyPage role="admin" />, {
    ...routes,
    "PATCH /api/ontology/versions/2/classes/Software": () => {
      state.classes = flip(state.classes, "Software");
      return { name: "Software" };
    },
    "PATCH /api/ontology/versions/2/relations/INSTALLED_ON": () => {
      state.relations = flip(state.relations, "INSTALLED_ON");
      return { name: "INSTALLED_ON" };
    },
  });
  const patches = () => calls.filter((c) => c.method === "PATCH");

  await openTab(/^Classes/);
  const softwareRow = () => within(screen.getByRole("table", { name: "Classes" })).getByText(/^Software/).closest("tr")!;
  await waitFor(() => expect(softwareRow()).toBeDefined());
  fireEvent.click(within(softwareRow()).getByRole("button", { name: "Deprecate" }));
  await waitFor(() => expect(within(softwareRow()).getByRole("button", { name: "Restore" })).toBeDefined());
  expect(within(softwareRow()).getByText("deprecated")).toBeDefined();
  expect(patches()[0]).toMatchObject({
    path: "/api/ontology/versions/2/classes/Software",
    body: { add_properties: [], deprecated: true },
  });
  fireEvent.click(within(softwareRow()).getByRole("button", { name: "Restore" }));
  await waitFor(() => expect(within(softwareRow()).getByRole("button", { name: "Deprecate" })).toBeDefined());
  expect(within(softwareRow()).queryByText("deprecated")).toBeNull();
  expect(patches()[1]).toMatchObject({
    path: "/api/ontology/versions/2/classes/Software",
    body: { add_properties: [], deprecated: false },
  });

  await openTab(/^Relations/);
  const installedOnRow = () => within(screen.getByRole("table", { name: "Relations" })).getByText(/^INSTALLED_ON/).closest("tr")!;
  await waitFor(() => expect(installedOnRow()).toBeDefined());
  fireEvent.click(within(installedOnRow()).getByRole("button", { name: "Deprecate" }));
  await waitFor(() => expect(within(installedOnRow()).getByRole("button", { name: "Restore" })).toBeDefined());
  expect(patches()[2]).toMatchObject({ path: "/api/ontology/versions/2/relations/INSTALLED_ON", body: { deprecated: true } });
  fireEvent.click(within(installedOnRow()).getByRole("button", { name: "Restore" }));
  await waitFor(() => expect(within(installedOnRow()).getByRole("button", { name: "Deprecate" })).toBeDefined());
  expect(patches()[3]).toMatchObject({ path: "/api/ontology/versions/2/relations/INSTALLED_ON", body: { deprecated: false } });
});

test("rollback asks for confirmation, posts, and shows the re-published version", async () => {
  let versions = [version(1, "retired"), version(2, "published")];
  const { calls } = renderWithApi(<OntologyPage role="admin" />, {
    "GET /api/ontology/versions": () => versions,
    "GET /api/ontology/versions/1": () => ({ ...versions[0]!, classes: [ASSET], relations: [] }),
    "GET /api/ontology/versions/2": () => ({ ...versions[1]!, classes: [ASSET, SOFTWARE], relations: [INSTALLED_ON] }),
    "POST /api/ontology/rollback": () => {
      versions = [version(1, "published"), version(2, "retired")];
      return { published: 1 };
    },
  });
  await screen.findByText("v2", { selector: "h3" });
  fireEvent.click(screen.getByRole("button", { name: "Roll back" }));
  const dialog = await screen.findByRole("dialog");
  expect(dialog.textContent).toContain("Roll back the ontology?");
  expect(calls.some((c) => c.path === "/api/ontology/rollback")).toBe(false);

  fireEvent.click(within(dialog).getByRole("button", { name: "Confirm rollback" }));
  await waitFor(() => expect(calls.some((c) => c.path === "/api/ontology/rollback")).toBe(true));
  const rollback = calls.find((c) => c.path === "/api/ontology/rollback")!;
  expect(rollback.method).toBe("POST");
  expect(rollback.body).toBeUndefined();

  // The re-published version is selected and the version list is refreshed.
  expect(await screen.findByText("v1", { selector: "h3" })).toBeDefined();
  const list = screen.getByRole("table", { name: "Ontology versions" });
  await waitFor(() =>
    expect(within(list).getAllByRole("row").map((r) => r.textContent)).toEqual(["VersionStatus", "v1published", "v2retired"]),
  );
  await waitFor(() => expect(screen.queryAllByRole("dialog")).toHaveLength(0), { timeout: 2000 });
});
