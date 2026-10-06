import { afterEach, describe, expect, it, vi } from "vitest";
import { readFile } from "node:fs/promises";
import {
  collection,
  DirectSocketIOTransport,
  field,
  query,
  RelayTransport,
  single,
  StateManager,
  StoreClient,
} from "../src/index.js";
import type { ClientTransport } from "../src/index.js";
import { decodeEvent, decodeResponse } from "../src/protocol.js";
import { PythonHost } from "./python.js";
import { Gateway } from "./gateway.js";

const root = single<
  { value: number; label: string },
  { ping: { source: string }; unping: { source: string } }
>("ClientRoot");
const rows = collection<
  { value: number; label: string; parent_id: string | null },
  { ping: number }
>("ClientRow");
const clients: StoreClient[] = [];
let python: PythonHost | undefined;
let gateway: Gateway | undefined;
afterEach(async () => {
  await Promise.all(clients.splice(0).map((client) => client.disconnect()));
  await python?.stop();
  python = undefined;
  await gateway?.stop();
  gateway = undefined;
});
function client(transport: ClientTransport, reconnectInterval = 0) {
  const client = new StoreClient({ transport, reconnectInterval });
  clients.push(client);
  const manager = new StateManager(client);
  const state = manager.create(root);
  const items = manager.create(rows);
  return { client, manager, state, items };
}
function relay(auth = () => ({ token: "test-user" }), storeId = "unit-1") {
  return new RelayTransport({
    endpoint: gateway!.endpoint,
    storeId,
    auth,
    requestTimeout: 2000,
    connectTimeout: 2000,
  });
}
async function stack() {
  gateway = new Gateway();
  await gateway.start();
  python = await PythonHost.start(gateway.endpoint);
}

describe("Python v3 compatibility", () => {
  it("keeps live joined query fields synchronized using Python's flattened shape", async () => {
    python = await PythonHost.start();
    const {
      client: store,
      manager,
      items,
    } = client(new DirectSocketIOTransport({ endpoint: python.endpoint }));
    await store.connect();
    await items.insert("parent", { label: "Parent", value: 1 }).unwrap();
    await items
      .insert("child", { label: "Child", value: 2, parent_id: "parent" })
      .unwrap();
    const joined = query(rows).join<{
      label: string;
      value: number;
      parent_label: string | null;
      parent_value: number | null;
    }>("parent_id", "parent", "left");
    const view = manager.create(joined);
    await store.whenSynced();
    expect(view.get("child")?.parent_label).toBe("Parent");
    expect(view.get("parent")?.parent_value).toBeNull();
    await items.get("parent")!.set({ value: 10 }).unwrap();
    expect(view.get("child")?.parent_value).toBe(10);
    const selected = await store.query(
      joined.where(field("parent.value").gte(10)),
    );
    expect(selected.map((row) => row.id)).toEqual(["child"]);
  });
  it("decodes the existing Python relay golden fixture", async () => {
    const fixture = JSON.parse(
      await readFile(
        new URL(
          "../../../tests/relay/fixtures/store-v3.json",
          import.meta.url,
        ),
        "utf8",
      ),
    );
    const responses = fixture.frames.filter(
      (frame: { type: string }) => frame.type === "response",
    );
    responses.forEach((frame: { payload: unknown }, index: number) =>
      expect(() =>
        decodeResponse(frame.payload, fixture.requests[index]),
      ).not.toThrow(),
    );
    const event = fixture.frames.find(
      (frame: { type: string }) => frame.type === "event",
    );
    expect(decodeEvent(event.payload).event.events[0]?.actions).toHaveLength(
      2,
    );
  });
  it("talks directly to the real Python host: values, validation, signals, rows, queries, history", async () => {
    python = await PythonHost.start();
    const {
      client: store,
      manager,
      state,
      items,
    } = client(new DirectSocketIOTransport({ endpoint: python.endpoint }));
    await store.connect();
    expect(state.value).toBe(0);
    expect(state.synced).toBe(true);
    const order: unknown[] = [];
    state.signals.ping.bind((data) => order.push([data.source, state.value]));
    const transaction = manager.transaction((tx) => {
      tx.set(state, { value: 8 });
      tx.emit(state.signals.ping, { source: "node" });
    });
    transaction.onSettled(() => order.push("confirmed"));
    await transaction.unwrap();
    expect(order).toEqual([["node", 8], "confirmed"]);
    const invalid = state.set({ value: -1 });
    expect((await invalid).status).toBe("rejected");
    expect(invalid.error?.failure?.code).toBe("field_validation_failed");
    expect(state.value).toBe(8);
    await items.insert("first", { label: "one", value: 1 }).unwrap();
    await items.insert("second", { label: "two", value: 2 }).unwrap();
    const selected = query(rows).where(field("value").gte(2));
    expect((await store.query(selected)).map((row) => row.id)).toEqual([
      "second",
    ]);
    const live = manager.create(selected);
    await store.whenSynced();
    expect(live.rows.map((row) => row.id)).toEqual(["second"]);
    await items.get("first")!.set({ value: 3 }).unwrap();
    expect(live.has("first")).toBe(true);
    await items.remove("second").unwrap();
    expect(live.has("second")).toBe(false);
    await state.set({ value: 9 }, { historyLabel: "Set value" }).unwrap();
    const history = await store.history();
    expect(history.status.can_undo).toBe(true);
    await store.undo(history.status.undo_position).unwrap();
    expect(state.value).toBe(8);
    await store.redo((await store.history()).status.redo_position).unwrap();
    expect(state.value).toBe(9);
    const stale = store.undo(history.status.undo_position);
    expect((await stale).status).toBe("rejected");
    expect(store.connected).toBe(true);
  });
  it("relays simultaneous clients with isolated IDs and confirms only after the controller response", async () => {
    await stack();
    const a = client(relay());
    const b = client(relay());
    await Promise.all([a.client.connect(), b.client.connect()]);
    expect(gateway!.host?.channels.size).toBe(2);
    gateway!.holdResponses = true;
    a.state.value = 11;
    const write = a.state.lastTransaction!;
    await vi.waitFor(() => expect(b.state.value).toBe(11));
    expect(write.status).toBe("pending");
    expect(a.state.value).toBe(11);
    expect(gateway!.held.some(({ frame }) => frame.type === "response")).toBe(
      true,
    );
    gateway!.release();
    await write.unwrap();
    const writes = [a.state.set({ value: 12 }), b.state.set({ value: 13 })];
    const results = await Promise.all(writes);
    expect(results.every((result) => result.success)).toBe(true);
    await vi.waitFor(() => expect(a.state.value).toBe(b.state.value));
    expect(writes[0]!.id.split(":")[2]).not.toBe(writes[1]!.id.split(":")[2]);
    expect(gateway!.userAcks.length).toBeGreaterThan(0);
    await a.client.disconnect();
    await vi.waitFor(() => expect(gateway!.host?.channels.size).toBe(1));
    await b.state.set({ value: 14 }).unwrap();
    expect(b.state.value).toBe(14);
  });
  it("uses store revision guards to reject a competing stale edit", async () => {
    await stack();
    const a = client(relay());
    const b = client(relay());
    await Promise.all([a.client.connect(), b.client.connect()]);
    const options = {
      expectedStoreInstanceId: a.state.storeInstanceId!,
      expectedRevisions: { ...a.state.revisions },
    };
    const results = await Promise.all([
      a.state.set({ value: 1 }, options),
      b.state.set({ value: 2 }, options),
    ]);
    expect(results.filter((result) => result.success)).toHaveLength(1);
    expect(
      results.filter((result) => result.status === "rejected"),
    ).toHaveLength(1);
    await vi.waitFor(() => expect(a.state.value).toBe(b.state.value));
  });
  it("reports an unknown outcome after committed state loses its response, without replay", async () => {
    await stack();
    const a = client(relay());
    const b = client(relay());
    await Promise.all([a.client.connect(), b.client.connect()]);
    gateway!.dropResponses = true;
    const write = a.state.set({ value: 27 });
    expect((await write).status).toBe("unknown");
    await vi.waitFor(() => expect(b.state.value).toBe(27));
    gateway!.dropResponses = false;
    await a.client.connect();
    expect(a.state.value).toBe(27);
    expect(write.status).toBe("unknown");
    const history = await a.client.history();
    expect(
      history.entries
        .flatMap((entry) => entry.transaction_ids as string[])
        .filter((id) => id === write.id),
    ).toHaveLength(1);
  });
  it("refreshes authentication and opens a new route after the Python relay reconnects", async () => {
    await stack();
    const auth = vi.fn(() => ({ token: "test-user" }));
    const a = client(relay(auth), 20);
    await a.client.connect();
    const firstGeneration = gateway!.host!.generation;
    await a.state.set({ value: 41 }).unwrap();
    await python!.reconnectRelay();
    await vi.waitFor(() => {
      expect(a.state.synced).toBe(true);
      expect(gateway!.host!.generation).not.toBe(firstGeneration);
      expect(auth).toHaveBeenCalledTimes(2);
    });
    expect(a.state.value).toBe(41);
    await a.state.set({ value: 42 }).unwrap();
    expect(a.state.value).toBe(42);
  });
  it("recovers a fresh store instance using a snapshot after process-level store restart", async () => {
    await stack();
    const a = client(relay());
    await a.client.connect();
    const instance = a.client.storeInstanceId;
    await a.state.set({ value: 50 }).unwrap();
    await python!.restart();
    await a.client.connect();
    expect(a.client.storeInstanceId).not.toBe(instance);
    expect(a.state.value).toBe(50);
  });
  it("leaves authentication and unit access decisions to the gateway", async () => {
    await stack();
    const unauthorized = client(relay(() => ({ token: "wrong" })));
    await expect(unauthorized.client.connect()).rejects.toThrow("refused");
    const denied = client(relay(undefined, "unit-2"));
    await expect(denied.client.connect()).rejects.toThrow("refused");
    expect(gateway!.host?.channels.size).toBe(0);
  });
});
