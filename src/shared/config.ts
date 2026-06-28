import { readFileSync } from "node:fs";
import type { Config, ConfigAutomation, Job } from "./types";
import { DEFAULT_AUTOMATION, CONFIG_PATH } from "./constants";

export function loadConfig(): Config {
  const raw = readFileSync(CONFIG_PATH, "utf-8");
  return JSON.parse(raw) as Config;
}

export function normalizeAutomationConfig(config: Config): ConfigAutomation {
  return deepMerge(DEFAULT_AUTOMATION, config.automation ?? {}) as ConfigAutomation;
}

export function deepMerge(
  defaults: Record<string, unknown>,
  overrides: Record<string, unknown>,
): Record<string, unknown> {
  const result = structuredClone(defaults);

  for (const [key, value] of Object.entries(overrides)) {
    if (
      typeof value === "object" &&
      value !== null &&
      !Array.isArray(value) &&
      typeof result[key] === "object" &&
      result[key] !== null &&
      !Array.isArray(result[key])
    ) {
      result[key] = deepMerge(
        result[key] as Record<string, unknown>,
        value as Record<string, unknown>,
      );
    } else {
      result[key] = value;
    }
  }

  return result;
}

export function buildJobWithDefaults(job: Job, defaults: Record<string, unknown>): Job {
  return deepMerge(defaults, job as unknown as Record<string, unknown>) as unknown as Job;
}
