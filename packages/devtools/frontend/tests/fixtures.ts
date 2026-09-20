import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test as base } from "@playwright/test";
import type { Values } from "../src/types";

interface Backend {
  url: string;
  mutate: (actions: Values[], group?: string) => Promise<void>;
  disconnectBrowsers: () => Promise<void>;
  restart: () => Promise<void>;
}

export const test = base.extend<{ backend: Backend; historyEnabled: boolean }>(
  {
    historyEnabled: [true, { option: true }],
    backend: async ({ browser, historyEnabled }, provide, testInfo) => {
      testInfo.annotations.push({
        type: "browser-version",
        description: browser.version(),
      });
      const directory = path.dirname(fileURLToPath(import.meta.url));
      const root = path.resolve(directory, "../../..");
      const python = process.env.DEVTOOLS_TEST_PYTHON || "python";
      const child = spawn(
        python,
        [
          path.join(directory, "backend.py"),
          "--transport",
          testInfo.project.name,
          ...(historyEnabled ? [] : ["--history-disabled"]),
        ],
        {
          cwd: root,
          env: {
            ...process.env,
            PYTHONPATH: [path.join(root, "src"), process.env.PYTHONPATH]
              .filter(Boolean)
              .join(path.delimiter),
            KIVY_NO_ARGS: "1",
            KIVY_NO_CONSOLELOG: "1",
          },
          stdio: ["pipe", "pipe", "pipe"],
        },
      );
      let stderr = "";
      child.stderr.on("data", (data: Buffer) => {
        stderr += data.toString();
      });
      const pending = new Map<
        string,
        { resolve: () => void; reject: (error: Error) => void }
      >();
      const lines = createInterface({ input: child.stdout });
      let readyResolve: (url: string) => void;
      let readyReject: (error: Error) => void;
      const ready = new Promise<string>((resolve, reject) => {
        readyResolve = resolve;
        readyReject = reject;
      });
      lines.on("line", (line) => {
        if (!line.startsWith("{")) return;
        const message = JSON.parse(line) as {
          ready?: string;
          ack?: string;
          error?: string;
        };
        if (message.ready) readyResolve(message.ready);
        if (message.ack) {
          const item = pending.get(message.ack);
          pending.delete(message.ack);
          if (message.error) item?.reject(new Error(message.error));
          else item?.resolve();
        }
      });
      child.on("error", (error) => readyReject(error));
      child.on("exit", (code) => {
        const error = new Error(`Real backend exited ${code}: ${stderr}`);
        readyReject(error);
        for (const item of pending.values()) item.reject(error);
      });
      try {
        const url = await ready;
        await provide({
          url,
          restart: async () => {
            const id = crypto.randomUUID();
            const result = new Promise<void>((resolve, reject) =>
              pending.set(id, { resolve, reject }),
            );
            child.stdin.write(JSON.stringify({ restart: true, id }) + "\n");
            return result;
          },
          disconnectBrowsers: async () => {
            const id = crypto.randomUUID();
            const result = new Promise<void>((resolve, reject) =>
              pending.set(id, { resolve, reject }),
            );
            child.stdin.write(
              JSON.stringify({ disconnect_browsers: true, id }) + "\n",
            );
            return result;
          },
          mutate: async (actions, group) => {
            const id = crypto.randomUUID();
            const result = new Promise<void>((resolve, reject) =>
              pending.set(id, { resolve, reject }),
            );
            child.stdin.write(
              JSON.stringify({
                type: "transaction",
                transaction_id: id,
                actions,
                ...(group ? { history_group: group } : {}),
              }) + "\n",
            );
            return result;
          },
        });
      } finally {
        const exited = new Promise<void>((resolve) =>
          child.once("exit", () => resolve()),
        );
        if (child.exitCode === null) {
          child.stdin.end('{"stop":true}\n');
          const timer = setTimeout(() => child.kill("SIGKILL"), 5_000);
          await exited;
          clearTimeout(timer);
        }
        lines.close();
        if (stderr)
          await testInfo.attach("backend-log", {
            body: stderr,
            contentType: "text/plain",
          });
      }
    },
  },
);

export { expect } from "@playwright/test";
