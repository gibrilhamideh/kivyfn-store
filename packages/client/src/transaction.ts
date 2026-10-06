import { makeObservable, observable, observableRef, runInAction } from "mobx";
import type { StoreError } from "./errors.js";
import { deferred, safeCall } from "./internal.js";
import type { Revisions } from "./protocol.js";

export type TransactionStatus =
  "pending" | "confirmed" | "rejected" | "not_sent" | "unknown";
export interface TransactionOptions {
  optimistic?: boolean;
  historyGroup?: string;
  historyLabel?: string;
  expectedStoreInstanceId?: string;
  expectedRevisions?: Revisions;
}
export interface TransactionResult {
  readonly id: string;
  readonly status: Exclude<TransactionStatus, "pending">;
  readonly success: boolean;
  readonly error: StoreError | null;
}

export class Transaction implements PromiseLike<TransactionResult> {
  status: TransactionStatus = "pending";
  error: StoreError | null = null;
  private readonly completion = deferred<TransactionResult>();
  private readonly listeners = new Set<(result: TransactionResult) => void>();
  private readonly retiring = new Set<() => void>();
  private result: TransactionResult | null = null;

  constructor(
    readonly id: string,
    private readonly report: (error: unknown) => void,
  ) {
    makeObservable(this, { status: observable, error: observableRef });
  }
  get done() {
    return this.status !== "pending";
  }
  get success() {
    return this.status === "confirmed";
  }
  get failed() {
    return this.done && !this.success;
  }

  then<TResult1 = TransactionResult, TResult2 = never>(
    onfulfilled?:
      ((value: TransactionResult) => TResult1 | PromiseLike<TResult1>) | null,
    onrejected?:
      ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null,
  ): Promise<TResult1 | TResult2> {
    return this.completion.promise.then(onfulfilled, onrejected);
  }
  async unwrap(): Promise<TransactionResult> {
    const result = await this;
    if (result.error) throw result.error;
    return result;
  }
  onSettled(listener: (result: TransactionResult) => void): () => void {
    if (this.result) safeCall(() => listener(this.result!), this.report);
    else this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  /** @internal Retire model overlays before publishing completion. */
  beforeSettle(listener: () => void): void {
    if (this.done) runInAction(listener);
    else this.retiring.add(listener);
  }
  /** @internal Completion follows canonical state and pending-overlay updates. */
  settle(error: StoreError | null = null): void {
    if (this.done) return;
    const result: TransactionResult = Object.freeze({
      id: this.id,
      status: error?.outcome ?? "confirmed",
      success: !error,
      error,
    });
    runInAction(() => {
      // Retire overlays before observers see the settled transaction status.
      this.result = result;
      for (const retire of this.retiring) safeCall(retire, this.report);
      this.retiring.clear();
      this.error = error;
      this.status = result.status;
      for (const listener of this.listeners)
        safeCall(() => listener(result), this.report);
      this.listeners.clear();
    });
    this.completion.resolve(result);
  }
}
