import { makeObservable, observable, observableRef, runInAction } from "mobx";
import {
  asStoreError,
  connectionError,
  notSent,
  ProtocolError,
  StoreError,
  SubscriptionError,
} from "./errors.js";
import {
  deferred,
  jsonBytes,
  jsonCopy,
  limit,
  randomId,
  safeCall,
  same,
} from "./internal.js";
import type { Deferred } from "./internal.js";
import { decodeEvent, decodeResponse, isSubscription } from "./protocol.js";
import type {
  EventMessage,
  HistoryPage,
  HistoryPosition,
  QueryPayload,
  RequestAction,
  RequestItem,
  ResponseItem,
  Revisions,
  StoreFailure,
  SubscriptionEvent,
  SubscriptionRequest,
  SubscriptionResponse,
  WireRow,
} from "./protocol.js";
import { Transaction } from "./transaction.js";
import type { TransactionOptions } from "./transaction.js";
import type { ClientTransport } from "./transport.js";
import type { QueryRef } from "./schema.js";

export interface StoreClientOptions {
  transport: ClientTransport;
  /** Milliseconds; omitted/zero disables automatic reconnect. */
  reconnectInterval?: number;
  maxPendingRequests?: number;
  maxPendingBytes?: number;
  maxBufferedEvents?: number;
  maxBufferedBytes?: number;
  onListenerError?: (error: unknown) => void;
}
export interface PreparedEvent {
  publish(): void;
  signals: { occurrence: number | null; emit(): void }[];
}
/** @internal A StateManager stages updates without exposing partial commits. */
export interface SubscriptionTarget {
  prepareSnapshot(reply: SubscriptionResponse): () => void;
  prepareEvent(event: SubscriptionEvent): PreparedEvent;
  retireTransaction(id: string): void;
  reset(failure?: StoreFailure): void;
}
interface Subscription {
  key: string;
  request: SubscriptionRequest;
  target: SubscriptionTarget;
  desired: boolean;
  pending: boolean;
  id: string | null;
  instance: string | null;
  revisions: Revisions | null;
  failure: StoreFailure | null;
}
interface Work {
  request: RequestItem;
  completion: Deferred<ResponseItem>;
  transaction?: Transaction;
  subscription?: Subscription;
  sent: boolean;
  bytes: number;
}

export class StoreClient {
  phase: "disconnected" | "connecting" | "connected" = "disconnected";
  lastError: StoreError | null = null;
  storeInstanceId: string | null = null;
  readonly transport: ClientTransport;
  private readonly subscriptions = new Map<string, Subscription>();
  private readonly active = new Map<string, Subscription>();
  private readonly waiters = new Set<Deferred<void>>();
  private readonly transactions = new Set<Transaction>();
  private readonly report: (error: unknown) => void;
  private readonly maxPending: number;
  private readonly maxEvents: number;
  private readonly maxPendingBytes: number;
  private readonly maxBufferedBytes: number;
  private pendingBytes = 0;
  private bufferedBytes = 0;
  private readonly reconnectInterval: number;
  private queue: Work[] = [];
  private inFlight: Work[] = [];
  private events: EventMessage[] = [];
  private epoch = 0;
  private worker = false;
  private wantConnection = false;
  private connecting: Promise<void> | null = null;
  private teardown: Promise<void> = Promise.resolve();
  private abort: AbortController | null = null;
  private retry: ReturnType<typeof setTimeout> | null = null;
  private prefix = "";
  private owner: object | null = null;

  constructor(options: StoreClientOptions) {
    this.transport = options.transport;
    this.maxPending = limit(
      options.maxPendingRequests ?? 1024,
      "maxPendingRequests",
    );
    this.maxEvents = limit(
      options.maxBufferedEvents ?? 1024,
      "maxBufferedEvents",
    );
    this.maxPendingBytes = limit(
      options.maxPendingBytes ?? 4 * 1024 * 1024,
      "maxPendingBytes",
    );
    this.maxBufferedBytes = limit(
      options.maxBufferedBytes ?? 4 * 1024 * 1024,
      "maxBufferedBytes",
    );
    this.reconnectInterval = options.reconnectInterval ?? 0;
    if (!Number.isFinite(this.reconnectInterval) || this.reconnectInterval < 0)
      throw new Error("reconnectInterval must be nonnegative");
    this.report = options.onListenerError ?? (() => {});
    makeObservable(this, {
      phase: observable,
      lastError: observableRef,
      storeInstanceId: observable,
    });
  }
  get connected() {
    return this.phase === "connected";
  }
  /** @internal One manager owns source identity and optimistic overlays. */
  claim(owner: object): void {
    if (this.owner && this.owner !== owner)
      throw new Error("A StoreClient can have only one StateManager");
    this.owner = owner;
  }
  /** @internal */
  release(owner: object): void {
    if (this.owner === owner) this.owner = null;
  }
  /** @internal Remove cached source ownership when a manager closes. */
  forget(key: string, target: SubscriptionTarget): void {
    if (this.subscriptions.get(key)?.target === target)
      this.subscriptions.delete(key);
  }
  /** @internal User callbacks cannot interrupt synchronization. */
  reportListenerError(error: unknown): void {
    safeCall(
      () => this.report(error),
      () => {},
    );
  }

  connect(): Promise<void> {
    this.wantConnection = true;
    if (this.retry) clearTimeout(this.retry);
    this.retry = null;
    if (this.connecting) return this.connecting;
    if (this.connected) return this.whenSynced();
    const promise = this.open();
    this.connecting = promise;
    void promise
      .finally(() => {
        if (this.connecting === promise) this.connecting = null;
      })
      .catch(() => {});
    return promise;
  }
  private async open(): Promise<void> {
    const epoch = ++this.epoch;
    const abort = new AbortController();
    this.abort = abort;
    runInAction(() => {
      this.phase = "connecting";
      this.lastError = null;
    });
    try {
      await this.teardown;
      if (epoch !== this.epoch)
        throw connectionError("Store connection was cancelled");
      const info = await this.transport.connect(
        {
          event: (value) => {
            if (epoch === this.epoch) this.receive(value);
          },
          disconnect: (error) => {
            if (epoch === this.epoch) this.drop(error);
          },
        },
        abort.signal,
      );
      if (epoch !== this.epoch)
        throw connectionError("Store connection was cancelled");
      if (!info.transactionPrefix)
        throw new ProtocolError(
          "Transport returned an empty transaction prefix",
        );
      this.prefix = info.transactionPrefix;
      runInAction(() => {
        this.phase = "connected";
      });
      for (const sub of this.subscriptions.values()) {
        sub.failure = null;
        if (sub.desired) this.subscribe(sub);
      }
      await this.whenSynced();
    } catch (error) {
      if (epoch === this.epoch && !(error instanceof SubscriptionError))
        this.drop(asStoreError(error));
      throw error;
    }
  }
  async disconnect(): Promise<void> {
    this.wantConnection = false;
    if (this.retry) clearTimeout(this.retry);
    this.retry = null;
    this.drop(connectionError("Store client disconnected"));
    await this.teardown;
  }
  private drop(error: StoreError): void {
    ++this.epoch;
    this.abort?.abort();
    this.abort = null;
    this.connecting = null;
    this.worker = false;
    this.prefix = "";
    const pending = [...this.inFlight, ...this.queue];
    this.inFlight = [];
    this.queue = [];
    this.events = [];
    this.pendingBytes = 0;
    this.bufferedBytes = 0;
    this.active.clear();
    runInAction(() => {
      this.phase = "disconnected";
      this.lastError = error;
      this.storeInstanceId = null;
      for (const sub of this.subscriptions.values()) {
        sub.id = null;
        sub.pending = false;
        sub.target.reset();
      }
      for (const work of pending) {
        const failure = new StoreError(
          error.message,
          error.kind,
          work.sent ? "unknown" : "not_sent",
          error.failure,
        );
        work.transaction?.settle(failure);
        work.completion.reject(failure);
      }
    });
    for (const waiter of this.waiters) waiter.reject(error);
    this.waiters.clear();
    this.teardown = this.transport.disconnect().catch(() => {});
    if (this.wantConnection && this.reconnectInterval > 0 && !this.retry) {
      this.retry = setTimeout(() => {
        this.retry = null;
        if (this.wantConnection) void this.connect().catch(() => {});
      }, this.reconnectInterval);
    }
  }
  whenSynced(): Promise<void> {
    if (!this.connected)
      return Promise.reject(notSent("Store is not connected"));
    const waiter = deferred<void>();
    this.waiters.add(waiter);
    this.checkSynced();
    return waiter.promise;
  }
  private checkSynced(): void {
    if (!this.connected) return;
    const desired = [...this.subscriptions.values()].filter(
      (sub) => sub.desired,
    );
    if (desired.some((sub) => !sub.id && !sub.failure)) return;
    const failures = desired.flatMap((sub) =>
      sub.failure ? [sub.failure] : [],
    );
    for (const waiter of this.waiters) {
      if (failures.length) waiter.reject(new SubscriptionError(failures));
      else waiter.resolve();
    }
    this.waiters.clear();
  }
  /** @internal Called by StateManager when the first consumer retains a source. */
  watch(
    key: string,
    request: SubscriptionRequest,
    target: SubscriptionTarget,
  ): () => void {
    let sub = this.subscriptions.get(key);
    if (sub && sub.target !== target)
      throw new Error("Source is already owned by another state object");
    if (!sub) {
      sub = {
        key,
        request,
        target,
        desired: true,
        pending: false,
        id: null,
        instance: null,
        revisions: null,
        failure: null,
      };
      this.subscriptions.set(key, sub);
    } else {
      sub.desired = true;
      sub.failure = null;
    }
    if (this.connected && !sub.id && !sub.pending) this.subscribe(sub);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      sub!.desired = false;
      if (sub!.id) {
        const id = sub!.id;
        this.active.delete(id);
        sub!.id = null;
        if (this.connected)
          this.enqueue({ type: "unsubscribe", subscription_id: id });
      }
      runInAction(() => sub!.target.reset());
      this.checkSynced();
    };
  }
  private subscribe(sub: Subscription): void {
    sub.pending = true;
    let request = sub.request;
    if (sub.instance && sub.revisions) {
      request =
        request.type === "subscribe_query"
          ? {
              ...request,
              store_instance_id: sub.instance,
              revisions: sub.revisions,
            }
          : {
              ...request,
              store_instance_id: sub.instance,
              revision: sub.revisions[request.schema],
            };
    }
    this.enqueue(request, undefined, sub);
  }
  private enqueue(
    request: RequestItem,
    transaction?: Transaction,
    subscription?: Subscription,
  ): Work {
    const frozen = jsonCopy(request);
    const work: Work = {
      request: frozen,
      transaction,
      subscription,
      sent: false,
      bytes: jsonBytes(frozen),
      completion: deferred<ResponseItem>(),
    };
    if (
      !this.connected ||
      this.queue.length + this.inFlight.length >= this.maxPending ||
      this.pendingBytes + work.bytes > this.maxPendingBytes
    ) {
      const error = notSent(
        this.connected
          ? "Store request queue is full"
          : "Store is not connected",
      );
      transaction?.settle(error);
      work.completion.reject(error);
      if (subscription) this.drop(error);
      return work;
    }
    this.pendingBytes += work.bytes;
    this.queue.push(work);
    queueMicrotask(() => {
      void this.flush();
    });
    return work;
  }
  private async flush(): Promise<void> {
    if (this.worker || !this.connected) return;
    const epoch = this.epoch;
    this.worker = true;
    try {
      while (this.queue.length && epoch === this.epoch) {
        const works = this.queue.splice(0);
        this.inFlight = works;
        const batch = { requests: works.map((work) => work.request) };
        for (const work of works) work.sent = true;
        const raw = await this.transport.request(batch);
        if (epoch !== this.epoch) return;
        const { responses } = decodeResponse(raw, batch);
        const installs: (() => void)[] = [];
        const ids = new Set(this.active.keys());
        let instance = this.storeInstanceId;
        // Validate every reply and cache precondition before publishing any.
        for (let index = 0; index < responses.length; ++index) {
          const reply = responses[index]!;
          const work = works[index]!;
          const sub = work.subscription;
          if (!sub || !isSubscription(reply)) continue;
          if (instance && instance !== reply.store_instance_id)
            throw new ProtocolError(
              "Store instance changed within a connection",
            );
          instance = reply.store_instance_id;
          if (ids.has(reply.subscription_id))
            throw new ProtocolError("Duplicate live subscription ID");
          ids.add(reply.subscription_id);
          const request = work.request as SubscriptionRequest;
          const revisions =
            reply.type === "subscribe_query"
              ? reply.revisions
              : { [(request as { schema: string }).schema]: reply.revision };
          const snapshot =
            reply.type === "subscribe_single" ? reply.values : reply.rows;
          if (
            snapshot === null &&
            (sub.instance !== reply.store_instance_id ||
              !same(sub.revisions, revisions))
          ) {
            throw new ProtocolError("Store omitted a required snapshot");
          }
          const publish = sub.target.prepareSnapshot(reply);
          installs.push(() => {
            sub.pending = false;
            sub.failure = null;
            if (sub.desired) {
              sub.instance = reply.store_instance_id;
              sub.revisions = { ...revisions };
              sub.id = reply.subscription_id;
              this.active.set(sub.id, sub);
              publish();
            } else {
              sub.target.reset();
              this.enqueue({
                type: "unsubscribe",
                subscription_id: reply.subscription_id,
              });
            }
          });
        }
        runInAction(() => {
          this.storeInstanceId = instance;
          for (const install of installs) install();
          responses.forEach((reply, index) => {
            const sub = works[index]!.subscription;
            if (sub && reply.type === "failure") {
              sub.pending = false;
              sub.failure = reply.failure;
              sub.target.reset(reply.failure);
            }
          });
        });
        // Socket.IO may deliver events before an ack continuation executes.
        // Install snapshots, then apply those events, then finish transactions.
        while (this.events.length) this.deliver(this.events.shift()!);
        this.bufferedBytes = 0;
        if (epoch !== this.epoch) return;
        this.inFlight = [];
        this.pendingBytes -= works.reduce((sum, work) => sum + work.bytes, 0);
        responses.forEach((reply, index) => {
          const work = works[index]!;
          if (
            reply.type === "failure" ||
            reply.type === "transaction_failure"
          ) {
            const error = new StoreError(
              reply.failure.message,
              "store",
              "rejected",
              reply.failure,
            );
            work.transaction?.settle(error);
            work.completion.reject(error);
          } else {
            work.transaction?.settle();
            work.completion.resolve(reply);
          }
        });
        this.checkSynced();
      }
    } catch (error) {
      if (epoch === this.epoch) {
        const failure = asStoreError(error);
        if (failure.outcome === "not_sent") {
          for (const work of this.inFlight) work.sent = false;
        }
        this.drop(failure);
      }
    } finally {
      if (epoch === this.epoch) this.worker = false;
    }
  }
  private receive(value: unknown): void {
    try {
      const event = decodeEvent(value);
      if (this.inFlight.length) {
        const bytes = jsonBytes(event);
        if (
          this.events.length >= this.maxEvents ||
          this.bufferedBytes + bytes > this.maxBufferedBytes
        )
          throw new ProtocolError("Store event buffer is full");
        this.bufferedBytes += bytes;
        this.events.push(event);
      } else this.deliver(event);
    } catch (error) {
      this.drop(asStoreError(error));
    }
  }
  private deliver(message: EventMessage): void {
    const event = message.event;
    if (
      this.storeInstanceId &&
      this.storeInstanceId !== event.store_instance_id
    )
      throw new ProtocolError("Store instance changed within a connection");
    const prepared: {
      sub: Subscription;
      revisions: Revisions;
      update: PreparedEvent;
    }[] = [];
    for (const group of event.events) {
      const sub = this.active.get(group.subscription_id);
      if (!sub) continue;
      const revisions = { ...sub.revisions };
      for (const [schema, revision] of Object.entries(group.revisions)) {
        if (!(schema in revisions) || revision !== revisions[schema]! + 1)
          throw new ProtocolError(
            "Subscription revision gap; a snapshot is required",
          );
        revisions[schema] = revision;
      }
      if (
        !Object.keys(group.revisions).length &&
        group.actions.some(
          (action) => action.type !== "signal" && action.type !== "signalrow",
        )
      ) {
        throw new ProtocolError("Retained state changed without a revision");
      }
      prepared.push({
        sub,
        revisions,
        update: sub.target.prepareEvent(group),
      });
    }
    runInAction(() => {
      for (const { sub, revisions, update } of prepared) {
        sub.revisions = revisions;
        update.publish();
      }
      if (event.transaction_id) {
        for (const sub of this.subscriptions.values())
          sub.target.retireTransaction(event.transaction_id);
      }
    });
    const signals = prepared.flatMap(({ update }) => update.signals);
    signals.sort(
      (a, b) => (a.occurrence ?? Infinity) - (b.occurrence ?? Infinity),
    );
    for (const signal of signals) safeCall(() => signal.emit(), this.report);
  }
  transaction(
    actions: readonly RequestAction[],
    options: TransactionOptions = {},
  ): Transaction {
    const transaction = new Transaction(
      `${this.prefix || "offline:"}${randomId()}`,
      this.report,
    );
    this.transactions.add(transaction);
    transaction.onSettled(() => this.transactions.delete(transaction));
    try {
      const request: RequestItem = {
        type: "transaction",
        transaction_id: transaction.id,
        actions: jsonCopy(actions),
        ...(options.historyGroup !== undefined && {
          history_group: options.historyGroup,
        }),
        ...(options.historyLabel !== undefined && {
          history_label: options.historyLabel,
        }),
        ...(options.expectedStoreInstanceId !== undefined && {
          expected_store_instance_id: options.expectedStoreInstanceId,
        }),
        ...(options.expectedRevisions !== undefined && {
          expected_revisions: options.expectedRevisions,
        }),
      };
      this.enqueue(request, transaction);
    } catch (error) {
      transaction.settle(
        notSent(
          error instanceof Error ? error.message : "Invalid store request",
        ),
      );
    }
    return transaction;
  }
  async query<T extends object>(
    query: QueryRef<T> | QueryPayload,
  ): Promise<readonly WireRow[]> {
    const payload = "kind" in query ? query.payload : query;
    const response = await this.enqueue({ type: "query", query: payload })
      .completion.promise;
    if (response.type !== "query")
      throw new ProtocolError("Expected query response");
    return response.rows;
  }
  async history(limit = 100, offset = 0): Promise<HistoryPage> {
    const response = await this.enqueue({ type: "history", limit, offset })
      .completion.promise;
    if (response.type !== "history")
      throw new ProtocolError("Expected history response");
    return response.page;
  }
  undo(position: HistoryPosition): Transaction {
    return this.restore("undo", position);
  }
  redo(position: HistoryPosition): Transaction {
    return this.restore("redo", position);
  }
  private restore(
    type: "undo" | "redo",
    position: HistoryPosition,
  ): Transaction {
    const transaction = new Transaction(
      `${this.prefix || "offline:"}${randomId()}`,
      this.report,
    );
    if (this.transactions.size) {
      transaction.settle(
        notSent("Wait for local transactions before undo or redo"),
      );
      return transaction;
    }
    this.transactions.add(transaction);
    transaction.onSettled(() => this.transactions.delete(transaction));
    try {
      this.enqueue(
        { type, transaction_id: transaction.id, expected_position: position },
        transaction,
      );
    } catch (error) {
      transaction.settle(
        notSent(
          error instanceof Error ? error.message : "Invalid history position",
        ),
      );
    }
    return transaction;
  }
}
