import { existsSync, mkdirSync, unlinkSync, appendFileSync, readFileSync } from "node:fs";
import { join } from "node:path";
import {
  getRuntimeStatus as dbGetStatus,
  updateRuntimeStatus as dbUpdateStatus,
  appendRuntimeError as dbAppendError,
  getRuntimeErrors as dbGetErrors,
} from "./db";
import {
  DEFAULT_RUNTIME_DIR,
  DEFAULT_LOGS_DIR,
  DEFAULT_CRON_LOCK_NAME,
  DEFAULT_CRON_LOG_NAME,
  DEFAULT_TELEGRAM_LOG_NAME,
} from "./constants";
import type { RuntimeStatus, RuntimeErrorEntry, Job } from "./types";

export function utcNowIso(): string {
  return new Date().toISOString();
}

export function ensureRuntimePaths() {
  mkdirSync(DEFAULT_RUNTIME_DIR, { recursive: true });
  mkdirSync(DEFAULT_LOGS_DIR, { recursive: true });

  return {
    runtimeDir: DEFAULT_RUNTIME_DIR,
    logsDir: DEFAULT_LOGS_DIR,
    lockPath: join(DEFAULT_RUNTIME_DIR, DEFAULT_CRON_LOCK_NAME),
    logPath: join(DEFAULT_LOGS_DIR, DEFAULT_CRON_LOG_NAME),
    telegramLogPath: join(DEFAULT_LOGS_DIR, DEFAULT_TELEGRAM_LOG_NAME),
  };
}

// ============================================================================
// File lock
// ============================================================================

function isProcessAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

export function acquireLock(lockPath: string, command: string) {
  if (existsSync(lockPath)) {
    try {
      const raw = readFileSync(lockPath, "utf-8");
      const payload = JSON.parse(raw);
      if (payload.pid && isProcessAlive(payload.pid)) {
        return { acquired: false, already_running: true, lock_payload: payload };
      }
      try { unlinkSync(lockPath); } catch { /* stale */ }
    } catch {
      try { unlinkSync(lockPath); } catch { /* corrupt */ }
    }
  }

  const payload = { pid: process.pid, started_at: utcNowIso(), command };
  try {
    Bun.write(lockPath, JSON.stringify(payload, null, 2) + "\n");
  } catch {
    return { acquired: false, already_running: true, lock_payload: null };
  }

  return { acquired: true, already_running: false, lock_payload: payload };
}

export function releaseLock(lockPath: string): void {
  if (!existsSync(lockPath)) return;
  unlinkSync(lockPath);
}

// ============================================================================
// Runtime status (delegates to db.ts)
// ============================================================================

export { dbGetStatus as getRuntimeStatus };

export function updateRuntimeStatus(patch: Partial<RuntimeStatus>): void {
  dbUpdateStatus(patch as Record<string, unknown>);
}

// ============================================================================
// Runtime errors
// ============================================================================

export function appendRuntimeError(params: {
  context: string;
  message: string;
  errorType: string;
  stage?: string | null;
  title?: string;
  titleRu?: string;
  season?: number;
  episodesRange?: string;
  currentEpisode?: number | null;
  totalEpisodes?: number | null;
  runStatus?: string;
}): RuntimeErrorEntry {
  const status = dbGetStatus();
  const currentJob = status.current_job ?? ({} as Record<string, unknown>);
  const lastRun = status.last_run ?? ({} as Record<string, unknown>);
  const sourceJob = (Object.keys(currentJob).length > 0 ? currentJob : lastRun) as Record<string, unknown>;

  const entry: RuntimeErrorEntry = {
    id: `${utcNowIso()}|${params.context}`,
    created_at: utcNowIso(),
    run_status: params.runStatus ?? status.run_status,
    context: params.context,
    stage: params.stage ?? (currentJob.stage as string) ?? status.current_stage ?? null,
    title: params.title ?? (sourceJob.title as string),
    title_ru: params.titleRu ?? (sourceJob.title_ru as string),
    season: params.season ?? (sourceJob.season as number),
    episodes_range: params.episodesRange ?? (sourceJob.episodes_range as string),
    current_episode: params.currentEpisode ?? (currentJob.current_episode as number | null) ?? null,
    total_episodes: params.totalEpisodes ?? (currentJob.total_episodes as number | null) ?? null,
    message: params.message,
    error_type: params.errorType,
  };

  dbAppendError(entry);
  return entry;
}

export { dbGetErrors as getRuntimeErrors };

// ============================================================================
// Job lifecycle markers
// ============================================================================

export function markRuntimeJobStart(
  job: Job,
  progress: { currentJobIndex: number; totalJobs: number; jobsProcessed: number; jobsFailed: number },
): void {
  updateRuntimeStatus({
    current_stage: "job_start",
    queue_progress: {
      current_job_index: progress.currentJobIndex,
      total_jobs: progress.totalJobs,
      jobs_processed: progress.jobsProcessed,
      jobs_failed: progress.jobsFailed,
    },
    current_job: {
      title: job.title,
      title_ru: job.title_ru,
      season: job.season,
      episodes_range: job.episodes_range,
      stage: "job_start",
      started_at: utcNowIso(),
    },
  });
}

export function markRuntimeJobFinish(
  job: Job,
  status: string,
  stage: string,
  progress: { jobsProcessed: number; jobsFailed: number; currentEpisode?: number | null; totalEpisodes?: number | null },
): void {
  const currentStatus = dbGetStatus();
  const currentJob = currentStatus.current_job ?? ({} as Record<string, unknown>);

  updateRuntimeStatus({
    current_stage: stage,
    queue_progress: {
      jobs_processed: progress.jobsProcessed,
      jobs_failed: progress.jobsFailed,
    },
    current_job: null,
    last_run: {
      status,
      finished_at: utcNowIso(),
      title: job.title,
      title_ru: job.title_ru,
      season: job.season,
      episodes_range: job.episodes_range,
      stage,
      current_episode: progress.currentEpisode ?? null,
      total_episodes: progress.totalEpisodes ?? null,
      jobs_processed: progress.jobsProcessed,
      jobs_failed: progress.jobsFailed,
      started_at: currentJob.started_at as string | undefined,
    },
  });
}

export function markRuntimeRunFinish(
  status: string,
  currentStage: string,
  jobsProcessed: number,
  jobsFailed: number,
): void {
  updateRuntimeStatus({
    run_status: status,
    run_finished_at: utcNowIso(),
    current_stage: currentStage,
    queue_progress: {
      jobs_processed: jobsProcessed,
      jobs_failed: jobsFailed,
      current_job_index: 0,
    },
    current_job: null,
  });
}

// ============================================================================
// Logging
// ============================================================================

export function logLine(logPath: string, message: string): string {
  const line = `[${utcNowIso()}] ${message}`;
  console.log(line);
  appendFileSync(logPath, line + "\n");
  return line;
}
