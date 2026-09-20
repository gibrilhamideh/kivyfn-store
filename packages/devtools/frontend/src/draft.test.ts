import { describe, expect, it } from "vitest";
import {
  formActions,
  formChanges,
  formErrors,
  makeForm,
  newRow,
  parseField,
  rebaseForm,
} from "./draft";
import { changesFrom, equal, queryOrder, queryRows } from "./display";
import type { Checkpoint, SchemaInfo } from "./types";

const schema: SchemaInfo = {
  name: "Rows",
  kind: "many",
  persistent: false,
  track_history: true,
  fields: {
    title: { type: "StringField", default: "" },
    count: { type: "NumericField", default: 0 },
  },
  signals: {},
};
const checkpoint = (rows: Checkpoint["schemas"][string]): Checkpoint => ({
  schemas: { Rows: rows },
  schema_revisions: { Rows: 2 },
});

describe("structured draft intent", () => {
  it("compiles only explicit changes, in draft order, without changing snapshots", () => {
    const data = { a: { title: "A", count: 1 }, b: { title: "B", count: 2 } };
    const form = makeForm(schema, data);
    expect(formActions(form)).toEqual([]);
    form.rows[0].values.count = 3;
    form.rows[1].removed = true;
    const added = newRow(schema, "c");
    form.rows.push(added);
    expect(formActions(form)).toEqual([
      { type: "setrow", schema: "Rows", row_id: "a", values: { count: 3 } },
      { type: "remove", schema: "Rows", row_id: "b" },
      {
        type: "insert",
        schema: "Rows",
        row_id: "c",
        values: { title: "", count: 0 },
      },
    ]);
    added.removed = true;
    form.rows[0].values.count = 1;
    form.rows[1].removed = false;
    expect(formActions(form)).toEqual([]);
    expect(data.a.count).toBe(1);
  });

  it("rebases untouched fields and input buffers while retaining explicit edits", () => {
    const form = makeForm(schema, { a: { title: "A", count: 1 } });
    form.rows[0].values.count = 3;
    form.rows[0].inputs = { title: "A", count: "3" };
    const rebased = rebaseForm(
      form,
      checkpoint({ a: { title: "Remote", count: 2 } }),
    );
    expect(rebased.rows[0].values).toEqual({ title: "Remote", count: 3 });
    expect(rebased.rows[0].inputs).toEqual({ count: "3" });
    expect(formChanges(rebased)).toEqual([
      { schema: "Rows", row: "a", field: "count", before: 2, after: 3 },
    ]);
  });

  it("does not resurrect deleted rows or overwrite colliding insert IDs", () => {
    const form = makeForm(schema, { a: { count: 1 } });
    form.rows[0].values.count = 2;
    expect(() => rebaseForm(form, checkpoint({}))).toThrow("Row was deleted");
    form.rows[0].removed = true;
    expect(formActions(rebaseForm(form, checkpoint({})))).toEqual([]);
    form.rows = [newRow(schema, "a")];
    expect(() => rebaseForm(form, checkpoint({ a: { count: 9 } }))).toThrow(
      "Row already exists",
    );
  });

  it("reports invalid input and duplicate IDs without silently dropping edits", () => {
    const form = makeForm(schema, { a: { count: 1 } });
    form.rows[0].inputs.count = "bad";
    form.rows[0].errors.count = "Enter a number.";
    form.rows.push(newRow(schema, "a"));
    expect(formErrors(form)).toEqual([
      "Enter a number.",
      "Duplicate row ID: a",
    ]);
    expect(form.rows[0].values.count).toBe(1);
  });

  it("keeps numeric tags exact and distinguishes null, empty text and false", () => {
    expect(parseField("9007199254740993", { type: "NumericField" })).toEqual({
      $integer: "9007199254740993",
    });
    expect(parseField("", { type: "StringField" })).toBe("");
    expect(
      parseField("null", { type: "NumericField", allow_none: true }),
    ).toBeNull();
    expect(parseField("false", { type: "BooleanField" })).toBe(false);
    expect(() => parseField('"1"', { type: "NumericField" })).toThrow(
      "number",
    );
    expect(() => parseField('{"$unsupported":"Widget"}', {})).toThrow(
      "Unsupported",
    );
  });

  it("handles reserved map keys as data and ignores object key ordering", () => {
    const values = JSON.parse('{"__proto__":{"title":"old","count":1}}');
    const form = makeForm(schema, values);
    form.rows[0].values.count = 2;
    expect(formActions(rebaseForm(form, checkpoint(values)))[0].row_id).toBe(
      "__proto__",
    );
    expect(equal({ a: null, b: [false, 0] }, { b: [false, 0], a: null })).toBe(
      true,
    );
    expect(equal([], {})).toBe(false);
  });
});

describe("diagnostic presentation", () => {
  it("preserves query order independently of JavaScript integer key ordering", () => {
    const response = {
      responses: [
        {
          type: "query",
          rows: [
            { id: "10", values: { n: 10 } },
            { id: "2", values: { n: 2 } },
            { id: "__proto__", values: { n: 0 } },
          ],
        },
      ],
    };
    expect(queryOrder(response)).toEqual(["10", "2", "__proto__"]);
    expect(Object.hasOwn(queryRows(response)!, "__proto__")).toBe(true);
  });

  it("distinguishes removal from a field being set to null", () => {
    const changes = changesFrom([
      {
        type: "setrow",
        schema: "Rows",
        id: "a",
        key: "value",
        old: 1,
        new: null,
      },
      {
        schema: "Rows",
        row_id: "b",
        key: null,
        before_exists: true,
        after_exists: false,
        before: { count: 2 },
        after: null,
      },
    ]);
    expect(changes[0].after).toBeNull();
    expect(changes[1].after).toBeUndefined();
    expect(changes[1].field).toBe("Remove row");
  });
});
