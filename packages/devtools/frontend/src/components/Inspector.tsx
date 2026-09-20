import { useView } from "../view";
import { format } from "../model";
import Dictionary from "./Dictionary";

export function ViewSwitch({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: "structured" | "dictionary") => void;
}) {
  return (
    <div className="view-switch" role="group" aria-label="View">
      <button
        aria-pressed={value === "structured"}
        onClick={() => onChange("structured")}
      >
        Structured
      </button>
      <button
        aria-pressed={value === "dictionary"}
        onClick={() => onChange("dictionary")}
      >
        Dictionary
      </button>
    </div>
  );
}

export function DataView({
  value,
  children,
  name,
}: {
  value: unknown;
  children: React.ReactNode;
  name: string;
}) {
  const [view, setView] = useView(name);
  return (
    <section data-view={name}>
      <ViewSwitch value={view} onChange={setView} />
      {view === "dictionary" ? <Dictionary value={value} /> : children}
    </section>
  );
}

export function Inspector({ value }: { value: unknown }) {
  return <pre className="inspector">{format(value)}</pre>;
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="empty">{children}</div>;
}
