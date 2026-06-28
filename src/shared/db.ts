import { Database } from "bun:sqlite";
import type {
  Job,
  CompletedJobEntry,
  BlacklistItem,
  SkippedItem,
  OngoingProgressEntry,
  RuntimeStatus,
  RuntimeErrorEntry,
  TelegramState,
  PendingAction,
  NotificationDetails,
} from "./types";

let db: Database | null = null;

export function initDb(path = "data.db"): Database {
  if (db) return db;
  db = new Database(path);
  db.run("PRAGMA journal_mode = WAL");
  db.run("PRAGMA foreign_keys = ON");
  migrate(db);
  return db;
}

export function getDb(): Database {
  if (!db) throw new Error("Database not initialized. Call initDb() first.");
  return db;
}

// ============================================================================
// Schema migration
// ============================================================================

function migrate(database: Database) {
  database.run(`
    CREATE TABLE IF NOT EXISTS schema_version (
      version INTEGER PRIMARY KEY
    )
  `);

  const row = database.query("SELECT version FROM schema_version").get() as
    | { version: number }
    | undefined;
  const current = row?.version ?? 0;

  if (current < 1) {
    database.run(`
      CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        title TEXT NOT NULL,
        title_ru TEXT,
        mal_id INTEGER,
        season INTEGER NOT NULL DEFAULT 1,
        episodes_range TEXT NOT NULL,
        processing_mode TEXT NOT NULL DEFAULT 'compilation',
        source_type TEXT NOT NULL,
        source_magnet TEXT,
        source_input_dir TEXT,
        source_download_dir TEXT,
        source_variant_codec TEXT,
        source_variant_label TEXT,
        skip_types TEXT,
        encoding TEXT,
        delivery TEXT,
        cleanup TEXT,
        processing TEXT,
        timing_detection TEXT,
        timing_providers TEXT,
        preferred_audio_language TEXT DEFAULT 'rus',
        watermark_path TEXT,
        output_dir TEXT,
        automation TEXT,
        status TEXT NOT NULL DEFAULT 'pending'
      )
    `);

    database.run(`
      CREATE TABLE IF NOT EXISTS completed_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER,
        status TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        output_display_name TEXT,
        output_video TEXT,
        output_timestamps TEXT,
        output_manifest TEXT,
        delivery_summary TEXT,
        partial_vk INTEGER NOT NULL DEFAULT 0,
        completion_source TEXT,
        completion_note TEXT,
        job_snapshot TEXT
      )
    `);

    database.run(`
      CREATE TABLE IF NOT EXISTS state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
      )
    `);

    database.run(`
      CREATE TABLE IF NOT EXISTS episode_tracking (
        release_id INTEGER NOT NULL,
        episode INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('queued', 'completed')),
        recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (release_id, episode, status)
      )
    `);

    database.run(`
      CREATE TABLE IF NOT EXISTS discovery_blacklist (
        release_id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        title_ru TEXT,
        season INTEGER NOT NULL DEFAULT 1,
        added_at TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'telegram'
      )
    `);

    database.run(`
      CREATE TABLE IF NOT EXISTS skipped_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        release_id INTEGER,
        alias TEXT,
        title TEXT,
        episodes TEXT,
        reason TEXT,
        recorded_at TEXT NOT NULL
      )
    `);

    database.run(`
      CREATE TABLE IF NOT EXISTS ongoing_progress (
        progress_key TEXT PRIMARY KEY,
        has_full_publish INTEGER NOT NULL DEFAULT 0,
        last_full_episode INTEGER,
        last_full_range TEXT,
        updated_at TEXT NOT NULL
      )
    `);

    database.run(`
      CREATE TABLE IF NOT EXISTS job_index (
        job_key TEXT PRIMARY KEY,
        title TEXT,
        season INTEGER,
        episodes_range TEXT,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
      )
    `);

    database.run(`
      CREATE TABLE IF NOT EXISTS runtime_status (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        updated_at TEXT,
        run_status TEXT NOT NULL DEFAULT 'idle',
        run_started_at TEXT,
        run_finished_at TEXT,
        current_stage TEXT,
        queue_progress TEXT,
        current_job TEXT,
        last_run TEXT
      )
    `);

    database.run(`
      INSERT OR IGNORE INTO runtime_status (id, run_status) VALUES (1, 'idle')
    `);

    database.run(`
      CREATE TABLE IF NOT EXISTS runtime_errors (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        run_status TEXT,
        context TEXT NOT NULL,
        stage TEXT,
        title TEXT,
        title_ru TEXT,
        season INTEGER,
        episodes_range TEXT,
        current_episode INTEGER,
        total_episodes INTEGER,
        message TEXT NOT NULL,
        error_type TEXT NOT NULL
      )
    `);

    database.run(`
      CREATE TABLE IF NOT EXISTS telegram_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
      )
    `);

    database.run("INSERT OR REPLACE INTO schema_version (version) VALUES (1)");
  }
}

// ============================================================================
// Jobs CRUD
// ============================================================================

export function getJobs(status?: string): Job[] {
  const database = getDb();
  const query = status
    ? "SELECT * FROM jobs WHERE status = ? ORDER BY id"
    : "SELECT * FROM jobs ORDER BY id";
  const rows = status
    ? database.query(query).all(status)
    : database.query(query).all();

  return (rows as Record<string, unknown>[]).map(rowToJob);
}

export function getJobById(id: number): Job | null {
  const database = getDb();
  const row = database.query("SELECT * FROM jobs WHERE id = ?").get(id) as
    | Record<string, unknown>
    | undefined;
  return row ? rowToJob(row) : null;
}

export function insertJob(job: Job): number {
  const database = getDb();
  const now = new Date().toISOString();
  const source = job.source;
  const automation = job.automation;

  const result = database.run(
    `INSERT INTO jobs (
      created_at, updated_at, title, title_ru, mal_id, season,
      episodes_range, processing_mode, source_type, source_magnet,
      source_input_dir, source_download_dir, source_variant_codec,
      source_variant_label, skip_types, encoding, delivery, cleanup,
      processing, timing_detection, timing_providers,
      preferred_audio_language, watermark_path, output_dir, automation
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    [
      now,
      now,
      job.title,
      job.title_ru ?? null,
      job.mal_id ?? null,
      job.season,
      job.episodes_range,
      job.processing_mode ?? "compilation",
      source.type,
      source.magnet ?? null,
      source.input_dir ?? null,
      source.download_dir ?? null,
      source.variant_codec ?? null,
      source.variant_label ?? null,
      job.skip_types ? JSON.stringify(job.skip_types) : null,
      job.encoding ? JSON.stringify(job.encoding) : null,
      job.delivery ? JSON.stringify(job.delivery) : null,
      job.cleanup ? JSON.stringify(job.cleanup) : null,
      job.processing ? JSON.stringify(job.processing) : null,
      job.timing_detection ? JSON.stringify(job.timing_detection) : null,
      job.timing_providers ? JSON.stringify(job.timing_providers) : null,
      job.preferred_audio_language ?? "rus",
      job.watermark_path ?? null,
      job.output_dir ?? null,
      automation ? JSON.stringify(automation) : null,
    ],
  );

  return Number(result.lastInsertRowid);
}

export function updateJob(id: number, patch: Partial<Job>): void {
  const database = getDb();
  const sets: string[] = ["updated_at = datetime('now')"];
  const values: unknown[] = [];

  if (patch.title !== undefined) { sets.push("title = ?"); values.push(patch.title); }
  if (patch.episodes_range !== undefined) { sets.push("episodes_range = ?"); values.push(patch.episodes_range); }
  if (patch.processing_mode !== undefined) { sets.push("processing_mode = ?"); values.push(patch.processing_mode); }
  if (patch.source) {
    if (patch.source.magnet !== undefined) { sets.push("source_magnet = ?"); values.push(patch.source.magnet); }
    if (patch.source.variant_codec !== undefined) { sets.push("source_variant_codec = ?"); values.push(patch.source.variant_codec); }
    if (patch.source.variant_label !== undefined) { sets.push("source_variant_label = ?"); values.push(patch.source.variant_label); }
  }
  if (patch.automation !== undefined) {
    sets.push("automation = ?");
    values.push(JSON.stringify(patch.automation));
  }

  if (sets.length === 1) return;
  values.push(id);
  database.run(`UPDATE jobs SET ${sets.join(", ")} WHERE id = ?`, values);
}

export function deleteJob(id: number): void {
  const database = getDb();
  database.run("DELETE FROM jobs WHERE id = ?", [id]);
}

export function jobCompleted(
  jobId: number,
  entry: CompletedJobEntry,
): void {
  const database = getDb();
  const jobRow = database.query("SELECT * FROM jobs WHERE id = ?").get(jobId) as
    | Record<string, unknown>
    | undefined;

  database.transaction(() => {
    database.run("DELETE FROM jobs WHERE id = ?", [jobId]);
    database.run(
      `INSERT INTO completed_jobs (
        job_id, status, completed_at, output_display_name, output_video,
        output_timestamps, output_manifest, delivery_summary, partial_vk,
        completion_source, completion_note, job_snapshot
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      [
        jobId,
        entry.status,
        entry.completed_at,
        entry.output_display_name ?? null,
        entry.output_video ?? null,
        entry.output_timestamps ?? null,
        entry.output_manifest ?? null,
        entry.delivery_summary ? JSON.stringify(entry.delivery_summary) : null,
        entry.partial_vk ? 1 : 0,
        entry.completion_source ?? null,
        entry.completion_note ?? null,
        jobRow ? JSON.stringify(rowToJob(jobRow)) : null,
      ],
    );
  })();
}

// ============================================================================
// State (key-value)
// ============================================================================

export function getState(key: string): string | null {
  const database = getDb();
  const row = database.query("SELECT value FROM state WHERE key = ?").get(key) as
    | { value: string }
    | undefined;
  return row?.value ?? null;
}

export function setState(key: string, value: string): void {
  const database = getDb();
  database.run(
    "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, datetime('now'))",
    [key, value],
  );
}

// ============================================================================
// Episode tracking
// ============================================================================

export function isEpisodeTracked(releaseId: number, episode: number): boolean {
  const database = getDb();
  const row = database
    .query(
      "SELECT 1 FROM episode_tracking WHERE release_id = ? AND episode = ? LIMIT 1",
    )
    .get(releaseId, episode);
  return row !== null;
}

export function getTrackedEpisodeKeys(): Set<string> {
  const database = getDb();
  const rows = database.query("SELECT release_id, episode FROM episode_tracking").all() as {
    release_id: number;
    episode: number;
  }[];
  return new Set(rows.map(r => `${r.release_id}:${String(r.episode).padStart(3, "0")}`));
}

export function markEpisodesQueued(releaseId: number, episodes: number[]): void {
  const database = getDb();
  const now = new Date().toISOString();
  const stmt = database.prepare(
    "INSERT OR IGNORE INTO episode_tracking (release_id, episode, status, recorded_at) VALUES (?, ?, 'queued', ?)",
  );
  database.transaction(() => {
    for (const ep of episodes) {
      stmt.run(releaseId, ep, now);
    }
  })();
}

export function unmarkEpisodesQueued(releaseId: number, episodes: number[]): void {
  const database = getDb();
  const stmt = database.prepare(
    "DELETE FROM episode_tracking WHERE release_id = ? AND episode = ? AND status = 'queued'",
  );
  database.transaction(() => {
    for (const ep of episodes) {
      stmt.run(releaseId, ep);
    }
  })();
}

export function markEpisodesCompleted(releaseId: number, episodes: number[]): void {
  const database = getDb();
  const now = new Date().toISOString();
  const delStmt = database.prepare(
    "DELETE FROM episode_tracking WHERE release_id = ? AND episode = ? AND status = 'queued'",
  );
  const insStmt = database.prepare(
    "INSERT OR IGNORE INTO episode_tracking (release_id, episode, status, recorded_at) VALUES (?, ?, 'completed', ?)",
  );
  database.transaction(() => {
    for (const ep of episodes) {
      delStmt.run(releaseId, ep);
      insStmt.run(releaseId, ep, now);
    }
  })();
}

export function getQueuedReleaseEpisodesCount(): number {
  const database = getDb();
  const row = database
    .query("SELECT COUNT(*) as cnt FROM episode_tracking WHERE status = 'queued'")
    .get() as { cnt: number };
  return row.cnt;
}

export function getCompletedReleaseEpisodesCount(): number {
  const database = getDb();
  const row = database
    .query("SELECT COUNT(*) as cnt FROM episode_tracking WHERE status = 'completed'")
    .get() as { cnt: number };
  return row.cnt;
}

// ============================================================================
// Blacklist
// ============================================================================

export function getBlacklist(): BlacklistItem[] {
  const database = getDb();
  const rows = database.query("SELECT * FROM discovery_blacklist ORDER BY title").all() as Record<string, unknown>[];
  return rows.map(r => ({
    release_id: r.release_id as number,
    title: r.title as string,
    title_ru: (r.title_ru as string) ?? null,
    season: r.season as number,
    added_at: r.added_at as string,
    source: r.source as string,
  }));
}

export function findBlacklistItem(releaseId: number): BlacklistItem | null {
  const database = getDb();
  const row = database
    .query("SELECT * FROM discovery_blacklist WHERE release_id = ?")
    .get(releaseId) as Record<string, unknown> | undefined;
  if (!row) return null;
  return {
    release_id: row.release_id as number,
    title: row.title as string,
    title_ru: (row.title_ru as string) ?? null,
    season: row.season as number,
    added_at: row.added_at as string,
    source: row.source as string,
  };
}

export function addToBlacklist(item: BlacklistItem): boolean {
  const database = getDb();
  const existing = findBlacklistItem(item.release_id);
  if (existing) return true;
  database.run(
    `INSERT INTO discovery_blacklist (release_id, title, title_ru, season, added_at, source)
     VALUES (?, ?, ?, ?, ?, ?)`,
    [item.release_id, item.title, item.title_ru ?? null, item.season, item.added_at, item.source],
  );
  return false;
}

export function removeFromBlacklist(releaseId: number): BlacklistItem | null {
  const database = getDb();
  const item = findBlacklistItem(releaseId);
  if (!item) return null;
  database.run("DELETE FROM discovery_blacklist WHERE release_id = ?", [releaseId]);
  return item;
}

export function getBlacklistCount(): number {
  const database = getDb();
  const row = database.query("SELECT COUNT(*) as cnt FROM discovery_blacklist").get() as { cnt: number };
  return row.cnt;
}

// ============================================================================
// Skipped items
// ============================================================================

export function recordSkippedItem(item: SkippedItem): void {
  const database = getDb();
  database.run(
    `INSERT INTO skipped_items (release_id, alias, title, episodes, reason, recorded_at)
     VALUES (?, ?, ?, ?, ?, ?)`,
    [
      item.release_id ?? null,
      item.alias ?? null,
      item.title ?? null,
      JSON.stringify(item.episodes),
      item.reason,
      item.recorded_at,
    ],
  );
}

export function getSkippedItemsCount(): number {
  const database = getDb();
  const row = database.query("SELECT COUNT(*) as cnt FROM skipped_items").get() as { cnt: number };
  return row.cnt;
}

// ============================================================================
// Ongoing progress
// ============================================================================

export function getOngoingProgress(key: string): OngoingProgressEntry | null {
  const database = getDb();
  const row = database
    .query("SELECT * FROM ongoing_progress WHERE progress_key = ?")
    .get(key) as Record<string, unknown> | undefined;
  if (!row) return null;
  return {
    has_full_publish: Boolean(row.has_full_publish),
    last_full_episode: (row.last_full_episode as number) ?? null,
    last_full_range: (row.last_full_range as string) ?? null,
    updated_at: row.updated_at as string,
  };
}

export function setOngoingProgress(key: string, entry: OngoingProgressEntry): void {
  const database = getDb();
  database.run(
    `INSERT OR REPLACE INTO ongoing_progress
     (progress_key, has_full_publish, last_full_episode, last_full_range, updated_at)
     VALUES (?, ?, ?, ?, ?)`,
    [
      key,
      entry.has_full_publish ? 1 : 0,
      entry.last_full_episode ?? null,
      entry.last_full_range ?? null,
      entry.updated_at,
    ],
  );
}

// ============================================================================
// Runtime status
// ============================================================================

export function getRuntimeStatus(): RuntimeStatus {
  const database = getDb();
  const row = database.query("SELECT * FROM runtime_status WHERE id = 1").get() as
    | Record<string, unknown>
    | undefined;
  if (!row) {
    return {
      schema_version: 1,
      updated_at: null,
      run_status: "idle",
      run_started_at: null,
      run_finished_at: null,
      current_stage: null,
      queue_progress: { current_job_index: 0, total_jobs: 0, jobs_processed: 0, jobs_failed: 0 },
      current_job: null,
      last_run: null,
    };
  }
  return {
    schema_version: 1,
    updated_at: row.updated_at as string | null,
    run_status: row.run_status as string,
    run_started_at: row.run_started_at as string | null,
    run_finished_at: row.run_finished_at as string | null,
    current_stage: row.current_stage as string | null,
    queue_progress: row.queue_progress
      ? (JSON.parse(row.queue_progress as string) as RuntimeStatus["queue_progress"])
      : { current_job_index: 0, total_jobs: 0, jobs_processed: 0, jobs_failed: 0 },
    current_job: row.current_job
      ? (JSON.parse(row.current_job as string) as RuntimeStatus["current_job"])
      : null,
    last_run: row.last_run
      ? (JSON.parse(row.last_run as string) as RuntimeStatus["last_run"])
      : null,
  };
}

export function updateRuntimeStatus(patch: Record<string, unknown>): void {
  const database = getDb();
  const current = getRuntimeStatus();
  const merged = deepMergeRuntime(current, patch);
  merged.updated_at = new Date().toISOString();

  database.run(
    `UPDATE runtime_status SET
      updated_at = ?, run_status = ?, run_started_at = ?, run_finished_at = ?,
      current_stage = ?, queue_progress = ?, current_job = ?, last_run = ?
    WHERE id = 1`,
    [
      merged.updated_at,
      merged.run_status,
      merged.run_started_at ?? null,
      merged.run_finished_at ?? null,
      merged.current_stage ?? null,
      JSON.stringify(merged.queue_progress),
      merged.current_job ? JSON.stringify(merged.current_job) : null,
      merged.last_run ? JSON.stringify(merged.last_run) : null,
    ],
  );
}

function deepMergeRuntime(current: Record<string, unknown>, patch: Record<string, unknown>): Record<string, unknown> {
  const result = { ...current };
  for (const [key, value] of Object.entries(patch)) {
    if (value === null && key in result) {
      result[key] = null;
    } else if (typeof value === "object" && value !== null && !Array.isArray(value)
      && typeof result[key] === "object" && result[key] !== null && !Array.isArray(result[key])) {
      result[key] = deepMergeRuntime(result[key] as Record<string, unknown>, value as Record<string, unknown>);
    } else {
      result[key] = value;
    }
  }
  return result;
}

// ============================================================================
// Runtime errors
// ============================================================================

export function appendRuntimeError(entry: RuntimeErrorEntry): void {
  const database = getDb();
  database.run(
    `INSERT INTO runtime_errors (
      id, created_at, run_status, context, stage, title, title_ru,
      season, episodes_range, current_episode, total_episodes,
      message, error_type
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    [
      entry.id,
      entry.created_at,
      entry.run_status,
      entry.context,
      entry.stage ?? null,
      entry.title ?? null,
      entry.title_ru ?? null,
      entry.season ?? null,
      entry.episodes_range ?? null,
      entry.current_episode ?? null,
      entry.total_episodes ?? null,
      entry.message,
      entry.error_type,
    ],
  );

  const count = (database.query("SELECT COUNT(*) as cnt FROM runtime_errors").get() as { cnt: number }).cnt;
  if (count > 20) {
    database.run(
      "DELETE FROM runtime_errors WHERE id NOT IN (SELECT id FROM runtime_errors ORDER BY created_at DESC LIMIT 20)",
    );
  }
}

export function getRuntimeErrors(limit = 20): RuntimeErrorEntry[] {
  const database = getDb();
  const rows = database
    .query("SELECT * FROM runtime_errors ORDER BY created_at DESC LIMIT ?")
    .all(limit) as Record<string, unknown>[];
  return rows.map(r => ({
    id: r.id as string,
    created_at: r.created_at as string,
    run_status: r.run_status as string,
    context: r.context as string,
    stage: r.stage as string | null,
    title: r.title as string | undefined,
    title_ru: r.title_ru as string | undefined,
    season: r.season as number | undefined,
    episodes_range: r.episodes_range as string | undefined,
    current_episode: r.current_episode as number | null | undefined,
    total_episodes: r.total_episodes as number | null | undefined,
    message: r.message as string,
    error_type: r.error_type as string,
  }));
}

// ============================================================================
// Telegram state
// ============================================================================

export function getTelegramState(): TelegramState {
  const database = getDb();
  const rows = database.query("SELECT key, value FROM telegram_state").all() as {
    key: string;
    value: string;
  }[];

  const map: Record<string, string> = {};
  for (const row of rows) {
    map[row.key] = row.value;
  }

  return {
    schema_version: Number(map["schema_version"] ?? 1),
    last_update_id: map["last_update_id"] ? Number(map["last_update_id"]) : null,
    last_handled_at: map["last_handled_at"] ? Number(map["last_handled_at"]) : null,
    pending_actions: map["pending_actions"]
      ? (JSON.parse(map["pending_actions"]) as Record<string, PendingAction>)
      : {},
    jobs_pagination: map["jobs_pagination"]
      ? (JSON.parse(map["jobs_pagination"]) as Record<string, number>)
      : {},
    notification_details: map["notification_details"]
      ? (JSON.parse(map["notification_details"]) as Record<string, NotificationDetails>)
      : {},
  };
}

export function setTelegramStateKey(key: string, value: string): void {
  const database = getDb();
  database.run(
    "INSERT OR REPLACE INTO telegram_state (key, value, updated_at) VALUES (?, ?, datetime('now'))",
    [key, value],
  );
}

export function saveTelegramState(state: TelegramState): void {
  const database = getDb();
  database.transaction(() => {
    setTelegramStateKey("schema_version", String(state.schema_version));
    if (state.last_update_id !== null) setTelegramStateKey("last_update_id", String(state.last_update_id));
    if (state.last_handled_at !== null) setTelegramStateKey("last_handled_at", String(state.last_handled_at));
    setTelegramStateKey("pending_actions", JSON.stringify(state.pending_actions));
    setTelegramStateKey("jobs_pagination", JSON.stringify(state.jobs_pagination));
    setTelegramStateKey("notification_details", JSON.stringify(state.notification_details));
  })();
}

// ============================================================================
// Completed jobs
// ============================================================================

export function getCompletedJobs(): CompletedJobEntry[] {
  const database = getDb();
  const rows = database.query("SELECT * FROM completed_jobs ORDER BY completed_at DESC").all() as Record<string, unknown>[];
  return rows.map(r => ({
    status: r.status as string,
    completed_at: r.completed_at as string,
    job: r.job_snapshot ? (JSON.parse(r.job_snapshot as string) as Job) : ({} as Job),
    output_display_name: r.output_display_name as string | null,
    output_video: r.output_video as string | null,
    output_timestamps: r.output_timestamps as string | null,
    output_manifest: r.output_manifest as string | null,
    delivery_summary: r.delivery_summary
      ? (JSON.parse(r.delivery_summary as string) as CompletedJobEntry["delivery_summary"])
      : {},
    partial_vk: Boolean(r.partial_vk),
    completion_source: r.completion_source as string | undefined,
    completion_note: r.completion_note as string | undefined,
  }));
}

export function insertCompletedJob(entry: CompletedJobEntry): void {
  const database = getDb();
  database.run(
    `INSERT INTO completed_jobs (
      status, completed_at, output_display_name, output_video,
      output_timestamps, output_manifest, delivery_summary, partial_vk,
      completion_source, completion_note, job_snapshot
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    [
      entry.status,
      entry.completed_at,
      entry.output_display_name ?? null,
      entry.output_video ?? null,
      entry.output_timestamps ?? null,
      entry.output_manifest ?? null,
      entry.delivery_summary ? JSON.stringify(entry.delivery_summary) : null,
      entry.partial_vk ? 1 : 0,
      entry.completion_source ?? null,
      entry.completion_note ?? null,
      JSON.stringify(entry.job),
    ],
  );
}

// ============================================================================
// Helpers
// ============================================================================

function rowToJob(row: Record<string, unknown>): Job {
  const sourceType = (row.source_type as string) ?? "magnet";
  return {
    id: (row.id as number) ?? undefined,
    title: row.title as string,
    title_ru: (row.title_ru as string) ?? undefined,
    mal_id: (row.mal_id as number) ?? undefined,
    season: (row.season as number) ?? 1,
    episodes_range: row.episodes_range as string,
    processing_mode: (row.processing_mode as string) ?? "compilation",
    source: {
      type: (sourceType === "local" ? "local" : "magnet") as "magnet" | "local",
      magnet: (row.source_magnet as string) ?? undefined,
      input_dir: (row.source_input_dir as string) ?? undefined,
      download_dir: (row.source_download_dir as string) ?? undefined,
      variant_codec: (row.source_variant_codec as string) ?? undefined,
      variant_label: (row.source_variant_label as string) ?? undefined,
    },
    skip_types: row.skip_types ? (JSON.parse(row.skip_types as string) as string[]) : undefined,
    encoding: row.encoding ? (JSON.parse(row.encoding as string) as Job["encoding"]) : undefined,
    delivery: row.delivery ? (JSON.parse(row.delivery as string) as Job["delivery"]) : undefined,
    cleanup: row.cleanup ? (JSON.parse(row.cleanup as string) as Job["cleanup"]) : undefined,
    processing: row.processing ? (JSON.parse(row.processing as string) as Job["processing"]) : undefined,
    timing_detection: row.timing_detection ? (JSON.parse(row.timing_detection as string) as Job["timing_detection"]) : undefined,
    timing_providers: row.timing_providers ? (JSON.parse(row.timing_providers as string) as Job["timing_providers"]) : undefined,
    preferred_audio_language: (row.preferred_audio_language as string) ?? "rus",
    watermark_path: (row.watermark_path as string) ?? undefined,
    output_dir: (row.output_dir as string) ?? undefined,
    automation: row.automation ? (JSON.parse(row.automation as string) as Job["automation"]) : undefined,
  };
}
