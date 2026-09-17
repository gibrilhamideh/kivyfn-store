import { summary } from "../display";
import Dictionary from "./Dictionary";

export default function Value({ value }: { value: unknown }) {
  const text = summary(value);
  if ((value !== null && typeof value === "object") || text.length > 100)
    return (
      <details className="cell-value">
        <summary>{text.length > 100 ? text.slice(0, 97) + "…" : text}</summary>
        <Dictionary value={value} />
      </details>
    );
  return <span className={"value-" + typeof value}>{text}</span>;
}
