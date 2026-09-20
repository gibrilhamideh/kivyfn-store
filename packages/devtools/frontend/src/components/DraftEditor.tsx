import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { actionsFromText, format } from "../model";
import {
  currentRow,
  formActions,
  formChanges,
  formErrors,
  rebaseForm,
} from "../draft";
import type { EditForm } from "../draft";
import type { Checkpoint, Draft, RequestBatch, ResponseBatch } from "../types";
import Changes from "./Changes";
import Editor from "./Editor";
import Value from "./Value";

interface Props {
  draft: Draft;
  children: ReactNode;
  checkpoint: Checkpoint;
  storeInstanceId: string;
  disabled: boolean;
  onChange: (draft: Draft | null) => void;
  onSubmit: (batch: RequestBatch) => Promise<ResponseBatch | null>;
  onMinimize: () => void;
}

export default function DraftEditor({
  draft,
  children,
  checkpoint,
  storeInstanceId,
  disabled,
  onChange,
  onSubmit,
  onMinimize,
}: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const submittingRef = useRef(false);
  const initialText = useRef(draft.originalText);
  const [step, setStep] = useState<"edit" | "review" | "actions">(
    draft.form ? "edit" : "actions",
  );
  const [error, setError] = useState("");
  const label = draft.historyLabel ?? "";
  const group = draft.historyGroup ?? "";
  const [comparison, setComparison] = useState<{
    checkpoint: Checkpoint;
    instance: string;
  } | null>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    const previousFocus = document.activeElement;
    dialog?.showModal();
    return () => {
      dialog?.close();
      if (previousFocus instanceof HTMLElement) previousFocus.focus();
    };
  }, []);
  let schemas: string[] = [];
  try {
    schemas = [
      ...new Set(
        actionsFromText(draft.text).map((action) => String(action.schema)),
      ),
    ];
  } catch {
    schemas = draft.form
      ? [draft.form.schema.name]
      : Object.keys(draft.revisions);
  }
  const additionalSchemas = schemas.some(
    (schema) => !Object.hasOwn(draft.revisions, schema),
  );
  const stale =
    draft.storeInstanceId !== storeInstanceId ||
    schemas.some(
      (schema) =>
        !Object.hasOwn(draft.revisions, schema) ||
        checkpoint.schema_revisions[schema] !== draft.revisions[schema],
    );
  const errors = draft.form ? formErrors(draft.form) : [];
  const changes = draft.form ? formChanges(draft.form) : [];
  const changeForm = (form: EditForm) => {
    onChange({
      ...draft,
      form,
      text: format(formActions(form)),
      completed: false,
    });
    setComparison(null);
  };
  const close = () => {
    const dirty =
      !draft.completed &&
      (draft.form
        ? changes.length > 0 || errors.length > 0
        : draft.text !== initialText.current);
    if (!dirty || window.confirm("Discard this unfinished draft?"))
      onChange(null);
  };
  const apply = async () => {
    if (disabled || draft.completed || submittingRef.current) return;
    if (draft.form && step !== "review") {
      setStep("review");
      return;
    }
    submittingRef.current = true;
    try {
      if (errors.length) throw new Error(errors[0]);
      if (draft.form && stale)
        throw new Error(
          "Compare with current values and adopt the reviewed revisions first.",
        );
      const actions = actionsFromText(draft.text);
      const missing = schemas.filter(
        (schema) => !Object.hasOwn(draft.revisions, schema),
      );
      if (missing.length)
        throw new Error(
          "Use current revisions after loading: " + missing.join(", "),
        );
      setError("");
      const response = await onSubmit({
        requests: [
          {
            type: "transaction",
            transaction_id: crypto.randomUUID(),
            actions,
            expected_store_instance_id: draft.storeInstanceId,
            expected_revisions: Object.fromEntries(
              schemas.map((schema) => [schema, draft.revisions[schema]]),
            ),
            ...(label ? { history_label: label } : {}),
            ...(group ? { history_group: group } : {}),
          },
        ],
      });
      if (response) initialText.current = draft.text;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      submittingRef.current = false;
    }
  };
  const adopt = () => {
    if (!comparison) return;
    try {
      const form = draft.form
        ? rebaseForm(draft.form, comparison.checkpoint)
        : undefined;
      onChange({
        ...draft,
        form,
        text: form ? format(formActions(form)) : draft.text,
        storeInstanceId: comparison.instance,
        revisions: { ...comparison.checkpoint.schema_revisions },
      });
      setComparison(null);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };
  return (
    <dialog
      ref={dialogRef}
      className={"draft" + (draft.form ? " structured-draft" : "")}
      aria-label="Mutation draft"
      tabIndex={-1}
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
          event.preventDefault();
          void apply();
        }
        if (event.key !== "Tab") return;
        const controls = [
          ...event.currentTarget.querySelectorAll<HTMLElement>(
            "button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled)",
          ),
        ].filter((item) => item.getClientRects().length);
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
    >
      <div className="section-heading">
        <div>
          <span className="eyebrow">
            {draft.completed ? "TRANSACTION CONFIRMED" : "LOCAL DRAFT"}
          </span>
          <h2>{draft.title}</h2>
        </div>
        <div className="toolbar">
          <button onClick={onMinimize}>Keep draft and browse</button>
          <button onClick={close} aria-label="Close draft">
            Close
          </button>
        </div>
      </div>
      <div className="draft-body">
        {children}
        {draft.completed && (
          <p className="notice" role="status">
            Transaction confirmed. Editing the actions starts new work.
          </p>
        )}
        {stale && !draft.completed && (
          <div className="notice" role="status">
            {additionalSchemas
              ? "Additional schema values loaded. Use their current revisions to guard this draft."
              : "Source changed. Your draft has been preserved."}
            {draft.form ? (
              <button
                disabled={disabled}
                onClick={() =>
                  setComparison({
                    checkpoint: structuredClone(checkpoint),
                    instance: storeInstanceId,
                  })
                }
              >
                Compare with current
              </button>
            ) : (
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
            )}
          </div>
        )}
        {comparison && draft.form && (
          <section aria-label="Compare draft with current">
            <h3>Original / current / draft</h3>
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Row / field</th>
                    <th>Original</th>
                    <th>Current</th>
                    <th>Draft</th>
                  </tr>
                </thead>
                <tbody>
                  {changes.map((change, index) => {
                    const row = draft.form!.rows.find(
                      (item) => item.id === change.row,
                    )!;
                    const current = currentRow(
                      draft.form!,
                      row,
                      comparison.checkpoint,
                    );
                    const value =
                      row.before === null || row.removed
                        ? (current ?? undefined)
                        : current?.[change.field];
                    return (
                      <tr key={index}>
                        <th>
                          {change.row ?? draft.form!.schema.name} /{" "}
                          {change.field}
                        </th>
                        <td>
                          <Value value={change.before} />
                        </td>
                        <td>
                          <Value value={value} />
                        </td>
                        <td>
                          <Value value={change.after} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="muted">
              Adopting this baseline preserves your explicit edits. Apply is a
              separate action.
            </p>
            <button disabled={disabled} onClick={adopt}>
              Keep draft with reviewed revisions
            </button>
          </section>
        )}
        {draft.form && (
          <div className="toolbar draft-steps">
            <button
              aria-pressed={step === "edit"}
              onClick={() => setStep("edit")}
            >
              Edit values
            </button>
            <button
              aria-pressed={step === "review"}
              onClick={() => setStep("review")}
            >
              Review changes ({changes.length})
            </button>
            <button
              aria-pressed={step === "actions"}
              onClick={() => setStep("actions")}
            >
              Advanced actions
            </button>
          </div>
        )}
        {draft.form && step === "edit" && (
          <fieldset disabled={disabled} className="editor-fields">
            <Editor form={draft.form} onChange={changeForm} />
          </fieldset>
        )}
        {draft.form && step === "review" && (
          <>
            <Changes changes={changes} empty="No changes to submit." />
            <p className="muted">
              Requested changes only. The Store validates the transaction and
              runs its triggers.
            </p>
            <details>
              <summary>Generated request actions</summary>
              <pre className="inspector">{draft.text}</pre>
            </details>
          </>
        )}
        {step === "actions" && (
          <>
            <p className="muted">
              Ordered action JSON. Editing here switches to an advanced draft.
              Ctrl/⌘ + Enter applies. Large integers retain their exact digits.
            </p>
            <label htmlFor="draft-actions">Actions</label>
            <textarea
              id="draft-actions"
              spellCheck={false}
              value={draft.text}
              disabled={disabled}
              onChange={(event) => {
                onChange({
                  ...draft,
                  form: undefined,
                  text: event.target.value,
                  completed: false,
                });
                setComparison(null);
              }}
            />
          </>
        )}
        <div className="form-row">
          <label>
            History label
            <input
              value={label}
              onChange={(event) =>
                onChange({ ...draft, historyLabel: event.target.value })
              }
            />
          </label>
          <label>
            Group token (optional)
            <input
              value={group}
              onChange={(event) =>
                onChange({ ...draft, historyGroup: event.target.value })
              }
            />
          </label>
        </div>
        <p className="muted">
          A matching token groups adjacent eligible commands from this session.
          Each Apply remains independently atomic.
        </p>
        {errors.length > 0 && (
          <p className="error" role="alert">
            Fix {errors.length} input error(s) before applying.
          </p>
        )}
        {error && (
          <pre className="error" role="alert">
            {error}
          </pre>
        )}
      </div>
      <div className="draft-footer">
        <button
          className="primary"
          disabled={
            disabled ||
            draft.completed ||
            !!(
              draft.form &&
              (errors.length ||
                !changes.length ||
                (step === "review" && stale))
            )
          }
          onClick={() => void apply()}
        >
          {draft.form
            ? step === "review"
              ? "Apply transaction"
              : "Review before applying"
            : "Apply new mutation"}
        </button>
      </div>
    </dialog>
  );
}
