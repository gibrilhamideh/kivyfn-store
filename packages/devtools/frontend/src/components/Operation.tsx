import { useState } from "react";
import { changesFrom, commitDetail, queryRows, queryOrder } from "../display";
import { object } from "../model";
import type { Activity, Json, Values } from "../types";
import Changes from "./Changes";
import Dictionary from "./Dictionary";
import Record from "./Record";
import Table from "./Table";

export function Actions({ request }: { request: Values }) {
  const items = Array.isArray(request.requests) ? request.requests : [];
  return (
    <>
      {items.map((value, index) => {
        const item = object(value);
        if (!item) return <Dictionary key={index} value={value} />;
        const actions = Array.isArray(item.actions) ? item.actions : null;
        return (
          <section key={index}>
            <h3>
              {String(item.type)}
              {item.transaction_id ? " · " + String(item.transaction_id) : ""}
            </h3>
            {actions ? (
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Action</th>
                      <th>Schema</th>
                      <th>Row</th>
                      <th>Values / payload</th>
                    </tr>
                  </thead>
                  <tbody>
                    {actions.map((action, i) => {
                      const data = object(action) ?? {};
                      return (
                        <tr key={i}>
                          <th>{String(data.type)}</th>
                          <td>{String(data.schema)}</td>
                          <td>{String(data.row_id ?? "—")}</td>
                          <td>
                            <Dictionary
                              value={data.values ?? data.data ?? data}
                            />
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <Record values={item} />
            )}
          </section>
        );
      })}
    </>
  );
}

export function ResponseDetails({ response }: { response: Values }) {
  const rows = queryRows(response);
  if (rows)
    return (
      <Table
        rows={rows}
        order={queryOrder(response)}
        label="Recorded query result"
      />
    );
  const items = response.responses;
  return Array.isArray(items) ? (
    <>
      {items.map((item, index) => (
        <Record key={index} values={object(item) ?? { result: item }} />
      ))}
    </>
  ) : (
    <Record values={response} />
  );
}

/** Correlation is limited to retained records; missing payloads are never inferred. */
export default function Operation({
  record,
  records,
  onPreview,
}: {
  record: Activity;
  records: Activity[];
  onPreview: (record: Activity) => void;
}) {
  const [panel, setPanel] = useState(
    record.category === "request"
      ? "Request"
      : record.category === "response"
        ? "Response"
        : "Changes",
  );
  const transactions = new Set<string>(
    record.transaction_id ? [record.transaction_id] : [],
  );
  const request =
    record.category === "request"
      ? record
      : records.find(
          (item) =>
            item.category === "request" &&
            (record.capture_id
              ? item.capture_id === record.capture_id
              : record.transaction_id &&
                item.transaction_id === record.transaction_id),
        );
  const requestData = object(request?.detail.request);
  const items = requestData?.requests;
  if (Array.isArray(items))
    for (const item of items) {
      const id = object(item)?.transaction_id;
      if (typeof id === "string") transactions.add(id);
    }
  const response =
    record.category === "response"
      ? record
      : records.find(
          (item) =>
            item.category === "response" &&
            item.capture_id &&
            item.capture_id === (request?.capture_id ?? record.capture_id),
        );
  const commits = records.filter(
    (item) =>
      item.category === "commit" &&
      item.transaction_id &&
      transactions.has(item.transaction_id),
  );
  if (
    record.category === "commit" &&
    !commits.some((item) => item.id === record.id)
  )
    commits.push(record);
  const changes = commits.flatMap((item) =>
    changesFrom(commitDetail(item)?.events),
  );
  const signals: Json[] = commits.flatMap((item) => {
    const values = commitDetail(item)?.signals;
    return Array.isArray(values) ? values : [];
  });
  const original = object(record.detail.request);
  const reply = object(record.detail.response);
  if (!["request", "response", "commit"].includes(record.category))
    return <Record values={record.detail} />;
  return (
    <section className="operation" aria-label="Connected operation">
      <div
        className="toolbar operation-tabs"
        role="group"
        aria-label="Operation details"
      >
        {["Request", "Response", "Changes", "State afterward"].map((name) => (
          <button
            key={name}
            aria-pressed={panel === name}
            onClick={() => setPanel(name)}
          >
            {name}
          </button>
        ))}
      </div>
      {panel === "Request" &&
        (request?.truncated ? (
          <p className="notice">Original request was truncated.</p>
        ) : requestData || original ? (
          <Actions request={(requestData ?? original)!} />
        ) : (
          <p className="empty">
            Original request unavailable in retained Activity.
          </p>
        ))}
      {panel === "Response" &&
        (response?.truncated ? (
          <p className="notice">Response payload was truncated.</p>
        ) : response || reply ? (
          <>
            <p className="muted">
              Response produced · {response?.status}. Delivery is not
              confirmed.
            </p>
            <ResponseDetails
              response={
                object(response?.detail.response) ?? reply ?? response!.detail
              }
            />
          </>
        ) : (
          <p className="empty">
            Response unavailable. This does not establish that execution is
            pending.
          </p>
        ))}
      {panel === "Changes" && (
        <>
          <Changes
            changes={changes}
            empty={
              commits.length
                ? "No recorded data changes in this operation."
                : "Commit details unavailable in retained Activity."
            }
          />
          {commits.some((item) => item.truncated) && (
            <p className="notice">Some commit details were truncated.</p>
          )}
          {signals.length > 0 && (
            <>
              <h3>Signals</h3>
              <Dictionary value={signals} />
            </>
          )}
        </>
      )}
      {panel === "State afterward" && (
        <>
          <p className="muted">
            Open a detached preview after a retained commit. Availability
            depends on the captured scope and timeline.
          </p>
          {commits
            .filter((item) => !item.truncated)
            .map((item) => (
              <button key={item.id} onClick={() => onPreview(item)}>
                Preview after commit #{item.sequence}
              </button>
            ))}
          {!commits.length && (
            <p className="empty">
              No retained commit is available for this operation.
            </p>
          )}
        </>
      )}
    </section>
  );
}
