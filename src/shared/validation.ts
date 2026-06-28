import type { Job, Config } from "./types";
import {
  REQUIRED_ENV_VARS,
  VK_REQUIRED_ENV_VARS,
  VK_PUBLIC_ENV_VARS,
  VK_PRIVATE_ENV_VARS,
  BASE_REQUIRED_TOOLS,
} from "./constants";

export function validateRequiredEnv(_config: Config, jobs: Job[]): void {
  const deliveryEnabled = { s3: false, vk: false };
  let requiresPrivateVk = false;

  for (const job of jobs) {
    const delivery = job.delivery ?? {};
    if (delivery.s3_enabled !== false) deliveryEnabled.s3 = true;
    if (delivery.vk_enabled) {
      deliveryEnabled.vk = true;
      if ((delivery.vk_privacy_view ?? 0) === 5) requiresPrivateVk = true;
    }
  }

  const requiredVars: string[] = [];
  if (deliveryEnabled.s3) requiredVars.push(...REQUIRED_ENV_VARS);
  if (deliveryEnabled.vk) {
    requiredVars.push(...VK_REQUIRED_ENV_VARS, ...VK_PUBLIC_ENV_VARS);
    if (requiresPrivateVk) requiredVars.push(...VK_PRIVATE_ENV_VARS);
  }

  const missing = requiredVars.filter(name => !Bun.env[name]);
  if (missing.length > 0) {
    throw new Error(`Missing required environment variables: ${missing.join(", ")}`);
  }
}

export function validateRequiredTools(_config: Config, jobs: Job[]): void {
  const requiredTools = [...BASE_REQUIRED_TOOLS];
  if (jobs.some(j => j.source.type === "magnet")) {
    requiredTools.push("aria2c");
  }

  const missing: string[] = [];
  for (const tool of requiredTools) {
    const result = Bun.spawnSync([tool, "-version"], { stdout: null, stderr: null });
    if (result.exitCode !== 0 && result.exitCode !== 1) {
      // ffprobe returns exit code 1 for -version but still works
      const result2 = Bun.spawnSync([tool], { stdout: null, stderr: null });
      if (result2.exitCode !== 0) missing.push(tool);
    }
  }

  if (missing.length > 0) {
    throw new Error(`Missing required tools: ${missing.join(", ")}`);
  }
}

export function validateRequiredFiles(_config: Config): void {
  // Watermark and banner are optional — only validate if referenced
}
