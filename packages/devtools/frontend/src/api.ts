import type { Envelope, RequestBatch, ResponseBatch, Snapshot } from "./types";

export class AmbiguousOutcome extends Error {
  constructor(message?: string) {
    super(
      message ??
        "The response was lost. Work may have committed. " +
          "Retry the same command ID on this live Store to check its result.",
    );
  }
}

export async function read<T>(path: string, session = ""): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: session ? { "X-DevTools-Session": session } : {},
  });
  const value: unknown = await response.json();
  if (!response.ok) throw new Error(errorText(value));
  return value as T;
}

export function errorText(value: unknown): string {
  if (value instanceof Error) return value.message;
  if (value !== null && typeof value === "object" && "error" in value) {
    return String(value.error);
  }
  return typeof value === "string" ? value : JSON.stringify(value);
}

export async function post<T>(
  path: string,
  value: unknown,
  session: string,
  mutation = false,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-DevTools-Session": session,
      },
      body: JSON.stringify(value),
    });
  } catch (error) {
    throw mutation ? new AmbiguousOutcome() : error;
  }
  let result: unknown;
  try {
    result = await response.json();
  } catch (error) {
    throw mutation ? new AmbiguousOutcome() : error;
  }
  if (!response.ok) {
    const unresolved =
      result !== null &&
      typeof result === "object" &&
      "outcome" in result &&
      result.outcome === "unknown";
    if (mutation && (unresolved || response.status >= 500)) {
      throw new AmbiguousOutcome(errorText(result));
    }
    throw new Error(errorText(result));
  }
  return result as T;
}

export async function command(
  batch: RequestBatch,
  session: string,
): Promise<ResponseBatch> {
  const response = await post<ResponseBatch>(
    "/api/commands",
    batch,
    session,
    batch.requests.some((item) => item.type !== "query"),
  );
  const failure = response.responses.find(
    (item) => item.type === "failure" || item.type === "transaction_failure",
  );
  if (failure) throw new Error(JSON.stringify(failure.failure, null, 2));
  return response;
}

export function stream(
  onEnvelope: (envelope: Envelope) => void,
  onState: (connected: boolean) => void,
  onError: (message: string) => void,
  initialScope: string[] = [],
  onScopeReady: (ready: boolean) => void = () => undefined,
): { close: () => void; scope: (schemas: string[]) => void } {
  let socket: WebSocket | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;
  let epoch = "";
  let sequence = -1;
  let scope = initialScope;
  let requestedScope = initialScope;
  let awaitingSnapshot = true;
  const requestScope = () => {
    if (awaitingSnapshot || socket?.readyState !== WebSocket.OPEN) return;
    if (
      scope.length === requestedScope.length &&
      scope.every((schema, index) => schema === requestedScope[index])
    ) {
      onScopeReady(true);
      return;
    }
    requestedScope = [...scope];
    awaitingSnapshot = true;
    socket.send(JSON.stringify({ kind: "scope", schemas: requestedScope }));
  };
  const connect = () => {
    if (stopped) return;
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const query = new URLSearchParams({ schemas: scope.join(",") });
    requestedScope = [...scope];
    awaitingSnapshot = true;
    const current = new WebSocket(
      `${protocol}//${location.host}/stream?${query}`,
    );
    socket = current;
    current.onmessage = (message: MessageEvent<string>) => {
      if (stopped || current !== socket) return;
      try {
        const envelope = JSON.parse(message.data) as Envelope;
        if (envelope.version !== 2) {
          throw new Error("Unsupported diagnostic protocol version.");
        }
        if (envelope.kind === "snapshot") {
          epoch = envelope.epoch;
          sequence = envelope.sequence;
          onEnvelope(envelope);
          onState(true);
          const loaded = Object.keys(
            (envelope.payload as Snapshot).checkpoint.schemas,
          );
          if (
            loaded.length === requestedScope.length &&
            requestedScope.every((schema) => loaded.includes(schema))
          ) {
            awaitingSnapshot = false;
            requestScope();
          }
          return;
        }
        if (envelope.epoch !== epoch) return;
        if (envelope.sequence <= sequence) return;
        if (envelope.sequence !== sequence + 1) {
          throw new Error(
            "Diagnostic stream gap. Reconnecting for a snapshot.",
          );
        }
        sequence = envelope.sequence;
        onEnvelope(envelope);
      } catch (error) {
        onError(errorText(error));
        current.close();
      }
    };
    current.onclose = () => {
      if (stopped || current !== socket) return;
      awaitingSnapshot = true;
      onScopeReady(false);
      onState(false);
      if (!stopped) timer = setTimeout(connect, 800);
    };
    current.onerror = () => current.close();
  };
  connect();
  return {
    close: () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      socket?.close();
    },
    scope: (schemas) => {
      scope = [...schemas];
      onScopeReady(false);
      requestScope();
    },
  };
}
