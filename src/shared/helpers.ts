import { type Subprocess, $ } from "bun";
import type { Job } from "./types";

export function slugify(value: string): string {
  return value
    .trim()
    .replace(/[^\w\s.-]/g, "")
    .replace(/\s+/g, "_");
}

export function ensureNonEmptySlug(title: string): string {
  const slug = slugify(title);
  if (!slug) throw new Error(`Title '${title}' produced an empty slug`);
  return slug;
}

export function getDisplayTitle(job: Record<string, unknown> | null | undefined): string {
  if (!job || typeof job !== "object") return "Без названия";
  for (const key of ["title_ru", "title"]) {
    const value = job[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "Без названия";
}

export function parseEpisodesRange(episodesRange: string): Set<number> {
  if (!episodesRange.trim()) throw new Error("episodes_range must be a non-empty string");

  const allowed = new Set<number>();
  for (const rawPart of episodesRange.split(",")) {
    const part = rawPart.trim();
    if (!part) continue;

    if (part.includes("-")) {
      const [startRaw, endRaw] = part.split("-", 2).map(s => s.trim());
      if (!startRaw || !endRaw || !/^\d+$/.test(startRaw) || !/^\d+$/.test(endRaw)) {
        throw new Error(`Invalid range segment: ${part}`);
      }
      const start = Number(startRaw);
      const end = Number(endRaw);
      if (start > end) throw new Error(`Invalid range segment: ${part}`);
      for (let i = start; i <= end; i++) allowed.add(i);
    } else {
      if (!/^\d+$/.test(part)) throw new Error(`Invalid episode number: ${part}`);
      allowed.add(Number(part));
    }
  }

  if (allowed.size === 0) throw new Error("episodes_range did not contain any episode numbers");
  return allowed;
}

export function formatEpisodesRange(episodes: number[]): string {
  const normalized = [...new Set(episodes.map(Number))].sort((a, b) => a - b);
  if (normalized.length === 0) throw new Error("episodes must contain at least one value");

  const parts: string[] = [];
  let start = normalized[0]!;
  let prev = normalized[0]!;

  for (let i = 1; i < normalized.length; i++) {
    const ep = normalized[i]!;
    if (ep === prev + 1) {
      prev = ep;
    } else {
      parts.push(start === prev ? `${String(start).padStart(3, "0")}` : `${String(start).padStart(3, "0")}-${String(prev).padStart(3, "0")}`);
      start = ep;
      prev = ep;
    }
  }
  parts.push(start === prev ? `${String(start).padStart(3, "0")}` : `${String(start).padStart(3, "0")}-${String(prev).padStart(3, "0")}`);
  return parts.join(",");
}

export function formatEpisodesLabel(episodesRange: string): string {
  const normalizedParts: string[] = [];
  for (const rawPart of episodesRange.split(",")) {
    const part = rawPart.trim();
    if (!part) continue;

    if (part.includes("-")) {
      const [startRaw, endRaw] = part.split("-", 2).map(s => s.trim());
      normalizedParts.push(`${Number(startRaw)}-${Number(endRaw)}`);
    } else {
      normalizedParts.push(String(Number(part)));
    }
  }
  return `${normalizedParts.join(",")} Серия`;
}

export function buildCompilationDisplayName(
  job: Job,
  season: string | number,
  episodesRange: string,
  suffix = "[Без OP/ED]",
): string {
  const displayTitle = getDisplayTitle(job);
  const seasonNumber = Number(season);
  const episodesLabel = formatEpisodesLabel(episodesRange);
  return `${displayTitle} - ${seasonNumber} Сезон ${episodesLabel} ${suffix}`.trim();
}

export function buildSingleEpisodeDisplayName(
  job: Job,
  season: string | number,
  episodeNumber: number,
): string {
  const displayTitle = getDisplayTitle(job);
  const seasonNumber = Number(season);
  return `${displayTitle} - ${seasonNumber} Сезон ${Math.floor(episodeNumber)} Серия`.trim();
}

export function sanitizeFilename(value: string): string {
  let cleaned = value.replace(/\//g, "-");
  cleaned = cleaned.replace(/[<>:"\\|?*]/g, "");
  cleaned = cleaned.replace(/\s+/g, " ").trim().replace(/^[ .]+|[ .]+$/g, "");
  if (!cleaned) throw new Error(`Value '${value}' produced an empty filename`);
  return cleaned;
}

export function buildTimestampsDescription(timestamps: (string | null | undefined)[]): string {
  return timestamps.filter(t => typeof t === "string" && t.trim()).join("\n");
}

export function buildVkWallPostText(job: Job, prettyBaseName: string): string {
  const displayTitle = getDisplayTitle(job);
  if (prettyBaseName.startsWith(displayTitle)) return prettyBaseName;
  return `${displayTitle}\n\n${prettyBaseName}`;
}

export function buildVkCommentText(template: string): string {
  return (template ?? "").trim();
}

export function secondsToTimestamp(seconds: number): string {
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

export function run(cmd: string[]): void {
  console.log("\n[RUN]", cmd.join(" "));
  const result = Bun.spawnSync(cmd.map(String), { stdout: "inherit", stderr: "inherit" });
  if (result.exitCode !== 0) {
    throw new Error(`Command failed with exit code ${result.exitCode}: ${cmd.join(" ")}`);
  }
}

export function createConcatFile(
  segmentFiles: string[],
  outputPath: string,
): void {
  const lines = segmentFiles.map(f => {
    const resolved = f.replace(/'/g, "'\\''");
    return `file '${resolved}'`;
  });
  Bun.write(outputPath, lines.join("\n") + "\n");
}

// Thin wrapper around bun shell for non-inherited subprocess calls
export async function exec(cmd: string[], options?: { cwd?: string }): Promise<{ stdout: string; stderr: string; exitCode: number }> {
  const proc = Bun.spawn(cmd.map(String), {
    cwd: options?.cwd,
    stdout: "pipe",
    stderr: "pipe",
  });
  const stdout = await new Response(proc.stdout).text();
  const stderr = await new Response(proc.stderr).text();
  const exitCode = await proc.exited;
  return { stdout, stderr, exitCode };
}
