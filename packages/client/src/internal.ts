import { ProtocolError } from "./errors.js";

export type Json = null | boolean | number | string | Json[] | JsonObject;
export type JsonObject = { [key: string]: Json };

export function jsonCopy<T>(value: T): T {
  // Data is detached at the boundary and frozen before exposing it to callers.
  const ancestors = new Set<object>();
  function visit(value: unknown): unknown {
    if (
      value === null ||
      typeof value === "string" ||
      typeof value === "boolean"
    ) {
      return value;
    }
    if (typeof value === "number" && Number.isFinite(value)) {
      if (Number.isInteger(value) && !Number.isSafeInteger(value)) {
        throw new ProtocolError("Integer exceeds JavaScript precision");
      }
      return value;
    }
    if (typeof value === "object" && value !== null) {
      if (ancestors.has(value))
        throw new ProtocolError("Store values cannot contain cycles");
      ancestors.add(value);
      if (Array.isArray(value)) {
        const result = Object.freeze(value.map(visit));
        ancestors.delete(value);
        return result;
      }
      const prototype = Object.getPrototypeOf(value);
      if (prototype !== Object.prototype && prototype !== null) {
        throw new ProtocolError("Store values must be plain JSON data");
      }
      const copy: Record<string, unknown> = Object.create(null);
      for (const [key, item] of Object.entries(value)) copy[key] = visit(item);
      ancestors.delete(value);
      return Object.freeze(copy);
    }
    throw new ProtocolError("Store values must be finite JSON data");
  }
  return visit(value) as T;
}

/** Size of compact JSON with non-ASCII UTF-16 units escaped, in bytes. */
export function jsonBytes(value: unknown): number {
  return JSON.stringify(value).replace(
    /[\u007f-\uffff]/g,
    (character) =>
      `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  ).length;
}

export function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new ProtocolError("Expected a Store protocol object");
  }
  return value as Record<string, unknown>;
}

export function text(value: unknown): string {
  if (typeof value !== "string" || !value.length) {
    throw new ProtocolError("Expected a non-empty protocol string");
  }
  return value;
}

export function integer(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new ProtocolError("Expected a nonnegative safe integer");
  }
  return value;
}

export function array(value: unknown): unknown[] {
  if (!Array.isArray(value))
    throw new ProtocolError("Expected a protocol array");
  return value;
}

export function canonicalKey(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalKey).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalKey(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export function same(a: unknown, b: unknown): boolean {
  return a === b || canonicalKey(a) === canonicalKey(b);
}

export interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}

export function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  // Some internal waiters only acquire a consumer after completion.
  void promise.catch(() => {});
  return { promise, resolve, reject };
}

export function positive(value: number, label: string): number {
  if (!Number.isFinite(value) || value <= 0)
    throw new Error(`${label} must be positive`);
  return value;
}

export function limit(value: number, label: string): number {
  positive(value, label);
  if (!Number.isSafeInteger(value))
    throw new Error(`${label} must be an integer`);
  return value;
}

export function safeCall(
  action: () => void,
  report: (error: unknown) => void,
) {
  try {
    action();
  } catch (error) {
    try {
      report(error);
    } catch {
      /* User error handlers cannot break sync. */
    }
  }
}

export function randomId(): string {
  return globalThis.crypto.randomUUID().replaceAll("-", "");
}
