import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { Me, UserOut } from "../api/types";
import { renderWithApi } from "../testing";
import { UsersPage } from "./Users";

afterEach(cleanup);

const ME: Me = { username: "alice", role: "admin" };
const user = (username: string, role: string, active = true): UserOut => ({
  username,
  role,
  active,
  created_at: "2026-11-01T10:00:00Z",
});

/** Opens a Mantine Select by its input and clicks the named option. */
async function pick(input: HTMLElement, option: string) {
  fireEvent.click(input);
  await waitFor(() => expect(input.getAttribute("aria-expanded")).toBe("true"));
  const listbox = document.getElementById(input.getAttribute("aria-controls") ?? "")!;
  fireEvent.click(within(listbox).getByRole("option", { name: option, hidden: true }));
}

test("creates a user, changes a role and deactivates, then shows the refreshed list", async () => {
  let users = [user("alice", "admin"), user("bob", "reviewer")];
  const { calls } = renderWithApi(<UsersPage me={ME} />, {
    "GET /api/users": () => users,
    "POST /api/users": () => {
      users = [...users, user("carol", "admin")];
      return users.at(-1);
    },
    "PATCH /api/users/bob": () => users.find((u) => u.username === "bob"),
  });
  const table = await screen.findByRole("table", { name: "Users" });
  await within(table).findByText("bob");
  // You cannot change yourself.
  expect((screen.getByLabelText("alice active") as HTMLInputElement).disabled).toBe(true);

  fireEvent.change(screen.getByLabelText("Username"), { target: { value: "carol" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "correct-horse-battery" } });
  await pick(screen.getByRole("combobox", { name: "Role" }), "admin");
  fireEvent.click(screen.getByRole("button", { name: "Create user" }));
  await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/users")).toBe(true));
  const create = calls.find((c) => c.method === "POST" && c.path === "/api/users")!;
  expect(create.body).toEqual({ username: "carol", password: "correct-horse-battery", role: "admin" });
  expect(await within(table).findByText("carol")).toBeDefined();
  expect((screen.getByLabelText("Username") as HTMLInputElement).value).toBe("");

  await pick(screen.getByRole("combobox", { name: "Role of bob" }), "admin");
  await waitFor(() => expect(calls.filter((c) => c.method === "PATCH")).toHaveLength(1));
  expect(calls.find((c) => c.method === "PATCH")!).toMatchObject({
    path: "/api/users/bob",
    body: { role: "admin", active: null },
  });

  users = users.map((u) => (u.username === "bob" ? { ...u, role: "admin", active: false } : u));
  fireEvent.click(screen.getByLabelText("bob active"));
  await waitFor(() => expect(calls.filter((c) => c.method === "PATCH")).toHaveLength(2));
  expect(calls.filter((c) => c.method === "PATCH")[1]!).toMatchObject({
    path: "/api/users/bob",
    body: { role: null, active: false },
  });
  await waitFor(() => expect((screen.getByLabelText("bob active") as HTMLInputElement).checked).toBe(false));
});
