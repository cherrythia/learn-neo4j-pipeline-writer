import { expect, test } from "bun:test";
import { join } from "node:path";

const ROOT = join(import.meta.dir, "..", "..");

test("generated API types match openapi.json (run: bun run gen:api)", async () => {
  const proc = Bun.spawn(
    [process.execPath, "node_modules/openapi-typescript/bin/cli.js", "src/api/openapi.json"],
    { cwd: ROOT, stdout: "pipe", stderr: "pipe" },
  );
  const generated = await new Response(proc.stdout).text();
  expect(await proc.exited).toBe(0);
  const committed = await Bun.file(join(ROOT, "src/api/schema.d.ts")).text();
  expect(committed === generated).toBe(true);
}, 30_000);
