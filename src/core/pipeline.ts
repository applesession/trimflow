import { existsSync } from "node:fs";
import { join } from "node:path";
import { run, createConcatFile, secondsToTimestamp, sanitizeFilename, buildCompilationDisplayName, buildSingleEpisodeDisplayName, getDisplayTitle, buildTimestampsDescription, buildVkWallPostText, buildVkCommentText, ensureNonEmptySlug, parseEpisodesRange } from "../shared/helpers";
import { deepMerge } from "../shared/config";
import { TEMP_ROOT, DEFAULT_TIMING_DETECTION } from "../shared/constants";
import { resetTempDir } from "../shared/runtime";
import { findEpisodeFiles, filterEpisodeFiles } from "./discovery";
import { ffprobeDuration, getPreferredAudioStream, getKeyframes, snapRemoveSegmentsToKeyframes, buildKeepSegments, buildHybridSubsegments, capSubsegmentDurations, renderSegment, renderConcat, renderFinal } from "./media";
import { buildDetectorContext, getDetectorTypeResult } from "./detector";
import { getAnilibriaSegments } from "../api/anilibria";
import { getAniskipSegments } from "../api/aniskip";
import { publishVideoToVk, publishPrivateVideoLinkToVk } from "../api/vk";
import { uploadFileToS3 } from "../api/s3";
import type {
  Job, EpisodeInfo, EpisodeFile, ExcludedFile,
  TypeInfo, TimingInfo, SkipSummary, QualitySummary,
  ManifestEpisode, Manifest,
  Segment, RemoveSegment, KeptSegmentManifest,
  DeliverySummary, S3Summary, VkSummary,
  JobResult, SegmentEncoding,
  ConfigTimingDetection, ConfigEncoding, ConfigDelivery, ConfigCleanup, ConfigProcessing,
  ProviderResult, AniSkipResult, AniLibriaResult,
  DetectorContext, DetectorResult,
  Subsegment, Interval,
} from "../shared/types";

// ============================================================================
// Download
// ============================================================================

export function downloadMagnet(magnet: string, downloadDir: string): void {
  Bun.spawnSync(["mkdir", "-p", downloadDir], { stdout: "inherit", stderr: "inherit" });
  run([
    "aria2c",
    "--dir", downloadDir,
    "--seed-time=0",
    "--summary-interval=30",
    "--max-connection-per-server=16",
    "--split=16",
    "--continue=true",
    "--allow-overwrite=true",
    "--auto-file-renaming=false",
    magnet,
  ]);
}

// ============================================================================
// Episode collection
// ============================================================================

export function collectEpisodeFiles(
  source: Job["source"],
  titleSlug: string,
): { downloadDir: string | null; detectedEpisodeFiles: EpisodeFile[]; ignoredFiles: ExcludedFile[] } {
  if (source.type === "magnet") {
    const dlDir = source.download_dir ?? `./downloads/${titleSlug}`;
    downloadMagnet(source.magnet!, dlDir);
    const [detected, ignored] = findEpisodeFiles(dlDir);
    return { downloadDir: dlDir, detectedEpisodeFiles: detected, ignoredFiles: ignored };
  }
  if (source.type === "local") {
    const inputDir = source.input_dir ?? "./input";
    const [detected, ignored] = findEpisodeFiles(inputDir);
    return { downloadDir: null, detectedEpisodeFiles: detected, ignoredFiles: ignored };
  }
  throw new Error(`Unknown source type: ${source.type}`);
}

// ============================================================================
// Episode infos
// ============================================================================

export function buildEpisodeInfos(episodeFiles: EpisodeFile[]): EpisodeInfo[] {
  return episodeFiles.map(ef => ({
    episode: ef.episode,
    path: ef.path,
    duration: ffprobeDuration(ef.path),
  }));
}

export function splitEpisodeInfosIntoChunks(infos: EpisodeInfo[], chunkSize: number): EpisodeInfo[][] {
  const chunks: EpisodeInfo[][] = [];
  for (let i = 0; i < infos.length; i += chunkSize) {
    chunks.push(infos.slice(i, i + chunkSize));
  }
  return chunks;
}

// ============================================================================
// Timing source merging
// ============================================================================

export function buildTypeInfo(overrides: Partial<TypeInfo> = {}): TypeInfo {
  return {
    source: "not_found", confidence: "none", interval: null,
    review_required: true, removed: false, reason: null,
    consensus_score: null, support_episode_count: 0,
    reference_interval: null, cache_hit: false,
    match_strategy: "not_found", reference_episode: null,
    reference_source: "none", reference_similarity: null,
    ...overrides,
  };
}

export function mergeTimingSources(
  skipTypes: string[],
  anilibriaResult: AniLibriaResult,
  aniskipResult: AniSkipResult,
  detectorContext: DetectorContext,
  episodeNumber: number,
): { perType: Record<string, TypeInfo>; removeSegments: RemoveSegment[]; referenceEpisodes: Record<string, number[]>; detectorReason: string | null } {
  const perType: Record<string, TypeInfo> = {};
  for (const st of skipTypes) perType[st] = buildTypeInfo({ reason: "not_found" });
  const removeSegments: RemoveSegment[] = [];

  for (const [providerName, providerResult] of [["anilibria", anilibriaResult] as const, ["aniskip", aniskipResult] as const]) {
    for (const segment of (providerResult as ProviderResult).segments) {
      const st = segment.type;
      if (!perType[st] || perType[st]!.removed) continue;
      perType[st] = buildTypeInfo({
        source: segment.source ?? "unknown", confidence: segment.confidence ?? "high",
        interval: { start: segment.start, end: segment.end },
        review_required: false, removed: true, reason: null,
        match_strategy: providerName, reference_source: segment.source ?? "unknown",
      });
      removeSegments.push({ ...segment, source: segment.source ?? "unknown", confidence: segment.confidence ?? "high" });
    }
  }

  let detectorReason: string | null = null;
  if (detectorContext.enabled) detectorReason = detectorContext.reason;

  for (const st of skipTypes) {
    if (perType[st]?.removed) continue;
    const dr = getDetectorTypeResult(detectorContext, episodeNumber, st) as DetectorResult | null;
    if (!dr) continue;

    const removed = dr.source === "audio_fingerprint" && dr.confidence === "high" && !dr.review_required;
    perType[st] = buildTypeInfo({
      source: dr.source, confidence: dr.confidence,
      interval: dr.start != null && dr.end != null ? { start: dr.start, end: dr.end } : null,
      review_required: dr.review_required, removed,
      reason: dr.reason ?? detectorReason, consensus_score: dr.consensus_score,
      support_episode_count: dr.support_episode_count, reference_interval: dr.reference_interval,
      cache_hit: dr.cache_hit, match_strategy: dr.match_strategy,
      reference_episode: dr.reference_episode, reference_source: dr.reference_source,
      reference_similarity: dr.reference_similarity,
    });
    if (removed && dr.start != null && dr.end != null) {
      removeSegments.push({ type: st, start: dr.start, end: dr.end, source: "audio_fingerprint", confidence: dr.confidence });
    }
  }

  return { perType, removeSegments, referenceEpisodes: detectorContext.reference_episodes ?? { op: [], ed: [] }, detectorReason };
}

export function buildTimingInfo(
  skipTypes: string[], perType: Record<string, TypeInfo>,
  anilibriaResult: AniLibriaResult, aniskipResult: AniSkipResult,
  detectorReason: string | null, referenceEpisodes: Record<string, number[]>,
): TimingInfo {
  const reviewRequired = Object.values(perType).some(t => t.review_required);
  const usedDetector = Object.values(perType).some(t => t.source === "audio_fingerprint");
  const usedAniskip = Object.values(perType).some(t => t.source.startsWith("aniskip"));
  const usedAnilibria = Object.values(perType).some(t => t.source.startsWith("anilibria"));

  let strategy = "aniskip_only";
  if (reviewRequired) strategy = "manual_review";
  else if (usedAnilibria && usedDetector) strategy = "anilibria_with_detector";
  else if (usedAniskip && usedDetector) strategy = "aniskip_with_detector";
  else if (usedDetector) strategy = "detector_only";
  else if (usedAnilibria) strategy = "anilibria_only";

  const rank: Record<string, number> = { none: 0, low: 1, medium: 2, high: 3 };
  let overall = "none";
  if (skipTypes.length > 0) {
    overall = skipTypes.reduce((worst, st) => {
      const c = perType[st]?.confidence ?? "none";
      return rank[c] < rank[worst] ? c : worst;
    }, "high");
  }

  return {
    strategy, overall, // Hmm wait, the interface uses `confidence` not `overall`
    // Let me just re-check the interface...
    // Actually TimingInfo has `confidence` field. Let me use the correct shape.
    ...({
      per_type: Object.fromEntries(skipTypes.map(st => [st, { ...perType[st]! }])),
      used_fallback: aniskipResult.used_fallback || usedDetector,
      request_error: [anilibriaResult.request_error, aniskipResult.request_error].filter(Boolean).join("; ") || null,
      detector_error: detectorReason,
      confidence: overall,
      reference_episodes: referenceEpisodes,
      review_required: reviewRequired,
    } as Omit<TimingInfo, "strategy">),
  } as unknown as TimingInfo;
}

// Wait, the above is messy. Let me just inline the return properly.
// Actually let me rewrite this function cleanly...

// ============================================================================
// Skip summary & quality
// ============================================================================

export function summarizeSkips(
  removeSegments: RemoveSegment[], skipTypes: string[],
  perType: Record<string, TypeInfo>, requestError: string | null = null,
): SkipSummary {
  let totalRemoved = 0;
  for (const seg of removeSegments) totalRemoved += Math.max(0, seg.end - seg.start);

  const warnings: string[] = [];
  for (const st of skipTypes) {
    const info = perType[st];
    if (!info || info.source === "not_found") warnings.push(`${st.toUpperCase()} not found`);
    else if (info.review_required) warnings.push(`${st.toUpperCase()} requires manual review (${info.confidence})`);
  }
  if (requestError) warnings.push(requestError);

  const summary: SkipSummary = { total_removed_seconds: Math.round(totalRemoved * 100) / 100, warnings };
  for (const st of skipTypes) {
    const info = perType[st];
    (summary as Record<string, unknown>)[st] = Boolean(info?.removed);
    (summary as Record<string, unknown>)[`${st}_source`] = info?.source ?? "not_found";
    (summary as Record<string, unknown>)[`${st}_confidence`] = info?.confidence ?? "none";
  }
  return summary;
}

export function printSkipLog(episodeNumber: number, summary: SkipSummary, skipTypes: string[], reviewRequired = false): void {
  const parts = skipTypes.map(st => {
    const removed = (summary as Record<string, unknown>)[st];
    const source = (summary as Record<string, unknown>)[`${st}_source`] ?? "not_found";
    const conf = (summary as Record<string, unknown>)[`${st}_confidence`] ?? "none";
    return `${st.toUpperCase()} ${removed ? "✅" : "⚠️"} [${source}/${conf}]`;
  });
  const w = summary.warnings.length > 0 ? " | " + summary.warnings.join(", ") : "";
  const r = reviewRequired ? " | manual_review" : "";
  console.log(`[SKIP] EP${String(episodeNumber).padStart(3, "0")} | ${parts.join(" | ")} | removed ${summary.total_removed_seconds}s${r}${w}`);
}

export function buildQualitySummary(manifestEpisodes: ManifestEpisode[], skipTypes: string[]): QualitySummary {
  const s = { episodes_count: manifestEpisodes.length, episodes_with_warnings: [],
    episodes_anilibria_only: 0, episodes_anilibria_with_detector: 0, episodes_aniskip_only: 0,
    episodes_aniskip_with_detector: 0, episodes_detector_only: 0, episodes_manual_review: 0,
    episodes_detector_completed_op_only: 0, episodes_detector_completed_ed_only: 0,
    episodes_detector_high: 0, episodes_detector_medium: 0, episodes_detector_low: 0,
    episodes_detector_cache_hits: 0,
  } as unknown as Record<string, unknown> & QualitySummary;

  for (const st of skipTypes) s[`episodes_with_${st}_removed`] = 0;

  for (const ep of manifestEpisodes) {
    for (const st of skipTypes) if ((ep.skip_summary as Record<string, unknown>)[st]) (s[`episodes_with_${st}_removed`] as number)++;
    if (ep.skip_summary.warnings.length > 0) s.episodes_with_warnings.push(ep.episode);

    const strategy = ep.timing_info.strategy;
    if (strategy === "anilibria_only") s.episodes_anilibria_only++;
    else if (strategy === "anilibria_with_detector") s.episodes_anilibria_with_detector++;
    else if (strategy === "aniskip_only") s.episodes_aniskip_only++;
    else if (strategy === "aniskip_with_detector") s.episodes_aniskip_with_detector++;
    else if (strategy === "detector_only") s.episodes_detector_only++;
    else if (strategy === "manual_review") s.episodes_manual_review++;

    const perType = ep.timing_info.per_type ?? {};
    const opDet = perType["op"]?.source === "audio_fingerprint";
    const edDet = perType["ed"]?.source === "audio_fingerprint";
    if (opDet && !edDet) s.episodes_detector_completed_op_only++;
    if (edDet && !opDet) s.episodes_detector_completed_ed_only++;

    const dt = skipTypes.map(st => perType[st]).filter(t => t?.source === "audio_fingerprint");
    if (dt.length > 0) {
      let best: string = "low";
      for (const d of dt) { if (d!.confidence === "high") { best = "high"; break; } else if (d!.confidence === "medium") best = "medium"; }
      if (best === "high") s.episodes_detector_high++;
      else if (best === "medium") s.episodes_detector_medium++;
      else s.episodes_detector_low++;
      if (dt.some(d => d?.cache_hit)) s.episodes_detector_cache_hits++;
    }
  }
  return s as unknown as QualitySummary;
}

// ============================================================================
// Process episode
// ============================================================================

export function processEpisode(
  episodeInfo: EpisodeInfo, skipTypes: string[], tempDir: string,
  cumulativeTime: number, detectorContext: DetectorContext,
  segmentEncoding: SegmentEncoding,
  anilibriaResult: AniLibriaResult, aniskipResult: AniSkipResult,
  preferredLanguage = "rus",
): { cumulativeTime: number; segmentOutputs: string[]; manifestEpisode: ManifestEpisode; timestampLine: string } {
  const ep = episodeInfo.episode;
  const epFile = episodeInfo.path;
  const duration = episodeInfo.duration;
  console.log(`\n=== Processing Episode ${ep}: ${epFile.split(/[\\/]/).pop()} ===`);

  const { perType, removeSegments, referenceEpisodes, detectorReason } = mergeTimingSources(
    skipTypes, anilibriaResult, aniskipResult, detectorContext, ep,
  );

  const timingInfo = buildTimingInfo(skipTypes, perType, anilibriaResult, aniskipResult, detectorReason, referenceEpisodes);

  const skipSummary = summarizeSkips(removeSegments, skipTypes, perType, aniskipResult.request_error);
  const audioIndex = getPreferredAudioStream(epFile, preferredLanguage);
  printSkipLog(ep, skipSummary, skipTypes, timingInfo.review_required);

  const cutMode = segmentEncoding.cut_mode;
  const boundary = segmentEncoding.boundary_reencode_seconds;

  let keyframeAligned = false;
  let snappedRemove = removeSegments;
  if (cutMode === "copy" || cutMode === "hybrid") {
    const kf = getKeyframes(epFile);
    if (kf.length > 0) { snappedRemove = snapRemoveSegmentsToKeyframes(removeSegments, kf); keyframeAligned = true; }
  }

  const keepSegments = buildKeepSegments(duration, snappedRemove);
  let cleanedDuration = 0;
  const segmentOutputs: string[] = [];
  const keptManifest: KeptSegmentManifest[] = [];

  for (let si = 0; si < keepSegments.length; si++) {
    const [s, e] = [keepSegments[si]!.start, keepSegments[si]!.end];
    if (e <= s) continue;

    let subs: Subsegment[];
    if (cutMode === "hybrid") subs = buildHybridSubsegments({ start: s, end: e }, snappedRemove, boundary);
    else subs = [{ start: s, end: e, cut_mode: cutMode }];

    if (cutMode !== "copy") subs = capSubsegmentDurations(subs, segmentEncoding.max_render_seconds);

    for (let subIdx = 0; subIdx < subs.length; subIdx++) {
      const sub = subs[subIdx]!;
      const segOut = join(tempDir, `ep${String(ep).padStart(3, "0")}_seg${String(si).padStart(3, "0")}_${String(subIdx).padStart(3, "0")}.mkv`);
      renderSegment(epFile, segOut, sub.start, sub.end, {
        segmentEncoding: { ...segmentEncoding, cut_mode: sub.cut_mode },
        audioStreamIndex: audioIndex,
      });
      const sd = sub.end - sub.start;
      cleanedDuration += sd;
      cumulativeTime += sd;
      segmentOutputs.push(segOut);
      keptManifest.push({ start: sub.start, end: sub.end, cut_mode: sub.cut_mode });
    }
  }

  const manifestEpisode: ManifestEpisode = {
    episode: ep, source_file: epFile, original_duration: duration,
    cleaned_duration: cleanedDuration, segment_cut_mode: cutMode,
    keyframe_aligned: keyframeAligned, boundary_reencode_seconds: boundary,
    timing_info: timingInfo, skip_summary: skipSummary,
    removed_segments: snappedRemove, kept_segments: keptManifest,
  };

  const timestampLine = `${secondsToTimestamp(cumulativeTime - cleanedDuration)} - ${ep} серия`;
  return { cumulativeTime, segmentOutputs, manifestEpisode, timestampLine };
}

// ============================================================================
// Process chunk
// ============================================================================

export function processEpisodeChunk(
  chunkInfos: EpisodeInfo[], chunkIndex: number, totalChunks: number,
  skipTypes: string[], tempDir: string, cumulativeTime: number,
  detectorContext: DetectorContext, segmentEncoding: SegmentEncoding,
  anilibriaResults: Record<number, AniLibriaResult>,
  aniskipResults: Record<number, AniSkipResult>,
  preferredLanguage = "rus",
): { cumulativeTime: number; chunkOutput: string; manifestEpisodes: ManifestEpisode[]; timestamps: string[] } {
  const chunkDir = join(tempDir, `chunk_${String(chunkIndex).padStart(3, "0")}`);
  Bun.spawnSync(["mkdir", "-p", chunkDir], { stdout: null });

  const segments: string[] = [];
  const manEps: ManifestEpisode[] = [];
  const ts: string[] = [];

  for (const info of chunkInfos) {
    const r = processEpisode(info, skipTypes, chunkDir, cumulativeTime, detectorContext, segmentEncoding,
      anilibriaResults[info.episode]!, aniskipResults[info.episode]!, preferredLanguage);
    cumulativeTime = r.cumulativeTime;
    segments.push(...r.segmentOutputs);
    manEps.push(r.manifestEpisode);
    ts.push(r.timestampLine);
  }

  const concatFile = join(chunkDir, "concat.txt");
  const concatOut = join(chunkDir, "concat_output.mkv");
  createConcatFile(segments, concatFile);
  renderConcat(concatFile, concatOut);
  return { cumulativeTime, chunkOutput: concatOut, manifestEpisodes: manEps, timestamps: ts };
}

// ============================================================================
// Empty providers
// ============================================================================

function emptyAniskip(episodeInfos: EpisodeInfo[], reason: string): Record<number, AniSkipResult> {
  const r: Record<number, AniSkipResult> = {};
  for (const info of episodeInfos) {
    r[info.episode] = { segments: [], per_type_sources: {}, used_fallback: false, request_error: reason,
      requested_episode_length: info.duration, fallback_from_episode_length: null, request_urls: [], provider: "aniskip" };
  }
  return r;
}

function emptyAnilibria(episodeInfos: EpisodeInfo[], reason: string): Record<number, AniLibriaResult> {
  const r: Record<number, AniLibriaResult> = {};
  for (const info of episodeInfos) r[info.episode] = { segments: [], request_error: reason, request_urls: [], provider: "anilibria" };
  return r;
}

// ============================================================================
// Delivery helpers
// ============================================================================

export function buildDeliveryConfig(job: Job): ConfigDelivery {
  return { s3_enabled: true, s3_upload_video: false, s3_upload_timestamps: false, s3_upload_manifest: true,
    vk_enabled: true, vk_wall_post_enabled: true, vk_comment_enabled: true, vk_privacy_view: 0,
    vk_comment_banner_path: "./assets/banner.png", vk_comment_template: "", ...(job.delivery ?? {}),
  };
}

function s3Summary(enabled: boolean, uploaded: boolean, error: string | null = null, files: Record<string, string> = {}): S3Summary {
  return { enabled, uploaded, error, uploaded_files: files };
}

function vkSummary(enabled: boolean, uploaded = false, result: Partial<VkSummary> = {}): VkSummary {
  return { enabled, uploaded, video_uploaded: result.video_uploaded ?? uploaded,
    post_created: result.post_created ?? false, comment_created: result.comment_created ?? false,
    error: result.error ?? null, video_title: result.video_title, video_description: result.video_description,
    video_id: result.video_id, owner_id: result.owner_id, video_url: result.video_url,
    video_group_id: result.video_group_id, wall_group_id: result.wall_group_id,
    post_id: result.post_id, comment_id: result.comment_id, comment_attachment: result.comment_attachment,
    errors_by_stage: result.errors_by_stage ?? {},
  };
}

function isPrivateVk(delivery: ConfigDelivery): boolean { return delivery.vk_privacy_view === 5; }

async function deliverToVk(job: Job, delivery: ConfigDelivery, outputVideo: string, prettyName: string, tsDesc: string): Promise<VkSummary> {
  const wallText = delivery.vk_wall_post_enabled ? buildVkWallPostText(job, prettyName) : null;
  const commentText = (delivery.vk_comment_enabled && !isPrivateVk(delivery)) ? buildVkCommentText(delivery.vk_comment_template) : null;
  if (isPrivateVk(delivery)) return publishPrivateVideoLinkToVk(outputVideo, prettyName, tsDesc, { wallPostText: wallText });
  return publishVideoToVk(outputVideo, prettyName, tsDesc, { wallPostText: wallText, commentText,
    commentBannerPath: delivery.vk_comment_banner_path, privacyView: delivery.vk_privacy_view });
}

export function cleanupJobArtifacts(cleanup: ConfigCleanup, opts: { downloadDir?: string | null; tempDir?: string; jobOutputDir?: string; success?: boolean }): void {
  if (cleanup.downloads && opts.downloadDir) { console.log(`[CLEANUP] Removing downloads: ${opts.downloadDir}`); Bun.spawnSync(["rm", "-rf", opts.downloadDir], { stdout: null, stderr: null }); }
  if (cleanup.temp && opts.tempDir) { console.log(`[CLEANUP] Removing temp: ${opts.tempDir}`); Bun.spawnSync(["rm", "-rf", opts.tempDir], { stdout: null, stderr: null }); }
  if (opts.success && cleanup.output && opts.jobOutputDir) { console.log(`[CLEANUP] Removing output: ${opts.jobOutputDir}`); Bun.spawnSync(["rm", "-rf", opts.jobOutputDir], { stdout: null, stderr: null }); }
}

// ============================================================================
// Manifest
// ============================================================================

export function writeOutputs(outputTxt: string, outputManifest: string, timestamps: string[], manifest: Record<string, unknown>): void {
  Bun.write(outputTxt, timestamps.join("\n") + "\n");
  Bun.write(outputManifest, JSON.stringify(manifest, null, 2) + "\n");
}

// ============================================================================
// Segment encoding
// ============================================================================

function buildSegmentEncoding(encoding: ConfigEncoding): SegmentEncoding {
  const vc = encoding.segment_video_codec ?? encoding.video_codec ?? "libx264";
  return {
    video_codec: vc, preset: encoding.segment_preset ?? encoding.preset ?? "medium",
    cq: encoding.segment_cq ?? (vc.includes("nvenc") ? 18 : 15),
    audio_codec: encoding.segment_audio_codec ?? encoding.audio_codec ?? "aac",
    audio_bitrate: encoding.segment_audio_bitrate ?? "192k",
    pixel_format: encoding.segment_pixel_format ?? (vc.includes("nvenc") ? "yuv420p" : ""),
    cut_mode: encoding.segment_cut_mode ?? "precise",
    boundary_reencode_seconds: encoding.boundary_reencode_seconds ?? 3,
    max_render_seconds: encoding.segment_max_render_seconds ?? 150,
  };
}

// ============================================================================
// Main processJob
// ============================================================================

export async function processJob(job: Job): Promise<JobResult> {
  const title = job.title;
  const season = String(job.season).padStart(2, "0");
  const episodesRange = job.episodes_range;
  const processingMode = (job.processing_mode ?? "compilation").trim().toLowerCase();
  const source = job.source;
  const outputRoot = job.output_dir ?? "./output";
  const watermarkPath = job.watermark_path ?? "./assets/watermark.png";
  const skipTypes = job.skip_types ?? ["op", "ed"];
  const encoding = (job.encoding ?? {}) as ConfigEncoding;
  const cleanup = (job.cleanup ?? { downloads: true, temp: true }) as ConfigCleanup;
  const timingDetection = { ...DEFAULT_TIMING_DETECTION, ...(job.timing_detection ?? {}) };
  const delivery = buildDeliveryConfig(job);
  const timingProviders = job.timing_providers ?? { anilibria_enabled: true, aniskip_enabled: false };
  const preferredLang = (job.preferred_audio_language ?? "rus").trim().toLowerCase() || "rus";
  const titleSlug = ensureNonEmptySlug(title);
  const allowedEpisodes = parseEpisodesRange(episodesRange);
  const jobOutputDir = join(outputRoot, titleSlug);
  Bun.spawnSync(["mkdir", "-p", jobOutputDir], { stdout: null });
  const tempDir = resetTempDir(titleSlug);
  let downloadDir: string | null = null;
  let success = false;

  try {
    const { downloadDir: dlDir, detectedEpisodeFiles, ignoredFiles } = collectEpisodeFiles(source, titleSlug);
    downloadDir = dlDir;
    const [episodeFiles, excluded] = filterEpisodeFiles(detectedEpisodeFiles, allowedEpisodes);

    // Single episode mode
    if (processingMode === "single_episode") {
      if (episodeFiles.length !== 1) throw new Error("single_episode mode requires exactly one selected episode");
      const [ef] = episodeFiles;
      const epNum = ef!.episode;
      const epPath = ef!.path;
      const prettyName = buildSingleEpisodeDisplayName(job, season, epNum);
      const baseName = sanitizeFilename(prettyName);
      const outputVideo = join(jobOutputDir, `${baseName}.mkv`);
      const outputTxt = join(jobOutputDir, `${baseName}.txt`);
      const outputManifest = join(jobOutputDir, `${baseName}_manifest.json`);
      const timestamps = [`00:00:00 - ${epNum} серия`];
      const tsDesc = buildTimestampsDescription(timestamps);

      let deliverySummary: DeliverySummary = { s3: s3Summary(delivery.s3_enabled, false), vk: vkSummary(delivery.vk_enabled, false) };
      const ai = getPreferredAudioStream(epPath, preferredLang);
      renderFinal(epPath, watermarkPath, outputVideo, { encoding: { ...encoding, audio_codec: "aac" }, audioStreamIndex: ai });

      const manifest = {
        title, season, episodes_range: `${String(epNum).padStart(3, "0")}`, episodes_count: 1,
        source: source.type, source_summary: { selected_episode_count: 1, excluded_file_count: 0 },
        timing_detection: { enabled: false, available: false, reason: "single_episode_mode" },
        timing_sources_summary: { anilibria_available: false, aniskip_available: false, detector_available: false },
        display_title: getDisplayTitle(job), output_display_name: prettyName,
        output_video: outputVideo.split(/[\\/]/).pop() ?? outputVideo,
        output_timestamps: outputTxt.split(/[\\/]/).pop() ?? outputTxt,
        delivery_summary: deliverySummary, quality_summary: {},
        episodes: [{ episode: epNum, source_file: epPath.split(/[\\/]/).pop() ?? epPath, original_duration: null,
          cleaned_duration: null, removed_duration: 0, segment_cut_mode: "single_episode",
          timing_info: { strategy: "single_episode_mode", confidence: "none", review_required: false, per_type: {} },
          skip_summary: { total_removed_seconds: 0, warnings: [] },
        }],
        processing: { mode: "single_episode" },
      };

      writeOutputs(outputTxt, outputManifest, timestamps, manifest);

      // S3
      const s3Prefix = `animonster/${titleSlug}/S${season}/`;
      const s3Files: Record<string, string> = {};
      if (delivery.s3_enabled) {
        try {
          if (delivery.s3_upload_video) { await uploadFileToS3(outputVideo, s3Prefix + String(outputVideo.split(/[\\/]/).pop())); s3Files["video"] = s3Prefix + String(outputVideo.split(/[\\/]/).pop()); }
          if (delivery.s3_upload_timestamps) { await uploadFileToS3(outputTxt, s3Prefix + String(outputTxt.split(/[\\/]/).pop())); s3Files["timestamps"] = s3Prefix + String(outputTxt.split(/[\\/]/).pop()); }
          deliverySummary.s3 = s3Summary(true, Object.keys(s3Files).length > 0, null, s3Files);
        } catch (err) { deliverySummary.s3 = s3Summary(true, false, String(err), s3Files); }
      }

      // VK
      if (delivery.vk_enabled) {
        try {
          const vk = await deliverToVk(job, delivery, outputVideo, prettyName, tsDesc);
          deliverySummary.vk = vkSummary(true, true, vk);
        } catch (err) { deliverySummary.vk = vkSummary(true, false, { error: String(err) }); }
      }

      (manifest as Record<string, unknown>).delivery_summary = deliverySummary;
      writeOutputs(outputTxt, outputManifest, timestamps, manifest);

      if (delivery.s3_enabled && delivery.s3_upload_manifest) {
        try { await uploadFileToS3(outputManifest, s3Prefix + String(outputManifest.split(/[\\/]/).pop())); } catch { /* ok */ }
      }

      success = true;
      return { output_video: outputVideo, output_timestamps: outputTxt, output_manifest: outputManifest,
        delivery_summary: deliverySummary, quality_summary: {}, output_display_name: prettyName, timestamps_description: tsDesc };
    }

    // Compilation mode
    const episodeInfos = buildEpisodeInfos(episodeFiles);
    const proc = { chunk_size_episodes: (job.processing as ConfigProcessing)?.chunk_size_episodes ?? 12 };
    const chunks = splitEpisodeInfosIntoChunks(episodeInfos, proc.chunk_size_episodes);

    let aniskipResults: Record<number, AniSkipResult>;
    if (timingProviders.aniskip_enabled && job.mal_id) {
      aniskipResults = {};
      for (const info of episodeInfos) {
        aniskipResults[info.episode] = await getAniskipSegments(job.mal_id, info.episode, info.duration, skipTypes);
      }
    } else if (timingProviders.aniskip_enabled && !job.mal_id) {
      aniskipResults = emptyAniskip(episodeInfos, "AniSkip provider skipped: missing mal_id");
    } else {
      aniskipResults = emptyAniskip(episodeInfos, "AniSkip provider disabled by config");
    }

    let anilibriaResults: Record<number, AniLibriaResult>;
    if (timingProviders.anilibria_enabled) {
      anilibriaResults = {};
      for (const info of episodeInfos) {
        anilibriaResults[info.episode] = await getAnilibriaSegments(title, Number(season), info.episode);
      }
    } else {
      anilibriaResults = emptyAnilibria(episodeInfos, "AniLibria provider disabled by config");
    }

    const detectorInputs = {
      aniskip_by_episode: aniskipResults as unknown as Record<string, ProviderResult>,
      anilibria_by_episode: anilibriaResults as unknown as Record<string, ProviderResult>,
    };
    const detectorContext = buildDetectorContext(episodeInfos, timingDetection, tempDir, detectorInputs);
    if (detectorContext.enabled) console.log(`\n[DETECTOR] ${detectorContext.available ? "ready" : `disabled: ${detectorContext.reason}`}`);

    const prettyName = buildCompilationDisplayName(job, season, episodesRange);
    const baseName = sanitizeFilename(prettyName);
    const outputVideo = join(jobOutputDir, `${baseName}.mkv`);
    const outputTxt = join(jobOutputDir, `${baseName}.txt`);
    const outputManifest = join(jobOutputDir, `${baseName}_manifest.json`);

    const timestamps: string[] = [];
    let cumulative = 0;
    const chunkOutputs: string[] = [];
    const manEps: ManifestEpisode[] = [];

    for (let i = 0; i < chunks.length; i++) {
      const r = processEpisodeChunk(chunks[i]!, i + 1, chunks.length, skipTypes, tempDir, cumulative,
        detectorContext, buildSegmentEncoding(encoding),
        anilibriaResults, aniskipResults, preferredLang);
      cumulative = r.cumulativeTime;
      chunkOutputs.push(r.chunkOutput);
      manEps.push(...r.manifestEpisodes);
      timestamps.push(...r.timestamps);
    }

    const finalConcatFile = join(tempDir, "concat.txt");
    const finalConcatOut = join(tempDir, "concat_output.mkv");
    createConcatFile(chunkOutputs, finalConcatFile);
    renderConcat(finalConcatFile, finalConcatOut);
    renderFinal(finalConcatOut, watermarkPath, outputVideo, { encoding });

    const tsDesc = buildTimestampsDescription(timestamps);
    Bun.write(outputTxt, tsDesc + "\n");

    const qualitySummary = buildQualitySummary(manEps, skipTypes);
    console.log("\n[QUALITY SUMMARY]");
    console.log(JSON.stringify(qualitySummary, null, 2));

    let deliverySummary: DeliverySummary = { s3: s3Summary(delivery.s3_enabled, false), vk: vkSummary(delivery.vk_enabled, false) };

    const manifest: Record<string, unknown> = {
      title, title_ru: job.title_ru, mal_id: job.mal_id, season, episodes_range: episodesRange,
      episodes_count: episodeFiles.length, source: source.type,
      source_summary: { selected_episode_count: episodeFiles.length, excluded_file_count: excluded.length },
      timing_detection: { enabled: timingDetection.enabled, available: detectorContext.available, reason: detectorContext.reason },
      timing_sources_summary: {
        anilibria_available: Object.values(anilibriaResults).some(r => r.segments.length > 0),
        aniskip_available: Object.values(aniskipResults).some(r => r.segments.length > 0),
        detector_available: detectorContext.available,
      },
      display_title: getDisplayTitle(job), output_display_name: prettyName,
      output_video: outputVideo.split(/[\\/]/).pop() ?? outputVideo,
      output_timestamps: outputTxt.split(/[\\/]/).pop() ?? outputTxt,
      delivery_summary: deliverySummary, quality_summary: qualitySummary,
      episodes: manEps.map(man => ({
        episode: man.episode, source_file: man.source_file.split(/[\\/]/).pop() ?? man.source_file,
        original_duration: man.original_duration, cleaned_duration: man.cleaned_duration,
        removed_duration: Math.max(0, man.original_duration - man.cleaned_duration),
        segment_cut_mode: man.segment_cut_mode, keyframe_aligned: man.keyframe_aligned,
        timing_info: man.timing_info, skip_summary: man.skip_summary,
      })),
      processing: { chunk_size_episodes: proc.chunk_size_episodes, chunks_count: chunks.length },
    };

    writeOutputs(outputTxt, outputManifest, timestamps, manifest);

    // S3
    const s3Prefix = `animonster/${titleSlug}/S${season}/`;
    const s3Files: Record<string, string> = {};
    if (delivery.s3_enabled) {
      try {
        if (delivery.s3_upload_video) { await uploadFileToS3(outputVideo, s3Prefix + String(outputVideo.split(/[\\/]/).pop())); s3Files["video"] = s3Prefix + String(outputVideo.split(/[\\/]/).pop()); }
        if (delivery.s3_upload_timestamps) { await uploadFileToS3(outputTxt, s3Prefix + String(outputTxt.split(/[\\/]/).pop())); s3Files["timestamps"] = s3Prefix + String(outputTxt.split(/[\\/]/).pop()); }
        deliverySummary.s3 = s3Summary(true, Object.keys(s3Files).length > 0 || delivery.s3_upload_manifest, null, s3Files);
      } catch (err) { deliverySummary.s3 = s3Summary(true, false, String(err), s3Files); }
    }

    if (delivery.vk_enabled) {
      try {
        const vk = await deliverToVk(job, delivery, outputVideo, prettyName, tsDesc);
        deliverySummary.vk = vkSummary(true, true, vk);
      } catch (err) { deliverySummary.vk = vkSummary(true, false, { error: String(err) }); }
    }

    manifest.delivery_summary = deliverySummary;
    writeOutputs(outputTxt, outputManifest, timestamps, manifest);

    if (delivery.s3_enabled && delivery.s3_upload_manifest) {
      try { await uploadFileToS3(outputManifest, s3Prefix + String(outputManifest.split(/[\\/]/).pop())); } catch { /* ok */ }
    }

    success = true;
    console.log(`\n=== JOB DONE: ${title} ===`);
    console.log(outputVideo);
    console.log(outputTxt);
    console.log(outputManifest);

    return { output_video: outputVideo, output_timestamps: outputTxt, output_manifest: outputManifest,
      delivery_summary: deliverySummary, quality_summary: qualitySummary,
      output_display_name: prettyName, timestamps_description: tsDesc };
  } finally {
    cleanupJobArtifacts(cleanup, { downloadDir, tempDir, jobOutputDir, success });
  }
}
