import { afterEach, expect, it, vi } from "vitest";
import {
  StoreClient,
  StateManager,
  single,
  collection,
} from "../src/index.js";
import { decodeEvent, decodeResponse } from "../src/protocol.js";
import { jsonBytes, jsonCopy } from "../src/internal.js";
import { FakeTransport, confirmed, reply, snapshot } from "./fake.js";

const source = single<{ value: number }, { ping: number }>("Root");
const clients: StoreClient[] = [];
afterEach(async () => {
  await Promise.all(clients.splice(0).map((client) => client.disconnect()));
});
async function ready(
  options: Partial<ConstructorParameters<typeof StoreClient>[0]> = {},
) {
  const transport = new FakeTransport();
  const client = new StoreClient({ transport, ...options });
  clients.push(client);
  const manager = new StateManager(client);
  const state = manager.create(source);
  const connected = client.connect();
  const batch = await transport.next();
  reply(batch, [snapshot(batch.batch.requests[0]!)]);
  await connected;
  return { transport, client, manager, state };
}

it.each([
  NaN,
  Infinity,
  -Infinity,
  2 ** 60,
  undefined,
  new Date(),
  new Uint8Array(2),
])("rejects nonportable JSON: %s", (value) => {
  expect(() => jsonCopy({ value })).toThrow();
});
it("rejects cycles but permits repeated JSON objects and counts Unicode frame bytes", () => {
  const a: { a?: object } = {};
  a.a = a;
  expect(() => jsonCopy(a)).toThrow("cycles");
  const value = { x: 1 };
  expect(jsonCopy({ a: value, b: value })).toEqual({ a: value, b: value });
  expect(jsonBytes({ v: "é" })).toBe(14);
  expect(jsonBytes({ v: "🙂" })).toBe(20);
});
it.each([
  {
    type: "subscribe_single",
    subscription_id: "s",
    store_instance_id: "x",
    revision: true,
    values: {},
  },
  {
    type: "subscribe_single",
    subscription_id: "s",
    store_instance_id: "x",
    revision: 0,
  },
  { type: "transaction", transaction_id: "wrong" },
])("rejects malformed or mismatched responses", (response) => {
  expect(() =>
    decodeResponse(
      { responses: [response] },
      { requests: [{ type: "subscribe_single", schema: "Root" }] },
    ),
  ).toThrow();
});
it("rejects signal events with missing payloads", () => {
  expect(() =>
    decodeEvent({
      event: {
        store_instance_id: "s",
        transaction_id: null,
        events: [
          {
            subscription_id: "x",
            revisions: {},
            actions: [{ type: "signal", signal: "ping" }],
          },
        ],
      },
    }),
  ).toThrow("data");
});
it("prevents partial state publication when another subscription has a revision gap", async () => {
  const { client, transport, manager, state } = await ready();
  const rows = manager.create(collection<{ value: number }>("Row"));
  const batch = await transport.next();
  reply(batch, [snapshot(batch.batch.requests[0]!, { id: "rows" })]);
  await client.whenSynced();
  transport.handlers!.event({
    event: {
      store_instance_id: "store",
      transaction_id: null,
      events: [
        {
          subscription_id: "root",
          revisions: { Root: 1 },
          actions: [{ type: "set", values: { value: 5 } }],
        },
        {
          subscription_id: "rows",
          revisions: { Row: 2 },
          actions: [{ type: "insert", row_id: "a", values: { value: 1 } }],
        },
      ],
    },
  });
  expect(state.value).toBe(0);
  expect(rows.size).toBe(0);
  expect(client.connected).toBe(false);
});
it("sorts cross-source signals and publishes all retained state before callbacks", async () => {
  const { client, transport, manager, state } = await ready();
  const rows = manager.create(
    collection<{ value: number }, { ping: number }>("Row"),
  );
  const batch = await transport.next();
  reply(batch, [
    snapshot(batch.batch.requests[0]!, {
      id: "rows",
      rows: [{ id: "a", values: { value: 0 } }],
    }),
  ]);
  await client.whenSynced();
  const observed: unknown[] = [];
  state.signals.ping.bind((data) =>
    observed.push([data, state.value, rows.get("a")!.value]),
  );
  rows.signals.ping.bind((data, info) =>
    observed.push([data, info.rowId, state.value, rows.get("a")!.value]),
  );
  transport.handlers!.event({
    event: {
      store_instance_id: "store",
      transaction_id: null,
      events: [
        {
          subscription_id: "root",
          revisions: { Root: 1 },
          actions: [
            { type: "set", values: { value: 1 } },
            { type: "signal", signal: "ping", data: 2, occurrence_id: 1 },
          ],
        },
        {
          subscription_id: "rows",
          revisions: { Row: 1 },
          actions: [
            { type: "setrow", row_id: "a", values: { value: 2 } },
            {
              type: "signalrow",
              row_id: "a",
              signal: "ping",
              data: 1,
              occurrence_id: 0,
            },
          ],
        },
      ],
    },
  });
  expect(observed).toEqual([
    [1, "a", 1, 2],
    [2, 1, 2],
  ]);
});
it("exposes settled status and retired overlays inside completion callbacks", async () => {
  const { state, transport } = await ready();
  state.value = 5;
  const tx = state.lastTransaction!;
  const observed: unknown[] = [];
  const callback = vi.fn(() =>
    observed.push([tx.status, state.pendingCount, state.value]),
  );
  tx.onSettled(callback);
  const batch = await transport.next();
  transport.event([{ type: "set", values: { value: 5 } }], 1, tx.id);
  reply(batch, [confirmed(batch.batch.requests[0]!)]);
  await tx;
  expect(callback).toHaveBeenCalledOnce();
  expect(observed).toEqual([["confirmed", 0, 5]]);
});
it("rejects excess queued writes as not_sent without changing other pending writes", async () => {
  const { state, transport } = await ready({ maxPendingRequests: 1 });
  const first = state.set({ value: 1 });
  const second = state.set({ value: 2 });
  expect((await second).status).toBe("not_sent");
  expect(first.status).toBe("pending");
  const batch = await transport.next();
  transport.event([{ type: "set", values: { value: 1 } }], 1, first.id);
  reply(batch, [confirmed(batch.batch.requests[0]!)]);
  await first.unwrap();
  expect(state.pendingCount).toBe(0);
});
it("bounds queued request bytes", async () => {
  const { client } = await ready({ maxPendingBytes: 500 });
  const tx = client.transaction([
    { type: "set", schema: "Root", values: { label: "x".repeat(1000) } },
  ]);
  expect((await tx).status).toBe("not_sent");
  expect(client.connected).toBe(true);
});

it("exposes immutable confirmed revisions independently of optimistic values", async () => {
  const { state, transport } = await ready();
  const captured = state.revisions;
  expect(state.storeInstanceId).toBe("store");
  expect(captured).toEqual({ Root: 0 });
  state.value = 5;
  const tx = state.lastTransaction!;
  expect(state.revisions).toBe(captured);
  expect(Object.isFrozen(captured)).toBe(true);
  const batch = await transport.next();
  transport.event([{ type: "set", values: { value: 5 } }], 1, tx.id);
  reply(batch, [confirmed(batch.batch.requests[0]!)]);
  await tx;
  expect(state.revisions).toEqual({ Root: 1 });
  expect(captured).toEqual({ Root: 0 });
  transport.lose();
  expect(state.synced).toBe(false);
  expect(state.revisions).toEqual({ Root: 1 });
  expect(state.storeInstanceId).toBe("store");
});
it.each([{ maxBufferedEvents: 1 }, { maxBufferedBytes: 250 }])(
  "drops an overflowing event backlog and requires recovery",
  async (options) => {
    const { client, state, transport } = await ready(options);
    const tx = state.set({ value: 9 });
    await transport.next();
    transport.event([{ type: "set", values: { value: 1 } }], 1);
    transport.event([{ type: "set", values: { value: 2 } }], 2);
    expect((await tx).status).toBe("unknown");
    expect(client.lastError?.message).toContain("buffer");
    expect(state.value).toBe(0);
  },
);
it("cancels an in-progress connection without resurrecting stale subscriptions", async () => {
  const transport = new FakeTransport();
  const client = new StoreClient({ transport });
  clients.push(client);
  const manager = new StateManager(client);
  const state = manager.create(source);
  const connect = client.connect();
  const rejected = expect(connect).rejects.toThrow();
  const batch = await transport.next();
  await client.disconnect();
  reply(batch, [
    snapshot(batch.batch.requests[0]!, { values: { value: 99 } }),
  ]);
  await rejected;
  expect(state.synced).toBe(false);
  expect(state.value).toBeUndefined();
});
it("unsubscribes a released source whose first snapshot arrives late", async () => {
  const { client, transport, manager } = await ready();
  const ref = manager.prepare(collection<{ value: number }>("Row"));
  const release = ref.retain();
  const pending = await transport.next();
  release();
  reply(pending, [snapshot(pending.batch.requests[0]!, { id: "late" })]);
  const unsubscribe = await transport.next();
  expect((ref.view as { synced: boolean }).synced).toBe(false);
  expect(unsubscribe.batch.requests).toEqual([
    { type: "unsubscribe", subscription_id: "late" },
  ]);
  reply(unsubscribe, [{ type: "unsubscribe", subscription_id: "late" }]);
  await client.whenSynced();
});
