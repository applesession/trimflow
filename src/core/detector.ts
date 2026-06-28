import type { EpisodeInfo, DetectorContext, DetectorInputs, DetectorConfig, ProviderResult } from "../shared/types";

// ============================================================================
// Subprocess wrapper around lib/detector.py
// ============================================================================

export function getDetectorSupportStatus(): { supported: boolean; reason: string | null } {
  // Check Python is available
  const pyCheck = Bun.spawnSync(["python3", "-c", "import numpy, librosa"], {
    stdout: null,
    stderr: null,
  });
  if (pyCheck.exitCode !== 0) {
    return { supported: false, reason: "detector_dependencies_missing:numpy,librosa" };
  }

  // Check ffmpeg
  const ffCheck = Bun.spawnSync(["ffmpeg", "-version"], { stdout: null, stderr: null });
  if (ffCheck.exitCode !== 0 && ffCheck.exitCode !== 1) {
    return { supported: false, reason: "ffmpeg_not_available" };
  }

  return { supported: true, reason: null };
}

export function buildDetectorContext(
  episodeInfos: EpisodeInfo[],
  config: DetectorConfig,
  tempDir: string,
  detectorInputs: DetectorInputs,
): DetectorContext {
  if (!config.enabled) {
    return buildEmptyContext("timing_detection_disabled", config, tempDir);
  }

  if (episodeInfos.length < config.min_support_episodes) {
    return buildEmptyContext("not_enough_episodes_for_detector", config, tempDir);
  }

  const support = getDetectorSupportStatus();
  if (!support.supported) {
    return buildEmptyContext(support.reason ?? "detector_not_supported", config, tempDir);
  }

  // Serialize inputs and call Python detector
  const inputPayload = {
    episode_infos: episodeInfos.map(e => ({
      episode: e.episode,
      path: e.path,
      duration: e.duration,
    })),
    config: {
      mode: config.mode,
      search_head_seconds: config.search_head_seconds,
      search_tail_seconds: config.search_tail_seconds,
      min_support_episodes: config.min_support_episodes,
      frame_step_seconds: config.frame_step_seconds,
      min_segment_seconds: config.min_segment_seconds,
      max_segment_seconds: config.max_segment_seconds,
      feature_sample_rate: config.feature_sample_rate,
      feature_hop_length: config.feature_hop_length,
      consensus_min_similarity: config.consensus_min_similarity,
      pair_match_min_seconds: config.pair_match_min_seconds,
      cache_enabled: config.cache_enabled,
      cache_dir: config.cache_dir,
      detector_version: config.detector_version,
      auto_cut_min_confidence: config.auto_cut_min_confidence,
    },
    temp_dir: tempDir,
    detector_inputs: {
      aniskip_by_episode: Object.fromEntries(
        Object.entries(detectorInputs.aniskip_by_episode).map(([k, v]) => [k, providerResultToPayload(v)]),
      ),
      anilibria_by_episode: Object.fromEntries(
        Object.entries(detectorInputs.anilibria_by_episode).map(([k, v]) => [k, providerResultToPayload(v)]),
      ),
    },
  };

  const pythonPath = Bun.env.PYTHON_PATH || "python3";

  const proc = Bun.spawnSync(
    [pythonPath, "src/detector.py"],
    {
      stdin: Buffer.from(JSON.stringify(inputPayload)),
      stdout: "pipe",
      stderr: "pipe",
    },
  );

  if (proc.exitCode !== 0) {
    const stderr = new TextDecoder().decode(proc.stderr);
    console.error(`[DETECTOR] Python subprocess failed: ${stderr}`);
    return buildEmptyContext(`detector_subprocess_error: exit ${proc.exitCode}`, config, tempDir);
  }

  try {
    const result = JSON.parse(new TextDecoder().decode(proc.stdout)) as Record<string, unknown>;
    return result as unknown as DetectorContext;
  } catch (err) {
    console.error(`[DETECTOR] Failed to parse subprocess output: ${err}`);
    return buildEmptyContext(`detector_parse_error: ${err}`, config, tempDir);
  }
}

function providerResultToPayload(result: ProviderResult): Record<string, unknown> {
  return {
    segments: result.segments.map(s => ({
      type: s.type,
      start: s.start,
      end: s.end,
      source: s.source ?? "unknown",
    })),
    request_error: result.request_error,
    request_urls: result.request_urls,
    provider: result.provider,
  };
}

function buildEmptyContext(reason: string, config: DetectorConfig, tempDir: string): DetectorContext {
  return {
    enabled: config.enabled,
    available: false,
    reason,
    config,
    results: { op: {}, ed: {} },
    reference_episodes: { op: [], ed: [] },
    reference_intervals: { op: null, ed: null },
    consensus_scores: { op: null, ed: null },
    zone_confidences: { op: "none", ed: "none" },
    input_reference_episodes: { op: [], ed: [] },
    analysis_dir: `${tempDir}/timing_detection`,
    cache_key: "",
    cache_root: "",
  };
}

export function getDetectorTypeResult(
  detectorContext: DetectorContext,
  episodeNumber: number,
  skipType: string,
) {
  return detectorContext.results?.[skipType]?.[episodeNumber] ?? null;
}
