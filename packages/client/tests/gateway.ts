/** Test peer for docs/client-relay.md; deliberately not an application gateway. */
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { Server } from "socket.io";
import type { Socket } from "socket.io";

interface Frame {
  generation: string;
  channel_id: number;
  sequence: number;
  type: string;
  request_sequence?: number;
  payload: unknown;
}
interface Channel {
  user: Socket;
  nextInput: number;
  nextOutput: number;
  closed: boolean;
}
interface Host {
  socket: Socket;
  generation: string;
  nextChannel: number;
  channels: Map<number, Channel>;
}

export class Gateway {
  readonly server = createServer();
  readonly io = new Server(this.server, {
    path: "/relay/socket.io",
    maxHttpBufferSize: 1024 * 1024,
  });
  endpoint = "";
  host: Host | null = null;
  readonly frames: Frame[] = [];
  readonly credentials: unknown[] = [];
  readonly userAcks: unknown[] = [];
  holdResponses = false;
  dropResponses = false;
  held: { user: Socket; frame: Frame }[] = [];

  constructor() {
    this.io.use((socket, next) => {
      const auth = socket.handshake.auth;
      this.credentials.push(auth);
      const expected = auth.role === "store" ? "test-store" : "test-user";
      if (
        auth.credentials?.token !== expected ||
        auth.protocol_version !== 3 ||
        auth.relay_version !== 1
      ) {
        next(new Error("unauthorized"));
      } else next();
    });
    this.io.on("connection", (socket) => {
      if (socket.handshake.auth.role === "store") this.store(socket);
      else this.user(socket);
    });
  }
  async start() {
    await new Promise<void>((resolve) =>
      this.server.listen(0, "127.0.0.1", resolve),
    );
    this.endpoint = `http://127.0.0.1:${(this.server.address() as AddressInfo).port}/relay/socket.io`;
  }
  private store(socket: Socket) {
    socket.on("store_register", (value, ack) => {
      if (
        value.store_id !== "unit-1" ||
        value.generation !== socket.handshake.auth.generation ||
        value.protocol_version !== 3 ||
        value.relay_version !== 1
      ) {
        ack({ accepted: false });
        return;
      }
      this.host?.socket.disconnect(true);
      this.host = {
        socket,
        generation: value.generation,
        nextChannel: 1,
        channels: new Map(),
      };
      ack({ ...value, accepted: true });
    });
    socket.on("store_relay", (frame: Frame) => {
      const host = this.host;
      const channel = host?.channels.get(frame.channel_id);
      const valid =
        host?.socket === socket &&
        frame.generation === host.generation &&
        !!channel &&
        frame.sequence === channel.nextOutput;
      if (valid) {
        ++channel!.nextOutput;
        this.frames.push(frame);
        // The physical host is acknowledged after enqueue, independently of
        // browser application progress. Production must bound that FIFO.
        if (frame.type === "response" && this.dropResponses)
          channel!.user.disconnect(true);
        else if (
          (frame.type === "response" && this.holdResponses) ||
          this.held.some((entry) => entry.user === channel!.user)
        ) {
          this.held.push({ user: channel!.user, frame });
        } else if (channel!.user.connected)
          channel!.user.emit("store_relay", frame);
        if (frame.type === "closed") {
          channel!.closed = true;
          host!.channels.delete(frame.channel_id);
        }
      }
      socket.emit("store_relay_ack", {
        generation: frame.generation,
        channel_id: frame.channel_id,
        sequence: frame.sequence,
        accepted: valid,
      });
    });
    socket.on("disconnect", () => {
      if (this.host?.socket !== socket) return;
      for (const channel of this.host.channels.values())
        channel.user.disconnect(true);
      this.host = null;
    });
  }
  private user(socket: Socket) {
    let route: { host: Host; id: number; channel: Channel } | null = null;
    socket.on("store_attach", (value, ack) => {
      const host = this.host;
      if (
        route ||
        !host ||
        value.store_id !== "unit-1" ||
        socket.handshake.auth.store_id !== value.store_id ||
        value.protocol_version !== 3 ||
        value.relay_version !== 1
      ) {
        ack({ accepted: false });
        return;
      }
      const id = host.nextChannel++;
      const channel: Channel = {
        user: socket,
        nextInput: 1,
        nextOutput: 0,
        closed: false,
      };
      route = { host, id, channel };
      host.channels.set(id, channel);
      host.socket.timeout(2000).emit(
        "store_relay",
        {
          generation: host.generation,
          channel_id: id,
          sequence: 0,
          type: "open",
          payload: { protocol_version: 3 },
        },
        (error: Error | null, receipt: { accepted: boolean }) =>
          ack({ accepted: !error && receipt?.accepted === true }),
      );
    });
    socket.on(
      "store_relay",
      (frame: Frame, ack?: (value: unknown) => void) => {
        if (
          !route ||
          this.host !== route.host ||
          frame.generation !== route.host.generation ||
          frame.channel_id !== route.id ||
          frame.sequence !== route.channel.nextInput ||
          route.channel.closed ||
          !["request", "close"].includes(frame.type)
        ) {
          ack?.({ accepted: false });
          return;
        }
        ++route.channel.nextInput;
        if (frame.type === "close") route.channel.closed = true;
        route.host.socket
          .timeout(2000)
          .emit(
            "store_relay",
            frame,
            (error: Error | null, receipt: { accepted: boolean }) =>
              ack?.({ accepted: !error && receipt?.accepted === true }),
          );
      },
    );
    socket.on("store_relay_ack", (value) => this.userAcks.push(value));
    socket.on("disconnect", () => {
      if (!route || route.channel.closed || !route.host.socket.connected)
        return;
      route.channel.closed = true;
      route.host.socket.emit("store_relay", {
        generation: route.host.generation,
        channel_id: route.id,
        sequence: route.channel.nextInput++,
        type: "close",
        payload: null,
      });
    });
  }
  release() {
    this.holdResponses = false;
    for (const { user, frame } of this.held.splice(0))
      if (user.connected) user.emit("store_relay", frame);
  }
  async stop() {
    await new Promise<void>((resolve) => this.io.close(() => resolve()));
  }
}
