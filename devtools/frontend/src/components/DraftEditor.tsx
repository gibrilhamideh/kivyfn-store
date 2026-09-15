import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { actionsFromText } from "../model";
import type { Checkpoint, Draft, RequestBatch } from "../types";

interface Props {
  draft: Draft;
  children: ReactNode;
  checkpoint: Checkpoint;
  storeInstanceId: string;
  disabled: boolean;
  onChange: (draft: Draft | null) => void;
  onSubmit: (batch: RequestBatch) => Promise<unknown>;
}

export default function DraftEditor({
  draft,
  children,
  checkpoint,
  storeInstanceId,
  disabled,
  onChange,
  onSubmit,
}: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const submittingRef = useRef(false);
  useEffect(() => {
    const dialog = dialogRef.current;
    const previousFocus = document.activeElement;
    dialog?.showModal();
    return () => {
      dialog?.close();
      if (previousFocus instanceof HTMLElement) previousFocus.focus();
    };
  }, []);
  const [error, setError] = useState("");
  const [label, setLabel] = useState("");
  const [group, setGroup] = useState("");
  const additionalSchemas = Object.keys(checkpoint.schema_revisions).some(
    (schema) => !Object.hasOwn(draft.revisions, schema),
  );
  const stale =
    additionalSchemas ||
    draft.storeInstanceId !== storeInstanceId ||
    Object.entries(draft.revisions).some(
      ([schema, revision]) => checkpoint.schema_revisions[schema] !== revision,
    );
  const apply = async () => {
    if (disabled || submittingRef.current) return;
    submittingRef.current = true;
    try {
      const actions = actionsFromText(draft.text);
      const schemas = new Set(actions.map((action) => String(action.schema)));
      const missing = [...schemas].filter(
        (schema) => !Object.hasOwn(draft.revisions, schema),
      );
      if (missing.length) {
        throw new Error(
          `Use current revisions after loading: ${missing.join(", ")}. Check that every schema name exists.`,
        );
      }
      setError("");
      await onSubmit({
        requests: [
          {
            type: "transaction",
            transaction_id: crypto.randomUUID(),
            actions,
            expected_store_instance_id: draft.storeInstanceId,
            expected_revisions: Object.fromEntries(
              [...schemas].map((schema) => [schema, draft.revisions[schema]]),
            ),
            ...(label ? { history_label: label } : {}),
            ...(group ? { history_group: group } : {}),
          },
        ],
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      submittingRef.current = false;
    }
  };
  return (
    <dialog
      ref={dialogRef}
      className="draft"
      aria-label="Mutation draft"
      tabIndex={-1}
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        const controls = [
          ...event.currentTarget.querySelectorAll<HTMLElement>(
            "button:not(:disabled), input:not(:disabled), textarea:not(:disabled)",
          ),
        ];
        const first = controls[0];
        const last = controls.at(-1);
        if (
          event.shiftKey &&
          (document.activeElement === first ||
            document.activeElement === event.currentTarget)
        ) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }}
      onCancel={(event) => {
        event.preventDefault();
        onChange(null);
      }}
    >
      <div className="section-heading">
        <div>
          <span className="eyebrow">NEW TRANSACTION</span>
          <h2>{draft.title}</h2>
        </div>
        <button onClick={() => onChange(null)} aria-label="Close draft">
          Close
        </button>
      </div>
      <div className="draft-body">
        {children}
        <p className="muted">
          Canonical actions in JSON. Large integers use{" "}
          {'{$integer: "digits"}'}; literal objects with reserved keys use
          $object wrappers. Values stay inert. Ctrl/⌘ + Enter applies a new
          transaction.
        </p>
        {stale && (
          <div className="notice" role="status">
            {additionalSchemas
              ? "Additional schema values loaded. Use their current revisions to guard this draft."
              : "Source changed. Your draft has been preserved. Compare current State before explicitly refreshing its guards."}
            <button
              disabled={disabled}
              onClick={() =>
                onChange({
                  ...draft,
                  storeInstanceId,
                  revisions: { ...checkpoint.schema_revisions },
                })
              }
            >
              Use current revisions
            </button>
          </div>
        )}
        <label htmlFor="draft-actions">Actions</label>
        <textarea
          id="draft-actions"
          spellCheck={false}
          value={draft.text}
          onChange={(event) =>
            onChange({ ...draft, text: event.target.value })
          }
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
              event.preventDefault();
              if (!disabled) void apply();
            }
          }}
        />
        <div className="form-row">
          <label>
            History label
            <input
              value={label}
              onChange={(event) => setLabel(event.target.value)}
            />
          </label>
          <label>
            Group token (optional)
            <input
              value={group}
              onChange={(event) => setGroup(event.target.value)}
            />
          </label>
        </div>
        <p className="muted">
          A matching token groups adjacent eligible commands from this browser
          session. Each Apply remains independently atomic.
        </p>
        {error && (
          <pre className="error" role="alert">
            {error}
          </pre>
        )}
      </div>
      <div className="draft-footer">
        <button
          className="primary"
          disabled={disabled}
          onClick={() => void apply()}
        >
          Apply new mutation
        </button>
      </div>
    </dialog>
  );
}
