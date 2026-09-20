export type Json =
  | null
  | boolean
  | number
  | string
  | Json[]
  | { [key: string]: Json };
export type Values = Record<string, Json>;

export interface SchemaInfo {
  name: string;
  kind: string;
  persistent: boolean;
  track_history: boolean;
  fields: Record<string, Json>;
  signals: Record<string, { undo?: string | null }>;
}

export interface Info {
  protocol_version: number;
  store_instance_id: string;
  schemas: Record<string, SchemaInfo>;
  capture: ActivityPage;
}

export interface Position {
  store_instance_id: string;
  revision: number;
  entry_id: string | null;
}

export interface HistoryStatus {
  store_instance_id: string;
  revision: number;
  can_undo: boolean;
  can_redo: boolean;
  undo_position: Position | null;
  redo_position: Position | null;
  barrier: Json;
  retained_bytes: number;
  total_entries: number;
  cursor: number;
  enabled?: boolean;
  oldest_entry_id?: string | null;
}

export interface HistoryEntry {
  id: string;
  transaction_ids: string[];
  origin_client_id: string | null;
  label: string | null;
  group: string | null;
  records: Json[];
  signals: Json[];
  byte_size: number;
  [key: string]: Json;
}

export interface HistoryPage {
  status: HistoryStatus;
  entries: HistoryEntry[];
  offset?: number;
  limit?: number;
}

export interface Checkpoint {
  schemas: Record<string, Values>;
  schema_revisions: Record<string, number>;
  history?: HistoryStatus;
}

export interface Activity {
  id: string;
  sequence: number;
  time: number | string;
  category: string;
  status: string;
  client_id: string | null;
  capture_id?: string | null;
  transaction_id?: string | null;
  schemas: string[];
  detail: Values;
  truncated?: boolean;
}

export interface CommitEvent {
  type: "set" | "setrow" | "insert" | "remove";
  schema: string;
  key?: string;
  id?: string;
  old?: Json;
  new?: Json;
}

export interface Commit {
  transaction_id: string;
  sequence: number;
  kind: string;
  events: CommitEvent[];
  signals: Json[];
  schema_revisions: Record<string, number>;
}

export interface Envelope {
  version: number;
  epoch: string;
  sequence: number;
  store_instance_id: string;
  kind: string;
  payload: unknown;
}

export interface Snapshot {
  session_id: string;
  checkpoint: Checkpoint;
  activity: ActivityPage;
  history: HistoryPage;
  capture?: Info["capture"];
}

export interface ActivityPage {
  records: Activity[];
  paused: boolean;
  filters: Record<string, string>;
  dropped: number;
  sequence: number;
  oldest_preview_sequence: number;
  preview_available: boolean;
  retained_bytes: number;
}

export interface Preview {
  available: boolean;
  reason?: string;
  checkpoint?: Checkpoint;
  sequence: number;
  scope?: string[];
}

export interface RequestBatch {
  requests: Values[];
}

export interface ResponseBatch {
  responses: Values[];
}

export interface Draft {
  title: string;
  text: string;
  originalText: string;
  storeInstanceId: string;
  revisions: Record<string, number>;
  sourceTransaction?: string;
  form?: import("./draft").EditForm;
  completed?: boolean;
  historyLabel?: string;
  historyGroup?: string;
}

export type Connection = "connecting" | "live" | "disconnected";

export interface Filters {
  client_id: string;
  schema: string;
  category: string;
  status: string;
  identity: string;
  row?: string;
  field?: string;
}
