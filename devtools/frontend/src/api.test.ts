import { afterEach, describe, expect, it, vi } from "vitest";
import { AmbiguousOutcome, command, stream } from "./api";
import type { Envelope } from "./types";

class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 1;
  onmessage: ((message: MessageEvent<string>) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  constructor(readonly url: string) {
    Socket.instances.push(this);
  }
  send(value: string) {
    this.sent.push(value);
  }
  close() {
    this.readyState = 3;
    this.onclose?.();
  }
  receive(value: Envelope) {
    this.onmessage?.(
      new MessageEvent("message", { data: JSON.stringify(value) }),
    );
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  Socket.instances = [];
});

describe("diagnostic continuity", () => {
  it("ignores duplicate and stale envelopes; gaps reconnect to a fresh checkpoint", () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", Socket);
    vi.stubGlobal("location", { protocol: "http:", host: "127.0.0.1:8766" });
    const received: Envelope[] = [];
    const errors: string[] = [];
    const connection = stream(
      (value) => received.push(value),
      () => undefined,
      (value) => errors.push(value),
      ["Model"],
    );
    const socket = Socket.instances[0];
    const snapshot: Envelope = {
      version: 2,
      epoch: "first",
      sequence: 1,
      store_instance_id: "store",
      kind: "snapshot",
      payload: { checkpoint: { schemas: { Model: {} } } },
    };
    socket.receive(snapshot);
    socket.receive({ ...snapshot, kind: "commit", sequence: 2 });
    socket.receive({ ...snapshot, kind: "commit", sequence: 2 });
    socket.receive({ ...snapshot, kind: "commit", epoch: "old", sequence: 3 });
    expect(received.map((value) => value.sequence)).toEqual([1, 2]);
    socket.receive({ ...snapshot, kind: "commit", sequence: 4 });
    expect(errors[0]).toContain("gap");
    expect(socket.readyState).toBe(3);
    vi.advanceTimersByTime(800);
    expect(Socket.instances).toHaveLength(2);
    Socket.instances[1].receive({ ...snapshot, epoch: "second" });
    expect(received.at(-1)?.epoch).toBe("second");
    connection.close();
    vi.advanceTimersByTime(1000);
    expect(Socket.instances).toHaveLength(2);
  });
  it("rejects incompatible diagnostic versions and scopes reconnect URLs", () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", Socket);
    vi.stubGlobal("location", { protocol: "http:", host: "127.0.0.1:8766" });
    const errors: string[] = [];
    const connection = stream(
      () => undefined,
      () => undefined,
      (value) => errors.push(value),
      ["Model"],
    );
    Socket.instances[0].receive({
      version: 2,
      epoch: "initial",
      sequence: 1,
      store_instance_id: "store",
      kind: "snapshot",
      payload: { checkpoint: { schemas: { Model: {} } } },
    });
    connection.scope(["Rows"]);
    expect(JSON.parse(Socket.instances[0].sent[0])).toEqual({
      kind: "scope",
      schemas: ["Rows"],
    });
    Socket.instances[0].receive({
      version: 1,
      epoch: "one",
      sequence: 1,
      store_instance_id: "store",
      kind: "snapshot",
      payload: {},
    });
    expect(errors[0]).toContain("protocol");
    vi.advanceTimersByTime(800);
    expect(Socket.instances[1].url).toContain("schemas=Rows");
    connection.close();
  });
  it("coalesces rapid scopes and waits for matching snapshots without disconnecting", () => {
    vi.stubGlobal("WebSocket", Socket);
    vi.stubGlobal("location", { protocol: "http:", host: "127.0.0.1:3000" });
    const readiness = vi.fn();
    const connected = vi.fn();
    const connection = stream(
      () => undefined,
      connected,
      () => undefined,
      ["Model"],
      readiness,
    );
    const socket = Socket.instances[0];
    const snapshot = (schema: string, epoch: string): Envelope => ({
      version: 2,
      epoch,
      sequence: 1,
      store_instance_id: "store",
      kind: "snapshot",
      payload: { checkpoint: { schemas: { [schema]: {} } } },
    });
    socket.receive(snapshot("Model", "initial"));
    expect(readiness).toHaveBeenLastCalledWith(true);
    connection.scope(["Rows"]);
    connection.scope(["Other"]);
    connection.scope(["Model"]);
    expect(socket.sent).toHaveLength(1);
    socket.receive(snapshot("Model", "old-scope-resync"));
    expect(readiness).toHaveBeenLastCalledWith(false);
    socket.receive(snapshot("Rows", "rows"));
    expect(readiness).toHaveBeenLastCalledWith(false);
    expect(socket.sent.map((value) => JSON.parse(value).schemas)).toEqual([
      ["Rows"],
      ["Model"],
    ]);
    socket.receive(snapshot("Model", "latest"));
    expect(readiness).toHaveBeenLastCalledWith(true);
    expect(connected).not.toHaveBeenCalledWith(false);
    connection.close();
  });
  it("selects the newest scope after opening and ignores callbacks from a closed socket", () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", Socket);
    vi.stubGlobal("location", { protocol: "http:", host: "127.0.0.1:3000" });
    const receive = vi.fn();
    const ready = vi.fn();
    const connection = stream(
      receive,
      () => undefined,
      () => undefined,
      ["Model"],
      ready,
    );
    const original = Socket.instances[0];
    connection.scope(["Rows"]);
    expect(original.sent).toHaveLength(0);
    original.close();
    vi.advanceTimersByTime(800);
    const replacement = Socket.instances[1];
    expect(replacement.url).toContain("schemas=Rows");
    const snapshot: Envelope = {
      version: 2,
      epoch: "new",
      sequence: 1,
      store_instance_id: "store",
      kind: "snapshot",
      payload: { checkpoint: { schemas: { Rows: {} } } },
    };
    replacement.receive(snapshot);
    const count = receive.mock.calls.length;
    original.receive({ ...snapshot, epoch: "old" });
    original.close();
    vi.advanceTimersByTime(800);
    expect(receive).toHaveBeenCalledTimes(count);
    expect(Socket.instances).toHaveLength(2);
    expect(ready).toHaveBeenLastCalledWith(true);
    connection.close();
  });
});

describe("command outcomes", () => {
  const batch = {
    requests: [{ type: "transaction", transaction_id: "same-id" }],
  };
  it("preserves unresolved post-execution responses for explicit same-ID retry", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            error: "Response exceeds limit",
            outcome: "unknown",
          }),
          { status: 502 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            responses: [{ type: "transaction", transaction_id: "same-id" }],
          }),
        ),
      );
    vi.stubGlobal("fetch", fetch);
    await expect(command(batch, "session")).rejects.toBeInstanceOf(
      AmbiguousOutcome,
    );
    await expect(command(batch, "session")).resolves.toEqual({
      responses: [{ type: "transaction", transaction_id: "same-id" }],
    });
    expect(fetch.mock.calls[0][1].body).toBe(fetch.mock.calls[1][1].body);
  });
  it("keeps pre-execution rejection distinct from an uncertain mutation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: "Invalid transaction" }), {
          status: 400,
        }),
      ),
    );
    const result = command(batch, "session");
    await expect(result).rejects.toThrow("Invalid transaction");
    await expect(result).rejects.not.toBeInstanceOf(AmbiguousOutcome);
  });
});
