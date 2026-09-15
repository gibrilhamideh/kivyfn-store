import { useCallback, useEffect, useRef, useState } from "react";
import {
  AmbiguousOutcome,
  command,
  errorText,
  post,
  read,
  stream,
} from "./api";
import { actionsFromText, applyCommit, format, sourceActions } from "./model";
import DraftEditor from "./components/DraftEditor";
import { Inspector } from "./components/Inspector";
import ActivityView from "./views/ActivityView";
import HistoryView from "./views/HistoryView";
import StateView from "./views/StateView";
import type {
  Activity,
  ActivityPage,
  Checkpoint,
  Commit,
  Connection,
  Draft,
  Envelope,
  Filters,
  HistoryPage,
  Info,
  Preview,
  RequestBatch,
  ResponseBatch,
  Snapshot,
  Values,
} from "./types";

const emptyCheckpoint: Checkpoint = { schemas: {}, schema_revisions: {} };
const emptyFilters: Filters = {
  client_id: "",
  schema: "",
  category: "",
  status: "",
  identity: "",
};

export default function App() {
  const [info, setInfo] = useState<Info | null>(null);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [checkpoint, setCheckpoint] = useState<Checkpoint>(emptyCheckpoint);
  const checkpointRef = useRef(checkpoint);
  const [history, setHistory] = useState<HistoryPage | null>(null);
  const [activity, setActivity] = useState<Activity[]>([]);
  const [frozenActivity, setFrozenActivity] = useState<Activity[] | null>(
    null,
  );
  const [dropped, setDropped] = useState(0);
  const [capturePaused, setCapturePaused] = useState(false);
  const [captureFilters, setCaptureFilters] = useState<Record<string, string>>(
    {},
  );
  const [filters, setFilters] = useState<Filters>(emptyFilters);
  const [tab, setTab] = useState<"State" | "History" | "Activity">("State");
  const [selectedSchema, setSelectedSchema] = useState("");
  const [schemaReady, setSchemaReady] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewRecord, setPreviewRecord] = useState<Activity | null>(null);
  const [previewLoading, setPreviewLoading] = useState<number | null>(null);
  const previewGeneration = useRef(0);
  const leavePreview = useCallback(() => {
    previewGeneration.current += 1;
    setPreview(null);
    setPreviewRecord(null);
    setPreviewLoading(null);
  }, []);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [ambiguous, setAmbiguous] = useState<RequestBatch | null>(null);
  const sessionRef = useRef("");
  const instanceRef = useRef("");
  const streamRef = useRef<ReturnType<typeof stream> | null>(null);
  const gapsRef = useRef<Activity[]>([]);

  const gap = useCallback((reason: string) => {
    const record: Activity = {
      id: crypto.randomUUID(),
      sequence: 0,
      time: Date.now() / 1000,
      category: "gap",
      status: "unavailable",
      client_id: null,
      schemas: [],
      detail: { reason },
    };
    gapsRef.current = [...gapsRef.current, record].slice(-20);
    setActivity((records) => [...records.slice(-479), record]);
    setNotice(reason);
  }, []);

  useEffect(() => {
    let stopped = false;
    let connectionStream: ReturnType<typeof stream> | null = null;
    const receive = (envelope: Envelope) => {
      if (stopped) return;
      if (envelope.kind === "snapshot") {
        const snapshot = envelope.payload as Snapshot;
        if (
          instanceRef.current &&
          instanceRef.current !== envelope.store_instance_id
        ) {
          leavePreview();
          setHistory(null);
          gap("Store restarted. Previous positions and previews are invalid.");
          void read<Info>("/api/info")
            .then(setInfo)
            .catch((cause: unknown) => setError(errorText(cause)));
        }
        instanceRef.current = envelope.store_instance_id;
        const sameSession = sessionRef.current === snapshot.session_id;
        sessionRef.current = snapshot.session_id;
        checkpointRef.current = snapshot.checkpoint;
        setCheckpoint(snapshot.checkpoint);
        setHistory(snapshot.history);
        setActivity((records) => {
          const retained = sameSession ? records : gapsRef.current;
          return [
            ...new Map(
              [...retained, ...snapshot.activity.records].map((record) => [
                record.id,
                record,
              ]),
            ).values(),
          ].slice(-500);
        });
        setDropped(snapshot.activity.dropped);
        setCapturePaused(snapshot.activity.paused);
        setCaptureFilters(snapshot.activity.filters);
      } else if (envelope.kind === "commit") {
        const updated = applyCommit(
          checkpointRef.current,
          envelope.payload as Commit,
        );
        checkpointRef.current = updated;
        setCheckpoint(updated);
      } else if (envelope.kind === "history") {
        const page = envelope.payload as HistoryPage;
        setHistory((previous) =>
          !previous || previous.status.revision <= page.status.revision
            ? page
            : previous,
        );
      } else if (envelope.kind === "capture") {
        const capture = envelope.payload as ActivityPage;
        setCapturePaused(capture.paused);
        setCaptureFilters(capture.filters);
        setDropped(capture.dropped);
      } else if (envelope.kind === "activity") {
        const record = envelope.payload as Activity;
        setActivity((records) => [...records.slice(-499), record]);
      } else if (envelope.kind === "gap") {
        gap(
          "Diagnostic continuity lost. Replacing live State with a fresh checkpoint.",
        );
      } else if (envelope.kind === "error") {
        setError(errorText(envelope.payload));
      }
    };
    void read<Info>("/api/info")
      .then((value) => {
        if (stopped) return;
        setInfo(value);
        const selected = Object.keys(value.schemas)[0];
        setSelectedSchema(selected ?? "");
        setCapturePaused(value.capture.paused);
        setCaptureFilters(value.capture.filters);
        connectionStream = stream(
          receive,
          (live) => {
            if (stopped) return;
            setConnection(live ? "live" : "disconnected");
            if (!live) sessionRef.current = "";
          },
          gap,
          selected ? [selected] : [],
          setSchemaReady,
        );
        streamRef.current = connectionStream;
      })
      .catch((cause: unknown) => !stopped && setError(errorText(cause)));
    return () => {
      stopped = true;
      previewGeneration.current += 1;
      connectionStream?.close();
      streamRef.current = null;
    };
  }, [gap, leavePreview]);

  useEffect(() => {
    if (!selectedSchema) return;
    const schemas = new Set([selectedSchema]);
    if (draft) {
      try {
        for (const action of actionsFromText(draft.text)) {
          if (
            typeof action.schema === "string" &&
            Object.hasOwn(info?.schemas ?? {}, action.schema)
          )
            schemas.add(action.schema);
        }
      } catch {
        // Keep editing incomplete JSON; Apply reports actionable errors.
      }
    }
    streamRef.current?.scope([...schemas]);
  }, [selectedSchema, draft, info]);

  const disabled =
    connection !== "live" ||
    busy ||
    preview !== null ||
    previewLoading !== null ||
    ambiguous !== null;
  const execute = async (
    batch: RequestBatch,
    retry = false,
  ): Promise<ResponseBatch | null> => {
    if (
      !sessionRef.current ||
      preview ||
      previewLoading !== null ||
      busy ||
      (ambiguous && !retry)
    )
      return null;
    setBusy(true);
    setError("");
    try {
      const response = await command(batch, sessionRef.current);
      setAmbiguous(null);
      setNotice(
        "Response received. Canonical State follows the diagnostic stream.",
      );
      return response;
    } catch (cause) {
      if (cause instanceof AmbiguousOutcome) setAmbiguous(batch);
      else if (retry) setAmbiguous(null);
      setError(errorText(cause));
      return null;
    } finally {
      setBusy(false);
    }
  };
  const newDraft = (title: string, actions: Values[]) => {
    if (disabled || !schemaReady) return;
    if (draft) {
      setNotice(
        "An unfinished draft is open. Close it before starting another.",
      );
      return;
    }
    setNotice("");
    setError("");
    setDraft({
      title,
      text: format(actions),
      storeInstanceId: instanceRef.current,
      revisions: { ...checkpoint.schema_revisions },
    });
  };
  const copy = (record: Activity) => {
    const actions = sourceActions(record);
    if (actions) newDraft("Copy original caller actions", actions);
  };
  const navigate = (kind: "undo" | "redo") => {
    const position =
      kind === "undo"
        ? history?.status.undo_position
        : history?.status.redo_position;
    if (!position || disabled) return;
    void execute({
      requests: [
        {
          type: kind,
          transaction_id: crypto.randomUUID(),
          expected_position: { ...position },
        },
      ],
    });
  };
  const setCapture = async (options: {
    paused?: boolean;
    filters?: Record<string, string>;
  }) => {
    try {
      const result = await post<ActivityPage>(
        "/api/capture",
        options,
        sessionRef.current,
      );
      setCapturePaused(result.paused);
      setCaptureFilters(result.filters);
      setDropped(result.dropped);
      setNotice(
        "Shared Activity recording controls updated. State and history remain live.",
      );
    } catch (cause) {
      setError(errorText(cause));
    }
  };
  const loadPreview = async (record: Activity) => {
    const generation = ++previewGeneration.current;
    setPreviewLoading(record.sequence);
    try {
      const result = await read<Preview>(
        `/api/preview?sequence=${record.sequence}`,
        sessionRef.current,
      );
      if (generation !== previewGeneration.current) return;
      setPreview({ ...result, sequence: record.sequence });
      setPreviewRecord(record);
      setTab("State");
    } catch (cause) {
      if (generation === previewGeneration.current) setError(errorText(cause));
    } finally {
      if (generation === previewGeneration.current) setPreviewLoading(null);
    }
  };
  const loadHistory = async (offset: number) => {
    try {
      const page = await read<HistoryPage>(
        `/api/history?limit=100&offset=${offset}`,
        sessionRef.current,
      );
      setHistory((current) =>
        !current || current.status.revision <= page.status.revision
          ? { ...page, offset }
          : current,
      );
    } catch (cause) {
      setError(errorText(cause));
    }
  };
  const unavailableReason =
    history?.status.enabled === false
      ? "History is disabled for this Store."
      : history?.status.barrier
        ? `History boundary: ${String(history.status.barrier)}`
        : !history?.status.can_undo && !history?.status.can_redo
          ? "No retained history to navigate."
          : "Navigation checks current data and the displayed history position.";

  const feedback = (
    <>
      {error && (
        <div className="error-banner" role="alert">
          <pre>{error}</pre>
          <button onClick={() => setError("")}>Dismiss</button>
        </div>
      )}
      {ambiguous && (
        <div className="notice ambiguous">
          <strong>Unresolved command</strong>
          <p>
            The same ID can return a cached result while this live Host retains
            it. A restart or evicted result can leave the outcome unresolved.
            No automatic resubmission occurs.
          </p>
          <button
            disabled={connection !== "live" || busy || preview !== null}
            onClick={() => void execute(ambiguous, true)}
          >
            Retry same command ID
          </button>
          <button
            disabled={busy}
            onClick={() => {
              setAmbiguous(null);
              setNotice(
                "Unresolved command dismissed. Inspect canonical State before new work.",
              );
            }}
          >
            Dismiss unresolved command
          </button>
        </div>
      )}
      {notice && (
        <div className="notice status-notice" role="status">
          {notice}
          <button aria-label="Dismiss status" onClick={() => setNotice("")}>
            ×
          </button>
        </div>
      )}
    </>
  );

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-icon">k</span>
          <strong>kivyfn</strong>
          <span className="brand-divider">/</span>
          <span>Store devtools</span>
        </div>
        <div className={`connection ${connection}`} role="status">
          <span className="status-dot" />
          {connection === "live"
            ? "Live"
            : connection === "connecting"
              ? "Connecting"
              : "Disconnected · reconnecting"}
        </div>
      </header>
      <div className="navigation">
        <nav aria-label="Inspector views">
          {(["State", "History", "Activity"] as const).map((name) => (
            <button
              key={name}
              className={tab === name ? "active" : ""}
              onClick={() => setTab(name)}
            >
              {name}
            </button>
          ))}
        </nav>
        <div className="toolbar history-controls">
          <button
            disabled={disabled || !history?.status.can_undo}
            title={unavailableReason}
            onClick={() => navigate("undo")}
          >
            ↶ Undo last
          </button>
          <button
            disabled={disabled || !history?.status.can_redo}
            title={unavailableReason}
            onClick={() => navigate("redo")}
          >
            ↷ Redo next
          </button>
        </div>
      </div>
      <div className="context-line">
        <span>
          {Object.keys(info?.schemas ?? {}).length} schemas ·{" "}
          {unavailableReason}
        </span>
        <span title={instanceRef.current}>
          Store {instanceRef.current.slice(0, 12) || "—"}
        </span>
      </div>
      {(preview || previewLoading !== null) && (
        <div className="preview-banner" role="status">
          <div>
            <strong>
              {previewLoading !== null
                ? `Loading historical preview · #${previewLoading}`
                : `Historical preview · #${preview?.sequence}`}
            </strong>
            <span>
              {previewLoading !== null
                ? "Waiting for captured values. Return to live cancels this preview."
                : preview?.available
                  ? "Captured values only. Live updates continue separately. Commands are disabled."
                  : `Unavailable: ${preview?.reason || "Captured continuity is incomplete."}`}
            </span>
          </div>
          <button onClick={leavePreview}>Return to live</button>
        </div>
      )}
      {!draft && feedback}
      <main>
        {tab === "State" && (
          <StateView
            schemas={Object.values(info?.schemas ?? {})}
            checkpoint={
              preview ? (preview.checkpoint ?? emptyCheckpoint) : checkpoint
            }
            selected={selectedSchema}
            disabled={disabled}
            loading={!schemaReady}
            connected={connection === "live"}
            preview={preview !== null}
            onSelect={(schema) => {
              if (schema === selectedSchema) return;
              leavePreview();
              setSchemaReady(false);
              setSelectedSchema(schema);
            }}
            onDraft={newDraft}
            onQuery={execute}
          />
        )}
        {tab === "History" && (
          <HistoryView
            page={history}
            activity={activity}
            disabled={disabled || !schemaReady}
            onCopy={copy}
            onPage={(offset) => void loadHistory(offset)}
          />
        )}
        {tab === "Activity" && (
          <ActivityView
            records={frozenActivity ?? activity}
            paused={frozenActivity !== null}
            capturePaused={capturePaused}
            disabled={disabled || !schemaReady}
            filters={filters}
            schemas={Object.keys(info?.schemas ?? {})}
            dropped={dropped}
            onFilters={setFilters}
            onPause={() =>
              setFrozenActivity(frozenActivity ? null : [...activity])
            }
            onCapturePause={() => void setCapture({ paused: !capturePaused })}
            onCaptureFilter={() =>
              void setCapture({
                filters: Object.fromEntries(
                  Object.entries(filters).filter(([, value]) => value !== ""),
                ),
              })
            }
            onCopy={copy}
            onQuery={(query) =>
              void execute({ requests: [{ type: "query", query }] })
            }
            onPreview={(record) => void loadPreview(record)}
          />
        )}
        {previewRecord && (
          <section className="captured-record">
            <details>
              <summary>Original captured Activity / query response</summary>
              <Inspector value={previewRecord.detail} />
            </details>
          </section>
        )}
        {draft && (
          <DraftEditor
            draft={draft}
            checkpoint={checkpoint}
            storeInstanceId={instanceRef.current}
            disabled={disabled || !schemaReady}
            onChange={setDraft}
            onSubmit={execute}
          >
            {feedback}
          </DraftEditor>
        )}
      </main>
      <footer>
        <span>Canonical Store · local diagnostics</span>
        <span>
          {capturePaused
            ? "Activity recording paused"
            : "Activity recording on"}
          {Object.values(captureFilters).some(Boolean) ? " · filtered" : ""} ·
          history independent
        </span>
      </footer>
    </div>
  );
}
