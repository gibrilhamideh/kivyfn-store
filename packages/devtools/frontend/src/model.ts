import type {
  Activity,
  Checkpoint,
  Commit,
  Filters,
  HistoryEntry,
  Json,
  Values,
} from "./types";

export const format = (value: unknown): string =>
  JSON.stringify(value, null, 2) ?? "Unavailable";

/** Preserve integer tokens before JSON.parse can round them. */
export function parseEditor(text: string): Json {
  let output = "";
  let cursor = 0;
  while (cursor < text.length) {
    const char = text[cursor];
    if (char === '"') {
      const start = cursor++;
      while (cursor < text.length) {
        if (text[cursor++] === "\\") cursor++;
        else if (text[cursor - 1] === '"') break;
      }
      output += text.slice(start, cursor);
      continue;
    }
    if (char === "-" || /[0-9]/.test(char)) {
      const token = text
        .slice(cursor)
        .match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/);
      if (token) {
        const value = token[0];
        const number = Number(value);
        if (!Number.isFinite(number)) {
          throw new Error("Non-finite numbers cannot be submitted.");
        }
        if (/^-?\d+$/.test(value) && !Number.isSafeInteger(number)) {
          output += JSON.stringify({ $integer: value });
        } else if (!Number.isSafeInteger(number) && Number.isInteger(number)) {
          throw new Error(
            "Write large integers as complete digits, without exponents.",
          );
        } else output += value;
        cursor += value.length;
        continue;
      }
    }
    output += char;
    cursor++;
  }
  return JSON.parse(output) as Json;
}

export function object(value: Json | undefined): Values | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value
    : null;
}

export function actionsFromText(text: string): Values[] {
  const value = parseEditor(text);
  if (!Array.isArray(value) || value.length === 0) {
    throw new Error("A mutation needs a nonempty JSON array of actions.");
  }
  if (value.some((item) => object(item) === null)) {
    throw new Error("Each action must be a JSON object.");
  }
  return value as Values[];
}

export function sourceActions(record: Activity): Values[] | null {
  if (record.truncated) return null;
  const request = object(record.detail.request);
  const requests = request?.requests;
  if (!Array.isArray(requests)) return null;
  const transactions = requests
    .map(object)
    .filter((item) => item?.type === "transaction");
  if (transactions.length !== 1) return null;
  const actions = transactions[0]?.actions;
  return Array.isArray(actions) && actions.every((item) => object(item))
    ? (structuredClone(actions) as Values[])
    : null;
}

export function sourceQuery(record: Activity): Values | null {
  if (record.truncated) return null;
  const request = object(record.detail.request);
  const requests = request?.requests;
  if (!Array.isArray(requests) || requests.length !== 1) return null;
  const item = object(requests[0]);
  return item?.type === "query" ? object(item.query) : null;
}

export function historySource(
  entry: HistoryEntry,
  activity: Activity[],
): Activity | null {
  if (entry.transaction_ids.length !== 1) return null;
  return (
    activity.find((record) => {
      const request = object(record.detail.request);
      const items = request?.requests;
      if (!Array.isArray(items) || items.length !== 1) return false;
      const item = object(items[0]);
      return (
        item?.transaction_id === entry.transaction_ids[0] &&
        sourceActions(record) !== null &&
        Boolean(record.capture_id) &&
        activity.some((response) => {
          if (response.truncated || response.capture_id !== record.capture_id)
            return false;
          const items = object(response.detail.response)?.responses;
          return (
            Array.isArray(items) &&
            items.some((value) => {
              const completion = object(value);
              return (
                completion?.type === "transaction" &&
                completion.transaction_id === entry.transaction_ids[0]
              );
            })
          );
        })
      );
    }) ?? null
  );
}

export function matches(record: Activity, filters: Filters): boolean {
  return (
    (!filters.client_id ||
      (record.client_id ?? "").includes(filters.client_id)) &&
    (!filters.schema || record.schemas.includes(filters.schema)) &&
    (!filters.category || record.category === filters.category) &&
    (!filters.status || record.status === filters.status) &&
    (!filters.identity ||
      [record.id, record.capture_id, record.transaction_id].some((value) =>
        value?.includes(filters.identity),
      ))
  );
}

/** Apply complete canonical deltas only to the checkpoint's captured scope. */
export function applyCommit(
  checkpoint: Checkpoint,
  commit: Commit,
): Checkpoint {
  const next = structuredClone(checkpoint);
  for (const [schema, revision] of Object.entries(commit.schema_revisions)) {
    if (!Object.hasOwn(next.schemas, schema)) continue;
    const previous = next.schema_revisions[schema];
    if (revision !== previous + 1) {
      throw new Error(`Revision gap in ${schema}; refreshing checkpoint.`);
    }
    next.schema_revisions[schema] = revision;
  }
  for (const event of commit.events) {
    if (!Object.hasOwn(next.schemas, event.schema)) continue;
    const schema = next.schemas[event.schema];
    switch (event.type) {
      case "set":
        if (event.key === undefined || event.new === undefined) {
          throw new Error("Incomplete set event.");
        }
        schema[event.key] = event.new;
        break;
      case "setrow": {
        const row = event.id === undefined ? null : object(schema[event.id]);
        if (!row || event.key === undefined || event.new === undefined) {
          throw new Error("Incomplete row event.");
        }
        row[event.key] = event.new;
        break;
      }
      case "insert":
        if (event.id === undefined || !object(event.new)) {
          throw new Error("Incomplete insert event.");
        }
        Object.defineProperty(schema, event.id, {
          value: event.new,
          enumerable: true,
          writable: true,
          configurable: true,
        });
        break;
      case "remove":
        if (event.id === undefined) {
          throw new Error("Incomplete remove event.");
        }
        delete schema[event.id];
        break;
      default:
        throw new Error("Unsupported diagnostic event.");
    }
  }
  return next;
}
