import { observable, reaction, runInAction } from "mobx";
import { StoreClient } from "./client.js";
import type { PreparedEvent, SubscriptionTarget } from "./client.js";
import { notSent, ProtocolError } from "./errors.js";
import {
  canonicalKey,
  jsonCopy,
  object,
  same,
  safeCall,
  text,
} from "./internal.js";
import type { Json, JsonObject } from "./internal.js";
import type {
  EventAction,
  RequestAction,
  Revisions,
  StoreFailure,
  SubscriptionEvent,
  SubscriptionRequest,
  SubscriptionResponse,
} from "./protocol.js";
import type {
  CollectionRef,
  QueryRef,
  SingleRef,
  SourceRef,
} from "./schema.js";
import type { Transaction, TransactionOptions } from "./transaction.js";

export type Disposer = () => void;
export type Snapshot<T> = T extends readonly (infer V)[]
  ? readonly Snapshot<V>[]
  : T extends object
    ? { readonly [K in keyof T]: Snapshot<T[K]> }
    : T;
export interface SignalInfo {
  readonly rowId: string | null;
}
export interface Signal<T = void> {
  emit(
    ...args: T extends void
      ? [data?: T, options?: TransactionOptions]
      : [data: T, options?: TransactionOptions]
  ): Transaction;
  bind(callback: (data: T, info: SignalInfo) => void): Disposer;
}
export type Signals<S extends object> = {
  readonly [K in keyof S]: Signal<S[K]>;
};
export type Bindings<T extends object> = {
  [K in keyof T]?: (
    value: Snapshot<T[K]> | undefined,
    previous: Snapshot<T[K]> | undefined,
  ) => void;
};
export interface StateHandle<T extends object, S extends object = {}> {
  readonly id: string | null;
  readonly synced: boolean;
  readonly storeInstanceId: string | null;
  readonly revisions: Readonly<Revisions>;
  readonly exists: boolean;
  readonly failure: StoreFailure | null;
  readonly pendingCount: number;
  readonly lastTransaction: Transaction | null;
  readonly signals: Signals<S>;
  getField<K extends keyof T>(name: K): Snapshot<T[K]> | undefined;
  set(values: Partial<T>, options?: TransactionOptions): Transaction;
  bind(
    bindings: Bindings<T>,
    options?: { fireImmediately?: boolean },
  ): Disposer;
}
// Undefined explicitly models the interval before the first snapshot.
export type State<T extends object, S extends object = {}> = StateHandle<
  T,
  S
> & {
  [K in Exclude<keyof T, keyof StateHandle<T, S>>]: Snapshot<T[K]> | undefined;
};
export type ReadonlyState<T extends object> = Readonly<
  Omit<State<T>, "set" | "signals">
>;
export interface CollectionBindings<T extends object, S extends object = {}> {
  onInsert?: (row: State<T, S>) => void;
  onRemove?: (row: State<T, S>) => void;
}
export interface CollectionState<T extends object, S extends object = {}> {
  readonly rows: readonly State<T, S>[];
  readonly size: number;
  readonly synced: boolean;
  readonly storeInstanceId: string | null;
  readonly revisions: Readonly<Revisions>;
  readonly failure: StoreFailure | null;
  readonly pendingCount: number;
  readonly lastTransaction: Transaction | null;
  readonly signals: { readonly [K in keyof S]: Pick<Signal<S[K]>, "bind"> };
  get(id: string): State<T, S> | undefined;
  has(id: string): boolean;
  insert(
    id: string,
    values: Partial<T>,
    options?: TransactionOptions,
  ): Transaction;
  remove(id: string, options?: TransactionOptions): Transaction;
  bind(bindings: CollectionBindings<T, S>): Disposer;
}
export interface QueryState<T extends object> {
  readonly rows: readonly ReadonlyState<T>[];
  readonly size: number;
  readonly synced: boolean;
  readonly storeInstanceId: string | null;
  readonly revisions: Readonly<Revisions>;
  readonly failure: StoreFailure | null;
  get(id: string): ReadonlyState<T> | undefined;
  has(id: string): boolean;
}

interface HandleInfo {
  model: Model;
  rowId: string | null;
}
const handles = new WeakMap<object, HandleInfo>();
const signalHandles = new WeakMap<object, HandleInfo & { name: string }>();
type Listener = (data: unknown, info: SignalInfo) => void;
interface RowData {
  fields: ReturnType<typeof observable.map<string, Json>>;
  exists: ReturnType<typeof observable.box<boolean>>;
  last: ReturnType<typeof observable.box<Transaction | null>>;
  view: State<object, object>;
}
interface Tracked {
  transaction: Transaction;
  actions: readonly RequestAction[];
}

class Model implements SubscriptionTarget {
  readonly synced = observable.box(false);
  readonly instance = observable.box<string | null>(null);
  readonly confirmedRevisions = observable.box<Readonly<Revisions>>(
    Object.freeze({}),
    { deep: false },
  );
  readonly failure = observable.box<StoreFailure | null>(null, {
    deep: false,
  });
  readonly last = observable.box<Transaction | null>(null, { deep: false });
  readonly pending = observable.map<string, Tracked>({}, { deep: false });
  readonly visibleRows = observable.array<State<object, object>>([], {
    deep: false,
  });
  readonly rows = new Map<string, RowData>();
  readonly listeners = new Map<string, Set<Listener>>();
  readonly view: State<object, object> | CollectionState<object, object>;
  readonly reference: { view: unknown; retain(): Disposer };
  private canonical = new Map<string, JsonObject>();
  private overlays = new Map<string, readonly RequestAction[]>();
  private release: Disposer | null = null;
  private consumers = 0;

  constructor(
    readonly manager: StateManager,
    readonly source: SourceRef,
    readonly key: string,
  ) {
    if (source.kind === "single") this.view = this.row("").view;
    else {
      const model = this;
      this.view = {
        get rows() {
          return model.visibleRows;
        },
        get size() {
          return model.visibleRows.length;
        },
        get synced() {
          return model.synced.get();
        },
        get storeInstanceId() {
          return model.instance.get();
        },
        get revisions() {
          return model.confirmedRevisions.get();
        },
        get failure() {
          return model.failure.get();
        },
        get pendingCount() {
          return model.pending.size;
        },
        get lastTransaction() {
          return model.last.get();
        },
        signals: this.signalProxy(null),
        get(id) {
          return model.visibleRows.find((row) => row.id === id);
        },
        has(id) {
          return model.visibleRows.some((row) => row.id === id);
        },
        insert(id, values, options) {
          return model.manager.transaction(
            (tx) =>
              tx.insert(model.view as CollectionState<object>, id, values),
            options,
          );
        },
        remove(id, options) {
          return model.manager.transaction(
            (tx) => tx.remove(model.view as CollectionState<object>, id),
            options,
          );
        },
        bind(bindings: CollectionBindings<object, object>) {
          return reaction(
            () => [...model.visibleRows],
            (next, previous) => {
              for (const row of previous)
                if (!next.includes(row))
                  model.call(() => bindings.onRemove?.(row));
              for (const row of next)
                if (!previous.includes(row))
                  model.call(() => bindings.onInsert?.(row));
            },
          );
        },
      };
      handles.set(this.view, { model: this, rowId: null });
    }
    this.reference = { view: this.view, retain: () => this.retain() };
  }
  call(action: () => void) {
    safeCall(action, (error) =>
      this.manager.client.reportListenerError(error),
    );
  }
  retain(): Disposer {
    if (++this.consumers === 1) {
      const request: SubscriptionRequest =
        this.source.kind === "query"
          ? { type: "subscribe_query", query: this.source.payload }
          : {
              type:
                this.source.kind === "single"
                  ? "subscribe_single"
                  : "subscribe_collection",
              schema: this.source.name,
            };
      this.release = this.manager.client.watch(this.key, request, this);
    }
    let released = false;
    return () => {
      if (released) return;
      released = true;
      if (--this.consumers === 0) {
        this.release?.();
        this.release = null;
      }
    };
  }
  close(): void {
    this.release?.();
    this.release = null;
    this.reset();
    this.manager.client.forget(this.key, this);
  }
  reset(failure: StoreFailure | null = null): void {
    runInAction(() => {
      this.synced.set(false);
      this.failure.set(failure);
    });
  }
  row(id: string): RowData {
    const existing = this.rows.get(id);
    if (existing) return existing;
    const model = this;
    const fields = observable.map<string, Json>({}, { deep: false });
    const exists = observable.box(false);
    const last = observable.box<Transaction | null>(null, { deep: false });
    const rowId = this.source.kind === "single" ? null : id;
    const handle: StateHandle<object, object> = {
      id: rowId,
      get synced() {
        return model.synced.get() && exists.get();
      },
      get storeInstanceId() {
        return model.instance.get();
      },
      get revisions() {
        return model.confirmedRevisions.get();
      },
      get exists() {
        return exists.get();
      },
      get failure() {
        return model.failure.get();
      },
      get pendingCount() {
        return [...model.pending.values()].filter(({ actions }) =>
          actions.some(
            (action) =>
              rowId === null ||
              ("row_id" in action && action.row_id === rowId),
          ),
        ).length;
      },
      get lastTransaction() {
        return last.get();
      },
      signals: this.signalProxy(rowId),
      getField(name) {
        return fields.get(name as string) as never;
      },
      set(values, options) {
        return model.manager.transaction(
          (tx) => tx.set(view, values),
          options,
        );
      },
      bind(bindings, options = {}) {
        const disposers = Object.entries(bindings).map(([name, callback]) =>
          reaction(
            () => fields.get(name),
            (value, previous) =>
              model.call(() =>
                (callback as (value: unknown, previous: unknown) => void)(
                  value,
                  previous,
                ),
              ),
            { fireImmediately: options.fireImmediately },
          ),
        );
        return () => disposers.forEach((dispose) => dispose());
      },
    };
    const view = new Proxy(handle, {
      get(target, property, receiver) {
        if (property in target || typeof property !== "string")
          return Reflect.get(target, property, receiver);
        if (property === "then") return undefined;
        return fields.get(property);
      },
      set(target, property, value) {
        if (
          typeof property !== "string" ||
          property in target ||
          property === "then"
        )
          throw new Error(
            "Use .set() for fields that collide with state API names",
          );
        model.manager.transaction(
          (tx) => tx.set(view, { [property]: value }),
          { optimistic: true },
        );
        return true;
      },
      deleteProperty() {
        throw new Error("Replace store fields with assignment or .set()");
      },
      defineProperty() {
        throw new Error("Store fields cannot be redefined");
      },
      ownKeys() {
        return [...fields.keys()];
      },
      getOwnPropertyDescriptor(_target, property) {
        if (typeof property === "string" && fields.has(property))
          return {
            enumerable: true,
            configurable: true,
            value: fields.get(property),
          };
        return undefined;
      },
    }) as State<object, object>;
    const data = { fields, exists, last, view };
    this.rows.set(id, data);
    handles.set(view, { model, rowId });
    return data;
  }
  private signalProxy(rowId: string | null): Signals<object> {
    const cache = new Map<string, Signal<unknown>>();
    return new Proxy(Object.create(null) as Signals<object>, {
      get: (_target, property) => {
        if (typeof property !== "string" || property === "then")
          return undefined;
        let signal = cache.get(property);
        if (!signal) {
          const key = canonicalKey([rowId, property]);
          signal = {
            emit: (data = null, options?: TransactionOptions) =>
              this.manager.transaction(
                (tx) => tx.emit(signal!, data),
                options,
              ),
            bind: (callback) => {
              let listeners = this.listeners.get(key);
              if (!listeners) {
                listeners = new Set();
                this.listeners.set(key, listeners);
              }
              listeners.add(callback);
              return () => {
                listeners!.delete(callback);
                if (!listeners!.size) this.listeners.delete(key);
              };
            },
          };
          cache.set(property, signal);
          signalHandles.set(signal, { model: this, rowId, name: property });
        }
        return signal;
      },
    });
  }
  private emit(
    action: Extract<EventAction, { type: "signal" | "signalrow" }>,
  ): void {
    const rowId = "row_id" in action ? action.row_id : null;
    const keys = [canonicalKey([rowId, action.signal])];
    if (rowId !== null) keys.push(canonicalKey([null, action.signal]));
    const info = Object.freeze({ rowId });
    for (const key of keys) {
      for (const listener of [...(this.listeners.get(key) ?? [])])
        this.call(() => listener(action.data, info));
    }
  }
  prepareSnapshot(reply: SubscriptionResponse): () => void {
    const expected =
      this.source.kind === "single"
        ? "subscribe_single"
        : this.source.kind === "query"
          ? "subscribe_query"
          : "subscribe_collection";
    if (reply.type !== expected)
      throw new ProtocolError("Snapshot type does not match state");
    let next = this.canonical;
    if (reply.type === "subscribe_single" && reply.values !== null)
      next = new Map([["", reply.values]]);
    else if (reply.type !== "subscribe_single" && reply.rows !== null)
      next = new Map(reply.rows.map((row) => [row.id, row.values]));
    const revisions =
      reply.type === "subscribe_query"
        ? reply.revisions
        : { [(this.source as { name: string }).name]: reply.revision };
    return () => {
      this.canonical = next;
      this.instance.set(reply.store_instance_id);
      this.confirmedRevisions.set(jsonCopy(revisions));
      this.synced.set(true);
      this.failure.set(null);
      this.publish();
    };
  }
  prepareEvent(event: SubscriptionEvent): PreparedEvent {
    const next = new Map(this.canonical);
    const signals: PreparedEvent["signals"] = [];
    for (const action of event.actions) {
      const single = this.source.kind === "single";
      if ((action.type === "set" || action.type === "signal") !== single) {
        throw new ProtocolError(
          "Event action does not match subscription kind",
        );
      }
      if (action.type === "signal" || action.type === "signalrow") {
        signals.push({
          occurrence: action.occurrence_id ?? null,
          emit: () => this.emit(action),
        });
        continue;
      }
      const id = "row_id" in action ? action.row_id : "";
      if (action.type === "insert") {
        if (next.has(id))
          throw new ProtocolError("Store inserted an existing row");
        next.set(id, action.values);
      } else if (action.type === "remove") {
        if (!next.delete(id))
          throw new ProtocolError("Store removed an unknown row");
      } else {
        const previous = next.get(id);
        if (!previous) throw new ProtocolError("Store updated an unknown row");
        next.set(id, jsonCopy({ ...previous, ...action.values }));
      }
    }
    return {
      publish: () => {
        this.canonical = next;
        if (Object.keys(event.revisions).length) {
          this.confirmedRevisions.set(
            jsonCopy({ ...this.confirmedRevisions.get(), ...event.revisions }),
          );
        }
        this.publish();
      },
      signals,
    };
  }
  track(
    transaction: Transaction,
    actions: readonly RequestAction[],
    optimistic: boolean,
  ): void {
    this.pending.set(transaction.id, { transaction, actions });
    this.last.set(transaction);
    for (const action of actions) {
      const id = "row_id" in action ? action.row_id : "";
      this.row(id).last.set(transaction);
    }
    if (optimistic && !transaction.done)
      this.overlays.set(transaction.id, actions);
    this.publish();
    transaction.beforeSettle(() => {
      this.pending.delete(transaction.id);
      this.retireTransaction(transaction.id);
    });
  }
  retireTransaction(id: string): void {
    if (this.overlays.delete(id)) this.publish();
  }
  private publish(): void {
    const visible = new Map(this.canonical);
    for (const actions of this.overlays.values()) {
      for (const action of actions) {
        const id = "row_id" in action ? action.row_id : "";
        if (action.type === "remove") visible.delete(id);
        else if (action.type === "insert") visible.set(id, action.values);
        else if (
          (action.type === "set" || action.type === "setrow") &&
          visible.has(id)
        ) {
          visible.set(id, { ...visible.get(id)!, ...action.values });
        }
      }
    }
    for (const [id, data] of this.rows) {
      const values = visible.get(id);
      data.exists.set(!!values);
      if (!values) continue; // Detached row references retain their last values.
      for (const key of data.fields.keys())
        if (!(key in values)) data.fields.delete(key);
      for (const [key, value] of Object.entries(values)) {
        if (!same(data.fields.get(key), value)) data.fields.set(key, value);
      }
    }
    const views = [...visible].map(([id, values]) => {
      const data = this.row(id);
      if (!data.exists.get()) {
        data.exists.set(true);
        for (const [key, value] of Object.entries(values))
          data.fields.set(key, value);
      }
      return data.view;
    });
    if (
      views.length !== this.visibleRows.length ||
      views.some((view, index) => view !== this.visibleRows[index])
    )
      this.visibleRows.replace(views);
  }
  writable(rowId?: string | null): void {
    if (this.source.kind === "query")
      throw notSent("Query views are read-only; update the source collection");
    if (!this.synced.get())
      throw notSent("Wait for the state subscription before writing");
    if (rowId !== undefined && !this.rows.get(rowId ?? "")?.exists.get())
      throw notSent("Cannot update a detached row");
  }
}

export class TransactionBuilder {
  /** @internal */
  readonly actions = new Map<Model, RequestAction[]>();
  /** @internal Preserve action order even when sources are interleaved. */
  readonly sequence: RequestAction[] = [];
  private active = true;
  constructor(private readonly manager: StateManager) {}
  /** @internal */
  finish(): void {
    this.active = false;
  }
  private target(handle: object): HandleInfo {
    if (!this.active)
      throw new Error("Transaction builder has already finished");
    const info = handles.get(handle);
    if (!info || info.model.manager !== this.manager)
      throw new Error("State belongs to another StateManager");
    return info;
  }
  private append(model: Model, action: RequestAction): void {
    const actions = this.actions.get(model) ?? [];
    const frozen = jsonCopy(action);
    actions.push(frozen);
    this.sequence.push(frozen);
    this.actions.set(model, actions);
  }
  set<T extends object, S extends object>(
    state: State<T, S>,
    values: Partial<T>,
  ): void {
    const { model, rowId } = this.target(state);
    model.writable(rowId);
    if (model.source.kind === "query") return;
    object(values);
    this.append(
      model,
      rowId === null
        ? {
            type: "set",
            schema: model.source.name,
            values: values as JsonObject,
          }
        : {
            type: "setrow",
            schema: model.source.name,
            row_id: rowId,
            values: values as JsonObject,
          },
    );
  }
  insert<T extends object, S extends object>(
    collection: CollectionState<T, S>,
    id: string,
    values: Partial<T>,
  ): void {
    const { model } = this.target(collection);
    model.writable();
    if (model.source.kind !== "collection")
      throw new Error("Insert requires a collection");
    text(id);
    object(values);
    this.append(model, {
      type: "insert",
      schema: model.source.name,
      row_id: id,
      values: values as JsonObject,
    });
  }
  remove<T extends object, S extends object>(
    collection: CollectionState<T, S>,
    id: string,
  ): void {
    const { model } = this.target(collection);
    model.writable();
    if (model.source.kind !== "collection")
      throw new Error("Remove requires a collection");
    this.append(model, {
      type: "remove",
      schema: model.source.name,
      row_id: text(id),
    });
  }
  emit<T>(
    signal: Signal<T>,
    ...args: T extends void ? [data?: T] : [data: T]
  ): void {
    if (!this.active)
      throw new Error("Transaction builder has already finished");
    const info = signalHandles.get(signal);
    if (!info || info.model.manager !== this.manager)
      throw new Error("Signal belongs to another StateManager");
    const { model, rowId, name } = info;
    model.writable(rowId);
    if (model.source.kind === "query") return;
    if (model.source.kind === "collection" && rowId === null)
      throw new Error("Emit collection signals on a row");
    const data = jsonCopy(args[0] ?? null) as Json;
    this.append(
      model,
      rowId === null
        ? { type: "signal", schema: model.source.name, signal: name, data }
        : {
            type: "signalrow",
            schema: model.source.name,
            row_id: rowId,
            signal: name,
            data,
          },
    );
  }
}

export class StateManager {
  private readonly models = new Map<string, Model>();
  private readonly permanent = new Map<Model, Disposer>();
  private closed = false;
  constructor(readonly client: StoreClient) {
    client.claim(this);
  }

  create<T extends object, S extends object>(
    source: SingleRef<T, S>,
  ): State<T, S>;
  create<T extends object, S extends object>(
    source: CollectionRef<T, S>,
  ): CollectionState<T, S>;
  create<T extends object>(source: QueryRef<T>): QueryState<T>;
  create(source: SourceRef): unknown {
    const model = this.model(source);
    if (!this.permanent.has(model)) this.permanent.set(model, model.retain());
    return model.view;
  }
  release(source: SourceRef): void {
    const key = canonicalKey(
      source.kind === "query"
        ? [source.kind, source.payload]
        : [source.kind, source.name],
    );
    const model = this.models.get(key);
    if (!model) return;
    this.permanent.get(model)?.();
    this.permanent.delete(model);
  }
  /** @internal React prepares a stable object in render, retains it in an effect. */
  prepare(source: SourceRef): { view: unknown; retain(): Disposer } {
    const model = this.model(source);
    return model.reference;
  }
  private model(source: SourceRef): Model {
    if (this.closed) throw new Error("StateManager is closed");
    const key = canonicalKey(
      source.kind === "query"
        ? [source.kind, source.payload]
        : [source.kind, source.name],
    );
    let model = this.models.get(key);
    if (!model) {
      model = new Model(this, source, key);
      this.models.set(key, model);
    }
    return model;
  }
  transaction(
    build: (transaction: TransactionBuilder) => void,
    options: TransactionOptions = {},
  ): Transaction {
    if (this.closed) throw new Error("StateManager is closed");
    const builder = new TransactionBuilder(this);
    try {
      const result: unknown = build(builder);
      if (
        result &&
        typeof (result as PromiseLike<unknown>).then === "function"
      )
        throw new Error("Transaction builders must be synchronous");
    } finally {
      builder.finish();
    }
    const transaction = this.client.transaction(builder.sequence, options);
    runInAction(() => {
      for (const [model, actions] of builder.actions)
        model.track(transaction, actions, options.optimistic ?? false);
    });
    return transaction;
  }
  close(): void {
    if (this.closed) return;
    this.closed = true;
    for (const model of this.models.values()) model.close();
    this.permanent.clear();
    this.client.release(this);
  }
}
