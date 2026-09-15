import { useState } from "react";
import { Empty, Inspector } from "../components/Inspector";
import { historySource } from "../model";
import type { Activity, HistoryPage } from "../types";

interface Props {
  page: HistoryPage | null;
  activity: Activity[];
  disabled: boolean;
  onCopy: (record: Activity) => void;
  onPage: (offset: number) => void;
}

export default function HistoryView({
  page,
  activity,
  disabled,
  onCopy,
  onPage,
}: Props) {
  const [selected, setSelected] = useState("");
  const entries = page?.entries ?? [];
  const entry = entries.find((item) => item.id === selected) ?? entries.at(-1);
  const source = entry ? historySource(entry, activity) : null;
  const offset = page?.offset ?? 0;
  return (
    <div className="split-view">
      <aside className="item-list" aria-label="History entries">
        <div className="list-heading">
          RETAINED HISTORY <span>{page?.status.total_entries ?? 0}</span>
        </div>
        {[...entries].reverse().map((item) => (
          <button
            className={`list-item ${entry?.id === item.id ? "selected" : ""}`}
            key={item.id}
            onClick={() => setSelected(item.id)}
          >
            <span className="item-title">{item.label || item.id}</span>
            <span className="item-meta">
              {item.transaction_ids.length} transaction
              {item.transaction_ids.length === 1 ? "" : "s"}
              {item.group ? " · grouped" : ""}
            </span>
          </button>
        ))}
        {!entries.length && <Empty>No retained entries.</Empty>}
        <div className="list-footer toolbar">
          <button
            disabled={offset === 0}
            onClick={() => onPage(Math.max(0, offset - 100))}
          >
            Previous page
          </button>
          <button
            disabled={
              offset + entries.length >= (page?.status.total_entries ?? 0)
            }
            onClick={() => onPage(offset + 100)}
          >
            Next page
          </button>
        </div>
      </aside>
      <div className="details">
        <span className="eyebrow">SHARED LINEAR HISTORY</span>
        <h1>{entry?.label || "History"}</h1>
        {page?.status.enabled === false && (
          <div className="notice history-empty" role="status">
            <strong>History is disabled for this Store</strong>
            <p>
              Enable HistoryConfig() in Store configuration and set
              track_history = True on the schemas to track. Restart the Store,
              then make a new edit. Earlier work is not backfilled.
            </p>
            <p>Activity recording is independent of history retention.</p>
          </div>
        )}
        {page?.status.enabled && !entries.length && !page.status.barrier && (
          <div className="notice history-empty" role="status">
            <strong>History is enabled, with no retained entries</strong>
            <p>
              A committed change on a schema with track_history = True selects
              its transaction for history.
            </p>
          </div>
        )}
        <p className="muted">
          Undo last and Redo next act on the current history head. Selecting an
          entry only changes these details. Entries have stable identities and
          revisioned contents; this detached inspection cannot edit stored
          history.
        </p>
        <div className="facts">
          <div>
            <span>History revision</span>
            <strong>{page?.status.revision ?? "—"}</strong>
          </div>
          <div>
            <span>Cursor</span>
            <strong>{page?.status.cursor ?? "—"}</strong>
          </div>
          <div>
            <span>Retained bytes</span>
            <strong>{page?.status.retained_bytes ?? 0}</strong>
          </div>
        </div>
        {page?.status.barrier && (
          <div className="notice">
            <strong>History boundary</strong>
            <Inspector value={page.status.barrier} />
          </div>
        )}
        <p className="muted">
          Oldest retained boundary:{" "}
          {page?.status.oldest_entry_id ??
            entries[0]?.id ??
            "No retained entry"}
          . Earlier evicted history is unavailable.
        </p>
        {entry ? (
          <>
            {entry.truncated && (
              <div className="notice">
                History details unavailable:{" "}
                {String(entry.reason ?? "Diagnostic size limit exceeded.")}
              </div>
            )}
            <div className="toolbar">
              <button
                disabled={disabled || !source}
                title={
                  !source
                    ? "Original Activity actions are unavailable, truncated, or this group has multiple source requests."
                    : "Draft original caller actions with a fresh transaction ID"
                }
                onClick={() => source && onCopy(source)}
              >
                Copy as new mutation
              </button>
              <span className="badge">{entry.id}</span>
            </div>
            {!source && (
              <p className="muted">
                Copy unavailable: no single retained original caller request.
              </p>
            )}
            <dl className="metadata">
              <dt>Origin</dt>
              <dd>{entry.origin_client_id ?? "Standalone Store"}</dd>
              <dt>Group</dt>
              <dd>{entry.group ?? "Ungrouped"}</dd>
              <dt>Transactions</dt>
              <dd>{entry.transaction_ids.join(", ")}</dd>
            </dl>
            <h2>Recorded data effects</h2>
            <Inspector value={entry.records} />
            <h2>Forward and inverse signals</h2>
            <Inspector value={entry.signals} />
            <p className="muted">
              Restoration replays recorded effects, including previous trigger
              results. It does not rerun triggers or recalculate against
              current untracked inputs. Compensation does not confirm external
              success.
            </p>
          </>
        ) : (
          <Empty>
            Tracked successful work appears here when history is enabled.
          </Empty>
        )}
      </div>
    </div>
  );
}
