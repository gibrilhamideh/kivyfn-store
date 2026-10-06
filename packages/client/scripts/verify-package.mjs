// Verify the actual npm tarball in an isolated consumer, including browser build.
import { execFileSync } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const repository = fileURLToPath(new URL("../../..", import.meta.url));
const directory = await mkdtemp(join(tmpdir(), "kivyfn-package-"));
const npm = process.platform === "win32" ? "npm.cmd" : "npm";
const run = (command, args, cwd = directory) =>
  execFileSync(command, args, {
    cwd,
    encoding: "utf8",
    env: { ...process.env, NODE_PATH: "" },
    stdio: ["ignore", "pipe", "inherit"],
  });
const version = async (name) =>
  JSON.parse(
    await readFile(
      join(repository, "node_modules", name, "package.json"),
      "utf8",
    ),
  ).version;

try {
  const packed = JSON.parse(
    run(
      npm,
      [
        "pack",
        "--workspace=@kivyfn/store",
        "--json",
        "--pack-destination",
        directory,
      ],
      repository,
    ),
  );
  const tarball = join(directory, packed[0].filename);
  await writeFile(
    join(directory, "package.json"),
    JSON.stringify(
      {
        private: true,
        type: "module",
        dependencies: {
          "@kivyfn/store": `file:${tarball}`,
          mobx: await version("mobx"),
        },
      },
      null,
      2,
    ),
  );
  run(npm, ["install", "--ignore-scripts", "--no-audit", "--no-fund"]);
  await writeFile(
    join(directory, "node-check.mjs"),
    `
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { StoreClient, StateManager, single } from "@kivyfn/store";
const require = createRequire(import.meta.url);
assert.throws(() => require.resolve("react"));
const client = new StoreClient({ transport: {
  connect: async () => ({ transactionPrefix: "package:" }),
  request: async (batch) => ({ responses: batch.requests.map(() => ({
    type: "subscribe_single", subscription_id: "root", store_instance_id: "s",
    revision: 0, values: { value: 4 },
  })) }),
  disconnect: async () => {},
} });
const manager = new StateManager(client);
const state = manager.create(single("Root"));
await client.connect();
assert.equal(state.value, 4);
assert.equal(state.synced, true);
assert.equal(state.storeInstanceId, "s");
assert.equal(state.revisions.Root, 0);
await client.disconnect();
manager.close();
`,
  );
  run(process.execPath, ["node-check.mjs"]);

  const dependencies = await Promise.all(
    [
      "react",
      "react-dom",
      "mobx-react-lite",
      "@types/react",
      "@types/react-dom",
      "typescript",
      "vite",
    ].map(async (name) => `${name}@${await version(name)}`),
  );
  run(npm, [
    "install",
    "--ignore-scripts",
    "--no-audit",
    "--no-fund",
    ...dependencies,
  ]);
  await writeFile(
    join(directory, "index.html"),
    '<div id="root"></div><script type="module" src="/main.tsx"></script>',
  );
  await writeFile(
    join(directory, "tsconfig.json"),
    JSON.stringify({
      compilerOptions: {
        target: "ES2022",
        module: "ESNext",
        moduleResolution: "bundler",
        jsx: "react-jsx",
        strict: true,
        noEmit: true,
        skipLibCheck: false,
        lib: ["ESNext", "DOM"],
      },
      include: ["main.tsx"],
    }),
  );
  await writeFile(
    join(directory, "main.tsx"),
    `
import React from "react";
import { createRoot } from "react-dom/client";
import { StoreClient, StateManager, single, RelayTransport, collection, query } from "@kivyfn/store";
import { observer, StoreProvider, useStoreState } from "@kivyfn/store/react";
const root = single<{ value: number; data: { label: string } }, { ping: void }>("Root");
const row = collection<{ value: number }>("Row");
const manager = new StateManager(new StoreClient({ transport: new RelayTransport({
  endpoint: "https://example.invalid/socket.io", storeId: "sample",
}) }));
function checkTypes() {
  const state = manager.create(root);
  state.value = 1;
  state.signals.ping.emit();
  state.set({ value: 2 });
  state.set({ value: 3 }, { expectedStoreInstanceId: state.storeInstanceId!, expectedRevisions: state.revisions });
  // @ts-expect-error confirmed revision metadata is readonly
  state.revisions.Root = 99;
  // @ts-expect-error wrong field type
  state.set({ value: "no" });
  // @ts-expect-error unknown field
  state.missing = 3;
  // @ts-expect-error nested snapshots are readonly
  state.data!.label = "no";
  const view = manager.create(query(row));
  // @ts-expect-error query views are read-only
  view.get("a")!.set({ value: 3 });
}
void checkTypes;
const App = observer(function App() {
  const state = useStoreState(root);
  return <button onClick={() => { state.value = 3; }}>{state.value ?? "Waiting"}</button>;
});
createRoot(document.getElementById("root")!).render(<StoreProvider manager={manager}><App /></StoreProvider>);
`,
  );
  run(npm, ["exec", "--", "tsc", "--noEmit"]);
  run(npm, ["exec", "--", "vite", "build"]);
  console.log(
    "Packed package passed: Node without React, installed declarations, and Vite browser build.",
  );
} finally {
  await rm(directory, { recursive: true, force: true });
}
