import { io } from "socket.io-client";
import type { Socket } from "socket.io-client";
import {
  connectionError,
  notSent,
  ProtocolError,
  StoreError,
} from "./errors.js";
import {
  deferred,
  integer,
  jsonBytes,
  jsonCopy,
  limit,
  object,
  positive,
  randomId,
  text,
} from "./internal.js";
import type { Deferred, JsonObject } from "./internal.js";
import { PROTOCOL_VERSION, RELAY_VERSION } from "./protocol.js";
import type { RequestBatch } from "./protocol.js";

export interface TransportHandlers {
  event(value: unknown): void;
  disconnect(error: StoreError): void;
}
export interface ConnectionInfo {
  transactionPrefix: string;
}
export interface ClientTransport {
  connect(
    handlers: TransportHandlers,
    signal?: AbortSignal,
  ): Promise<ConnectionInfo>;
  request(batch: RequestBatch): Promise<unknown>;
  disconnect(): Promise<void>;
}
export type AuthProvider =
  JsonObject | (() => JsonObject | Promise<JsonObject>);
export interface SocketIOOptions {
  /** HTTP(S) origin plus Socket.IO path, e.g. https://example.test/relay/socket.io. */
  endpoint: string;
  auth?: AuthProvider;
  connectTimeout?: number;
  requestTimeout?: number;
  maxFrameBytes?: number;
  withCredentials?: boolean;
}
export interface RelayOptions extends SocketIOOptions {
  storeId: string;
}

// Both transports disable Socket.IO reconnection and packet retries. The client
// owns recovery, creates a fresh session, and never replays pending mutations.
abstract class SocketTransport implements ClientTransport {
  protected socket: Socket | null = null;
  protected handlers: TransportHandlers | null = null;
  protected ready = false;
  protected pending = new Set<Deferred<unknown>>();
  protected opening: Deferred<ConnectionInfo> | null = null;
  protected readonly requestTimeout: number;
  protected readonly frameLimit: number;
  private epoch = 0;
  private cleanupOpening: (() => void) | null = null;
  private readonly url: URL;
  private readonly connectTimeout: number;

  constructor(protected readonly options: SocketIOOptions) {
    this.url = new URL(options.endpoint);
    if (
      !["http:", "https:"].includes(this.url.protocol) ||
      this.url.username ||
      this.url.password ||
      this.url.search ||
      this.url.hash
    ) {
      throw new Error(
        "Socket.IO endpoint must be an HTTP(S) URL without credentials, query, or fragment",
      );
    }
    this.connectTimeout = positive(
      options.connectTimeout ?? 10000,
      "connectTimeout",
    );
    this.requestTimeout = positive(
      options.requestTimeout ?? 10000,
      "requestTimeout",
    );
    this.frameLimit = limit(
      options.maxFrameBytes ?? 1024 * 1024,
      "maxFrameBytes",
    );
  }
  protected abstract authentication(credentials: JsonObject): JsonObject;
  protected abstract install(socket: Socket): void;
  protected abstract connected(): void;
  abstract request(batch: RequestBatch): Promise<unknown>;

  async connect(
    handlers: TransportHandlers,
    signal?: AbortSignal,
  ): Promise<ConnectionInfo> {
    await this.disconnect();
    const epoch = ++this.epoch;
    this.handlers = handlers;
    const opening = deferred<ConnectionInfo>();
    this.opening = opening;
    const abort = () =>
      this.fail(connectionError("Store connection was cancelled"));
    const timer = setTimeout(
      () => this.fail(connectionError("Store connection timed out")),
      this.connectTimeout,
    );
    signal?.addEventListener("abort", abort, { once: true });
    this.cleanupOpening = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    };
    if (signal?.aborted) abort();
    else {
      void (async () => {
        try {
          const credentials = jsonCopy(
            typeof this.options.auth === "function"
              ? await this.options.auth()
              : (this.options.auth ?? {}),
          );
          if (epoch !== this.epoch) return;
          object(credentials);
          const socket = io(this.url.origin, {
            path:
              this.url.pathname === "/"
                ? "/socket.io"
                : this.url.pathname.replace(/\/$/, ""),
            auth: this.authentication(credentials),
            autoConnect: false,
            reconnection: false,
            forceNew: true,
            retries: 0,
            timeout: this.connectTimeout,
            withCredentials: this.options.withCredentials ?? true,
          });
          this.socket = socket;
          socket.on("connect_error", () =>
            this.fail(connectionError("Socket.IO connection was refused")),
          );
          socket.on("disconnect", () => this.fail(connectionError()));
          socket.on("connect", () => {
            try {
              this.connected();
            } catch (error) {
              this.fail(this.error(error));
            }
          });
          this.install(socket);
          socket.connect();
        } catch (error) {
          if (epoch === this.epoch) this.fail(this.error(error));
        }
      })();
    }
    return opening.promise;
  }
  protected error(error: unknown): StoreError {
    return error instanceof StoreError
      ? error
      : connectionError("Socket.IO transport failed");
  }
  protected opened(prefix: string): void {
    this.ready = true;
    this.cleanupOpening?.();
    this.cleanupOpening = null;
    this.opening?.resolve({ transactionPrefix: prefix });
    this.opening = null;
  }
  protected validateFrame(value: unknown): void {
    if (jsonBytes(jsonCopy(value)) > this.frameLimit)
      throw new ProtocolError("Store frame exceeds maxFrameBytes");
  }
  protected fail(error: StoreError, notify = true): void {
    ++this.epoch;
    this.ready = false;
    this.cleanupOpening?.();
    this.cleanupOpening = null;
    this.opening?.reject(error);
    this.opening = null;
    for (const pending of this.pending) pending.reject(error);
    this.pending.clear();
    const socket = this.socket;
    this.socket = null;
    socket?.removeAllListeners();
    socket?.disconnect();
    const handlers = this.handlers;
    this.handlers = null;
    if (notify) handlers?.disconnect(error);
  }
  async disconnect(): Promise<void> {
    this.fail(connectionError("Store transport closed"), false);
  }
  protected async receipt(event: string, value: unknown): Promise<unknown> {
    const socket = this.socket;
    if (!socket?.connected) throw notSent("Store transport is offline");
    try {
      this.validateFrame(value);
    } catch (error) {
      throw notSent(
        error instanceof Error ? error.message : "Invalid outgoing frame",
      );
    }
    const pending = deferred<unknown>();
    this.pending.add(pending);
    socket
      .timeout(this.requestTimeout)
      .emit(event, value, (error: Error | null, response: unknown) => {
        if (socket !== this.socket) return;
        if (error)
          pending.reject(connectionError("Store request receipt timed out"));
        else pending.resolve(response);
      });
    try {
      return await pending.promise;
    } finally {
      this.pending.delete(pending);
    }
  }
}

/** Connects directly to the existing Python Socket.IO StoreHost. */
export class DirectSocketIOTransport extends SocketTransport {
  protected authentication(credentials: JsonObject): JsonObject {
    return { ...credentials, protocol_version: PROTOCOL_VERSION };
  }
  protected install(socket: Socket): void {
    socket.on("message", (value: unknown) => {
      try {
        this.validateFrame(value);
        this.handlers?.event(value);
      } catch (error) {
        this.fail(this.error(error));
      }
    });
  }
  protected connected(): void {
    this.opened(`js:${randomId()}:`);
  }
  async request(batch: RequestBatch): Promise<unknown> {
    if (!this.ready) throw notSent("Store transport is offline");
    const response = await this.receipt("message", batch);
    this.validateFrame(response);
    return response;
  }
}

/** Selected-store connection through the relay contract in docs/client-relay.md. */
export class RelayTransport extends SocketTransport {
  readonly storeId: string;
  private generation: string | null = null;
  private channel = 0;
  private incoming = 0;
  private outgoing = 1;
  private accepted = false;
  private prefix: string | null = null;
  private responses = new Map<number, Deferred<unknown>>();

  constructor(options: RelayOptions) {
    super(options);
    this.storeId = text(options.storeId);
  }
  protected authentication(credentials: JsonObject): JsonObject {
    return {
      role: "user",
      store_id: this.storeId,
      protocol_version: PROTOCOL_VERSION,
      relay_version: RELAY_VERSION,
      credentials,
    };
  }
  protected install(socket: Socket): void {
    this.generation = null;
    this.channel = 0;
    this.incoming = 0;
    this.outgoing = 1;
    this.accepted = false;
    this.prefix = null;
    this.responses.clear();
    socket.on("store_relay", (value: unknown) => {
      try {
        this.receive(value);
      } catch (error) {
        this.fail(this.error(error));
      }
    });
  }
  protected connected(): void {
    const socket = this.socket;
    void this.receipt("store_attach", {
      store_id: this.storeId,
      protocol_version: PROTOCOL_VERSION,
      relay_version: RELAY_VERSION,
    })
      .then((value) => {
        if (socket !== this.socket) return;
        if (object(value).accepted !== true)
          throw connectionError("Gateway refused the requested store");
        this.accepted = true;
        if (this.prefix) this.opened(this.prefix);
      })
      .catch((error) => {
        if (socket === this.socket) this.fail(this.error(error));
      });
  }
  private receive(value: unknown): void {
    this.validateFrame(value);
    const frame = object(value);
    const generation = text(frame.generation);
    const channel = integer(frame.channel_id);
    if (channel === 0) throw new ProtocolError("Invalid relay channel");
    const sequence = integer(frame.sequence);
    if (sequence !== this.incoming)
      throw new ProtocolError("Relay output sequence gap");
    const type = text(frame.type);
    if (this.generation === null) {
      if (type !== "opened" || sequence !== 0)
        throw new ProtocolError("Expected relay channel opening");
      const payload = object(frame.payload);
      if (payload.protocol_version !== PROTOCOL_VERSION)
        throw new ProtocolError("Store protocol version mismatch");
      const prefix = text(payload.transaction_prefix);
      if (prefix !== `relay:${generation}:${channel}:`)
        throw new ProtocolError("Invalid relay transaction prefix");
      this.generation = generation;
      this.channel = channel;
      this.prefix = prefix;
    } else {
      if (generation !== this.generation || channel !== this.channel)
        throw new ProtocolError("Relay route changed");
      if (type === "event") this.handlers?.event(frame.payload);
      else if (type === "response") {
        const request = integer(frame.request_sequence);
        const pending = this.responses.get(request);
        if (!pending) throw new ProtocolError("Unexpected relay response");
        this.responses.delete(request);
        pending.resolve(frame.payload);
      } else if (type !== "closed")
        throw new ProtocolError("Unknown relay frame type");
    }
    ++this.incoming;
    this.socket?.emit("store_relay_ack", {
      generation,
      channel_id: channel,
      sequence,
      accepted: true,
    });
    if (type === "closed")
      this.fail(connectionError("Store relay channel closed"));
    else if (type === "opened" && this.accepted) this.opened(this.prefix!);
  }
  async request(batch: RequestBatch): Promise<unknown> {
    if (!this.ready || !this.socket?.connected)
      throw notSent("Store relay is offline");
    const sequence = this.outgoing++;
    const frame = {
      generation: this.generation,
      channel_id: this.channel,
      sequence,
      type: "request",
      payload: batch,
    };
    try {
      this.validateFrame(frame);
    } catch (error) {
      throw notSent(
        error instanceof Error ? error.message : "Invalid outgoing frame",
      );
    }
    const pending = deferred<unknown>();
    this.pending.add(pending);
    this.responses.set(sequence, pending);
    const socket = this.socket;
    const timer = setTimeout(() => {
      if (socket === this.socket)
        this.fail(
          connectionError("Controller response timed out; outcome is unknown"),
        );
    }, this.requestTimeout);
    // The callback acknowledges gateway receipt only. It cannot confirm writes.
    void this.receipt("store_relay", frame)
      .then((value) => {
        if (object(value).accepted !== true)
          throw connectionError("Gateway rejected a relay frame");
      })
      .catch((error) => {
        if (socket === this.socket) this.fail(this.error(error));
      });
    try {
      return await pending.promise;
    } finally {
      clearTimeout(timer);
      this.pending.delete(pending);
      if (this.responses.get(sequence) === pending)
        this.responses.delete(sequence);
    }
  }
  override async disconnect(): Promise<void> {
    if (this.ready && this.socket?.connected) {
      this.socket.emit("store_relay", {
        generation: this.generation,
        channel_id: this.channel,
        sequence: this.outgoing++,
        type: "close",
        payload: null,
      });
    }
    await super.disconnect();
    this.responses.clear();
  }
}
