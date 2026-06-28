import { exec, run } from "../shared/helpers";
import type { AudioStream, RemoveSegment, KeepSegment, Subsegment } from "../shared/types";

// ============================================================================
// Audio stream detection
// ============================================================================

export function detectAudioStreams(videoPath: string): AudioStream[] {
  try {
    const result = Bun.spawnSync([
      "ffprobe", "-v", "error",
      "-select_streams", "a",
      "-show_entries", "stream=index:stream_tags=language,title,handler_name:stream_disposition=default",
      "-of", "json",
      videoPath,
    ], { stdout: "pipe", stderr: "pipe" });

    if (result.exitCode !== 0) return [];

    const data = JSON.parse(new TextDecoder().decode(result.stdout));
    const streams: AudioStream[] = [];

    for (let audioIndex = 0; audioIndex < (data.streams ?? []).length; audioIndex++) {
      const stream = data.streams[audioIndex];
      const tags = stream.tags ?? {};
      const disposition = stream.disposition ?? {};
      streams.push({
        audio_index: audioIndex,
        stream_index: stream.index ?? 0,
        language: tags.language ?? null,
        title: tags.title ?? null,
        handler_name: tags.handler_name ?? null,
        is_default: Boolean(disposition.default ?? 0),
      });
    }
    return streams;
  } catch {
    return [];
  }
}

function buildLanguageVariants(preferred: string): Set<string> {
  const normalized = preferred.trim().toLowerCase();
  const aliasMap: Record<string, Set<string>> = {
    rus: new Set(["rus", "ru", "russian", "russkiy", "рус", "русский", "дубляж", "озвучка"]),
    ru: new Set(["rus", "ru", "russian", "russkiy", "рус", "русский", "дубляж", "озвучка"]),
    russian: new Set(["rus", "ru", "russian", "russkiy", "рус", "русский", "дубляж", "озвучка"]),
    jpn: new Set(["jpn", "jp", "japanese", "nihongo", "япон", "японский"]),
    jp: new Set(["jpn", "jp", "japanese", "nihongo", "япон", "японский"]),
    japanese: new Set(["jpn", "jp", "japanese", "nihongo", "япон", "японский"]),
  };
  return aliasMap[normalized] ?? (normalized ? new Set([normalized]) : new Set());
}

export function getPreferredAudioStream(videoPath: string, preferredLanguage = "rus"): number {
  const streams = detectAudioStreams(videoPath);
  if (streams.length === 0) return 0;

  const preferredVariants = buildLanguageVariants(preferredLanguage);
  let bestMatch: { audioIndex: number; score: number } | null = null;

  for (const stream of streams) {
    let score = 0;
    const lang = stream.language?.toLowerCase();
    const title = stream.title?.toLowerCase();
    const handler = stream.handler_name?.toLowerCase();

    if (lang && preferredVariants.has(lang)) score = 3;
    else if (title && preferredVariants.has(title)) score = 2;
    else if (handler && preferredVariants.has(handler)) score = 1;

    if (stream.is_default) score += 1;

    if (score > 0 && (!bestMatch || score > bestMatch.score)) {
      bestMatch = { audioIndex: stream.audio_index, score };
    }
  }

  if (bestMatch) return bestMatch.audioIndex;

  // Return default stream if any
  for (const stream of streams) {
    if (stream.is_default) return stream.audio_index;
  }

  return 0;
}

// ============================================================================
// ffprobe
// ============================================================================

export function ffprobeDuration(videoPath: string): number {
  const result = Bun.spawnSync([
    "ffprobe", "-v", "error",
    "-show_entries", "format=duration",
    "-of", "default=noprint_wrappers=1:nokey=1",
    videoPath,
  ], { stdout: "pipe", stderr: "pipe" });

  if (result.exitCode !== 0) {
    throw new Error(`ffprobe failed on ${videoPath}`);
  }

  return parseFloat(new TextDecoder().decode(result.stdout).trim());
}

// ============================================================================
// Keyframes
// ============================================================================

export function getKeyframes(videoPath: string): number[] {
  const result = Bun.spawnSync([
    "ffprobe", "-v", "error",
    "-select_streams", "v:0",
    "-show_entries", "packet=pts_time,flags",
    "-of", "csv=p=0",
    videoPath,
  ], { stdout: "pipe", stderr: "pipe" });

  if (result.exitCode !== 0) return [];

  const lines = new TextDecoder().decode(result.stdout).trim().split("\n").filter(Boolean);
  const keyframes: number[] = [0];

  for (const line of lines) {
    const parts = line.split(",");
    if (parts.length >= 2) {
      const pts = parseFloat(parts[0]!);
      const flags = parts[1]!;
      if (flags.includes("K") && !isNaN(pts) && pts > 0) {
        keyframes.push(pts);
      }
    }
  }

  return keyframes;
}

// ============================================================================
// Segment manipulation
// ============================================================================

export function snapRemoveSegmentsToKeyframes(
  removeSegments: RemoveSegment[],
  keyframes: number[],
): RemoveSegment[] {
  if (keyframes.length === 0) return removeSegments;

  return removeSegments.map(seg => {
    let snapStart = seg.start;
    let snapEnd = seg.end;

    for (const kf of keyframes) {
      if (kf <= seg.start) snapStart = kf;
    }
    for (let i = keyframes.length - 1; i >= 0; i--) {
      if (keyframes[i]! >= seg.end) snapEnd = keyframes[i]!;
    }

    return { ...seg, start: snapStart, end: snapEnd };
  });
}

export function buildKeepSegments(
  duration: number,
  removeSegments: RemoveSegment[],
): KeepSegment[] {
  const sorted = [...removeSegments].sort((a, b) => a.start - b.start);
  const keep: KeepSegment[] = [];
  let current = 0;

  for (const seg of sorted) {
    if (seg.start > current) {
      keep.push({ start: current, end: seg.start });
    }
    current = Math.max(current, seg.end);
  }

  if (current < duration) {
    keep.push({ start: current, end: duration });
  }

  return keep;
}

export function buildHybridSubsegments(
  interval: KeepSegment,
  removeSegments: RemoveSegment[],
  boundaryWindow: number,
): Subsegment[] {
  const subsegments: Subsegment[] = [];
  let pos = interval.start;

  for (const seg of removeSegments.filter(s => s.start >= interval.start && s.end <= interval.end).sort((a, b) => a.start - b.start)) {
    // Before removed segment
    const beforeEnd = seg.start;
    if (beforeEnd > pos) {
      const beforeStart = Math.max(pos, beforeEnd - boundaryWindow);
      if (beforeStart < beforeEnd) {
        if (beforeStart > pos) {
          subsegments.push({ start: pos, end: beforeStart, cut_mode: "copy" });
        }
        subsegments.push({ start: beforeStart, end: beforeEnd, cut_mode: "precise" });
      } else {
        subsegments.push({ start: pos, end: beforeEnd, cut_mode: "copy" });
      }
    }
    pos = seg.end;

    // After removed segment
    const afterEnd = Math.min(interval.end, pos + boundaryWindow);
    if (afterEnd > pos) {
      subsegments.push({ start: pos, end: afterEnd, cut_mode: "precise" });
      pos = afterEnd;
    }
  }

  // Remaining
  if (pos < interval.end) {
    subsegments.push({ start: pos, end: interval.end, cut_mode: "copy" });
  }

  return mergeAdjacentCopySubsegments(subsegments);
}

function mergeAdjacentCopySubsegments(subsegments: Subsegment[]): Subsegment[] {
  if (subsegments.length <= 1) return subsegments;
  const merged: Subsegment[] = [];
  let current = subsegments[0]!;

  for (let i = 1; i < subsegments.length; i++) {
    const next = subsegments[i]!;
    if (current.cut_mode === "copy" && next.cut_mode === "copy") {
      current = { start: current.start, end: next.end, cut_mode: "copy" };
    } else {
      merged.push(current);
      current = next;
    }
  }
  merged.push(current);
  return merged;
}

export function capSubsegmentDurations(
  subsegments: Subsegment[],
  maxSeconds: number,
): Subsegment[] {
  const result: Subsegment[] = [];
  for (const seg of subsegments) {
    const duration = seg.end - seg.start;
    if (duration <= maxSeconds) {
      result.push(seg);
    } else {
      // Split into chunks
      for (let pos = seg.start; pos < seg.end; pos += maxSeconds) {
        const end = Math.min(seg.end, pos + maxSeconds);
        result.push({ start: pos, end, cut_mode: seg.cut_mode });
      }
    }
  }
  return result;
}

// ============================================================================
// Rendering
// ============================================================================

export function renderSegment(
  inputPath: string,
  outputPath: string,
  start: number,
  end: number,
  options: {
    segmentEncoding?: Record<string, unknown>;
    audioStreamIndex?: number;
  } = {},
): void {
  const encoding = options.segmentEncoding ?? {};
  const cutMode = (encoding.cut_mode as string) ?? "precise";
  const videoCodec = (encoding.video_codec as string) ?? "libx264";
  const preset = (encoding.preset as string) ?? "medium";
  const cq = String(encoding.cq ?? 18);
  const audioCodec = (encoding.audio_codec as string) ?? "aac";
  const audioBitrate = (encoding.audio_bitrate as string) ?? "192k";
  const pixelFormat = (encoding.pixel_format as string) || "yuv420p";
  const ai = options.audioStreamIndex ?? 0;

  const args: string[] = [
    "ffmpeg", "-y", "-v", "error",
    "-ss", start.toFixed(3),
    "-to", (end - start).toFixed(3),
    "-i", inputPath,
    "-map", "0:v:0",
    "-map", `0:a:${ai}`,
  ];

  if (cutMode === "copy") {
    args.push("-c", "copy");
  } else {
    args.push(
      "-c:v", videoCodec,
      "-preset", preset,
      "-cq", cq,
      "-pix_fmt", pixelFormat,
      "-c:a", audioCodec,
      "-b:a", audioBitrate,
    );
  }

  args.push(outputPath);
  run(args);
}

export function renderConcat(concatFilePath: string, outputPath: string): void {
  run([
    "ffmpeg", "-y", "-v", "error",
    "-f", "concat",
    "-safe", "0",
    "-i", concatFilePath,
    "-c", "copy",
    outputPath,
  ]);
}

export function renderFinal(
  concatOutputPath: string,
  watermarkPath: string,
  outputVideoPath: string,
  options: {
    encoding?: Record<string, unknown>;
    audioStreamIndex?: number;
  } = {},
): void {
  const encoding = options.encoding ?? {};
  const videoCodec = (encoding.video_codec as string) ?? "h264_nvenc";
  const preset = (encoding.preset as string) ?? "fast";
  const cq = String(encoding.cq ?? 23);
  const audioCodec = (encoding.audio_codec as string) ?? "aac";
  const ai = options.audioStreamIndex ?? 0;

  const args: string[] = [
    "ffmpeg", "-y", "-v", "error",
    "-i", concatOutputPath,
    "-i", watermarkPath,
    "-map", "0:v:0",
    "-map", `0:a:${ai}`,
  ];

  // Apply watermark overlay with NVENC
  args.push(
    "-filter_complex", "[1:v]format=rgba,colorchannelmixer=aa=0.5[wm];[0:v][wm]overlay=W-w-10:H-h-10:format=auto,format=yuv420p",
    "-c:v", videoCodec,
    "-preset", preset,
    "-cq", cq,
    "-c:a", audioCodec,
    outputVideoPath,
  );

  run(args);
}
