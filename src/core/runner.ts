import { getJobs, jobCompleted as dbJobCompleted, markEpisodesCompleted, setOngoingProgress } from "../shared/db";
import { buildJobWithDefaults } from "../shared/config";
import { utcNowIso } from "../shared/runtime";
import { processJob } from "./pipeline";
import { validateRequiredEnv, validateRequiredTools, validateRequiredFiles } from "../shared/validation";
import type { Job, JobResult, ProcessingSummary, Config } from "../shared/types";

export function buildJobIdentity(job: Job): string {
  const s = job.source ?? {};
  const st = (s.type ?? "").trim().toLowerCase();
  let sig = "";
  if (st === "magnet") sig = (s.magnet ?? "").trim();
  else if (st === "local") sig = (s.input_dir ?? "").trim();
  return [(job.title ?? "").trim().toLowerCase(), String(job.season ?? "").trim(),
    (job.episodes_range ?? "").trim(), (job.processing_mode ?? "compilation").trim().toLowerCase(), st, sig].join("|");
}

function getJobProcessingMode(job: Job): string { return (job.processing_mode ?? "compilation").trim().toLowerCase(); }
function getJobPublishStrategy(job: Job): string { return ((job.automation?.publish_strategy ?? "") as string).trim().toLowerCase(); }
function getJobOngoingProgressKey(job: Job): string { return ((job.automation?.ongoing_progress_key ?? "") as string).trim(); }

function isIncrementalFullRefreshJob(job: Job): boolean { return getJobPublishStrategy(job) === "full_refresh"; }

function isOngoingCompilationJob(job: Job): boolean {
  const a = job.automation ?? {};
  return Boolean(a.is_ongoing) && getJobProcessingMode(job) === "compilation"
    && ["initial_full", "full_refresh"].includes(getJobPublishStrategy(job));
}

function buildExecutionPriority(job: Job): number {
  const a = job.automation ?? {};
  if (a.is_ongoing && getJobProcessingMode(job) === "single_episode") return 0;
  if (a.is_ongoing && getJobPublishStrategy(job) === "full_refresh") return 1;
  if (a.is_ongoing) return 2;
  return 3;
}

export function buildExecutionOrder(jobs: Job[], defaults: Record<string, unknown>): Job[] {
  return jobs.map(j => buildJobWithDefaults(j, defaults))
    .map((job, i) => ({ job, prio: buildExecutionPriority(job), idx: i }))
    .sort((a, b) => a.prio - b.prio || a.idx - b.idx)
    .map(x => x.job);
}

export function isJobCompleted(result: JobResult): boolean {
  const vk = result.delivery_summary.vk;
  if (vk.enabled) return Boolean(vk.video_uploaded);
  if (result.delivery_summary.s3.enabled) return Boolean(result.delivery_summary.s3.uploaded);
  return Boolean(result.output_video);
}

function getJobReleaseId(job: Job): number | null { return job.automation?.release_id as number ?? null; }

function buildOngoingProgressKey(job: Job): string {
  const s = job.source ?? {};
  return [(job.title ?? "").trim().toLowerCase(), String(job.season ?? "").trim(), (s.type ?? "").trim().toLowerCase()].join("|");
}

function updateStateAfterSuccessfulJob(job: Job): void {
  const releaseId = getJobReleaseId(job);
  if (releaseId != null) {
    const eps = (job.episodes_range ?? "").split(",").flatMap(p => {
      const t = p.trim();
      if (t.includes("-")) { const [a, b] = t.split("-").map(Number); return Array.from({ length: (b ?? a) - (a ?? 0) + 1 }, (_, i) => (a ?? 0) + i); }
      return [Number(t)];
    }).filter(n => !isNaN(n) && n > 0);
    markEpisodesCompleted(releaseId, eps);
  }
  if (isOngoingCompilationJob(job)) {
    const key = buildOngoingProgressKey(job);
    if (key) {
      const eps = (job.episodes_range ?? "").match(/\d+/g)?.map(Number).filter(n => !isNaN(n)) ?? [];
      setOngoingProgress(key, { has_full_publish: true, last_full_episode: eps.length > 0 ? Math.max(...eps) : null,
        last_full_range: job.episodes_range, updated_at: utcNowIso() });
    }
  }
}

export async function runJobs(
  config: Config,
  jobs: Job[],
  opts: { onJobSuccess?: (job: Job, result: JobResult) => void; onJobFailure?: (job: Job, error: Error) => void; log?: (msg: string) => void } = {},
): Promise<ProcessingSummary> {
  const log = opts.log ?? console.log;
  const activeJobs = [...(jobs ?? [])];

  if (activeJobs.length === 0) {
    log("No jobs found");
    return { jobs_found: 0, jobs_processed: 0, jobs_failed: 0, jobs_skipped: 0, failed_titles: [] };
  }

  const merged = buildExecutionOrder(activeJobs, config.defaults as Record<string, unknown>);
  validateRequiredEnv(config, merged);
  validateRequiredTools(config, merged);
  validateRequiredFiles(config);

  const blocked = new Set<string>();
  const summary: ProcessingSummary = { jobs_found: activeJobs.length, jobs_processed: 0, jobs_failed: 0, jobs_skipped: 0, failed_titles: [] };

  for (let i = 0; i < merged.length; i++) {
    const job = merged[i]!;
    const idx = i + 1;

    if (isIncrementalFullRefreshJob(job) && blocked.has(getJobOngoingProgressKey(job))) {
      log(`SKIP JOB ${idx}/${merged.length} after failed single publish: ${job.title}`);
      summary.jobs_skipped++;
      continue;
    }

    log("\n" + "=".repeat(80));
    log(`START JOB ${idx}/${merged.length}: ${job.title}`);
    log("=".repeat(80));

    try {
      const result = await processJob(job);

      if (isJobCompleted(result)) {
        if (job.id != null) {
          dbJobCompleted(job.id, {
            status: "completed", completed_at: utcNowIso(), job,
            output_display_name: result.output_display_name, output_video: result.output_video,
            output_timestamps: result.output_timestamps, output_manifest: result.output_manifest,
            delivery_summary: result.delivery_summary,
            partial_vk: Boolean(result.delivery_summary.vk?.video_uploaded
              && (!result.delivery_summary.vk?.post_created || !result.delivery_summary.vk?.comment_created)),
          });
        }
        updateStateAfterSuccessfulJob(job);
      }

      summary.jobs_processed++;
      if (opts.onJobSuccess) opts.onJobSuccess(job, result);
    } catch (err) {
      const error = err instanceof Error ? err : new Error(String(err));
      log(`\n[JOB FAILED] ${job.title}`);
      log(String(error));
      summary.jobs_failed++;
      summary.failed_titles.push(job.title);
      if (getJobProcessingMode(job) === "single_episode") { const k = getJobOngoingProgressKey(job); if (k) blocked.add(k); }
      if (opts.onJobFailure) opts.onJobFailure(job, error);
    }
  }

  log("\n=== ALL JOBS FINISHED ===");
  return summary;
}
