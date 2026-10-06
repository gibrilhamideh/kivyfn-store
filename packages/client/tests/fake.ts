import { vi } from "vitest";
import type {
  ClientTransport,
  ConnectionInfo,
  TransportHandlers,
} from "../src/transport.js";
import { deferred } from "../src/internal.js";
import type { Deferred } from "../src/internal.js";
import type {
  EventAction,
  RequestBatch,
  RequestItem,
  ResponseItem,
} from "../src/protocol.js";
import { StoreError } from "../src/errors.js";

export interface PendingBatch {
  batch: RequestBatch;
  result: Deferred<unknown>;
}
export class FakeTransport implements ClientTransport {
  handlers: TransportHandlers | null = null;
  batches: PendingBatch[] = [];
  attempts = 0;
  closeCount = 0;
  async connect(handlers: TransportHandlers): Promise<ConnectionInfo> {
    this.handlers = handlers;
    return { transactionPrefix: `test:${++this.attempts}:` };
  }
  request(batch: RequestBatch): Promise<unknown> {
    const result = deferred<unknown>();
    this.batches.push({ batch, result });
    return result.promise;
  }
  async disconnect(): Promise<void> {
    ++this.closeCount;
    this.handlers = null;
  }
  async next(): Promise<PendingBatch> {
    await vi.waitFor(
      () => {
        if (!this.batches.length) throw new Error("Waiting for a request");
      },
      { interval: 1 },
    );
    return this.batches.shift()!;
  }
  event(
    actions: readonly EventAction[],
    revision: number | null,
    transactionId: string | null = null,
    subscription = "root",
    schema = "Root",
  ) {
    this.handlers?.event({
      event: {
        store_instance_id: "store",
        transaction_id: transactionId,
        events: [
          {
            subscription_id: subscription,
            revisions: revision === null ? {} : { [schema]: revision },
            actions,
          },
        ],
      },
    });
  }
  lose() {
    this.handlers?.disconnect(new StoreError("Lost", "connection"));
  }
}
export function snapshot(
  request: RequestItem,
  options: {
    id?: string;
    instance?: string;
    revision?: number;
    values?: object | null;
    rows?: readonly object[] | null;
  } = {},
): ResponseItem {
  const base = {
    subscription_id: options.id ?? "root",
    store_instance_id: options.instance ?? "store",
    revision: options.revision ?? 0,
  };
  if (request.type === "subscribe_single")
    return {
      type: request.type,
      ...base,
      values: (options.values === undefined
        ? { value: 0 }
        : options.values) as never,
    };
  if (request.type === "subscribe_collection")
    return {
      type: request.type,
      ...base,
      rows: (options.rows === undefined ? [] : options.rows) as never,
    };
  if (request.type === "subscribe_query")
    return {
      type: request.type,
      ...base,
      revisions: { Row: options.revision ?? 0 },
      rows: (options.rows === undefined ? [] : options.rows) as never,
    };
  throw new Error("Expected subscription");
}
export function reply(
  batch: PendingBatch,
  responses: readonly ResponseItem[],
) {
  batch.result.resolve({ responses });
}
export function confirmed(request: RequestItem): ResponseItem {
  if (!("transaction_id" in request)) throw new Error("Expected mutation");
  return { type: "transaction", transaction_id: request.transaction_id };
}
export const failure = {
  code: "invalid",
  category: "field",
  status: "invalid",
  message: "Invalid value",
  detail: null,
  context: null,
};
export function rejected(request: RequestItem): ResponseItem {
  if (!("transaction_id" in request)) throw new Error("Expected mutation");
  return {
    type: "transaction_failure",
    transaction_id: request.transaction_id,
    failure,
  };
}
