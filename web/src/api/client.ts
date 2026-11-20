/**
 * The console's two doors, kept deliberately apart.
 *
 * `/admin/api/*` authenticates with the httpOnly admin session cookie (`credentials: "include"`),
 * so the token never reaches JavaScript. `/api/v1/*` is the data plane: it is called with a
 * `Bearer okg_…` key, which is what an application would do — so what the console shows is what a
 * scoped credential can actually see. With no key stored the data plane falls back to the admin
 * session, and the UI says so.
 */

export interface ErrorEnvelope {
  code: string;
  message: string;
  retryable: boolean;
  details: Record<string, unknown>;
  request_id?: string;
  field?: string;
  value?: unknown;
  grafx_code?: string;
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly retryable: boolean;
  readonly details: Record<string, unknown>;
  readonly requestId?: string;
  readonly field?: string;

  constructor(
    status: number,
    envelope: Partial<ErrorEnvelope> | undefined,
    fallbackMessage = "The server refused this request.",
  ) {
    super(envelope?.message ?? fallbackMessage);
    this.name = "ApiError";
    this.status = status;
    this.code = envelope?.code ?? (status === 401 ? "unauthenticated" : "unknown_error");
    this.retryable = Boolean(envelope?.retryable);
    this.details = envelope?.details ?? {};
    this.requestId = envelope?.request_id;
    this.field = envelope?.field;
  }

  /** A refusal the operator can act on: the code plus the server's own sentence. */
  get summary(): string {
    const id = this.requestId ? ` · ${this.requestId}` : "";
    return `${this.code} (${this.status})${id}`;
  }
}

const KEY_STORAGE = "okg.dataKey";

export const dataKey = {
  get(): string {
    return window.localStorage.getItem(KEY_STORAGE) ?? "";
  },
  set(value: string): void {
    if (value) window.localStorage.setItem(KEY_STORAGE, value.trim());
    else window.localStorage.removeItem(KEY_STORAGE);
  },
};

interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  signal?: AbortSignal;
  /** Send the admin session cookie. Off for the data plane unless no key is stored. */
  credentials?: RequestCredentials;
  headers?: Record<string, string>;
}

function endpoint(
  base: string,
  path: string,
  query?: RequestOptions["query"],
): string {
  const url = new URL(base + path, window.location.origin);
  for (const [name, raw] of Object.entries(query ?? {})) {
    if (raw === undefined || raw === null || raw === "") continue;
    url.searchParams.set(name, String(raw));
  }
  return url.pathname + url.search;
}

async function call<T>(base: string, path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...options.headers };
  const init: RequestInit = {
    method: options.method ?? "GET",
    headers,
    credentials: options.credentials ?? "include",
    signal: options.signal,
  };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  if (options.headers?.Authorization) headers.Authorization = options.headers.Authorization;

  const response = await fetch(endpoint(base, path, options.query), init);
  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { code: "invalid_response", message: text.slice(0, 400) };
    }
  }
  if (!response.ok) {
    throw new ApiError(response.status, (payload as { error?: ErrorEnvelope })?.error, text.slice(0, 200));
  }
  return payload as T;
}

export const ADMIN_BASE = "/admin/api";
export const DATA_BASE = "/api/v1";

/** Admin door: session cookie only. */
export const admin = {
  login: (token: string) =>
    call<LoginResult>(ADMIN_BASE, "/login", { method: "POST", body: { token } }),
  logout: () => call<{ signed_in: false }>(ADMIN_BASE, "/logout", { method: "POST" }),
  session: () => call<SessionInfo>(ADMIN_BASE, "/session", { credentials: "same-origin" }),
  settings: () => call<Settings>(ADMIN_BASE, "/settings"),
  status: () => call<ServerStatus>(ADMIN_BASE, "/status"),
  roots: () => call<Roots>(ADMIN_BASE, "/roots"),
  rotateAdminToken: () =>
    call<{ admin_token: string; shown_once: boolean; note: string }>(ADMIN_BASE, "/admin-token/rotate", {
      method: "POST",
    }),

  databases: () => call<{ databases: DatabaseRecord[] }>(ADMIN_BASE, "/databases"),
  database: (slug: string) => call<DatabaseRecord>(ADMIN_BASE, `/databases/${slug}`),
  inspect: (path: string) => call<InspectResult>(ADMIN_BASE, "/databases/inspect", {
    method: "POST",
    body: { path },
  }),
  attach: (body: Record<string, unknown>) =>
    call<DatabaseRecord>(ADMIN_BASE, "/databases", { method: "POST", body }),
  patchDatabase: (slug: string, body: Record<string, unknown>) =>
    call<DatabaseRecord>(ADMIN_BASE, `/databases/${slug}`, { method: "PATCH", body }),
  removeDatabase: (slug: string) =>
    call<{ removed: string; path_untouched: string }>(ADMIN_BASE, `/databases/${slug}`, {
      method: "DELETE",
    }),
  openDatabase: (slug: string) =>
    call<{ slug: string; open: boolean; [key: string]: unknown }>(
      ADMIN_BASE,
      `/databases/${slug}/open`,
      { method: "POST" },
    ),
  ejectDatabase: (slug: string) =>
    call<{ slug: string; open: boolean; ejected: boolean }>(ADMIN_BASE, `/databases/${slug}/eject`, {
      method: "POST",
    }),
  closeDatabase: (slug: string, force = false) =>
    call<{ slug: string; open: boolean }>(
      ADMIN_BASE,
      `/databases/${slug}/close?force=${force ? "true" : "false"}`,
      { method: "POST" },
    ),
  inventory: (slug: string) => call<Inventory>(ADMIN_BASE, `/databases/${slug}/inventory`),
  schema: (slug: string) => call<SchemaPayload>(ADMIN_BASE, `/schema/${slug}`),
  action: (slug: string, action: string, args: Record<string, unknown> = {}) =>
    call<{ job_id: string; state: string }>(ADMIN_BASE, `/databases/${slug}/actions/${action}`, {
      method: "POST",
      body: { arguments: args },
    }),
  ddl: (slug: string, statements: string[]) =>
    call<DdlResult>(ADMIN_BASE, `/databases/${slug}/ddl`, { method: "POST", body: { statements } }),
  schemaDdl: (slug: string, body: Record<string, unknown>) =>
    call<DdlResult>(ADMIN_BASE, `/schema/${slug}/ddl`, { method: "POST", body }),
  /** The library's one-way catalog door: identity access paths. Needs a writable registration. */
  ensureIdentityIndexes: (slug: string) =>
    call<{ activated: string[]; already_present: string[]; indexes: Inventory }>(
      ADMIN_BASE,
      `/schema/${slug}/identity-indexes`,
      { method: "POST" },
    ),

  jobs: (database?: string, limit = 50) =>
    call<{ jobs: Job[] }>(ADMIN_BASE, "/jobs", { query: { database, limit } }),
  job: (id: string) => call<Job>(ADMIN_BASE, `/jobs/${id}`),

  keys: () => call<{ keys: KeyRecord[] }>(ADMIN_BASE, "/keys"),
  createKey: (body: { name: string; databases: string[]; allow_writes?: boolean }) =>
    call<CreatedKey>(ADMIN_BASE, "/keys", { method: "POST", body }),
  patchKey: (id: number, body: Record<string, unknown>) =>
    call<KeyRecord>(ADMIN_BASE, `/keys/${id}`, { method: "PATCH", body }),
  revokeKey: (id: number) => call<KeyRecord>(ADMIN_BASE, `/keys/${id}/revoke`, { method: "POST" }),
  deleteKey: (id: number) => call<{ removed: number }>(ADMIN_BASE, `/keys/${id}`, { method: "DELETE" }),

  audit: (filters: { actor?: string; database?: string; action?: string; limit?: number } = {}) =>
    call<{ entries: AuditEntry[] }>(ADMIN_BASE, "/audit", { query: { ...filters } }),
};

/** Data door: bearer key when one is stored, otherwise the admin session. */
export const data = {
  usingKey: () => Boolean(dataKey.get()),
  headers: (): Record<string, string> => {
    const key = dataKey.get();
    return key ? { Authorization: `Bearer ${key}` } : {};
  },
  credentials: (): RequestCredentials => (dataKey.get() ? "same-origin" : "include"),

  databases: () =>
    call<{ databases: DataDatabase[]; actor: { kind: string; label: string } }>(
      DATA_BASE,
      "/databases",
      requestOptions(),
    ),
  schema: (slug: string) =>
    call<SchemaPayload>(DATA_BASE, `/databases/${slug}/schema`, requestOptions()),
  indexes: (slug: string) =>
    call<IndexInventory>(DATA_BASE, `/databases/${slug}/indexes`, requestOptions()),
  health: () => call<Health>(DATA_BASE, "/health", requestOptions()),

  query: (slug: string, query: string, options: { parameters?: Record<string, unknown>; limit?: number } = {}) =>
    call<QueryResult>(DATA_BASE, `/databases/${slug}/query`, {
      ...requestOptions(),
      method: "POST",
      body: { query, parameters: options.parameters, limit: options.limit },
    }),
  /** The plan tree is the engine's own operator chain, returned as the response body. */
  explain: (slug: string, query: string) =>
    call<PlanNode>(DATA_BASE, `/databases/${slug}/explain`, {
      ...requestOptions(),
      query: { query },
    }),
  search: {
    text: (slug: string, body: TextSearchBody) =>
      call<TextHits>(DATA_BASE, `/databases/${slug}/search/text`, { ...requestOptions(), method: "POST", body }),
    vector: (slug: string, body: VectorSearchBody) =>
      call<VectorHits>(DATA_BASE, `/databases/${slug}/search/vector`, { ...requestOptions(), method: "POST", body }),
    hybrid: (slug: string, body: HybridSearchBody) =>
      call<HybridHits>(DATA_BASE, `/databases/${slug}/search/hybrid`, { ...requestOptions(), method: "POST", body }),
  },
  graph: (slug: string, body: GraphRequest) =>
    call<GraphPayload>(DATA_BASE, `/databases/${slug}/graph`, { ...requestOptions(), method: "POST", body }),
  /** One hop around one node; `limit` caps how many incident edges are expanded. */
  neighbors: (slug: string, table: string, record_id: number, limit = 100) =>
    call<GraphPayload>(DATA_BASE, `/databases/${slug}/neighbors`, {
      ...requestOptions(),
      query: { table, record_id, limit },
    }),
};

function requestOptions(): RequestOptions {
  return { headers: data.headers(), credentials: data.credentials() };
}

// --------------------------------------------------------------------------------------
// Payload shapes published by the daemon (see /api/v1/openapi.json)
// --------------------------------------------------------------------------------------

export interface SessionInfo {
  signed_in: boolean;
  token_configured: boolean;
  server_version?: string;
}

export interface LoginResult {
  signed_in: boolean;
  expires_in_seconds: number;
}

export interface DatabaseOption {
  name: string;
  /** `enum` is only ever present if the daemon publishes one; today it publishes the other four. */
  type: "integer" | "number" | "boolean" | "string" | "enum";
  default: unknown;
  nullable: boolean;
  group: string;
  label: string;
  description: string;
  minimum?: number;
  maximum?: number;
  choices?: string[];
}

export interface MaintenanceAction {
  action: string;
  description: string;
  read_only_ok: boolean;
  quiescing: boolean;
  confirm_quiescent: boolean;
  requires: string[];
}

export interface Settings {
  server_version: string;
  bind: { host: string; port: number };
  state_dir: string;
  allowed_roots: string[];
  limits: Record<string, number>;
  security: {
    tls: boolean;
    cors_origins: string[];
    metrics_enabled: boolean;
    /** Whether database options may point the store's own OpenMetrics sink at another machine. */
    allow_remote_store_metrics: boolean;
    note: string;
  };
  database_options: DatabaseOption[];
  maintenance_actions: MaintenanceAction[];
}

export interface DatabaseRecord {
  slug: string;
  name: string;
  path: string;
  database_uuid: string;
  format_version: number;
  read_only: boolean;
  allow_writes: boolean;
  options: Record<string, unknown>;
  enabled: boolean;
  /** `lazy` opens on demand and goes idle; `pinned` keeps a handle warm. */
  open_policy: "lazy" | "pinned";
  note: string;
  created_at: string;
  updated_at: string;
  // The list endpoint adds what the handle pool knows right now.
  open?: boolean;
  open_transactions?: number;
  active_operations?: number;
  keys?: { id: number; name: string; allow_writes: boolean }[];
  active_job?: Job | null;
}

export interface DataDatabase {
  slug: string;
  name: string;
  read_only: boolean;
  allow_writes: boolean;
  open: boolean;
  open_transactions: number;
  enabled: boolean;
}

/** What the daemon saw when it looked at a candidate store directory. */
export interface InspectResult {
  path: string;
  bytes: number;
  files: number;
  marker: string;
  already_registered_as: string | null;
  uuid_registered_elsewhere: string | null;
  database_uuid?: string;
  page_size?: number;
  format_version?: number;
  node_tables?: string[];
  relationship_tables?: string[];
  vector_spaces?: string[];
  recovery_required?: boolean;
}

export interface Roots {
  roots: { path: string; exists: boolean; stores: { path: string; name: string }[] }[];
  note?: string;
}

export interface ColumnInfo {
  name: string;
  type: string;
  nullable: boolean;
  vector_space: string | null;
  decimal_precision: number | null;
  decimal_scale: number | null;
  stored_type: string | null;
}

export interface TableInfo {
  table_id: number;
  name: string;
  kind: string;
  columns: ColumnInfo[];
  schema_version: number;
  /** Node tables carry a primary key; relationship tables carry their endpoints. */
  primary_key?: string | null;
  flexible_properties?: boolean;
  unlabeled?: boolean;
  from_table?: string | null;
  to_table?: string | null;
}

export interface VectorSpaceInfo {
  space_id: number;
  name: string;
  dimension: number;
  metric: string;
  normalized: boolean;
  storage_dtype: string;
  state: string;
}

export interface IndexInfo {
  name: string;
  table: string;
  columns: string[];
  /** Classified by the daemon from the index's own key derivation, not from its name. */
  kind: "text" | "vector" | "exact";
  layout: string;
  visibility: string;
  bucket_count: number | null;
  stale: boolean;
  stale_reason: string | null;
  built_through_lsn: number | null;
  missing_targets: number;
  automatic: boolean | null;
  expected_cardinality: number | null;
}

export interface VectorIndexInfo {
  name: string;
  space: string;
  table_id: number;
  dimension: number;
  metric: string;
  storage_dtype: string;
  ef_search: number;
  stale: boolean;
  stale_reason: string | null;
  built_through_lsn: number | null;
}

export interface MaintenanceStatus {
  wal_bytes: number;
  checkpoint_lag_lsn: number;
  recovery_required: boolean;
  stale_indexes: string[];
  heap_bloat_bytes: number | null;
  oldest_reader_age: number | null;
}

export interface SchemaPayload {
  slug?: string;
  database_uuid: string;
  read_only: boolean;
  tables: TableInfo[];
  relationships: TableInfo[];
  vector_spaces: VectorSpaceInfo[];
}

export interface IndexInventory {
  slug: string;
  indexes: IndexInfo[];
  vector_indexes: VectorIndexInfo[];
  stale: string[];
  maintenance: MaintenanceStatus;
}

export interface Inventory extends IndexInventory {
  schema: SchemaPayload;
}

export interface TextSearchBody {
  index: string;
  query: string;
  k?: number;
  prefix?: boolean;
  phrase?: boolean;
  return_positions?: boolean;
  record_ids?: number[] | null;
  timeout_seconds?: number | null;
}

export interface VectorSearchBody {
  space: string;
  vector: number[];
  k?: number;
  table?: string | null;
  table_kind?: "node" | "rel" | null;
  record_ids?: number[] | null;
  timeout_seconds?: number | null;
}

export interface HybridSearchBody {
  /** The table both signals are searched in: hybrid fusion is per-table, not global. */
  table: string;
  index?: string | null;
  query: string;
  space?: string | null;
  vector?: number[];
  k?: number;
  lexical_weight?: number;
  vector_weight?: number;
  candidate_k?: number;
  allow_partial?: boolean;
  graph_relations?: string[];
  graph_weight?: number;
  graph_filter?: boolean;
  timeout_seconds?: number | null;
}

export type Cell =
  | string
  | number
  | boolean
  | null
  | Cell[]
  | { [key: string]: unknown };

export interface QueryResult {
  columns: string[];
  rows: Cell[][];
  row_count: number;
  truncated: boolean;
  statistics: Record<string, number>;
  warnings: { kind: string; [key: string]: unknown }[];
  duration_ms: number;
  snapshot_lsn: number;
}

/**
 * `explain` returns the plan tree itself: each node is `{operator, child?, …}`.
 * It is the engine's own operator names, so the tree is rendered generically.
 */
export interface PlanNode {
  operator: string;
  child?: PlanNode | null;
  children?: PlanNode[];
  [key: string]: unknown;
}

export interface SearchHit {
  id: string;
  table: string;
  record_id: number;
  score: number;
  [key: string]: unknown;
}

export interface TextHits {
  hits: SearchHit[];
  regime: string;
  candidates: number;
  postings_visited: number;
  corpus_documents: number;
  index_built_through_commit: number;
  snapshot_commit: number;
  statistics_regime?: string;
  table: string;
}

export interface VectorHits {
  hits: SearchHit[];
  regime: string;
  space: string;
  table?: string;
  requested_k: number;
  achieved_k: number;
}

export interface HybridHits {
  hits: SearchHit[];
  regime: string;
  fusion: string;
  lexical_regime: string;
  vector_regime: string;
  snapshot_commit: number;
  source_errors: Record<string, string>;
}

export interface GraphRequest {
  mode: "projection" | "query";
  node_tables?: string[];
  relationship_tables?: string[];
  weight_columns?: Record<string, string>;
  query?: string;
  parameters?: Record<string, unknown>;
  max_nodes?: number;
  max_edges?: number;
}

export interface GraphNode {
  id: string;
  label: string;
  table: string | null;
  record_id: number | null;
  category: string;
  properties: Record<string, unknown>;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  table: string | null;
  record_id: number | null;
  properties: Record<string, unknown>;
}

export interface GraphPayload {
  nodes: GraphNode[];
  edges: GraphEdge[];
  shown_nodes: number;
  total_nodes: number;
  shown_edges: number;
  total_edges: number;
  truncated: boolean;
  snapshot_lsn: number;
  database_uuid: string;
  mode: string;
  warnings: { kind: string; [key: string]: unknown }[];
}

export interface Job {
  id: string;
  database_id: number;
  kind: string;
  state: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  detail: Record<string, unknown> | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

/** When a job last had anything to say about itself. */
export function jobTime(job: Job): string {
  return job.finished_at ?? job.started_at ?? job.created_at;
}

export interface DdlResult {
  statements: { statement: string; rows: number; statistics?: Record<string, unknown> }[];
  commit?: { csn: number; durable: boolean; wrote: boolean };
  schema?: SchemaPayload;
}

export interface KeyRecord {
  id: number;
  name: string;
  prefix: string;
  fingerprint: string;
  databases: string[];
  allow_writes: boolean;
  enabled: boolean;
  created_at: string;
  last_used_at?: string | null;
  revoked_at?: string | null;
}

/** Creating a key returns the record with the secret next to it — once, and never again. */
export type CreatedKey = KeyRecord & { key: string; shown_once: boolean };

export interface AuditEntry {
  id: number;
  at: string;
  actor: string;
  action: string;
  database: string | null;
  request: Record<string, unknown>;
  outcome: string;
  rows: number | null;
  duration_ms: number | null;
}

export interface HandleInfo {
  slug: string;
  path: string;
  readers: number;
  idle_readers: number;
  writer_open: boolean;
  active_operations: number;
  open_transactions: number;
  idle_seconds: number;
  /** Page-cache budget this store's handles may hold — the engine grants it per handle. */
  buffer_budget_bytes: number;
  /** Reads and writes are refused until recovery closes the lane. */
  quarantined: boolean;
}

export interface ServerStatus {
  handles: HandleInfo[];
  open_slugs: string[];
  runner: Record<string, number>;
  ws_sessions: number;
  uptime_seconds: number;
}

export interface Health {
  status: string;
  server: string;
  grafx: string;
  databases_registered: number;
  databases_open: number;
  uptime_seconds: number;
  runner: Record<string, number>;
}
