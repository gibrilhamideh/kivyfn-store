import { ProtocolError } from "./errors.js";
import { array, integer, jsonCopy, object, text } from "./internal.js";
import type { Json, JsonObject } from "./internal.js";

export const PROTOCOL_VERSION = 3;
export const RELAY_VERSION = 1;
export type Revisions = Record<string, number>;

export interface StoreFailure {
    code: string;
    category: string;
    status: string;
    message: string;
    detail: JsonObject | null;
    context: JsonObject | null;
}

export interface HistoryPosition {
    store_instance_id: string;
    revision: number;
    entry_id: string | null;
}

export interface HistoryPage {
    status: {
        store_instance_id: string;
        revision: number;
        can_undo: boolean;
        can_redo: boolean;
        undo_position: HistoryPosition;
        redo_position: HistoryPosition;
        barrier: string | null;
        retained_bytes: number;
        total_entries: number;
        cursor: number;
        enabled: boolean;
    };
    entries: readonly JsonObject[];
}

export interface QueryPayload {
    version: 1;
    source: { schema: string } | { address: readonly string[] };
    joins: readonly {
        path: readonly string[];
        kind: "inner" | "left";
        alias: string;
    }[];
    predicate: Expression | null;
}

export type Literal = null | string | number | boolean | readonly Literal[];
export type Expression =
    | {
        type: "comparison";
        field: readonly string[];
        operator: "eq" | "ne" | "lt" | "lte" | "gt" | "gte";
        value: Literal;
    }
    | {
        type: "membership";
        field: readonly string[];
        operator: "in" | "not_in";
        values: readonly Literal[];
    }
    | { type: "and" | "or"; expressions: readonly Expression[] }
    | { type: "not"; expression: Expression };

export type RequestAction =
    | { type: "set"; schema: string; values: JsonObject }
    | {
        type: "setrow" | "insert";
        schema: string;
        row_id: string;
        values: JsonObject;
    }
    | { type: "remove"; schema: string; row_id: string }
    | { type: "signal"; schema: string; signal: string; data: Json }
    | {
        type: "signalrow";
        schema: string;
        row_id: string;
        signal: string;
        data: Json;
    };

export interface TransactionRequest {
    type: "transaction";
    transaction_id: string;
    actions: readonly RequestAction[];
    history_group?: string;
    history_label?: string;
    expected_store_instance_id?: string;
    expected_revisions?: Revisions;
}
export type SubscriptionRequest =
    | {
        type: "subscribe_single" | "subscribe_collection";
        schema: string;
        store_instance_id?: string;
        revision?: number;
    }
    | {
        type: "subscribe_query";
        query: QueryPayload;
        store_instance_id?: string;
        revisions?: Revisions;
    };
export type RequestItem =
    | TransactionRequest
    | SubscriptionRequest
    | { type: "query"; query: QueryPayload }
    | { type: "unsubscribe"; subscription_id: string }
    | { type: "history"; limit: number; offset: number }
    | {
        type: "undo" | "redo";
        transaction_id: string;
        expected_position: HistoryPosition;
    };
export interface RequestBatch {
    requests: readonly RequestItem[];
}
export interface WireRow {
    id: string;
    values: JsonObject;
}
export type SubscriptionResponse =
    | {
        type: "subscribe_single";
        subscription_id: string;
        store_instance_id: string;
        revision: number;
        values: JsonObject | null;
    }
    | {
        type: "subscribe_collection";
        subscription_id: string;
        store_instance_id: string;
        revision: number;
        rows: readonly WireRow[] | null;
    }
    | {
        type: "subscribe_query";
        subscription_id: string;
        store_instance_id: string;
        revisions: Revisions;
        rows: readonly WireRow[] | null;
    };
export type ResponseItem =
    | SubscriptionResponse
    | { type: "transaction"; transaction_id: string }
    | {
        type: "transaction_failure";
        transaction_id: string;
        failure: StoreFailure;
    }
    | { type: "failure"; failure: StoreFailure }
    | { type: "query"; rows: readonly WireRow[] }
    | { type: "unsubscribe"; subscription_id: string }
    | { type: "history"; page: HistoryPage };
export interface ResponseBatch {
    responses: readonly ResponseItem[];
}
export type EventAction =
    | { type: "set"; values: JsonObject }
    | { type: "setrow" | "insert"; row_id: string; values: JsonObject }
    | { type: "remove"; row_id: string }
    | {
        type: "signal";
        signal: string;
        data: Json;
        occurrence_id: number | null;
    }
    | {
        type: "signalrow";
        row_id: string;
        signal: string;
        data: Json;
        occurrence_id: number | null;
    };
export interface SubscriptionEvent {
    subscription_id: string;
    revisions: Revisions;
    actions: readonly EventAction[];
}
export interface EventMessage {
    event: {
        store_instance_id: string;
        transaction_id: string | null;
        events: readonly SubscriptionEvent[];
    };
}

export function revisions(value: unknown): Revisions {
    const result: Revisions = Object.create(null);
    for (const [key, revision] of Object.entries(object(value))) {
        result[text(key)] = integer(revision);
    }
    return result;
}

function values(value: unknown): JsonObject {
    const data = object(value);
    for (const name of Object.keys(data)) text(name);
    return jsonCopy(data) as JsonObject;
}

function rows(value: unknown): readonly WireRow[] {
    const seen = new Set<string>();
    return array(value).map((item) => {
        const row = object(item);
        const id = text(row.id);
        if (seen.has(id)) throw new ProtocolError("Duplicate row ID");
        seen.add(id);
        return { id, values: values(row.values) };
    });
}

function failure(value: unknown): StoreFailure {
    const data = object(value);
    for (const key of ["code", "category", "status", "message"]) text(data[key]);
    for (const key of ["detail", "context"]) {
        if (data[key] !== null) object(data[key]);
    }
    return data as unknown as StoreFailure;
}

function position(value: unknown): HistoryPosition {
    const data = object(value);
    text(data.store_instance_id);
    integer(data.revision);
    if (data.entry_id !== null) text(data.entry_id);
    return data as unknown as HistoryPosition;
}

function history(value: unknown): HistoryPage {
    const data = object(value);
    const status = object(data.status);
    text(status.store_instance_id);
    for (const key of ["revision", "retained_bytes", "total_entries", "cursor"])
        integer(status[key]);
    for (const key of ["can_undo", "can_redo", "enabled"]) {
        if (typeof status[key] !== "boolean")
            throw new ProtocolError("Invalid history status");
    }
    position(status.undo_position);
    position(status.redo_position);
    if (status.barrier !== null) text(status.barrier);
    for (const entry of array(data.entries)) {
        const item = object(entry);
        text(item.id);
        array(item.transaction_ids).forEach(text);
        array(item.records).forEach(object);
        array(item.signals).forEach(object);
        integer(item.byte_size);
    }
    return data as unknown as HistoryPage;
}

function response(value: unknown): ResponseItem {
    const data = object(value);
    const type = text(data.type);
    switch (type) {
        case "transaction":
            text(data.transaction_id);
            break;
        case "transaction_failure":
            text(data.transaction_id);
            failure(data.failure);
            break;
        case "failure":
            failure(data.failure);
            break;
        case "query":
            rows(data.rows);
            break;
        case "history":
            history(data.page);
            break;
        case "unsubscribe":
            text(data.subscription_id);
            break;
        case "subscribe_single":
        case "subscribe_collection":
        case "subscribe_query":
            text(data.subscription_id);
            text(data.store_instance_id);
            if (type === "subscribe_query") {
                if (!Object.keys(revisions(data.revisions)).length)
                    throw new ProtocolError("Empty query dependencies");
            } else integer(data.revision);
            if (type === "subscribe_single") {
                if (data.values !== null) values(data.values);
            } else if (data.rows !== null) rows(data.rows);
            break;
        default:
            throw new ProtocolError(`Unknown response type: ${type}`);
    }
    return data as unknown as ResponseItem;
}

export function decodeResponse(
    value: unknown,
    batch: RequestBatch,
): ResponseBatch {
    const data = object(jsonCopy(value));
    const responses = array(data.responses).map(response);
    if (responses.length !== batch.requests.length || !responses.length) {
        throw new ProtocolError("Response batch does not match requests");
    }
    responses.forEach((reply, index) => {
        const request = batch.requests[index]!;
        if ("transaction_id" in request) {
            if (
                !(
                    reply.type === "transaction" || reply.type === "transaction_failure"
                ) ||
                reply.transaction_id !== request.transaction_id
            ) {
                throw new ProtocolError("Transaction response mismatch");
            }
        } else if (reply.type !== "failure") {
            if (reply.type !== request.type)
                throw new ProtocolError("Response type mismatch");
            if (
                request.type === "unsubscribe" &&
                reply.type === "unsubscribe" &&
                request.subscription_id !== reply.subscription_id
            ) {
                throw new ProtocolError("Unsubscribe response mismatch");
            }
        }
    });
    return { responses };
}

export function decodeEvent(value: unknown): EventMessage {
    const envelope = object(jsonCopy(value));
    const data = object(envelope.event);
    text(data.store_instance_id);
    if (data.transaction_id !== null) text(data.transaction_id);
    const events = array(data.events);
    if (!events.length) throw new ProtocolError("Empty event envelope");
    const seen = new Set<string>();
    for (const value of events) {
        const event = object(value);
        const id = text(event.subscription_id);
        if (seen.has(id))
            throw new ProtocolError("Duplicate subscription in event");
        seen.add(id);
        revisions(event.revisions);
        for (const value of array(event.actions)) {
            const action = object(value);
            const type = text(action.type);
            if (["setrow", "insert", "remove", "signalrow"].includes(type))
                text(action.row_id);
            if (["set", "setrow", "insert"].includes(type)) {
                const fields = values(action.values);
                if (type !== "insert" && !Object.keys(fields).length)
                    throw new ProtocolError("Empty event values");
            } else if (type === "signal" || type === "signalrow") {
                text(action.signal);
                if (!("data" in action))
                    throw new ProtocolError("Signal event is missing data");
                if (
                    action.occurrence_id !== null &&
                    action.occurrence_id !== undefined
                )
                    integer(action.occurrence_id);
            } else if (type !== "remove")
                throw new ProtocolError("Unknown event action");
        }
    }
    return envelope as unknown as EventMessage;
}

export function isSubscription(item: RequestItem): item is SubscriptionRequest;
export function isSubscription(
    item: ResponseItem,
): item is SubscriptionResponse;
export function isSubscription(item: RequestItem | ResponseItem): boolean {
    return (
        item.type === "subscribe_single" ||
        item.type === "subscribe_collection" ||
        item.type === "subscribe_query"
    );
}
