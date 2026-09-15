import { useState } from "react";
import { format, object, parseEditor } from "../model";
import { Empty, Inspector } from "../components/Inspector";
import type {
  Checkpoint,
  Json,
  RequestBatch,
  ResponseBatch,
  SchemaInfo,
  Values,
} from "../types";

interface Props {
  schemas: SchemaInfo[];
  checkpoint: Checkpoint;
  selected: string;
  disabled: boolean;
  loading: boolean;
  connected: boolean;
  preview: boolean;
  onSelect: (schema: string) => void;
  onDraft: (title: string, actions: Values[]) => void;
  onQuery: (batch: RequestBatch) => Promise<ResponseBatch | null>;
}

export default function StateView({
  schemas,
  checkpoint,
  selected,
  disabled,
  loading,
  connected,
  preview,
  onSelect,
  onDraft,
  onQuery,
}: Props) {
  const [rowId, setRowId] = useState("");
  const [query, setQuery] = useState("");
  const [queryResult, setQueryResult] = useState<Json | null>(null);
  const [queryError, setQueryError] = useState("");
  const schema = schemas.find((item) => item.name === selected);
  const values =
    (!loading || preview) && Object.hasOwn(checkpoint.schemas, selected)
      ? checkpoint.schemas[selected]
      : undefined;
  const many = schema?.kind === "many" || schema?.kind === "collection";
  const ids = many && values ? Object.keys(values) : [];
  const activeRow = ids.includes(rowId) ? rowId : (ids[0] ?? "");
  const current = many ? object(values?.[activeRow]) : values;
  const queryText =
    query ||
    format({
      version: 1,
      source: {
        schema: many
          ? selected
          : (schemas.find((item) => item.kind === "many")?.name ?? selected),
      },
      joins: [],
      predicate: null,
    });

  const runQuery = async () => {
    try {
      const parsed = object(parseEditor(queryText));
      if (!parsed) throw new Error("A query must be a JSON object.");
      setQueryError("");
      const response = await onQuery({
        requests: [{ type: "query", query: parsed }],
      });
      if (response) setQueryResult(response.responses);
    } catch (error) {
      setQueryError(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <div className="split-view">
      <aside className="item-list" aria-label="Schemas">
        <div className="list-heading">
          SCHEMAS <span>{schemas.length}</span>
        </div>
        {schemas.map((item) => (
          <button
            className={`list-item ${item.name === selected ? "selected" : ""}`}
            key={item.name}
            onClick={() => {
              onSelect(item.name);
              setRowId("");
              setQuery("");
              setQueryResult(null);
            }}
          >
            <span className="item-title">{item.name}</span>
            <span className="item-meta">
              {item.kind} · {item.track_history ? "tracked" : "untracked"}
            </span>
          </button>
        ))}
        {!schemas.length && <Empty>No schemas in this Store.</Empty>}
      </aside>
      <div className="details">
        {schema ? (
          <>
            <div className="section-heading">
              <div>
                <span className="eyebrow">CANONICAL STORE DATA</span>
                <h1>{schema.name}</h1>
              </div>
              <span className="badge">
                revision {values ? checkpoint.schema_revisions[selected] : "—"}
              </span>
            </div>
            <p className="muted">
              {schema.persistent ? "Persistent" : "Transient"} ·{" "}
              {schema.track_history
                ? "Effective changes select their complete transaction for history."
                : "Changes alone do not select a transaction for history."}{" "}
              Client-local properties and optimistic overlays are not shown.
            </p>
            <div className="state-actions">
              {many && values && (
                <div className="toolbar row-toolbar">
                  <label>
                    Row ID
                    <select
                      value={activeRow}
                      onChange={(event) => setRowId(event.target.value)}
                    >
                      {!ids.length && (
                        <option value="">Empty collection</option>
                      )}
                      {ids.map((id) => (
                        <option key={id}>{id}</option>
                      ))}
                    </select>
                  </label>
                  <span className="muted">{ids.length} rows keyed by ID</span>
                </div>
              )}
              <div className="toolbar">
                <button
                  disabled={disabled || !current}
                  onClick={() =>
                    onDraft(
                      `Edit ${schema.name}${many ? ` / ${activeRow}` : ""}`,
                      [
                        {
                          type: many ? "setrow" : "set",
                          schema: schema.name,
                          ...(many ? { row_id: activeRow } : {}),
                          values: structuredClone(current ?? {}),
                        },
                      ],
                    )
                  }
                >
                  Edit as new mutation
                </button>
                {many && values && (
                  <>
                    <button
                      disabled={disabled || !values}
                      onClick={() =>
                        onDraft(`Insert ${schema.name}`, [
                          {
                            type: "insert",
                            schema: schema.name,
                            row_id: crypto.randomUUID(),
                            values: {},
                          },
                        ])
                      }
                    >
                      Insert row
                    </button>
                    <button
                      disabled={disabled || !current}
                      onClick={() =>
                        onDraft(`Remove ${activeRow}`, [
                          {
                            type: "remove",
                            schema: schema.name,
                            row_id: activeRow,
                          },
                        ])
                      }
                    >
                      Remove row
                    </button>
                  </>
                )}
              </div>
            </div>
            {!values ? (
              <div className="empty schema-status" role="status">
                {preview
                  ? "Unavailable: this schema was outside the captured scope."
                  : connected
                    ? `Loading ${selected}…`
                    : "Waiting for the Store connection…"}
              </div>
            ) : current ? (
              <Inspector value={current} />
            ) : (
              <Empty>
                This collection is empty. Use Insert row to draft its first
                row.
              </Empty>
            )}
            <details>
              <summary>Fields and relationships</summary>
              <p className="muted">
                Null means no value or no related row where allowed.
              </p>
              <Inspector value={schema.fields} />
            </details>
            <details>
              <summary>Declared signals</summary>
              {!Object.keys(schema.signals ?? {}).length && (
                <Empty>No declared signals.</Empty>
              )}
              {Object.entries(schema.signals ?? {}).map(([name, signal]) => (
                <div className="signal-row" key={name}>
                  <div>
                    <strong>{name}</strong>
                    <p className="muted">
                      {signal.undo
                        ? `Compensation: ${signal.undo}`
                        : "No declared inverse"}
                    </p>
                  </div>
                  <button
                    disabled={disabled || !values || (many && !activeRow)}
                    onClick={() =>
                      onDraft(`Emit ${name}`, [
                        {
                          type: many ? "signalrow" : "signal",
                          schema: schema.name,
                          ...(many ? { row_id: activeRow } : {}),
                          signal: name,
                          data: null,
                        },
                      ])
                    }
                  >
                    Draft signal
                  </button>
                </div>
              ))}
              <p className="muted">
                Compensation emits a command; it cannot confirm an external
                action succeeded. New tracked work from a handler can clear
                redo.
              </p>
            </details>
            <details>
              <summary>Run a live query</summary>
              <p className="muted">
                Submit the existing query wire format. New historical queries
                are unavailable; captured responses are in Activity.
              </p>
              <label htmlFor="query-input">Query JSON</label>
              <textarea
                id="query-input"
                spellCheck={false}
                value={queryText}
                onChange={(event) => setQuery(event.target.value)}
              />
              <button disabled={disabled} onClick={() => void runQuery()}>
                Run query
              </button>
              {queryError && (
                <pre className="error" role="alert">
                  {queryError}
                </pre>
              )}
              {queryResult && !preview && <Inspector value={queryResult} />}
            </details>
          </>
        ) : (
          <Empty>Select a schema to inspect its canonical data.</Empty>
        )}
      </div>
    </div>
  );
}
