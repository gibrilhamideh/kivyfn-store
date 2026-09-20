import { useState } from "react";
import { object } from "../model";
import { summary } from "../display";
import type { Values } from "../types";
import Value from "./Value";

export default function Table({
  rows,
  order,
  fields,
  selected,
  onSelect,
  label = "Records",
}: {
  rows: Values;
  order?: string[];
  fields?: string[];
  selected?: string;
  onSelect?: (id: string) => void;
  label?: string;
}) {
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<{
    field: string;
    descending: boolean;
  } | null>(null);
  const [hidden, setHidden] = useState<string[]>([]);
  const [page, setPage] = useState(0);
  const columns = [
    ...new Set([
      ...(fields ?? []),
      ...Object.values(rows).flatMap((row) => Object.keys(object(row) ?? {})),
    ]),
  ];
  const visible = columns.filter((key) => !hidden.includes(key));
  const ids = (order ?? Object.keys(rows)).filter((id) =>
    (
      id +
      " " +
      Object.values(object(rows[id]) ?? {})
        .map(summary)
        .join(" ")
    )
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  if (sort)
    ids.sort((a, b) => {
      const left = sort.field === "" ? a : object(rows[a])?.[sort.field];
      const right = sort.field === "" ? b : object(rows[b])?.[sort.field];
      const result =
        typeof left === "number" && typeof right === "number"
          ? left - right
          : summary(left).localeCompare(summary(right), undefined, {
              numeric: true,
            });
      return (sort.descending ? -result : result) || a.localeCompare(b);
    });
  const activePage = Math.min(
    page,
    Math.max(0, Math.ceil(ids.length / 50) - 1),
  );
  const toggleSort = (field: string) => {
    setSort({
      field,
      descending: sort?.field === field ? !sort.descending : false,
    });
    setPage(0);
  };
  const heading = (field: string, title: string) => (
    <th
      key={field}
      aria-sort={
        sort?.field === field
          ? sort.descending
            ? "descending"
            : "ascending"
          : "none"
      }
    >
      <button onClick={() => toggleSort(field)}>
        {title}
        {sort?.field === field ? (sort.descending ? " ↓" : " ↑") : ""}
      </button>
    </th>
  );
  return (
    <section className="records" aria-label={label}>
      <div className="toolbar table-toolbar">
        <label>
          Search rows
          <input
            value={search}
            placeholder="Row ID or value"
            onChange={(event) => {
              setSearch(event.target.value);
              setPage(0);
            }}
          />
        </label>
        <details className="columns">
          <summary>Columns ({visible.length})</summary>
          {columns.map((key) => (
            <label key={key} className="check-label">
              <input
                type="checkbox"
                checked={!hidden.includes(key)}
                onChange={() =>
                  setHidden(
                    hidden.includes(key)
                      ? hidden.filter((name) => name !== key)
                      : [...hidden, key],
                  )
                }
              />
              {key}
            </label>
          ))}
        </details>
        <span className="muted">
          {ids.length} / {Object.keys(rows).length} loaded rows
        </span>
      </div>
      <div className="table-scroll">
        <table className="data-table collection-table">
          <thead>
            <tr>
              {heading("", "Row ID")}
              {visible.map((key) => heading(key, key))}
            </tr>
          </thead>
          <tbody>
            {ids.slice(activePage * 50, activePage * 50 + 50).map((id) => (
              <tr
                key={id}
                data-row-id={id}
                className={selected === id ? "selected" : ""}
              >
                <th scope="row">
                  {onSelect ? (
                    <button
                      onClick={() => onSelect(id)}
                      aria-pressed={selected === id}
                    >
                      {id}
                    </button>
                  ) : (
                    id
                  )}
                </th>
                {visible.map((key) => (
                  <td key={key}>
                    <Value value={object(rows[id])?.[key]} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!ids.length && (
        <p className="empty">
          {Object.keys(rows).length
            ? "No rows match this search."
            : "This collection is empty."}
        </p>
      )}
      {ids.length > 50 && (
        <div className="toolbar pagination">
          <button
            disabled={!activePage}
            onClick={() => setPage(activePage - 1)}
          >
            Previous rows
          </button>
          <span>
            Page {activePage + 1} of {Math.ceil(ids.length / 50)}
          </span>
          <button
            disabled={(activePage + 1) * 50 >= ids.length}
            onClick={() => setPage(activePage + 1)}
          >
            Next rows
          </button>
        </div>
      )}
    </section>
  );
}
