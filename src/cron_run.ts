import { loadConfig } from "./shared/config";
import { initDb } from "./shared/db";
import { acquireLock, releaseLock, utcNowIso, updateRuntimeStatus, ensureRuntimePaths, logLine } from "./shared/runtime";
import { discoverJobs } from "./modules/autojobs";
import { runJobs } from "./core/runner";
import { sendMessageToAllowedChats } from "./api/telegram";
import type { Job } from "./shared/types";

const paths = ensureRuntimePaths();
const lockPath = paths.lockPath;
const logPath = paths.logPath;
const command = "bun run src/cron_run.ts";

const lockResult = acquireLock(lockPath, command);

if (!lockResult.acquired) {
  const payload = lockResult.lock_payload ?? {};
  const extra = payload && typeof payload === "object" && "pid" in payload ? ` pid=${payload["pid"]} started_at=${payload["started_at"]}` : "";
  logLine(logPath, `already_running${extra}`);
  process.exit(0);
}

function getDisplayTitle(job: { title?: string; title_ru?: string }): string {
  return (job.title_ru ?? job.title ?? "Без названия").trim();
}

try {
  logLine(logPath, "start cron_run");

  initDb();
  const config = loadConfig();

  updateRuntimeStatus({
    run_status: "running",
    run_started_at: utcNowIso(),
    run_finished_at: null,
    current_stage: "cron_start",
    queue_progress: { current_job_index: 0, total_jobs: 0, jobs_processed: 0, jobs_failed: 0 },
    current_job: null,
  });

  // Discovery
  updateRuntimeStatus({ current_stage: "discovery" });
  let jobs: Job[] = [];

  try {
    const discoveryResult = await discoverJobs();
    jobs = discoveryResult.jobs;
    logLine(logPath, `discovery_summary ${JSON.stringify(discoveryResult.summary)}`);

    if (discoveryResult.summary.created_jobs > 0 || discoveryResult.summary.updated_jobs > 0) {
      try {
        const titles = discoveryResult.jobs
          .slice(-Math.max(discoveryResult.summary.created_jobs, 0))
          .map(j => j.title)
          .join(", ");
        await sendMessageToAllowedChats(
          `🛰️ *Автодискавери завершён*\n\n🆕 Новых аниме: \`${discoveryResult.summary.created_jobs}\`\n🔄 Обновлено: \`${discoveryResult.summary.updated_jobs}\`\n\n${titles ? `🎬 ${titles}` : ""}`,
          { parseMode: "MarkdownV2" },
        ).catch(() => {});
      } catch { /* notifications are best-effort */ }
    }
  } catch (err) {
    logLine(logPath, `warning discovery_failed error=${String(err)}`);
  }

  updateRuntimeStatus({ current_stage: "processing", queue_progress: { total_jobs: jobs.length } });

  // Processing
  const summary = await runJobs(config, jobs, {
    log: (msg: string) => logLine(logPath, msg),
    onJobSuccess: (job, result) => {
      const vk = result.delivery_summary.vk;
      const text = vk.enabled && vk.uploaded
        ? `✅ Видео опубликовано в VK\n\n${getDisplayTitle(job)}\nЭпизоды: ${job.episodes_range}`
        : `✅ Обработка завершена\n\n${getDisplayTitle(job)}\nЭпизоды: ${job.episodes_range}`;
      sendMessageToAllowedChats(text).catch(() => {});
    },
    onJobFailure: (job, err) => {
      sendMessageToAllowedChats(`❌ Ошибка\n\n${job.title}\n${String(err).slice(0, 200)}`).catch(() => {});
    },
  });

  logLine(logPath, `processing_summary ${JSON.stringify(summary)}`);
  updateRuntimeStatus({
    run_status: "completed",
    run_finished_at: utcNowIso(),
    current_stage: "completed",
    queue_progress: { jobs_processed: summary.jobs_processed, jobs_failed: summary.jobs_failed },
  });

  logLine(logPath, "finish cron_run");
} catch (err) {
  logLine(logPath, `error ${String(err)}`);
  updateRuntimeStatus({ run_status: "failed", run_finished_at: utcNowIso(), current_stage: "failed" });
  try {
    await sendMessageToAllowedChats(`❌ Ошибка cron_run\n\n${String(err).slice(0, 300)}`);
  } catch { /* ok */ }
} finally {
  releaseLock(lockPath);
}
