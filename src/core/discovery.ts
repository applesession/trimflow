import { readdirSync } from "node:fs";
import { join } from "node:path";
import { SUPPORTED_VIDEO_EXTENSIONS } from "../shared/constants";
import type { EpisodeFile, ExcludedFile } from "../shared/types";

const EPISODE_PATTERNS = [
  /^(\d{1,3})\./,
  /\[(\d{1,3})\]/,
  /[Ss]\d{1,2}[Ee](\d{1,3})/,
  /[\s._-](\d{1,3})[\s._-]/,
  /[Ee]pisode[\s._-]*(\d{1,3})/,
];

export function extractEpisodeNumber(filename: string): number | null {
  for (const pattern of EPISODE_PATTERNS) {
    const match = pattern.exec(filename);
    if (match) return Number(match[1]);
  }
  return null;
}

function* walkDir(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      yield* walkDir(full);
    } else if (entry.isFile()) {
      yield full;
    }
  }
}

export function findEpisodeFiles(sourceDir: string): [EpisodeFile[], ExcludedFile[]] {
  const detected: EpisodeFile[] = [];
  const ignored: ExcludedFile[] = [];

  for (const path of walkDir(sourceDir)) {
    const ext = path.slice(path.lastIndexOf(".")).toLowerCase();
    if (!SUPPORTED_VIDEO_EXTENSIONS.has(ext)) continue;

    const filename = path.split(/[\\/]/).pop() ?? "";
    const episodeNumber = extractEpisodeNumber(filename);

    if (episodeNumber !== null) {
      detected.push({ episode: episodeNumber, path });
    } else {
      ignored.push({ path, reason: "episode_number_not_detected" });
    }
  }

  detected.sort((a, b) => a.episode - b.episode);

  if (detected.length === 0) {
    throw new Error(`No episode files found in ${sourceDir}`);
  }

  return [detected, ignored];
}

export function filterEpisodeFiles(
  episodeFiles: EpisodeFile[],
  allowedEpisodes: Set<number>,
): [EpisodeFile[], ExcludedFile[]] {
  const filtered: EpisodeFile[] = [];
  const excluded: ExcludedFile[] = [];
  const detectedEpisodeNumbers: number[] = [];

  for (const [episodeNumber, path] of episodeFiles.map(ef => [ef.episode, ef.path] as const)) {
    detectedEpisodeNumbers.push(episodeNumber);
    if (allowedEpisodes.has(episodeNumber)) {
      filtered.push({ episode: episodeNumber, path });
    } else {
      excluded.push({ episode: episodeNumber, path, reason: "out_of_range" });
    }
  }

  if (filtered.length === 0) {
    const requested = [...allowedEpisodes].sort((a, b) => a - b);
    throw new Error(
      `No episodes remained after applying episodes_range; requested=${JSON.stringify(requested)}; found=${JSON.stringify(detectedEpisodeNumbers)}`,
    );
  }

  return [filtered, excluded];
}
