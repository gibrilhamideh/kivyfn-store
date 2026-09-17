import { useState } from "react";
import { format } from "../model";

function Branch({
  name,
  value,
  path,
  depth,
}: {
  name: string;
  value: unknown;
  path: string;
  depth: number;
}) {
  const [limit, setLimit] = useState(50);
  const [message, setMessage] = useState("");
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setMessage("Copied");
    } catch {
      setMessage("Copy unavailable");
    }
  };
  const entries =
    value !== null && typeof value === "object" ? Object.entries(value) : null;
  const tools = (
    <span className="tree-tools">
      <button
        onClick={() => void copy(format(value))}
        aria-label={"Copy value " + path}
      >
        Copy
      </button>
      <button onClick={() => void copy(path)} aria-label={"Copy path " + path}>
        Path
      </button>
      <span role="status">{message}</span>
    </span>
  );
  if (!entries)
    return (
      <div className="tree-leaf">
        <span>
          <span className="tree-key">
            {name ? JSON.stringify(name) + ": " : ""}
          </span>
          <span className={"value-" + typeof value}>{format(value)}</span>
        </span>
        {tools}
      </div>
    );
  return (
    <details className="tree-node" open={depth < 3}>
      <summary>
        <span className="tree-key">
          {name ? JSON.stringify(name) + ": " : ""}
        </span>
        {Array.isArray(value) ? "Array" : "Object"}{" "}
        <span className="muted">{entries.length} items</span>
      </summary>
      {tools}
      <div className="tree-children">
        {entries.slice(0, limit).map(([key, child]) => (
          <Branch
            key={key}
            name={key}
            value={child}
            path={path + "/" + key.replaceAll("~", "~0").replaceAll("/", "~1")}
            depth={depth + 1}
          />
        ))}
        {entries.length > limit && (
          <button onClick={() => setLimit(limit + 50)}>Show next 50</button>
        )}
      </div>
    </details>
  );
}

export default function Dictionary({ value }: { value: unknown }) {
  return (
    <div className="dictionary inspector">
      <Branch name="" value={value} path="" depth={0} />
    </div>
  );
}
