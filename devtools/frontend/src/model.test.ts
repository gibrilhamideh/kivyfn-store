import { describe, expect, it } from "vitest";
import {
  applyCommit,
  historySource,
  matches,
  parseEditor,
  sourceActions,
} from "./model";
import type { Activity, Checkpoint, Commit, HistoryEntry } from "./types";

const record: Activity = {
  id: "capture",
  sequence: 1,
  time: 1,
  category: "request",
  status: "received",
  client_id: "application",
  transaction_id: "original",
  capture_id: "original-capture",
  schemas: ["Model"],
  detail: {
    request: {
      requests: [
        {
          type: "transaction",
          transaction_id: "original",
          actions: [{ type: "set", schema: "Model", values: { x: 5 } }],
        },
      ],
    },
  },
};

describe("lossless editing", () => {
  it("preserves large positive and negative integers before parsing", () => {
    expect(
      parseEditor(
        '{"positive":9007199254740993,"negative":-9007199254740993}',
      ),
    ).toEqual({
      positive: { $integer: "9007199254740993" },
      negative: { $integer: "-9007199254740993" },
    });
  });
  it("leaves safe numbers, decimals, escaped strings and reserved wrappers intact", () => {
    const input = String.raw`{"n":9007199254740991,"d":0.125,"s":"\"9007199254740993","o":{"$object":[["$integer","literal"]]}}`;
    expect(parseEditor(input)).toEqual(JSON.parse(input));
  });
  it("rejects nonfinite and ambiguous unsafe exponent representations", () => {
    expect(() => parseEditor("1e999")).toThrow("Non-finite");
    expect(() => parseEditor("9.007199254740993e15")).toThrow(
      "complete digits",
    );
    expect(() => parseEditor("01")).toThrow();
  });
});

describe("copying recorded caller intent", () => {
  const completion: Activity = {
    ...record,
    id: "completion",
    category: "response",
    status: "ok",
    detail: {
      response: {
        responses: [{ type: "transaction", transaction_id: "original" }],
      },
    },
  };
  const entry: HistoryEntry = {
    id: "entry",
    transaction_ids: ["original"],
    origin_client_id: "application",
    group: null,
    label: null,
    byte_size: 100,
    records: [{ trigger: "expanded" }],
    signals: [],
  };
  it("only copies detached original caller actions", () => {
    const actions = sourceActions(record)!;
    expect(actions).toHaveLength(1);
    expect(actions[0].type).toBe("set");
    actions[0].type = "remove";
    expect(sourceActions(record)![0].type).toBe("set");
    expect(historySource(entry, [record, completion])).toBe(record);
  });
  it("cannot mistake a rejected changed-ID retry for original caller intent", () => {
    const rejected = {
      ...completion,
      detail: {
        response: {
          responses: [
            { type: "transaction_failure", transaction_id: "original" },
          ],
        },
      },
    };
    expect(historySource(entry, [record, rejected])).toBeNull();
    expect(historySource(entry, [record])).toBeNull();
  });
  it("disables copying when only normalized effects or ambiguous groups remain", () => {
    expect(historySource(entry, [])).toBeNull();
    expect(
      historySource({ ...entry, transaction_ids: ["original", "second"] }, [
        record,
      ]),
    ).toBeNull();
    expect(sourceActions({ ...record, truncated: true })).toBeNull();
    expect(
      sourceActions({ ...record, detail: { records: entry.records } }),
    ).toBeNull();
  });
  it("filters identity, schema, client, status and category independently", () => {
    const filters = {
      identity: "orig",
      schema: "Model",
      client_id: "app",
      category: "request",
      status: "received",
    };
    expect(matches(record, filters)).toBe(true);
    expect(matches(record, { ...filters, schema: "Other" })).toBe(false);
  });
});

describe("canonical synchronization", () => {
  const checkpoint: Checkpoint = {
    schemas: {
      Model: { x: 1 },
      Rows: { "10": { title: "a" }, "2": { title: "b" }, "1": { title: "c" } },
    },
    schema_revisions: { Model: 1, Rows: 1 },
  };
  const commit: Commit = {
    transaction_id: "next",
    sequence: 2,
    kind: "original",
    events: [],
    signals: [],
    schema_revisions: {},
  };
  it("keeps historical snapshots detached while live values advance", () => {
    const result = applyCommit(checkpoint, {
      ...commit,
      events: [{ type: "set", schema: "Model", key: "x", new: 5 }],
      schema_revisions: { Model: 2 },
    });
    expect(result.schemas.Model.x).toBe(5);
    expect(checkpoint.schemas.Model.x).toBe(1);
  });
  it("restores integer-like IDs as keyed rows without order metadata", () => {
    const removed = applyCommit(checkpoint, {
      ...commit,
      events: [{ type: "remove", schema: "Rows", id: "2" }],
      schema_revisions: { Rows: 2 },
    });
    expect(Object.hasOwn(removed.schemas.Rows, "2")).toBe(false);
    const restored = applyCommit(removed, {
      ...commit,
      events: [
        { type: "insert", schema: "Rows", id: "2", new: { title: "b" } },
      ],
      schema_revisions: { Rows: 3 },
    });
    expect(restored.schemas.Rows).toEqual(checkpoint.schemas.Rows);
  });
  it("rejects revision gaps instead of showing mixed data", () => {
    expect(() =>
      applyCommit(checkpoint, { ...commit, schema_revisions: { Model: 3 } }),
    ).toThrow("Revision gap");
  });
  it("supports ordinary row IDs colliding with Object prototype names", () => {
    const inserted = applyCommit(checkpoint, {
      ...commit,
      events: ["__proto__", "toString"].map((id) => ({
        type: "insert",
        schema: "Rows",
        id,
        new: { title: id },
      })),
      schema_revisions: { Rows: 2 },
    });
    expect(Object.hasOwn(inserted.schemas.Rows, "__proto__")).toBe(true);
    expect(inserted.schemas.Rows.__proto__).toEqual({ title: "__proto__" });
    expect(Object.hasOwn(inserted.schemas.Rows, "toString")).toBe(true);
    const removed = applyCommit(inserted, {
      ...commit,
      events: [{ type: "remove", schema: "Rows", id: "__proto__" }],
      schema_revisions: { Rows: 3 },
    });
    expect(Object.hasOwn(removed.schemas.Rows, "__proto__")).toBe(false);
    expect(Object.getPrototypeOf(removed.schemas.Rows)).toBe(Object.prototype);
  });
  it("does not populate uncaptured schemas from live deltas", () => {
    const result = applyCommit(checkpoint, {
      ...commit,
      events: [{ type: "set", schema: "Outside", key: "x", new: 2 }],
      schema_revisions: { Outside: 12 },
    });
    expect(result.schemas.Outside).toBeUndefined();
  });
});
