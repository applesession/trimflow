import { listRecentReleases, getReleaseDetails, selectReleaseSourceVariant, extractReleaseTitle, extractReleaseTitleRu, extractReleaseSeason, extractReleaseMalId } from "../api/anilibria";
import { initDb, getJobs, insertJob, updateJob, findBlacklistItem, recordSkippedItem, getTrackedEpisodeKeys, getOngoingProgress, markEpisodesQueued, getQueuedReleaseEpisodesCount, getCompletedReleaseEpisodesCount, getSkippedItemsCount, getBlacklistCount } from "../shared/db";
import { ensureNonEmptySlug, formatEpisodesRange, parseEpisodesRange } from "../shared/helpers";
import type { Job, ReleasePayload, ReleaseVariant, DiscoverySummary } from "../shared/types";

export function utcNowIso(): string { return new Date().toISOString(); }

// ============================================================================
// Job identity
// ============================================================================

export function getJobProcessingMode(job: Job): string { return (job.processing_mode ?? "compilation").trim().toLowerCase(); }
export function getJobReleaseId(job: Job): number | null { const v = job.automation?.release_id; return typeof v === "number" && v > 0 ? v : null; }

function buildSourceVariantIdentity(source: Job["source"]): string {
  const c = (source.variant_codec ?? "").trim().toLowerCase();
  const l = (source.variant_label ?? "").trim().toLowerCase();
  return (!c && !l) ? "" : `${c}|${l}`;
}

function buildJobKey(job: Job): string {
  const s = job.source ?? {};
  const st = (s.type ?? "").trim().toLowerCase();
  let sig = "";
  if (st === "magnet") sig = (s.magnet ?? "").trim();
  else if (st === "local") sig = (s.input_dir ?? "").trim();
  return [(job.title ?? "").trim().toLowerCase(), String(job.season ?? "").trim(),
    getJobProcessingMode(job), st, sig, buildSourceVariantIdentity(s)].join("|");
}

function buildOngoingProgressKey(title: string, season: number | string, sourceType: string): string {
  return [(title ?? "").trim().toLowerCase(), String(season ?? "").trim(), (sourceType ?? "").trim().toLowerCase()].join("|");
}

export function findMatchingJob(jobs: Job[], candidate: Job): Job | undefined {
  const ck = buildJobKey(candidate);
  for (const job of jobs) { if (buildJobKey(job) === ck) return job; }

  const ct = (candidate.title ?? "").trim().toLowerCase();
  const cs = String(candidate.season ?? "").trim();
  const cst = candidate.source?.type?.trim().toLowerCase() ?? "";
  const cm = getJobProcessingMode(candidate);
  const cv = buildSourceVariantIdentity(candidate.source ?? {});

  for (const job of jobs) {
    const jv = buildSourceVariantIdentity(job.source ?? {});
    if ((job.title ?? "").trim().toLowerCase() === ct
      && String(job.season ?? "").trim() === cs
      && (job.source?.type ?? "").trim().toLowerCase() === cst
      && getJobProcessingMode(job) === cm
      && (jv === cv || !jv || !cv)) {
      return job;
    }
  }
  return undefined;
}

// ============================================================================
// Build job from release
// ============================================================================

function buildJobFromRelease(
  releasePayload: ReleasePayload, newEpisodeNumbers: number[],
  automation: { default_source_type?: string; download_root?: string },
  opts: { selectedVariant?: ReleaseVariant; processingMode?: string; automationContext?: Record<string, unknown> } = {},
): Job {
  const title = extractReleaseTitle(releasePayload);
  const st = automation.default_source_type ?? "magnet";
  if (st !== "magnet") throw new Error(`unsupported_source_type:${st}`);
  const variant = opts.selectedVariant ?? selectReleaseSourceVariant(releasePayload);
  if (!variant.magnet) throw new Error("missing_magnet");
  const slug = ensureNonEmptySlug(title);
  const dlRoot = automation.download_root ?? "./downloads";
  const job: Job = {
    title, title_ru: extractReleaseTitleRu(releasePayload) ?? undefined,
    mal_id: extractReleaseMalId(releasePayload) ?? undefined,
    season: extractReleaseSeason(releasePayload), episodes_range: formatEpisodesRange(newEpisodeNumbers),
    processing_mode: opts.processingMode ?? "compilation",
    source: { type: "magnet", magnet: variant.magnet, download_dir: `${dlRoot}/${slug}`.replace(/\\/g, "/"),
      variant_codec: variant.codec, variant_label: variant.label ?? undefined },
  };
  if (opts.automationContext) job.automation = opts.automationContext as Job["automation"];
  return job;
}

// ============================================================================
// Queue management (writes directly to DB — NO "DELETE ALL")
// ============================================================================

function queueDiscoveredJob(currentJobs: Job[], candidateJob: Job): "created" | "updated" | "unchanged" {
  const existing = findMatchingJob(currentJobs, candidateJob);

  if (!existing) {
    const newId = insertJob(candidateJob);
    candidateJob.id = newId;
    currentJobs.push(candidateJob);
    return "created";
  }

  // Check if update needed
  const needsUpdate = Object.keys(candidateJob).some(k => {
    if (k === "id" || k === "source") return false;
    if (k === "automation") return JSON.stringify(existing[k]) !== JSON.stringify(candidateJob[k]);
    return String(existing[k as keyof Job]) !== String(candidateJob[k as keyof Job]);
  });

  if (needsUpdate && existing.id != null) {
    updateJob(existing.id, candidateJob);
    Object.assign(existing, candidateJob);
    return "updated";
  }

  return "unchanged";
}

// ============================================================================
// Main discovery
// ============================================================================

export async function discoverJobs(): Promise<{ jobs: Job[]; summary: DiscoverySummary }> {
  const automation = { enabled: true, provider: "aniliberty", poll_limit: 50, download_root: "./downloads", default_source_type: "magnet" as const };
  const currentJobs = getJobs();

  const summary: DiscoverySummary = {
    created_jobs: 0, updated_jobs: 0, skipped_items: 0,
    queued_release_episodes: 0, completed_release_episodes: 0,
    blacklisted_releases: 0, request_urls: [],
  };

  if (!automation.enabled) {
    summary.status = "disabled";
    return { jobs: currentJobs, summary };
  }

  const releasesResult = await listRecentReleases(automation.poll_limit);
  summary.request_urls = releasesResult.request_urls;

  for (const releaseStub of releasesResult.releases) {
    if (!releaseStub.is_ongoing) continue;

    const releaseIdOrAlias = releaseStub.alias ?? releaseStub.id ?? releaseStub.release_id;
    if (!releaseIdOrAlias) continue;

    let releasePayload: ReleasePayload;
    try { const d = await getReleaseDetails(releaseIdOrAlias as string | number); releasePayload = d.release; }
    catch (err) { recordSkippedItem({ alias: String(releaseIdOrAlias), title: extractReleaseTitle(releaseStub), episodes: [], reason: `release_details_failed: ${err}`, recorded_at: utcNowIso() }); continue; }

    const releaseId = (releasePayload.id ?? releaseStub.id ?? releaseStub.release_id) as number;
    if (releaseId == null) continue;

    if (findBlacklistItem(releaseId)) {
      recordSkippedItem({ release_id: releaseId, alias: releasePayload.alias, title: extractReleaseTitle(releasePayload), episodes: [], reason: "blacklisted_release", recorded_at: utcNowIso() });
      continue;
    }

    let selectedVariant: ReleaseVariant;
    try { selectedVariant = selectReleaseSourceVariant(releasePayload); }
    catch (err) { recordSkippedItem({ release_id: releaseId, alias: releasePayload.alias, title: extractReleaseTitle(releasePayload), episodes: [], reason: String(err), recorded_at: utcNowIso() }); continue; }

    const episodeNumbers = selectedVariant.available_episodes;
    const trackedKeys = getTrackedEpisodeKeys();
    const newEpisodes = episodeNumbers.filter(ep => !trackedKeys.has(`${releaseId}:${String(ep).padStart(3, "0")}`));
    if (newEpisodes.length === 0) continue;

    let baseJob: Job;
    try { baseJob = buildJobFromRelease(releasePayload, episodeNumbers, automation, { selectedVariant }); }
    catch (err) { recordSkippedItem({ release_id: releaseId, alias: releasePayload.alias, title: extractReleaseTitle(releasePayload), episodes: newEpisodes, reason: String(err), recorded_at: utcNowIso() }); continue; }

    const progressKey = buildOngoingProgressKey(baseJob.title, baseJob.season, baseJob.source.type);
    const ongoingProgress = getOngoingProgress(progressKey);
    const hasFullPublish = ongoingProgress?.has_full_publish ?? false;

    const existingComp = findMatchingJob(currentJobs, { ...baseJob, episodes_range: formatEpisodesRange(episodeNumbers), processing_mode: "compilation" });

    const releaseJobs: Job[] = [];

    if (hasFullPublish) {
      const latest = Math.max(...newEpisodes);
      releaseJobs.push(buildJobFromRelease(releasePayload, [latest], automation, { selectedVariant, processingMode: "single_episode",
        automationContext: { provider: automation.provider, release_id: releaseId, is_ongoing: true, ongoing_progress_key: progressKey, publish_strategy: "single_update" } }));
      releaseJobs.push(buildJobFromRelease(releasePayload, episodeNumbers, automation, { selectedVariant, processingMode: "compilation",
        automationContext: { provider: automation.provider, release_id: releaseId, is_ongoing: true, ongoing_progress_key: progressKey, publish_strategy: "full_refresh" } }));
    } else {
      let fullEps = [...episodeNumbers];
      if (existingComp) { const es = parseEpisodesRange(existingComp.episodes_range); for (const e of episodeNumbers) es.add(e); fullEps = [...es].sort((a, b) => a - b); }
      releaseJobs.push(buildJobFromRelease(releasePayload, fullEps, automation, { selectedVariant, processingMode: "compilation",
        automationContext: { provider: automation.provider, release_id: releaseId, is_ongoing: true, ongoing_progress_key: progressKey, publish_strategy: "initial_full" } }));
    }

    for (const candidate of releaseJobs) {
      const result = queueDiscoveredJob(currentJobs, candidate);
      if (result === "created") summary.created_jobs++;
      else if (result === "updated") summary.updated_jobs++;

      if (candidate.automation?.release_id) {
        markEpisodesQueued(candidate.automation.release_id, [...parseEpisodesRange(candidate.episodes_range)]);
      }
    }
  }

  summary.skipped_items = getSkippedItemsCount();
  summary.queued_release_episodes = getQueuedReleaseEpisodesCount();
  summary.completed_release_episodes = getCompletedReleaseEpisodesCount();
  summary.blacklisted_releases = getBlacklistCount();

  return { jobs: currentJobs, summary };
}
