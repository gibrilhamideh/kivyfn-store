import { object } from "../model";
import type { SchemaInfo, Values } from "../types";
import Value from "./Value";

export default function Record({
  values,
  schema,
  onRelated,
  onField,
}: {
  values: Values;
  schema?: SchemaInfo;
  onRelated?: (schema: string, row: string) => void;
  onField?: (field: string) => void;
}) {
  return (
    <div className="table-scroll">
      <table className="data-table record-table">
        <thead>
          <tr>
            <th>Field</th>
            <th>Value</th>
            <th>
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(values).map(([key, value]) => {
            const metadata = object(schema?.fields[key]);
            const target =
              metadata?.type === "ForeignKeyField" ? metadata.schema : null;
            return (
              <tr key={key} data-field={key}>
                <th
                  scope="row"
                  title={metadata ? JSON.stringify(metadata) : undefined}
                >
                  {key}
                </th>
                <td>
                  <Value value={value} />
                </td>
                <td className="row-tools">
                  {typeof target === "string" &&
                    typeof value === "string" &&
                    onRelated && (
                      <button onClick={() => onRelated(target, value)}>
                        Open related row
                      </button>
                    )}
                  {onField && (
                    <button
                      onClick={() => onField(key)}
                      aria-label={"Activity for " + key}
                    >
                      Activity
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
