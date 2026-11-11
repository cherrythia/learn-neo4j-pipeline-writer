import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { THROTTLED_MESSAGE } from "../api/client";
import { AppRoutes } from "../App";
import { json, renderWithApi } from "../testing";
import { INVALID_CREDENTIALS } from "./Login";

afterEach(cleanup);

const loggedOut = { "GET /api/auth/me": () => json({ detail: "not logged in" }, 401) };

async function submit(username: string, password: string) {
  fireEvent.change(await screen.findByLabelText(/Username/), { target: { value: username } });
  fireEvent.change(screen.getByLabelText(/Password/), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: "Log in" }));
}

test("logged out users see the login form", async () => {
  renderWithApi(<AppRoutes />, loggedOut);
  expect(await screen.findByRole("button", { name: "Log in" })).toBeDefined();
});

test("invalid credentials show an error and stay on login", async () => {
  const { calls } = renderWithApi(<AppRoutes />, {
    ...loggedOut,
    "POST /api/auth/login": () => json({ detail: "invalid username or password" }, 401),
  });
  await submit("rita", "wrong");
  expect((await screen.findByRole("alert")).textContent).toContain(INVALID_CREDENTIALS);
  const login = calls.find((c) => c.path === "/api/auth/login")!;
  expect(login.body).toEqual({ username: "rita", password: "wrong" });
  expect(login.headers.get("X-Requested-With")).toBe("writer-admin");
  expect(login.credentials).toBe("include");
});

test("throttled logins say so", async () => {
  renderWithApi(<AppRoutes />, {
    ...loggedOut,
    "POST /api/auth/login": () => json({ detail: "too many failed logins; try again later" }, 429),
  });
  await submit("rita", "wrong");
  expect((await screen.findByRole("alert")).textContent).toContain(THROTTLED_MESSAGE);
});

test("a successful login shows the app", async () => {
  renderWithApi(<AppRoutes />, {
    ...loggedOut,
    "POST /api/auth/login": { username: "rita", role: "reviewer" },
    "GET /api/review": [],
  });
  await submit("rita", "rita-password-123");
  await waitFor(() => expect(screen.getByTestId("current-user").textContent).toBe("rita"));
  expect(await screen.findByText("Nothing to review.")).toBeDefined();
});
