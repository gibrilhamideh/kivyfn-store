import { spawn } from "node:child_process";
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { delimiter, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { deferred } from "../src/internal.js";
import type { Deferred } from "../src/internal.js";

export class PythonHost {
  endpoint = "";
  private lines: { endpoint: string }[] = [];
  private waiters: Deferred<{ endpoint: string }>[] = [];
  private stderr = "";
  private readonly exited = deferred<void>();
  private constructor(
    readonly process: ChildProcessWithoutNullStreams,
    private readonly directory: string,
  ) {
    createInterface({ input: process.stdout }).on("line", (line) => {
      if (!line.startsWith('{"kind":')) return;
      const value = JSON.parse(line) as { kind: string; endpoint: string };
      if (value.kind !== "ready") return;
      const waiter = this.waiters.shift();
      if (waiter) waiter.resolve(value);
      else this.lines.push(value);
    });
    process.stderr.on("data", (data) => {
      this.stderr = (this.stderr + data).slice(-12000);
    });
    process.on("error", (error) => {
      for (const waiter of this.waiters) waiter.reject(error);
    });
    process.on("exit", (code) => {
      this.exited.resolve();
      for (const waiter of this.waiters)
        waiter.reject(
          new Error(`Python host exited (${code}): ${this.stderr}`),
        );
    });
  }
  static async start(gateway = ""): Promise<PythonHost> {
    const directory = await mkdtemp(join(tmpdir(), "kivyfn-client-"));
    const fixture = fileURLToPath(
      new URL("./python_host.py", import.meta.url),
    );
    const repository = resolve(
      fileURLToPath(new URL("../../..", import.meta.url)),
    );
    const child = spawn(
      process.env.PYTHON ?? "python",
      [fixture, directory, gateway],
      {
        env: {
          ...process.env,
          PYTHONPATH: [join(repository, "src"), process.env.PYTHONPATH]
            .filter(Boolean)
            .join(delimiter),
          KIVY_NO_ARGS: "1",
          KIVY_NO_CONSOLELOG: "1",
          PYTHONUNBUFFERED: "1",
        },
        stdio: "pipe",
      },
    );
    const host = new PythonHost(child, directory);
    try {
      host.endpoint = (await host.next()).endpoint;
      return host;
    } catch (error) {
      await host.stop();
      throw error;
    }
  }
  private async next(): Promise<{ endpoint: string }> {
    if (this.lines.length) return this.lines.shift()!;
    const waiter = deferred<{ endpoint: string }>();
    this.waiters.push(waiter);
    const timer = setTimeout(
      () => waiter.reject(new Error(`Python host timed out: ${this.stderr}`)),
      10000,
    );
    try {
      return await waiter.promise;
    } finally {
      clearTimeout(timer);
      this.waiters = this.waiters.filter((item) => item !== waiter);
    }
  }
  async restart(): Promise<void> {
    this.process.stdin.write("restart\n");
    this.endpoint = (await this.next()).endpoint;
  }
  async reconnectRelay(): Promise<void> {
    this.process.stdin.write("relay-reconnect\n");
    await this.next();
  }
  async stop(): Promise<void> {
    if (this.process.exitCode === null) this.process.stdin.end("stop\n");
    const timer = setTimeout(() => this.process.kill("SIGKILL"), 3000);
    await this.exited.promise;
    clearTimeout(timer);
    await rm(this.directory, { recursive: true, force: true });
  }
}
