import type { StoreFailure } from "./protocol.js";

export type FailureKind = "connection" | "protocol" | "state" | "store";
export type Outcome = "not_sent" | "rejected" | "unknown";

export class StoreError extends Error {
  override readonly name = "StoreError";

  constructor(
    message: string,
    readonly kind: FailureKind,
    readonly outcome: Outcome = "unknown",
    readonly failure?: StoreFailure,
  ) {
    super(message);
  }
}

export class ProtocolError extends StoreError {
  constructor(message: string) {
    super(message, "protocol");
  }
}

export class SubscriptionError extends StoreError {
  constructor(readonly failures: readonly StoreFailure[]) {
    super("One or more subscriptions were rejected", "store", "rejected");
  }
}

export function connectionError(message = "Store connection was lost") {
  return new StoreError(message, "connection", "unknown");
}

export function notSent(message: string) {
  return new StoreError(message, "state", "not_sent");
}

export function asStoreError(value: unknown): StoreError {
  return value instanceof StoreError
    ? value
    : connectionError("Store transport failed");
}
