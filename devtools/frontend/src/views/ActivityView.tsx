import { useEffect, useState } from "react";
import { Empty, Inspector, DataView } from "../components/Inspector";
import Operation from "../components/Operation";
import { matchesActivity, RequestRate } from "../activity";
import { sourceActions, sourceQuery } from "../model";
import type { Activity, Filters, Values } from "../types";

interface Props {
  records: Activity[];
  requestRate: RequestRate;
  connected: boolean;
  paused: boolean;
  capturePaused: boolean;
  captureFilters: Record<string, string>;
  disabled: boolean;
  filters: Filters;
  schemas: string[];
  dropped: number;
  onFilters: (filters: Filters) => void;
  onPause: () => void;
  onCapturePause: () => void;
  onClearCaptureFilters: () => void;
  onCaptureFilter: () => void;
  onCopy: (record: Activity) => void;
  onQuery: (query: Values) => void;
  onPreview: (record: Activity) => void;
}

export default function ActivityView({
  records,
  requestRate,
  connected,
  paused,
  capturePaused,
  captureFilters,
  disabled,
  filters,
  schemas,
  dropped,
  onFilters,
  onPause,
  onCapturePause,
  onClearCaptureFilters,
  onCaptureFilter,
  onCopy,
  onQuery,
  onPreview,
}: Props) {
  const [selected, setSelected] = useState("");
  const visible = records.filter((record) => matchesActivity(record, filters));
  const recordingFilters = Object.entries(captureFilters).filter(
    ([, value]) => value,
  );
  const record =
    visible.find((item) => item.id === selected) ?? visible.at(-1);
  const query = record ? sourceQuery(record) : null;
  const response =
    record?.category === "request" && record.capture_id
      ? records.find(
          (item) =>
            item.category === "response" &&
            item.capture_id === record.capture_id,
        )
      : undefined;
  return (
    <section className="activity-view" aria-label="Activity inspector">
      <div className="filter-bar">
        <label>
          Client
          <input
            placeholder="Client ID contains"
            value={filters.client_id}
            onChange={(event) =>
              onFilters({ ...filters, client_id: event.target.value })
            }
          />
        </label>
        <label>
          Schema
          <select
            value={filters.schema}
            onChange={(event) =>
              onFilters({ ...filters, schema: event.target.value })
            }
          >
            <option value="">All schemas</option>
            {schemas.map((schema) => (
              <option key={schema}>{schema}</option>
            ))}
          </select>
        </label>
        <label>
          Category
          <select
            aria-label="Category"
            value={filters.category}
            onChange={(event) =>
              onFilters({ ...filters, category: event.target.value })
            }
          >
            <option value="">All categories</option>
            {[
              ...new Set([
                ...records.map((item) => item.category),
                "request",
                "response",
                "commit",
                "lifecycle",
                "gap",
              ]),
            ]
              .sort()
              .map((category) => (
                <option key={category}>{category}</option>
              ))}
          </select>
        </label>
        <label>
          Status
          <input
            placeholder="Exact status"
            value={filters.status}
            onChange={(event) =>
              onFilters({ ...filters, status: event.target.value })
            }
          />
        </label>
        <label>
          Identity
          <input
            placeholder="Record / capture / transaction contains"
            value={filters.identity}
            onChange={(event) =>
              onFilters({ ...filters, identity: event.target.value })
            }
          />
        </label>
      </div>
      <div className="activity-controls toolbar">
        <label>
          Row ID filter
          <input
            value={filters.row ?? ""}
            onChange={(event) =>
              onFilters({ ...filters, row: event.target.value })
            }
          />
        </label>
        <label>
          Field filter
          <input
            value={filters.field ?? ""}
            onChange={(event) =>
              onFilters({ ...filters, field: event.target.value })
            }
          />
        </label>
        {Object.values(filters).some(Boolean) && (
          <button
            onClick={() =>
              onFilters({
                client_id: "",
                schema: "",
                category: "",
                status: "",
                identity: "",
                row: "",
                field: "",
              })
            }
          >
            Clear display filters
          </button>
        )}
        <button onClick={onPause}>
          {paused ? "Resume display" : "Pause display"}
        </button>
        <button disabled={disabled} onClick={onCapturePause}>
          {capturePaused ? "Resume recording" : "Pause recording"}
        </button>
        <button disabled={disabled} onClick={onCaptureFilter}>
          Use filters for recording
        </button>
        <RequestRateCounter
          rate={requestRate}
          filters={filters}
          connected={connected}
          capturePaused={capturePaused}
        />
        <span className="muted">
          Recording is shared across browsers. State updates and undo history
          remain live.
        </span>
      </div>
      {recordingFilters.length > 0 && (
        <div className="notice recording-filters">
          Recording filters:{" "}
          {recordingFilters
            .map(([key, value]) => `${key}=${value}`)
            .join(", ")}
          . Activity and its request rate only include captured traffic.
          <button disabled={disabled} onClick={onClearCaptureFilters}>
            Clear recording filters
          </button>
        </div>
      )}
      {dropped > 0 && (
        <div className="notice loss-marker" role="status">
          {dropped} captured records dropped or evicted. Historical preview may
          be unavailable before its retained boundary.
        </div>
      )}
      <div className="split-view activity-split">
        <aside className="item-list" aria-label="Activity records">
          <div className="list-heading">
            {paused ? "DISPLAY PAUSED" : "LIVE ACTIVITY"}{" "}
            <span>{visible.length}</span>
          </div>
          {[...visible].reverse().map((item) => (
            <button
              className={`list-item ${record?.id === item.id ? "selected" : ""} ${item.category === "gap" ? "loss-item" : ""}`}
              key={item.id}
              onClick={() => setSelected(item.id)}
            >
              <span className="item-title">
                {item.category}{" "}
                <span className="badge">
                  {item.status === "received" ? "Received" : item.status}
                </span>
              </span>
              <span className="item-meta">
                #{item.sequence} ·{" "}
                {item.transaction_id || item.client_id || "Store"}
              </span>
              {item.truncated && (
                <span className="warning-text">Payload truncated</span>
              )}
            </button>
          ))}
          {!visible.length && (
            <Empty>
              {capturePaused
                ? "Recording is paused. Resume recording to see new activity."
                : records.length
                  ? "No Activity matches these filters."
                  : "No activity captured yet. New Store requests and changes appear here."}
            </Empty>
          )}
        </aside>
        <div className="details">
          {record ? (
            <>
              <span className="eyebrow">
                RECORDED EVENT · #{record.sequence}
              </span>
              <h1>{record.category}</h1>
              <p className="muted">
                Captured responses mean a response was produced. Transport
                delivery and external signal success are not established here.
              </p>
              <div className="toolbar">
                <button
                  disabled={disabled || !sourceActions(record)}
                  onClick={() => onCopy(record)}
                >
                  Copy as new mutation
                </button>
                <button
                  disabled={disabled || !query}
                  onClick={() => query && onQuery(query)}
                >
                  Run query again
                </button>
                <button
                  disabled={record.truncated || record.category === "gap"}
                  onClick={() => onPreview(record)}
                >
                  Preview captured state
                </button>
              </div>
              <dl className="metadata">
                <dt>Status</dt>
                <dd>
                  {record.status === "received" ? "Received" : record.status}
                </dd>
                <dt>Client</dt>
                <dd>{record.client_id ?? "Store"}</dd>
                <dt>Transaction</dt>
                <dd>{record.transaction_id ?? "—"}</dd>
                <dt>Request / response ID</dt>
                <dd>{record.capture_id ?? "—"}</dd>
                <dt>Time</dt>
                <dd>
                  {typeof record.time === "number"
                    ? new Date(record.time * 1000).toISOString()
                    : record.time}
                </dd>
              </dl>
              {record.category === "request" && (
                <section
                  className="related-response"
                  aria-label="Related response"
                >
                  {response ? (
                    <>
                      <h2>Response produced · {response.status}</h2>
                      <p className="muted">
                        Request arrival is recorded separately from its result.
                      </p>
                      <details>
                        <summary>
                          Response #{response.sequence} details
                        </summary>
                        {response.truncated && (
                          <p className="muted">
                            The recorded response payload was truncated.
                          </p>
                        )}
                        <Inspector value={response.detail} />
                      </details>
                    </>
                  ) : (
                    <>
                      <h2>Response unavailable</h2>
                      <p className="muted">
                        No matching response is in this display. It may not
                        have arrived, recording may have excluded it, or it may
                        have been evicted. This does not establish that work is
                        pending.
                        {paused &&
                          " Resume display to see newly received records."}
                      </p>
                    </>
                  )}
                </section>
              )}
              {record.truncated && (
                <div className="notice">
                  Truncated payload. Copying and exact reconstruction are
                  unavailable.
                </div>
              )}
              <DataView name="Activity" value={record.detail}>
                <Operation
                  key={record.id}
                  record={record}
                  records={records}
                  onPreview={onPreview}
                />
              </DataView>
            </>
          ) : (
            <Empty>
              Requests, responses, commits, signals and connection activity
              appear here.
            </Empty>
          )}
        </div>
      </div>
    </section>
  );
}

function RequestRateCounter({
  rate,
  filters,
  connected,
  capturePaused,
}: {
  rate: RequestRate;
  filters: Filters;
  connected: boolean;
  capturePaused: boolean;
}) {
  const [, refresh] = useState(0);
  useEffect(() => {
    if (!connected || capturePaused) return;
    const timer = setInterval(() => refresh((tick) => tick + 1), 250);
    return () => clearInterval(timer);
  }, [connected, capturePaused]);
  const unavailable = !connected
    ? "Disconnected"
    : capturePaused
      ? "Recording paused"
      : null;
  return (
    <div
      className="activity-rate"
      title="Captured request batches received in the last second, matching all display filters. Responses and commits are excluded. The rate stays live when the display is paused."
    >
      <strong aria-label="Requests per second">
        {unavailable ? "—" : rate.count(filters)}
      </strong>{" "}
      requests/s
      <span className="muted">
        {unavailable ?? "Live · last 1s"}
        {Object.values(filters).some(Boolean) ? " · filtered" : ""}
      </span>
    </div>
  );
}
