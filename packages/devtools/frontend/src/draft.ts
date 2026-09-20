import { equal, unsupported } from "./display";
import type { Change } from "./display";
import { format, object, parseEditor } from "./model";
import type { Checkpoint, Json, SchemaInfo, Values } from "./types";

export interface EditRow {
  key: string;
  id: string | null;
  before: Values | null;
  values: Values;
  inputs: Record<string, string>;
  errors: Record<string, string>;
  removed: boolean;
}

export interface EditForm {
  schema: SchemaInfo;
  rows: EditRow[];
}

export function isMany(schema: SchemaInfo): boolean {
  return schema.kind === "many" || schema.kind === "collection";
}

export function makeForm(schema: SchemaInfo, data: Values): EditForm {
  const records: [string | null, Values][] = isMany(schema)
    ? Object.entries(data).map(([id, value]) => [id, object(value) ?? {}])
    : [[null, data]];
  return {
    schema,
    rows: records.map(([id, values]) => ({
      key: id ?? "singleton",
      id,
      before: structuredClone(values),
      values: structuredClone(values),
      inputs: {},
      errors: {},
      removed: false,
    })),
  };
}

export function newRow(schema: SchemaInfo, id: string): EditRow {
  const values = Object.fromEntries(
    Object.entries(schema.fields).flatMap(([key, field]) => {
      const metadata = object(field);
      return metadata &&
        Object.hasOwn(metadata, "default") &&
        !unsupported(metadata.default)
        ? [[key, structuredClone(metadata.default)]]
        : [];
    }),
  );
  return {
    key: crypto.randomUUID(),
    id,
    before: null,
    values,
    inputs: {},
    errors: {},
    removed: false,
  };
}

export function changedValues(row: EditRow): Values {
  return Object.fromEntries(
    Object.entries(row.values).filter(
      ([key, value]) =>
        row.before === null ||
        !Object.hasOwn(row.before, key) ||
        !equal(row.before[key], value),
    ),
  );
}

/** Compile the stable draft order; visual sorting never affects execution. */
export function formActions(form: EditForm): Values[] {
  return form.rows.flatMap((row) => {
    const target = {
      schema: form.schema.name,
      ...(row.id !== null ? { row_id: row.id } : {}),
    };
    if (row.removed)
      return row.before === null ? [] : [{ type: "remove", ...target }];
    if (row.before === null)
      return [{ type: "insert", ...target, values: row.values }];
    const values = changedValues(row);
    return Object.keys(values).length
      ? [{ type: isMany(form.schema) ? "setrow" : "set", ...target, values }]
      : [];
  });
}

export function formChanges(form: EditForm): Change[] {
  return form.rows.flatMap<Change>((row) => {
    const base = { schema: form.schema.name, row: row.id };
    if (row.removed)
      return row.before
        ? [
            {
              ...base,
              field: "Remove row",
              before: row.before,
              after: undefined,
            },
          ]
        : [];
    if (!row.before)
      return [
        { ...base, field: "Insert row", before: undefined, after: row.values },
      ];
    return Object.entries(changedValues(row)).map(([field, after]) => ({
      ...base,
      field,
      before:
        row.before && Object.hasOwn(row.before, field)
          ? row.before[field]
          : undefined,
      after,
    }));
  });
}

export function formErrors(form: EditForm): string[] {
  const ids = new Set<string>();
  return form.rows.flatMap((row) => {
    if (row.removed) return [];
    const errors = Object.values(row.errors);
    if (isMany(form.schema)) {
      if (!row.id?.trim()) errors.push("A new row needs a nonempty ID.");
      if (row.id !== null && ids.has(row.id))
        errors.push("Duplicate row ID: " + row.id);
      if (row.id !== null) ids.add(row.id);
    }
    return errors;
  });
}

export function fieldText(
  value: Json | undefined,
  type: Json | undefined,
): string {
  if (value === undefined) return "";
  if (typeof value === "string" && isText(type)) return value;
  const tag = object(value);
  if (tag && Object.keys(tag).length === 1 && typeof tag.$integer === "string")
    return tag.$integer;
  return format(value);
}

function isText(type: Json | undefined): boolean {
  return [
    "StringField",
    "DateField",
    "TimeField",
    "DatetimeField",
    "ForeignKeyField",
  ].includes(String(type));
}

export function parseField(text: string, metadata: Values): Json {
  if (isText(metadata.type)) return text;
  const value = parseEditor(text);
  if (value === null && metadata.allow_none) return value;
  const numeric = ["NumericField", "IntegerField", "FloatField"].includes(
    String(metadata.type),
  );
  const tag = object(value);
  const integer =
    tag &&
    Object.keys(tag).length === 1 &&
    typeof tag.$integer === "string" &&
    /^-?\d+$/.test(tag.$integer);
  if (numeric && typeof value !== "number" && !integer)
    throw new Error("Enter a number.");
  if (
    metadata.type === "IntegerField" &&
    typeof value === "number" &&
    !Number.isInteger(value)
  )
    throw new Error("Enter a whole number.");
  if (unsupported(value))
    throw new Error("Unsupported diagnostic objects cannot be submitted.");
  return value;
}

export function currentRow(
  form: EditForm,
  row: EditRow,
  checkpoint: Checkpoint,
): Values | null {
  const data = Object.hasOwn(checkpoint.schemas, form.schema.name)
    ? checkpoint.schemas[form.schema.name]
    : undefined;
  if (!data) return null;
  if (!isMany(form.schema)) return data;
  return row.id !== null && Object.hasOwn(data, row.id)
    ? object(data[row.id])
    : null;
}

/** Adopting a baseline preserves only explicit intent, not stale untouched fields. */
export function rebaseForm(form: EditForm, checkpoint: Checkpoint): EditForm {
  if (!Object.hasOwn(checkpoint.schemas, form.schema.name))
    throw new Error("Wait for the current schema snapshot.");
  const rows = form.rows
    .map((row) => {
      const current = currentRow(form, row, checkpoint);
      if (row.before === null) {
        if (!row.removed && current)
          throw new Error(
            "Row already exists: " + row.id + ". Choose another ID.",
          );
        return row;
      }
      const changes = changedValues(row);
      if (!current) {
        if (!row.removed && Object.keys(changes).length)
          throw new Error(
            "Row was deleted: " +
              row.id +
              ". Revert its edits or remove it from the draft.",
          );
        return null;
      }
      return {
        ...row,
        before: structuredClone(current),
        values: { ...structuredClone(current), ...changes },
        inputs: Object.fromEntries(
          Object.entries(row.inputs).filter(
            ([key]) =>
              Object.hasOwn(changes, key) || Object.hasOwn(row.errors, key),
          ),
        ),
      };
    })
    .filter((row): row is EditRow => row !== null);
  return { ...form, rows };
}
