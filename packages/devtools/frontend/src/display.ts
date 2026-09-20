import { format, object } from "./model";
import type { Activity, Json, Values } from "./types";

export interface Change {
  schema: string;
  row: string | null;
  field: string;
  before: Json | undefined;
  after: Json | undefined;
  source?: string;
}

export function equal(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (!left || !right || typeof left !== "object" || typeof right !== "object")
    return false;
  if (Array.isArray(left) !== Array.isArray(right)) return false;
  const a = Object.entries(left);
  const b = Object.entries(right);
  return (
    a.length === b.length &&
    a.every(
      ([key, value]) =>
        Object.hasOwn(right, key) &&
        equal(value, (right as Record<string, unknown>)[key]),
    )
  );
}

export function summary(value: unknown): string {
  if (value === undefined) return "Absent";
  if (value === null) return "null";
  if (typeof value === "string") return value === "" ? '""' : value;
  if (typeof value === "object" && !Array.isArray(value)) {
    const record = value as Values;
    if (
      Object.keys(record).length === 1 &&
      typeof record.$integer === "string"
    )
      return record.$integer;
  }
  return format(value);
}

export function unsupported(value: Json): boolean {
  if (!value || typeof value !== "object") return false;
  if (Array.isArray(value)) return value.some(unsupported);
  if (
    Object.keys(value).length === 1 &&
    typeof value.$unsupported === "string"
  )
    return true;
  return Object.values(value).some(unsupported);
}

/** Interpret diagnostic commits or detached history records, never live data. */
export function changesFrom(records: unknown): Change[] {
  if (!Array.isArray(records)) return [];
  return records.flatMap((value: Json) => {
    const item = object(value);
    if (!item || typeof item.schema !== "string") return [];
    const history = Object.hasOwn(item, "before_exists");
    const row = history ? item.row_id : item.id;
    const before = history
      ? item.before_exists
        ? item.before
        : undefined
      : item.type === "insert"
        ? undefined
        : item.old;
    const after = history
      ? item.after_exists
        ? item.after
        : undefined
      : item.type === "remove"
        ? undefined
        : item.new;
    return [
      {
        schema: item.schema,
        row: typeof row === "string" ? row : null,
        field:
          typeof item.key === "string"
            ? item.key
            : before === undefined
              ? "Insert row"
              : after === undefined
                ? "Remove row"
                : "Row",
        before,
        after,
        source: typeof item.source === "string" ? item.source : undefined,
      },
    ];
  });
}

export function commitDetail(record: Activity): Values | null {
  return record.category === "commit"
    ? (object(record.detail.commit) ?? record.detail)
    : null;
}

export function recordMatchesTarget(
  record: Activity,
  row: string,
  field: string,
  schema = "",
): boolean {
  if (!row && !field) return true;
  const changes = changesFrom(commitDetail(record)?.events);
  const request = object(record.detail.request);
  const actions = Array.isArray(request?.requests)
    ? request.requests.flatMap((item) => {
        const actions = object(item)?.actions;
        return Array.isArray(actions) ? actions : [];
      })
    : [];
  return (
    changes.some(
      (change) =>
        (!schema || change.schema === schema) &&
        (!row || change.row === row) &&
        (!field ||
          change.field === field ||
          Object.hasOwn(object(change.before) ?? {}, field) ||
          Object.hasOwn(object(change.after) ?? {}, field)),
    ) ||
    actions.some((item) => {
      const action = object(item);
      return (
        action &&
        (!schema || action.schema === schema) &&
        (!row || action.row_id === row) &&
        (!field || Object.hasOwn(object(action.values) ?? {}, field))
      );
    })
  );
}

export function queryRows(response: unknown): Values | null {
  const value = response as { responses?: Values[] } | null;
  const result = value?.responses?.find((item) => item.type === "query");
  if (!Array.isArray(result?.rows)) return null;
  return Object.fromEntries(
    result.rows.flatMap((value) => {
      const row = object(value);
      return row && typeof row.id === "string" && object(row.values)
        ? [[row.id, row.values]]
        : [];
    }),
  );
}

/** Preserve host query order even when row IDs look like integer object keys. */
export function queryOrder(response: unknown): string[] {
  const value = response as { responses?: Values[] } | null;
  const result = value?.responses?.find((item) => item.type === "query");
  if (!Array.isArray(result?.rows)) return [];
  return result.rows.flatMap((value) => {
    const row = object(value);
    return row && typeof row.id === "string" ? [row.id] : [];
  });
}
