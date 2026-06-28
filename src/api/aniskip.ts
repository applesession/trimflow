import type { AniSkipResult, AniSkipSegment } from "../shared/types";

async function requestAniskipSegments(
  malId: number,
  episodeNumber: number,
  episodeLength: number,
  skipTypes: string[],
) {
  const typesQuery = skipTypes.map(t => `types=${t}`).join("&");
  const url = `https://api.aniskip.com/v2/skip-times/${malId}/${episodeNumber}?${typesQuery}&episodeLength=${episodeLength}`;

  console.log(`[AniSkip] ${url}`);

  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(20000) });
    if (!response.ok) throw new Error(`AniSkip HTTP ${response.status}`);
    const data = (await response.json()) as { results?: { interval?: { startTime?: number; endTime?: number }; skipType?: string; skip_type?: string }[] };

    const results: AniSkipSegment[] = [];
    for (const item of data.results ?? []) {
      const interval = item.interval ?? {};
      const start = interval.startTime;
      const end = interval.endTime;
      const skipType = item.skipType ?? item.skip_type;
      if (start !== undefined && end !== undefined) {
        results.push({ type: skipType ?? "unknown", start: Number(start), end: Number(end), source: "aniskip_exact", confidence: "high" });
      }
    }
    results.sort((a, b) => a.start - b.start);

    return { segments: results, error: null, requested_episode_length: episodeLength, request_url: url };
  } catch (err) {
    console.log("[AniSkip ERROR]", err);
    return { segments: [], error: String(err), requested_episode_length: episodeLength, request_url: url };
  }
}

function groupSegmentsByType(segments: AniSkipSegment[]): Map<string, AniSkipSegment> {
  const grouped = new Map<string, AniSkipSegment>();
  for (const segment of segments) {
    grouped.set(segment.type, segment);
  }
  return grouped;
}

export async function getAniskipSegments(
  malId: number,
  episodeNumber: number,
  episodeLength: number,
  skipTypes: string[],
): Promise<AniSkipResult> {
  const primaryResult = await requestAniskipSegments(malId, episodeNumber, episodeLength, skipTypes);

  const exactSegments = primaryResult.segments.map(s => ({ ...s, source: "aniskip_exact" as const, confidence: "high" as const }));
  const segmentsByType = groupSegmentsByType(exactSegments);
  const requestUrls = [primaryResult.request_url];
  const errors: string[] = [];

  if (primaryResult.error) {
    errors.push(primaryResult.error);
    return {
      segments: [...segmentsByType.values()],
      per_type_sources: Object.fromEntries(skipTypes.map(t => [t, segmentsByType.get(t)?.source ?? "not_found"])),
      used_fallback: false,
      request_error: errors.join("; ") || null,
      requested_episode_length: episodeLength,
      fallback_from_episode_length: null,
      request_urls: requestUrls,
      provider: "aniskip",
    };
  }

  const missingTypes = skipTypes.filter(t => !segmentsByType.has(t));

  let fallbackUsed = false;
  let fallbackFromEpisodeLength: number | null = null;

  if (missingTypes.length > 0) {
    fallbackUsed = true;
    fallbackFromEpisodeLength = episodeLength;
    const fallbackResult = await requestAniskipSegments(malId, episodeNumber, 0, missingTypes);
    requestUrls.push(fallbackResult.request_url);

    if (fallbackResult.error) {
      errors.push(fallbackResult.error);
    } else {
      const fallbackSegments = fallbackResult.segments.map(s => ({ ...s, source: "aniskip_lengthless" as const, confidence: "high" as const }));
      for (const segment of fallbackSegments) {
        if (!segmentsByType.has(segment.type)) {
          segmentsByType.set(segment.type, segment);
        }
      }
    }
  }

  return {
    segments: [...segmentsByType.values()],
    per_type_sources: Object.fromEntries(skipTypes.map(t => [t, segmentsByType.get(t)?.source ?? "not_found"])),
    used_fallback: fallbackUsed,
    request_error: errors.length > 0 ? errors.join("; ") : null,
    requested_episode_length: episodeLength,
    fallback_from_episode_length: fallbackFromEpisodeLength,
    request_urls: requestUrls,
    provider: "aniskip",
  };
}
