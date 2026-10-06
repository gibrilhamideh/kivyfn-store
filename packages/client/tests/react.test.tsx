// @vitest-environment jsdom
import { StrictMode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  StateManager,
  StoreClient,
  collection,
  single,
} from "../src/index.js";
import { observer, StoreProvider, useStoreState } from "../src/react.js";
import { FakeTransport, confirmed, reply, snapshot } from "./fake.js";

const source = single<{ value: number }>("Root");
const items = collection<{ label: string }>("Row");
const clients: StoreClient[] = [];
afterEach(async () => {
  cleanup();
  await Promise.all(clients.splice(0).map((client) => client.disconnect()));
});
function setup() {
  const transport = new FakeTransport();
  const client = new StoreClient({ transport });
  const manager = new StateManager(client);
  clients.push(client);
  return { transport, client, manager };
}
const Value = observer(function Value() {
  // Equivalent references created during render must not resubscribe each time.
  const state = useStoreState(single<{ value: number }>("Root"));
  return (
    <button
      disabled={!state.synced}
      onClick={() => {
        state.value = 5;
      }}
    >
      {state.value ?? "loading"}
    </button>
  );
});

it("renders subscribed values with observer and cleans up in StrictMode", async () => {
  const { transport, client, manager } = setup();
  const view = render(
    <StrictMode>
      <StoreProvider manager={manager}>
        <Value />
      </StoreProvider>
    </StrictMode>,
  );
  expect(screen.getByRole("button").textContent).toBe("loading");
  await act(async () => {
    const connected = client.connect();
    const batch = await transport.next();
    expect(batch.batch.requests).toHaveLength(1);
    reply(batch, [snapshot(batch.batch.requests[0]!)]);
    await connected;
  });
  expect(screen.getByRole("button").textContent).toBe("0");
  fireEvent.click(screen.getByRole("button"));
  expect(screen.getByRole("button").textContent).toBe("5");
  await act(async () => {
    const batch = await transport.next();
    const request = batch.batch.requests[0]!;
    if (!("transaction_id" in request))
      throw new Error("Expected transaction");
    transport.event(
      [{ type: "set", values: { value: 5 } }],
      1,
      request.transaction_id,
    );
    reply(batch, [confirmed(request)]);
    await Promise.resolve();
  });
  expect(screen.getByRole("button").textContent).toBe("5");
  expect(transport.batches).toHaveLength(0);
  view.unmount();
  const unsubscribe = await transport.next();
  expect(unsubscribe.batch.requests).toEqual([
    { type: "unsubscribe", subscription_id: "root" },
  ]);
  reply(unsubscribe, [{ type: "unsubscribe", subscription_id: "root" }]);
});

it("switches selected stores without showing the previous store's values", async () => {
  const a = setup();
  const b = setup();
  const view = render(
    <StoreProvider manager={a.manager}>
      <Value />
    </StoreProvider>,
  );
  await act(async () => {
    const connected = a.client.connect();
    const batch = await a.transport.next();
    reply(batch, [
      snapshot(batch.batch.requests[0]!, { values: { value: 11 } }),
    ]);
    await connected;
  });
  expect(screen.getByRole("button").textContent).toBe("11");
  view.rerender(
    <StoreProvider manager={b.manager}>
      <Value />
    </StoreProvider>,
  );
  expect(screen.getByRole("button").textContent).toBe("loading");
  await act(async () => {
    const connected = b.client.connect();
    const batch = await b.transport.next();
    reply(batch, [
      snapshot(batch.batch.requests[0]!, { values: { value: 22 } }),
    ]);
    await connected;
  });
  expect(screen.getByRole("button").textContent).toBe("22");
  const unsubscribe = await a.transport.next();
  reply(unsubscribe, [{ type: "unsubscribe", subscription_id: "root" }]);
});

it("observes initially absent collection rows and reuses a shared subscription", async () => {
  const { client, transport, manager } = setup();
  const renders = vi.fn();
  const Row = observer(function Row() {
    const collection = useStoreState(items);
    renders();
    return <span>{collection.get("a")?.label ?? "missing"}</span>;
  });
  render(
    <StoreProvider manager={manager}>
      <Row />
      <Row />
    </StoreProvider>,
  );
  await act(async () => {
    const connected = client.connect();
    const batch = await transport.next();
    expect(batch.batch.requests).toHaveLength(1);
    reply(batch, [snapshot(batch.batch.requests[0]!, { id: "rows" })]);
    await connected;
  });
  act(() =>
    transport.event(
      [{ type: "insert", row_id: "a", values: { label: "arrived" } }],
      1,
      null,
      "rows",
      "Row",
    ),
  );
  expect(screen.getAllByText("arrived")).toHaveLength(2);
  expect(renders.mock.calls.length).toBeGreaterThanOrEqual(4);
});

it("does not subscribe when preparing a state outside a mounted effect", async () => {
  const { manager, client, transport } = setup();
  const a = manager.prepare(source);
  const b = manager.prepare(single<{ value: number }>("Root"));
  expect(a).toBe(b);
  await client.connect();
  expect(transport.batches).toHaveLength(0);
});
