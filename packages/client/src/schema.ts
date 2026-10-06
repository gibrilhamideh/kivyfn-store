import { jsonCopy, text } from "./internal.js";
import type { Expression, Literal, QueryPayload } from "./protocol.js";

// TODO: Discover portable schema metadata from the authoritative store.
// These references carry identity and TypeScript hints, never validation rules.
declare const shape: unique symbol;
export interface SchemaRef<T extends object, S extends object = {}> {
    readonly kind: "single" | "collection";
    readonly name: string;
    readonly [shape]?: { values: T; signals: S };
}
export interface SingleRef<
    T extends object,
    S extends object = {},
> extends SchemaRef<T, S> {
    readonly kind: "single";
}
export interface CollectionRef<
    T extends object,
    S extends object = {},
> extends SchemaRef<T, S> {
    readonly kind: "collection";
}
export interface QueryRef<T extends object> {
    readonly kind: "query";
    readonly payload: QueryPayload;
    readonly [shape]?: { values: T };
    where(...predicates: Expression[]): QueryRef<T>;
    join<R extends object = T>(
        path: string | readonly string[],
        alias: string,
        kind?: "inner" | "left",
    ): QueryRef<R>;
}
export type SourceRef<T extends object = object, S extends object = {}> =
    SchemaRef<T, S> | QueryRef<T>;

export function single<T extends object, S extends object = {}>(
    name: string,
): SingleRef<T, S> {
    return Object.freeze({ kind: "single", name: text(name) });
}
export function collection<T extends object, S extends object = {}>(
    name: string,
): CollectionRef<T, S> {
    return Object.freeze({ kind: "collection", name: text(name) });
}
function path(value: string | readonly string[]): readonly string[] {
    const result = typeof value === "string" ? value.split(".") : [...value];
    if (!result.length) throw new Error("A field or join path cannot be empty");
    result.forEach(text);
    return result;
}
function queryRef<T extends object>(payload: QueryPayload): QueryRef<T> {
    return Object.freeze({
        kind: "query" as const,
        payload: jsonCopy(payload),
        where(...predicates: Expression[]) {
            if (!predicates.length) throw new Error("where() needs an expression");
            return queryRef<T>({
                ...payload,
                predicate: and(
                    ...(payload.predicate ? [payload.predicate] : []),
                    ...predicates,
                ),
            });
        },
        join<R extends object = T>(
            value: string | readonly string[],
            alias: string,
            kind: "inner" | "left" = "inner",
        ) {
            text(alias);
            if (alias.includes("."))
                throw new Error("Join aliases cannot contain dots");
            return queryRef<R>({
                ...payload,
                joins: [...payload.joins, { path: path(value), alias, kind }],
            });
        },
    });
}
export function query<T extends object>(
    source: CollectionRef<T, object> | QueryPayload,
): QueryRef<T> {
    return queryRef(
        "kind" in source
            ? {
                version: 1,
                source: { schema: source.name },
                joins: [],
                predicate: null,
            }
            : source,
    );
}
export function field(value: string | readonly string[]) {
    const parts = path(value);
    const compare = (
        operator: "eq" | "ne" | "lt" | "lte" | "gt" | "gte",
        value: Literal,
    ): Expression =>
        jsonCopy({ type: "comparison", field: parts, operator, value });
    const membership = (
        operator: "in" | "not_in",
        values: readonly Literal[],
    ): Expression => {
        if (!values.length) throw new Error("Membership needs at least one value");
        return jsonCopy({ type: "membership", field: parts, operator, values });
    };
    return {
        eq: (value: Literal) => compare("eq", value),
        ne: (value: Literal) => compare("ne", value),
        lt: (value: Literal) => compare("lt", value),
        lte: (value: Literal) => compare("lte", value),
        gt: (value: Literal) => compare("gt", value),
        gte: (value: Literal) => compare("gte", value),
        in: (values: readonly Literal[]) => membership("in", values),
        notIn: (values: readonly Literal[]) => membership("not_in", values),
    };
}
export function and(...expressions: Expression[]): Expression {
    if (!expressions.length) throw new Error("AND needs an expression");
    if (expressions.length === 1) return expressions[0]!;
    return jsonCopy({ type: "and", expressions });
}
export function or(...expressions: Expression[]): Expression {
    if (!expressions.length) throw new Error("OR needs an expression");
    if (expressions.length === 1) return expressions[0]!;
    return jsonCopy({ type: "or", expressions });
}
export function not(expression: Expression): Expression {
    return jsonCopy({ type: "not", expression });
}
