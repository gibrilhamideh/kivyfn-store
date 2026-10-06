import { afterEach, describe, expect, it, vi } from "vitest";
import { autorun } from "mobx";
import {
  StoreClient,
  StateManager,
  single,
  collection,
  query,
  field,
  and,
  not,
  SubscriptionError,
} from "../src/index.js";
import {
  FakeTransport,
  confirmed,
  failure,
  rejected,
  reply,
  snapshot,
} from "./fake.js";

const root = single<
  { value: number; nested: { x: number } },
  { ping: { value: number } }
>("Root");
const rows = collection<{ label: string; value: number }, { ping: number }>(
  "Row",
);
const clients: StoreClient[] = [];
afterEach(async () => {
  await Promise.all(clients.splice(0).map((client) => client.disconnect()));
});
function setup(
  options: Partial<ConstructorParameters<typeof StoreClient>[0]> = {},
) {
  const transport = new FakeTransport();
  const client = new StoreClient({ transport, ...options });
  clients.push(client);
  const manager = new StateManager(client);
  const state = manager.create(root);
  return { transport, client, manager, state };
}
async function ready() {
  const stack = setup();
  const connected = stack.client.connect();
  const batch = await stack.transport.next();
  reply(batch, [
    snapshot(batch.batch.requests[0]!, {
      values: { value: 0, nested: { x: 1 } },
    }),
  ]);
  await connected;
  return stack;
}

describe("state and transaction synchronization", () => {
  it("installs a snapshot and observes only replaced JSON fields", async () => {
    const { state, transport } = await ready();
    expect(state.synced).toBe(true);
    expect(state.value).toBe(0);
    expect(() => {
      (state.nested as { x: number }).x = 9;
    }).toThrow();
    const values: (number | undefined)[] = [];
    const dispose = autorun(() => values.push(state.value));
    transport.event([{ type: "set", values: { value: 2 } }], 1);
    expect(values).toEqual([0, 2]);
    expect(Object.keys(state)).toEqual(["value", "nested"]);
    dispose();
  });
  it("keeps canonical updates below overlapping optimistic writes", async () => {
    const { state, transport } = await ready();
    state.value = 10;
    const first = state.lastTransaction!;
    state.value = 20;
    const second = state.lastTransaction!;
    expect(state.value).toBe(20);
    expect(state.pendingCount).toBe(2);
    const batch = await transport.next();
    transport.event([{ type: "set", values: { value: 10 } }], 1, first.id);
    transport.event([{ type: "set", values: { value: 15 } }], 2, "other-user");
    reply(batch, [
      confirmed(batch.batch.requests[0]!),
      rejected(batch.batch.requests[1]!),
    ]);
    expect((await first).status).toBe("confirmed");
    expect((await second).status).toBe("rejected");
    expect(second.error?.failure?.code).toBe("invalid");
    expect(state.value).toBe(15);
    expect(state.pendingCount).toBe(0);
  });
  it("keeps .set confirmed and signals before completion, with canonical state visible", async () => {
    const { state, transport, manager } = await ready();
    const observations: unknown[] = [];
    const transaction = manager.transaction((tx) => {
      tx.set(state, { value: 7 });
      tx.emit(state.signals.ping, { value: 7 });
    });
    const dispose = state.signals.ping.bind((data) =>
      observations.push([data.value, state.value, transaction.status]),
    );
    transaction.onSettled(() =>
      observations.push([state.value, state.pendingCount]),
    );
    expect(state.value).toBe(0);
    const batch = await transport.next();
    transport.event(
      [
        { type: "set", values: { value: 7 } },
        {
          type: "signal",
          signal: "ping",
          data: { value: 7 },
          occurrence_id: 0,
        },
      ],
      1,
      transaction.id,
    );
    reply(batch, [confirmed(batch.batch.requests[0]!)]);
    await transaction;
    expect(observations).toEqual([
      [7, 7, "pending"],
      [7, 0],
    ]);
    dispose();
  });
  it("completes a no-op without waiting for an event", async () => {
    const { state, transport } = await ready();
    state.value = 0;
    const transaction = state.lastTransaction!;
    const batch = await transport.next();
    reply(batch, [confirmed(batch.batch.requests[0]!)]);
    await transaction.unwrap();
    expect(state.pendingCount).toBe(0);
  });
  it("isolates user binding and signal exceptions", async () => {
    const report = vi.fn();
    const { state, transport, client } = setup({ onListenerError: report });
    const connected = client.connect();
    const batch = await transport.next();
    reply(batch, [snapshot(batch.batch.requests[0]!)]);
    await connected;
    state.bind({
      value: () => {
        throw new Error("binding");
      },
    });
    state.signals.ping.bind(() => {
      throw new Error("signal");
    });
    const callback = vi.fn();
    state.signals.ping.bind(callback);
    transport.event(
      [
        { type: "set", values: { value: 9 } },
        {
          type: "signal",
          signal: "ping",
          data: { value: 9 },
          occurrence_id: 0,
        },
      ],
      1,
    );
    expect(report).toHaveBeenCalledTimes(2);
    expect(callback).toHaveBeenCalledOnce();
    expect(client.connected).toBe(true);
  });
  it("stages an entire transaction and preserves interleaved action order", async () => {
    const { state, transport, client, manager } = await ready();
    const items = manager.create(rows);
    const batch = await transport.next();
    reply(batch, [
      snapshot(batch.batch.requests[0]!, {
        id: "rows",
        rows: [{ id: "a", values: { value: 1, label: "A" } }],
      }),
    ]);
    await client.whenSynced();
    expect(() =>
      manager.transaction(
        (tx) => {
          tx.set(state, { value: 20 });
          throw new Error("cancel build");
        },
        { optimistic: true },
      ),
    ).toThrow("cancel build");
    expect(state.value).toBe(0);
    const transaction = manager.transaction((tx) => {
      tx.set(state, { value: 1 });
      tx.set(items.get("a")!, { value: 2 });
      tx.emit(state.signals.ping, { value: 1 });
    });
    const pending = await transport.next();
    const request = pending.batch.requests[0]!;
    expect(
      request.type === "transaction" &&
        request.actions.map((action) => action.type),
    ).toEqual(["set", "setrow", "signal"]);
    reply(pending, [rejected(request)]);
    await transaction;
  });
  it("keeps collection row identity through snapshots and overlay rejection", async () => {
    const { manager, client, transport } = await ready();
    const items = manager.create(rows);
    let batch = await transport.next();
    reply(batch, [
      snapshot(batch.batch.requests[0]!, {
        id: "rows",
        rows: [{ id: "a", values: { label: "A", value: 1 } }],
      }),
    ]);
    await client.whenSynced();
    const row = items.get("a")!;
    const transaction = items.remove("a", { optimistic: true });
    expect(items.has("a")).toBe(false);
    expect(row.exists).toBe(false);
    batch = await transport.next();
    reply(batch, [rejected(batch.batch.requests[0]!)]);
    await transaction;
    expect(items.get("a")).toBe(row);
    transport.event([{ type: "remove", row_id: "a" }], 1, null, "rows", "Row");
    expect(row.exists).toBe(false);
    expect(() => row.set({ value: 3 })).toThrow("detached");
    transport.event(
      [{ type: "insert", row_id: "a", values: { label: "New", value: 4 } }],
      2,
      null,
      "rows",
      "Row",
    );
    expect(items.get("a")).toBe(row);
    expect(row.value).toBe(4);
  });
  it("supports live query snapshots and revision-only events", async () => {
    const { manager, client, transport } = await ready();
    const source = query(rows).where(
      and(field("value").gte(1), not(field("label").eq("hidden"))),
    );
    const view = manager.create(source);
    const batch = await transport.next();
    reply(batch, [snapshot(batch.batch.requests[0]!, { id: "query" })]);
    await client.whenSynced();
    transport.event([], 1, null, "query", "Row");
    transport.event(
      [{ type: "insert", row_id: "a", values: { value: 2, label: "yes" } }],
      2,
      null,
      "query",
      "Row",
    );
    expect(view.get("a")?.value).toBe(2);
    expect(client.connected).toBe(true);
  });
});

describe("disconnect, recovery, and protocol failures", () => {
  it("classifies sent and queued writes, then restores subscriptions without replay", async () => {
    const { client, state, transport } = await ready();
    state.value = 1;
    const sent = state.lastTransaction!;
    await transport.next();
    state.value = 2;
    const queued = state.lastTransaction!;
    transport.lose();
    expect((await sent).status).toBe("unknown");
    expect((await queued).status).toBe("not_sent");
    expect(state.value).toBe(0);
    expect(state.synced).toBe(false);
    const reconnect = client.connect();
    const batch = await transport.next();
    expect(batch.batch.requests).toEqual([
      {
        type: "subscribe_single",
        schema: "Root",
        store_instance_id: "store",
        revision: 0,
      },
    ]);
    reply(batch, [
      snapshot(batch.batch.requests[0]!, {
        values: { value: 1 },
        revision: 1,
      }),
    ]);
    await reconnect;
    expect(state.value).toBe(1);
    expect(sent.status).toBe("unknown");
  });
  it("reuses an exact cached snapshot and accepts a new store instance with a snapshot", async () => {
    const { client, state, transport } = await ready();
    await client.disconnect();
    let reconnect = client.connect();
    let batch = await transport.next();
    reply(batch, [snapshot(batch.batch.requests[0]!, { values: null })]);
    await reconnect;
    expect(state.value).toBe(0);
    await client.disconnect();
    reconnect = client.connect();
    batch = await transport.next();
    reply(batch, [
      snapshot(batch.batch.requests[0]!, {
        instance: "fresh-store",
        values: { value: 12 },
      }),
    ]);
    await reconnect;
    expect(state.value).toBe(12);
  });
  it("rejects missing snapshots on changed instances", async () => {
    const { client, state, transport } = await ready();
    await client.disconnect();
    const reconnect = client.connect();
    const check = expect(reconnect).rejects.toThrow("snapshot");
    const batch = await transport.next();
    reply(batch, [
      snapshot(batch.batch.requests[0]!, {
        instance: "fresh-store",
        values: null,
      }),
    ]);
    await check;
    expect(state.synced).toBe(false);
    expect(state.value).toBe(0);
  });
  it("rejects revision gaps and recovers by snapshot on a new connection", async () => {
    const { state, client, transport } = await ready();
    transport.event([{ type: "set", values: { value: 99 } }], 2);
    expect(client.connected).toBe(false);
    expect(state.value).toBe(0);
    const reconnect = client.connect();
    const batch = await transport.next();
    reply(batch, [
      snapshot(batch.batch.requests[0]!, {
        values: { value: 99 },
        revision: 2,
      }),
    ]);
    await reconnect;
    expect(state.value).toBe(99);
  });
  it("does not partially install a malformed response batch", async () => {
    const { state, client, manager, transport } = setup();
    const items = manager.create(rows);
    const connected = client.connect();
    const check = expect(connected).rejects.toThrow();
    const batch = await transport.next();
    batch.result.resolve({
      responses: [
        snapshot(batch.batch.requests[0]!),
        {
          type: "subscribe_collection",
          subscription_id: "rows",
          store_instance_id: "store",
          revision: -1,
          rows: [],
        },
      ],
    });
    await check;
    expect(state.value).toBeUndefined();
    expect(items.synced).toBe(false);
  });
  it("reports subscription failures while retaining healthy subscriptions", async () => {
    const { state, client, manager, transport } = setup();
    const items = manager.create(rows);
    const connected = client.connect();
    const check = expect(connected).rejects.toBeInstanceOf(SubscriptionError);
    const batch = await transport.next();
    reply(batch, [
      snapshot(batch.batch.requests[0]!),
      { type: "failure", failure },
    ]);
    await check;
    expect(client.connected).toBe(true);
    expect(state.synced).toBe(true);
    expect(items.failure).toEqual(failure);
  });
  it("shares an in-progress connect and stops automatic retry on explicit disconnect", async () => {
    const { client, transport } = setup({ reconnectInterval: 10 });
    const first = client.connect();
    expect(client.connect()).toBe(first);
    const batch = await transport.next();
    reply(batch, [snapshot(batch.batch.requests[0]!)]);
    await first;
    transport.lose();
    await vi.waitFor(() => expect(transport.attempts).toBe(2));
    await client.disconnect();
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(transport.attempts).toBe(2);
  });
});
