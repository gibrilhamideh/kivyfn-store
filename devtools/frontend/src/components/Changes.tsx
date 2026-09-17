import type { Change } from "../display";
import Value from "./Value";

export default function Changes({
  changes,
  empty = "No recorded data changes.",
}: {
  changes: Change[];
  empty?: string;
}) {
  if (!changes.length) return <p className="empty">{empty}</p>;
  return (
    <div className="table-scroll">
      <table className="data-table changes-table">
        <thead>
          <tr>
            <th>Schema / row</th>
            <th>Field or operation</th>
            <th>Before</th>
            <th>After</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {changes.map((change, index) => (
            <tr key={index}>
              <th scope="row">
                {change.schema}
                {change.row !== null && <small>{change.row}</small>}
              </th>
              <td>{change.field}</td>
              <td className="before">
                <Value value={change.before} />
              </td>
              <td className="after">
                <Value value={change.after} />
              </td>
              <td>{change.source ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
