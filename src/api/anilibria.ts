import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { SocksProxyAgent } from "socks-proxy-agent";
import type { ReleasePayload, ReleaseEpisode, ReleaseVariant, AniLibriaResult, Segment } from "../shared/types";

const API_BASE_URL = "https://aniliberty.top/api/v1";
const TORRENTS_PAGE_URLS = [
  "https://www.anilibria.top/anime/torrents",
  "https://aniliberty.top/anime/torrents",
  "https://anilibria.top/anime/torrents",
];
const DEFAULT_HEADERS = {
  "User-Agent": "workspace-gojo-satoru/2.0 (+https://aniliberty.top)",
  "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
};

function getProxyUrl(): string | null {
  return Bun.env.ANILIBERTY_PROXY_URL?.trim() || null;
}

function createAgent(url: string): unknown | undefined {
  const proxy = getProxyUrl();
  if (!proxy) return undefined;

  const parsed = new URL(url);
  // Only use proxy agent for HTTPS targets with SOCKS proxy
  if (proxy.startsWith("socks") && parsed.protocol === "https:") {
    return new SocksProxyAgent(proxy);
  }
  return undefined;
}

async function nodeFetch(url: string, timeout = 20): Promise<{ text: string; status: number }> {
  const parsed = new URL(url);
  const agent = createAgent(url) as SocksProxyAgent | undefined;

  return new Promise((resolve, reject) => {
    const req = httpsRequest(
      url,
      {
        headers: DEFAULT_HEADERS,
        agent,
        timeout: timeout * 1000,
      },
      (res) => {
        let data = "";
        res.on("data", chunk => data += chunk);
        res.on("end", () => resolve({ text: data, status: res.statusCode ?? 0 }));
        res.on("error", reject);
      },
    );
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(); reject(new Error(`Request timeout: ${url}`)); });
    req.end();
  });
}

async function requestJson(url: string, timeout = 20): Promise<{ data: unknown; requestUrl: string }> {
  const { text, status } = await nodeFetch(url, timeout);
  if (status < 200 || status >= 300) throw new Error(`AniLibria HTTP ${status} for ${url}`);
  return { data: JSON.parse(text), requestUrl: url };
}

async function requestText(url: string, timeout = 30): Promise<{ text: string; requestUrl: string }> {
  const { text, status } = await nodeFetch(url, timeout);
  if (status < 200 || status >= 300) throw new Error(`AniLibria HTTP ${status} for ${url}`);
  return { text, requestUrl: url };
}

// ============================================================================
// Recent releases
// ============================================================================

function normalizeReleaseList(data: unknown): ReleasePayload[] {
  if (Array.isArray(data)) return data as ReleasePayload[];
  if (typeof data === "object" && data !== null) {
    const obj = data as Record<string, unknown>;
    for (const key of ["data", "items", "list", "releases"]) {
      if (Array.isArray(obj[key])) return obj[key] as ReleasePayload[];
    }
    return [data as ReleasePayload];
  }
  return [];
}

async function buildRecentReleasesFromApi(limit: number, urls: string[], errors: string[]): Promise<ReleasePayload[]> {
  const attempts: [string, string | null][] = [
    [`${API_BASE_URL}/anime/releases/latest`, `limit=${limit}`],
    [`${API_BASE_URL}/anime/releases/latest`, null],
  ];

  for (const [path, query] of attempts) {
    try {
      const url = query ? `${path}?${query}` : path;
      const { data, requestUrl } = await requestJson(url);
      urls.push(requestUrl);
      const releases = normalizeReleaseList(data);
      if (releases.length > 0) return releases;
    } catch (err) {
      errors.push(`${path}: ${err}`);
    }
  }
  return [];
}

function extractReleaseAliasesFromPage(html: string): string[] {
  const aliases: string[] = [];
  const seen = new Set<string>();
  const patterns = [/\/anime\/releases\/release\/([^/"'?]+)/g, /\/anime\/releases\/(?!release\/)([^/"'?]+)/g];
  for (const pattern of patterns) {
    for (const match of html.matchAll(pattern)) {
      const alias = match[1]!.trim();
      if (!seen.has(alias)) { seen.add(alias); aliases.push(alias); }
    }
  }
  return aliases;
}

async function buildRecentReleasesFromPage(limit: number, urls: string[], errors: string[]): Promise<ReleasePayload[]> {
  let aliases: string[] = [];
  for (const pageUrl of TORRENTS_PAGE_URLS) {
    try {
      const { text, requestUrl } = await requestText(pageUrl);
      urls.push(requestUrl);
      aliases = extractReleaseAliasesFromPage(text);
      if (aliases.length > 0) break;
    } catch (err) {
      errors.push(`torrents_page: ${err}`);
    }
  }

  if (aliases.length === 0) return [];

  const releases: ReleasePayload[] = [];
  for (const alias of aliases) {
    if (releases.length >= limit) break;
    try {
      const details = await getReleaseDetails(alias);
      urls.push(details.request_url);
      const releasePayload = details.release;
      if (typeof releasePayload === "object" && releasePayload !== null) {
        releases.push(releasePayload);
      }
    } catch (err) {
      errors.push(`anime/releases/${alias}: ${err}`);
    }
  }
  return releases;
}

export async function listRecentReleases(limit = 50) {
  const urls: string[] = [];
  const errors: string[] = [];

  let releases = await buildRecentReleasesFromApi(limit, urls, errors);
  if (releases.length === 0) {
    releases = await buildRecentReleasesFromPage(limit, urls, errors);
  }

  if (releases.length === 0) {
    const error = errors.length > 0 ? errors.join("; ") : "no_releases_found";
    throw new Error(`AniLibria recent releases lookup failed: ${error}`);
  }

  releases.sort((a, b) => {
    const aDate = a.fresh_at ?? a.updated_at ?? a.created_at ?? "";
    const bDate = b.fresh_at ?? b.updated_at ?? b.created_at ?? "";
    return bDate.localeCompare(aDate);
  });

  return { releases: releases.slice(0, limit), request_urls: urls };
}

// ============================================================================
// Release details
// ============================================================================

async function getReleasePayload(idOrAlias: string | number): Promise<{ data: unknown; requestUrl: string }> {
  return requestJson(`${API_BASE_URL}/anime/releases/${idOrAlias}`);
}

export async function getReleaseDetails(idOrAlias: string | number) {
  const { data, requestUrl } = await getReleasePayload(idOrAlias);
  return { release: data as ReleasePayload, request_url: requestUrl };
}

// ============================================================================
// Release search
// ============================================================================

function extractNames(payload: Record<string, unknown>): string[] {
  const values: string[] = [];
  const names = (payload.names ?? payload.name ?? {}) as Record<string, string>;
  if (typeof names === "object") {
    for (const key of ["ru", "en", "english", "alternative", "main"]) {
      const v = names[key];
      if (typeof v === "string" && v.trim()) values.push(v.trim());
    }
  }
  const title = payload.title;
  if (typeof title === "string" && title.trim()) values.push(title.trim());
  const alias = payload.alias;
  if (typeof alias === "string" && alias.trim()) values.push(alias.trim());

  const seen = new Set<string>();
  return values.filter(v => { const l = v.toLowerCase(); if (seen.has(l)) return false; seen.add(l); return true; });
}

function matchTitle(payload: Record<string, unknown>, title: string, aliases: string[]): boolean {
  const wanted = new Set([title.trim().toLowerCase(), ...aliases.filter(a => typeof a === "string" && a.trim()).map(a => a.trim().toLowerCase())]);
  const available = new Set(extractNames(payload).map(n => n.toLowerCase()));
  for (const w of wanted) { if (available.has(w)) return true; }
  return false;
}

async function findRelease(title: string, season: number | null, aliases: string[]) {
  const urls: string[] = [];
  const errors: string[] = [];
  const candidates: ReleasePayload[] = [];

  const searchAttempts: [string, string][] = [
    [`${API_BASE_URL}/app/search/releases`, `query=${encodeURIComponent(title)}`],
    [`${API_BASE_URL}/anime/releases/list`, `aliases=${encodeURIComponent([title, ...aliases].filter(Boolean).join(","))}`],
  ];

  for (const [path, query] of searchAttempts) {
    try {
      const { data, requestUrl } = await requestJson(`${path}?${query}`);
      urls.push(requestUrl);
      candidates.push(...normalizeReleaseList(data));
    } catch (err) {
      errors.push(`${path}: ${err}`);
    }
  }

  for (const candidate of candidates) {
    if (!matchTitle(candidate as Record<string, unknown>, title, aliases)) continue;
    if (season !== null) {
      const sv = (candidate as Record<string, unknown>).season_number;
      if (sv !== undefined && String(sv) !== String(season)) continue;
    }
    const releaseId = (candidate as Record<string, unknown>).id ?? (candidate as Record<string, unknown>).release_id;
    const releaseAlias = (candidate as Record<string, unknown>).alias as string | undefined;
    if (releaseId !== undefined || releaseAlias) return { release: candidate, urls, error: null };
  }

  return { release: null, urls, error: errors.length > 0 ? errors.join("; ") : "release_not_found" };
}

// ============================================================================
// Episode skips
// ============================================================================

function findEpisodePayload(releasePayload: ReleasePayload, episodeNumber: number): ReleaseEpisode | null {
  const episodes = releasePayload.episodes;
  if (!Array.isArray(episodes)) return null;

  for (const candidate of episodes) {
    const candidateNumber = candidate.number ?? candidate.episode ?? candidate.ordinal;
    if (candidateNumber === undefined) continue;
    if (Number(candidateNumber) === Number(episodeNumber)) return candidate;
  }
  return null;
}

function normalizeSkipInterval(skipType: string, payload: Record<string, unknown>): Segment | null {
  const start = payload.start ?? payload.from ?? payload.startTime ?? payload.start_time;
  const end = payload.end ?? payload.to ?? payload.stop ?? payload.endTime ?? payload.end_time;
  if (start === undefined || end === undefined) return null;
  try {
    return { type: skipType, start: Number(start), end: Number(end), source: "anilibria_exact", confidence: "high" };
  } catch {
    return null;
  }
}

function collectSkipSegments(payload: Record<string, unknown>): Segment[] {
  const candidates: Record<string, unknown>[] = [];
  if (payload.skips && typeof payload.skips === "object") candidates.push(payload.skips as Record<string, unknown>);
  candidates.push(payload);

  const segments: Segment[] = [];
  for (const item of candidates) {
    for (const [skipType, aliases] of [["op", ["op", "opening"]], ["ed", ["ed", "ending"]]] as [string, string[]][]) {
      for (const alias of aliases) {
        if (!(alias in item)) continue;
        const seg = normalizeSkipInterval(skipType, item[alias] as Record<string, unknown>);
        if (seg) { segments.push(seg); break; }
      }
    }
  }

  const deduped = new Map<string, Segment>();
  for (const seg of segments) {
    deduped.set(`${seg.type}|${seg.start}|${seg.end}`, seg);
  }
  return [...deduped.values()].sort((a, b) => a.start - b.start);
}

export async function getAnilibriaSegments(
  title: string,
  season: number,
  episodeNumber: number,
  _source?: Record<string, unknown>,
  aliases: string[] = [],
): Promise<AniLibriaResult> {
  const requestUrls: string[] = [];

  const { release: releaseStub, urls: releaseUrls, error: releaseError } = await findRelease(title, season, aliases);
  requestUrls.push(...releaseUrls);

  if (!releaseStub) {
    return { segments: [], request_error: `AniLibria release lookup failed: ${releaseError}`, request_urls: requestUrls, provider: "anilibria" };
  }

  const releaseIdOrAlias = (releaseStub as Record<string, unknown>).alias ?? (releaseStub as Record<string, unknown>).id ?? (releaseStub as Record<string, unknown>).release_id;
  try {
    const details = await getReleaseDetails(releaseIdOrAlias as string | number);
    const releasePayload = details.release;
    requestUrls.push(details.request_url);

    const episodePayload = findEpisodePayload(releasePayload, episodeNumber);
    if (!episodePayload) {
      return { segments: [], request_error: "AniLibria episode lookup failed: episode_not_found", request_urls: requestUrls, provider: "anilibria" };
    }

    let segments = collectSkipSegments(episodePayload as Record<string, unknown>);
    if (segments.length === 0) {
      segments = collectSkipSegments(releasePayload as Record<string, unknown>);
    }

    return {
      segments,
      request_error: segments.length > 0 ? null : "AniLibria returned no skip data",
      request_urls: requestUrls,
      provider: "anilibria",
    };
  } catch (err) {
    return { segments: [], request_error: `AniLibria release details failed: ${err}`, request_urls: requestUrls, provider: "anilibria" };
  }
}

// ============================================================================
// Release variant extraction (for discovery)
// ============================================================================

export function collectReleaseEpisodeNumbers(releasePayload: ReleasePayload): number[] {
  const episodes = releasePayload.episodes;
  if (!Array.isArray(episodes)) return [];

  const numbers = new Set<number>();
  for (const item of episodes) {
    const num = item.number ?? item.episode ?? item.ordinal;
    if (num === undefined) continue;
    const parsed = Number(num);
    if (Number.isNaN(parsed) || parsed <= 0) continue;
    numbers.add(parsed);
  }
  return [...numbers].sort((a, b) => a - b);
}

function parseVariantCodec(value: unknown): "avc" | "hevc" | null {
  const text = String(value ?? "").trim().toLowerCase();
  if (!text) return null;
  if (["avc", "x264", "h.264", "h264"].some(m => text.includes(m))) return "avc";
  if (["hevc", "x265", "h.265", "h265"].some(m => text.includes(m))) return "hevc";
  return null;
}

function extractVariantCodec(payload: Record<string, unknown>): "avc" | "hevc" | null {
  for (const value of [payload.codec, payload.video_codec, payload.videoCodec, payload.label, payload.title, payload.name]) {
    const codec = parseVariantCodec(value);
    if (codec) return codec;
  }
  return null;
}

function findMagnetValue(payload: unknown): string | null {
  if (typeof payload === "string") {
    const v = payload.trim();
    return v.startsWith("magnet:?") ? v : null;
  }
  if (Array.isArray(payload)) {
    for (const item of payload) { const m = findMagnetValue(item); if (m) return m; }
  }
  if (typeof payload === "object" && payload !== null) {
    for (const value of Object.values(payload)) { const m = findMagnetValue(value); if (m) return m; }
  }
  return null;
}

function parseVariantResolution(payload: Record<string, unknown>): string | null {
  for (const key of ["resolution", "quality", "video_quality", "videoQuality"]) {
    const v = payload[key];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return null;
}

function extractVariantLabel(payload: Record<string, unknown>): string | null {
  for (const key of ["label", "quality_label", "qualityLabel", "title", "name"]) {
    const v = payload[key];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  const codec = extractVariantCodec(payload);
  const resolution = parseVariantResolution(payload);
  const parts = [codec?.toUpperCase(), resolution].filter(Boolean);
  return parts.length > 0 ? parts.join(" ") : null;
}

function extractVariantLabelEpisodeNumbers(label: string | null): number[] | null {
  if (!label) return null;
  const matches = [...label.matchAll(/\[(\d{1,3})(?:\s*-\s*(\d{1,3}))?\]/g)];
  if (matches.length === 0) return null;
  const lastMatch = matches[matches.length - 1]!;
  const start = Number(lastMatch[1]);
  const end = lastMatch[2] ? Number(lastMatch[2]) : start;
  if (start <= 0 || end <= 0 || start > end) return null;
  return Array.from({ length: end - start + 1 }, (_, i) => start + i);
}

function* iterReleaseVariantPayloads(releasePayload: ReleasePayload): Generator<Record<string, unknown>> {
  const seen = new Set<unknown>();
  const variants: unknown[] = [];

  function push(candidate: unknown) {
    if (typeof candidate !== "object" || candidate === null || seen.has(candidate)) return;
    seen.add(candidate);
    variants.push(candidate);
  }

  const torrents = releasePayload.torrents;
  if (Array.isArray(torrents)) { for (const item of torrents) push(item); }
  else if (typeof torrents === "object" && torrents !== null) {
    for (const value of Object.values(torrents)) {
      if (Array.isArray(value)) { for (const item of value) push(item); }
      else push(value);
    }
  }

  for (const key of ["qualities", "quality", "variants", "versions"]) {
    const value = (releasePayload as Record<string, unknown>)[key];
    if (Array.isArray(value)) { for (const item of value) push(item); }
    else if (typeof value === "object" && value !== null) {
      for (const nested of Object.values(value as Record<string, unknown>)) {
        if (Array.isArray(nested)) { for (const item of nested) push(item); }
        else push(nested);
      }
    }
  }

  const torrent = (releasePayload as Record<string, unknown>).torrent;
  if (typeof torrent === "object" && torrent !== null) {
    const legacyMagnet = findMagnetValue(torrent);
    if (legacyMagnet) {
      yield* [{ codec: "avc", label: "legacy", magnet: legacyMagnet, episodes: releasePayload.episodes }];
    }
  }

  yield* variants as Record<string, unknown>[];
}

export function extractReleaseSourceVariants(releasePayload: ReleasePayload): ReleaseVariant[] {
  const variants: ReleaseVariant[] = [];
  const deduped = new Set<string>();
  const releaseEpisodes = collectReleaseEpisodeNumbers(releasePayload);

  for (const candidate of iterReleaseVariantPayloads(releasePayload)) {
    const magnet = findMagnetValue(candidate);
    const codec = extractVariantCodec(candidate);
    const label = extractVariantLabel(candidate);
    const labelEpisodes = extractVariantLabelEpisodeNumbers(label);

    let episodes = releaseEpisodes;
    if (labelEpisodes) {
      const labelSet = new Set(labelEpisodes);
      episodes = releaseEpisodes.filter(e => labelSet.has(e));
    }

    if (!magnet || !codec || episodes.length === 0) continue;

    const identity = `${magnet}|${codec}|${episodes.join(",")}`;
    if (deduped.has(identity)) continue;
    deduped.add(identity);

    variants.push({
      codec,
      resolution: parseVariantResolution(candidate),
      magnet,
      available_episodes: episodes,
      label: label ?? undefined,
    });
  }

  return variants;
}

export function selectReleaseSourceVariant(releasePayload: ReleasePayload): ReleaseVariant {
  const variants = extractReleaseSourceVariants(releasePayload);
  if (variants.length === 0) throw new Error("no_supported_torrent_variant");

  for (const preferredCodec of ["avc", "hevc"] as const) {
    const preferred = variants.filter(v => v.codec === preferredCodec && v.magnet && v.available_episodes.length > 0);
    if (preferred.length > 0) {
      preferred.sort((a, b) => {
        const maxA = Math.max(...a.available_episodes);
        const maxB = Math.max(...b.available_episodes);
        if (maxB !== maxA) return maxB - maxA;
        if (b.available_episodes.length !== a.available_episodes.length) return b.available_episodes.length - a.available_episodes.length;
        return (a.resolution ?? "").localeCompare(b.resolution ?? "") || (a.label ?? "").localeCompare(b.label ?? "");
      });
      return preferred[0]!;
    }
  }

  throw new Error("no_supported_torrent_variant");
}

export function extractReleaseTitle(releasePayload: ReleasePayload): string {
  const names = (releasePayload.name ?? releasePayload.names ?? {}) as Record<string, string>;
  if (typeof names === "object") {
    for (const key of ["english", "en", "main", "ru", "alternative"]) {
      const v = names[key];
      if (typeof v === "string" && v.trim()) return v.trim();
    }
  }
  if (typeof releasePayload.title === "string" && releasePayload.title.trim()) return releasePayload.title.trim();
  if (typeof releasePayload.alias === "string" && releasePayload.alias.trim()) return releasePayload.alias.trim();
  throw new Error("missing_title");
}

export function extractReleaseTitleRu(releasePayload: ReleasePayload): string | null {
  const names = (releasePayload.name ?? releasePayload.names ?? {}) as Record<string, string>;
  if (typeof names === "object") {
    for (const key of ["main", "ru"]) {
      const v = names[key];
      if (typeof v === "string" && v.trim()) return v.trim();
    }
  }
  return null;
}

export function extractReleaseSeason(releasePayload: ReleasePayload): number {
  const season = releasePayload.season_number ?? releasePayload.seasonNumber;
  const parsed = Number(season);
  return Number.isNaN(parsed) || parsed < 1 ? 1 : parsed;
}

export function extractReleaseMalId(releasePayload: ReleasePayload): number | null {
  const directKeys = ["mal_id", "malId", "myanimelist_id", "myanimelistId"];
  for (const key of directKeys) {
    const v = (releasePayload as Record<string, unknown>)[key];
    const parsed = parsePositiveInt(v);
    if (parsed !== null) return parsed;
  }

  const nestedCandidates: [string, string][] = [
    ["external_ids", "mal_id"], ["external_ids", "malId"], ["external_ids", "myanimelist"],
    ["external_ids", "myanimelist_id"], ["externalIds", "mal_id"], ["externalIds", "myanimelist"],
    ["codes", "mal"], ["codes", "mal_id"], ["player", "mal_id"], ["player", "myanimelist"],
    ["metadata", "mal_id"],
  ];
  for (const [parentKey, childKey] of nestedCandidates) {
    const parent = (releasePayload as Record<string, unknown>)[parentKey];
    if (typeof parent !== "object" || parent === null) continue;
    const parsed = parsePositiveInt((parent as Record<string, unknown>)[childKey]);
    if (parsed !== null) return parsed;
  }
  return null;
}

function parsePositiveInt(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isNaN(parsed) || parsed <= 0 ? null : parsed;
}
