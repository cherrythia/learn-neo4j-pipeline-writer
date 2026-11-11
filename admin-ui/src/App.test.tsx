import { afterEach, expect, test } from "bun:test";
import { cleanup, render, screen } from "@testing-library/react";
import { App } from "./App";

afterEach(cleanup);

test("renders the admin heading", () => {
  render(<App />);
  expect(screen.getByRole("heading", { name: "Writer Admin" })).toBeDefined();
});
