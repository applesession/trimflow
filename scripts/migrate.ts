// One-shot migration: reads legacy JSON files and populates the SQLite database.
// Run: bun run scripts/migrate.ts

import { initDb, insertJob, insertCompletedJob, setState, addToBlacklist, recordSkippedItem, setOngoingProgress, markEpisodesQueued, markEpisodesCompleted, setTelegramStateKey } from "../src/shared/db";
import type { Job, CompletedJobEntry, BlacklistItem, SkippedItem, OngoingProgressEntry } from "../src/shared/types";

const db = initDb();

// Check if already migrated
const migrated = db.query("SELECT value FROM state WHERE key = 'migrated_from_json'").get() as { value: string } | undefined;
if (migrated?.value === "true") {
  console.log("[MIGRATE] Already migrated, skipping.");
  process.exit(0);
}

// --- jobs.json ---
try {
  const jobsFile = Bun.file("jobs.json");
  if (await jobsFile.exists()) {
    const jobs: Job[] = await jobsFile.json();
    console.log(`[MIGRATE] Found ${jobs.length} jobs in jobs.json`);
    for (const job of jobs) {
      insertJob(job);
    }
  }
} catch (err) {
  console.warn("[MIGRATE] Skipping jobs.json:", err);
}

// --- completed_jobs.json ---
try {
  const completedFile = Bun.file("completed_jobs.json");
  if (await completedFile.exists()) {
    const completed: Record<string, unknown>[] = await completedFile.json();
    console.log(`[MIGRATE] Found ${completed.length} entries in completed_jobs.json`);
    for (const entry of completed) {
      insertCompletedJob(entry as unknown as CompletedJobEntry);
    }
  }
} catch (err) {
  console.warn("[MIGRATE] Skipping completed_jobs.json:", err);
}

// --- state.json ---
try {
  const stateFile = Bun.file("state.json");
  if (await stateFile.exists()) {
    const state: Record<string, unknown> = await stateFile.json();
    console.log("[MIGRATE] Processing state.json");

    if (state.schema_version !== undefined) {
      setState("schema_version", String(state.schema_version));
    }
    if (state.last_discovery_at) {
      setState("last_discovery_at", String(state.last_discovery_at));
    }

    // Episode tracking
    const queued = state.queued_release_episodes as Record<string, { release_id: number; episode: number }> | undefined;
    if (queued) {
      const byRelease: Record<number, number[]> = {};
      for (const v of Object.values(queued)) {
        (byRelease[v.release_id] ??= []).push(v.episode);
      }
      for (const [releaseId, episodes] of Object.entries(byRelease)) {
        markEpisodesQueued(Number(releaseId), episodes);
      }
    }

    const completed = state.completed_release_episodes as Record<string, { release_id: number; episode: number }> | undefined;
    if (completed) {
      const byRelease: Record<number, number[]> = {};
      for (const v of Object.values(completed)) {
        (byRelease[v.release_id] ??= []).push(v.episode);
      }
      for (const [releaseId, episodes] of Object.entries(byRelease)) {
        markEpisodesCompleted(Number(releaseId), episodes);
      }
    }

    // Blacklist
    const blacklist = state.discovery_blacklist as BlacklistItem[] | undefined;
    if (blacklist) {
      for (const item of blacklist) {
        addToBlacklist(item);
      }
    }

    // Skipped items
    const skipped = state.skipped_items as SkippedItem[] | undefined;
    if (skipped) {
      for (const item of skipped) {
        recordSkippedItem(item);
      }
    }

    // Ongoing progress
    const progress = state.ongoing_progress as Record<string, OngoingProgressEntry> | undefined;
    if (progress) {
      for (const [key, entry] of Object.entries(progress)) {
        setOngoingProgress(key, entry);
      }
    }

    console.log("[MIGRATE] state.json processed");
  }
} catch (err) {
  console.warn("[MIGRATE] Skipping state.json:", err);
}

// --- telegram_state.json ---
try {
  const tgFile = Bun.file("telegram_state.json");
  if (await tgFile.exists()) {
    const tgState: Record<string, unknown> = await tgFile.json();
    console.log("[MIGRATE] Processing telegram_state.json");

    if (tgState.schema_version !== undefined) setTelegramStateKey("schema_version", String(tgState.schema_version));
    if (tgState.last_update_id !== undefined) setTelegramStateKey("last_update_id", String(tgState.last_update_id));
    if (tgState.last_handled_at !== undefined) setTelegramStateKey("last_handled_at", String(tgState.last_handled_at));
    if (tgState.pending_actions) setTelegramStateKey("pending_actions", JSON.stringify(tgState.pending_actions));
    if (tgState.jobs_pagination) setTelegramStateKey("jobs_pagination", JSON.stringify(tgState.jobs_pagination));
    if (tgState.notification_details) setTelegramStateKey("notification_details", JSON.stringify(tgState.notification_details));
  }
} catch (err) {
  console.warn("[MIGRATE] Skipping telegram_state.json:", err);
}

// --- .runtime/runtime_status.json (optional) ---
try {
  const rtFile = Bun.file(".runtime/runtime_status.json");
  if (await rtFile.exists()) {
    const rtStatus: Record<string, unknown> = await rtFile.json();
    console.log("[MIGRATE] Processing .runtime/runtime_status.json");
    // runtime_status table already has a default row; update if we have data
    if (rtStatus.updated_at || rtStatus.run_status) {
      db.run(
        `UPDATE runtime_status SET
          updated_at = ?, run_status = ?, run_started_at = ?, run_finished_at = ?,
          current_stage = ?, queue_progress = ?, current_job = ?, last_run = ?
        WHERE id = 1`,
        [
          (rtStatus.updated_at as string) ?? null,
          (rtStatus.run_status as string) ?? "idle",
          (rtStatus.run_started_at as string) ?? null,
          (rtStatus.run_finished_at as string) ?? null,
          (rtStatus.current_stage as string) ?? null,
          rtStatus.queue_progress ? JSON.stringify(rtStatus.queue_progress) : null,
          rtStatus.current_job ? JSON.stringify(rtStatus.current_job) : null,
          rtStatus.last_run ? JSON.stringify(rtStatus.last_run) : null,
        ],
      );
    }
  }
} catch (err) {
  console.warn("[MIGRATE] Skipping .runtime/runtime_status.json:", err);
}

// --- .runtime/runtime_errors.json (optional) ---
try {
  const reFile = Bun.file(".runtime/runtime_errors.json");
  if (await reFile.exists()) {
    const rePayload = await reFile.json() as { errors?: Record<string, unknown>[] };
    const errors = rePayload.errors ?? [];
    console.log(`[MIGRATE] Processing ${errors.length} runtime errors`);
    const stmt = db.prepare(
      `INSERT OR IGNORE INTO runtime_errors (
        id, created_at, run_status, context, stage, title, title_ru,
        season, episodes_range, current_episode, total_episodes,
        message, error_type
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    );
    for (const e of errors.slice(0, 20)) {
      stmt.run(
        e.id ?? `${e.created_at}|${e.context}`,
        e.created_at ?? new Date().toISOString(),
        e.run_status ?? "unknown",
        e.context ?? "legacy",
        e.stage ?? null,
        e.title ?? null,
        e.title_ru ?? null,
        e.season ?? null,
        e.episodes_range ?? null,
        e.current_episode ?? null,
        e.total_episodes ?? null,
        e.message ?? "",
        e.error_type ?? "unknown",
      );
    }
  }
} catch (err) {
  console.warn("[MIGRATE] Skipping .runtime/runtime_errors.json:", err);
}

setState("migrated_from_json", "true");
setState("migrated_at", new Date().toISOString());
console.log("[MIGRATE] Migration complete.");
