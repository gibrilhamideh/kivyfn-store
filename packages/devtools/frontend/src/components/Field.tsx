import { fieldText } from "../draft";
import { equal, unsupported } from "../display";
import type { Json, Values } from "../types";
import Value from "./Value";

export default function Field({
  label,
  value,
  metadata,
  text,
  error,
  onText,
  onValue,
}: {
  label: string;
  value: Json | undefined;
  metadata: Values;
  text?: string;
  error?: string;
  onText: (text: string) => void;
  onValue: (value: Json) => void;
}) {
  if (value !== undefined && unsupported(value))
    return (
      <>
        <Value value={value} />
        <span className="muted">Read-only unsupported value</span>
      </>
    );
  const choices = metadata.values;
  let control;
  if (metadata.type === "BooleanField" && value !== null)
    control = (
      <input
        aria-label={label}
        type="checkbox"
        checked={value === true}
        onChange={(event) => onValue(event.target.checked)}
      />
    );
  else if (metadata.type === "EnumField" && Array.isArray(choices))
    control = (
      <select
        aria-label={label}
        value={choices.findIndex((item) => equal(item, value))}
        onChange={(event) => onValue(choices[Number(event.target.value)])}
      >
        <option value={-1} disabled>
          Select a value
        </option>
        {choices.map((item, index) => (
          <option key={index} value={index}>
            {JSON.stringify(item)}
          </option>
        ))}
      </select>
    );
  else
    control = (
      <textarea
        aria-label={label}
        className="field-input"
        spellCheck={false}
        rows={1}
        value={text ?? fieldText(value, metadata.type)}
        aria-invalid={!!error}
        onChange={(event) => onText(event.target.value)}
      />
    );
  return (
    <div className="field-editor">
      {control}
      {metadata.allow_none === true && (
        <button
          type="button"
          onClick={() => onValue(null)}
          disabled={value === null}
        >
          Set null
        </button>
      )}
      {value === null && metadata.type === "BooleanField" && (
        <button type="button" onClick={() => onValue(false)}>
          Set value
        </button>
      )}
      {error && (
        <span className="field-error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}
