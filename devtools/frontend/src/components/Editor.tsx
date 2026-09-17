import { useState } from "react";
import { changedValues, isMany, newRow, parseField } from "../draft";
import type { EditForm, EditRow } from "../draft";
import { equal } from "../display";
import { object } from "../model";
import type { Json } from "../types";
import Field from "./Field";

export default function Editor({
  form,
  onChange,
}: {
  form: EditForm;
  onChange: (form: EditForm) => void;
}) {
  const [page, setPage] = useState(0);
  const many = isMany(form.schema);
  const fields = Object.keys(form.schema.fields);
  const activePage = Math.min(
    page,
    Math.max(0, Math.ceil(form.rows.length / 25) - 1),
  );
  const update = (row: EditRow, patch: Partial<EditRow>) =>
    onChange({
      ...form,
      rows: form.rows.map((item) =>
        item.key === row.key ? { ...row, ...patch } : item,
      ),
    });
  const edit = (row: EditRow, key: string, text: string) => {
    const inputs = { ...row.inputs, [key]: text };
    const errors = { ...row.errors };
    try {
      const value = parseField(text, object(form.schema.fields[key]) ?? {});
      delete errors[key];
      update(row, { inputs, errors, values: { ...row.values, [key]: value } });
    } catch (error) {
      update(row, {
        inputs,
        errors: {
          ...errors,
          [key]: error instanceof Error ? error.message : String(error),
        },
      });
    }
  };
  const assign = (row: EditRow, key: string, value: Json) => {
    const inputs = { ...row.inputs };
    const errors = { ...row.errors };
    delete inputs[key];
    delete errors[key];
    update(row, { values: { ...row.values, [key]: value }, inputs, errors });
  };
  const cell = (row: EditRow, key: string) => (
    <td
      key={key}
      className={
        row.before && equal(row.before[key], row.values[key])
          ? ""
          : "dirty-cell"
      }
    >
      <Field
        label={(row.id === null ? "" : row.id + " / ") + key}
        value={row.values[key]}
        metadata={object(form.schema.fields[key]) ?? {}}
        text={Object.hasOwn(row.inputs, key) ? row.inputs[key] : undefined}
        error={Object.hasOwn(row.errors, key) ? row.errors[key] : undefined}
        onText={(text) => edit(row, key, text)}
        onValue={(value) => assign(row, key, value)}
      />
      {row.before &&
        (!equal(row.before[key], row.values[key]) ||
          Object.hasOwn(row.errors, key)) && (
          <button
            className="quiet"
            onClick={() => assign(row, key, row.before![key])}
          >
            Revert {key}
          </button>
        )}
    </td>
  );
  return (
    <section aria-label="Structured editor">
      <p className="muted">
        Draft values · changes stay local until Apply. Live updates continue
        separately.
      </p>
      {many && (
        <button
          onClick={() => {
            const rows = [
              ...form.rows,
              newRow(form.schema, crypto.randomUUID()),
            ];
            onChange({ ...form, rows });
            setPage(Math.floor((rows.length - 1) / 25));
          }}
        >
          Add row to draft
        </button>
      )}
      <div className="table-scroll">
        <table className="data-table edit-table">
          <thead>
            <tr>
              {many ? (
                <>
                  <th>Row ID</th>
                  {fields.map((key) => (
                    <th key={key}>{key}</th>
                  ))}
                  <th>Action</th>
                </>
              ) : (
                <>
                  <th>Field</th>
                  <th>Draft value</th>
                </>
              )}
            </tr>
          </thead>
          <tbody>
            {many
              ? form.rows
                  .slice(activePage * 25, activePage * 25 + 25)
                  .map((row) => (
                    <tr key={row.key}>
                      <th scope="row">
                        {row.before ? (
                          row.id
                        ) : (
                          <input
                            aria-label="New row ID"
                            value={row.id ?? ""}
                            onChange={(event) =>
                              update(row, { id: event.target.value })
                            }
                          />
                        )}
                        <small>
                          {row.before
                            ? row.removed
                              ? "Will be removed"
                              : Object.keys(changedValues(row)).length +
                                " changed fields"
                            : "New row"}
                        </small>
                      </th>
                      {row.removed ? (
                        <td colSpan={fields.length}>
                          Row marked for removal.
                        </td>
                      ) : (
                        fields.map((key) => cell(row, key))
                      )}
                      <td>
                        <button
                          onClick={() =>
                            row.before
                              ? update(row, { removed: !row.removed })
                              : onChange({
                                  ...form,
                                  rows: form.rows.filter(
                                    (item) => item.key !== row.key,
                                  ),
                                })
                          }
                        >
                          {row.before
                            ? row.removed
                              ? "Keep row"
                              : "Remove row"
                            : "Discard row"}
                        </button>
                      </td>
                    </tr>
                  ))
              : fields.map((key) => (
                  <tr key={key}>
                    <th scope="row">{key}</th>
                    {cell(form.rows[0], key)}
                  </tr>
                ))}
          </tbody>
        </table>
      </div>
      {many && form.rows.length > 25 && (
        <div className="toolbar pagination">
          <button
            disabled={!activePage}
            onClick={() => setPage(activePage - 1)}
          >
            Previous edit rows
          </button>
          <span>
            Page {activePage + 1} of {Math.ceil(form.rows.length / 25)}
          </span>
          <button
            disabled={(activePage + 1) * 25 >= form.rows.length}
            onClick={() => setPage(activePage + 1)}
          >
            Next edit rows
          </button>
        </div>
      )}
    </section>
  );
}
