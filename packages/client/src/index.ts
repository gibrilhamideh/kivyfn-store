export { StoreClient } from "./client.js";
export type { StoreClientOptions } from "./client.js";
export { StoreError, ProtocolError, SubscriptionError } from "./errors.js";
export type { FailureKind, Outcome } from "./errors.js";
export type { GatewayAuthContext, GatewayPolicy } from "./gateway.js";
export type { Json, JsonObject } from "./internal.js";
export { PROTOCOL_VERSION, RELAY_VERSION } from "./protocol.js";
export type {
    Expression,
    HistoryPage,
    HistoryPosition,
    Literal,
    QueryPayload,
    RequestAction,
    RequestBatch,
    RequestItem,
    Revisions,
    StoreFailure,
    WireRow,
} from "./protocol.js";
export { single, collection, query, field, and, or, not } from "./schema.js";
export type {
    SingleRef,
    CollectionRef,
    SchemaRef,
    SourceRef,
    QueryRef,
} from "./schema.js";
export { StateManager, TransactionBuilder } from "./state.js";
export type {
    State,
    StateHandle,
    CollectionState,
    QueryState,
    ReadonlyState,
    Signal,
    Signals,
    SignalInfo,
    Bindings,
    CollectionBindings,
    Disposer,
    Snapshot,
} from "./state.js";
export { Transaction } from "./transaction.js";
export type {
    TransactionOptions,
    TransactionResult,
    TransactionStatus,
} from "./transaction.js";
export { RelayTransport, DirectSocketIOTransport } from "./transport.js";
export type {
    AuthProvider,
    ClientTransport,
    ConnectionInfo,
    RelayOptions,
    SocketIOOptions,
    TransportHandlers,
} from "./transport.js";
