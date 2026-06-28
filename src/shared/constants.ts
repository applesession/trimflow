import type { ConfigTimingDetection, ConfigAutomation, ConfigDelivery } from "./types";

export const CONFIG_PATH = "config.json";
export const DEFAULT_JOBS_PATH = "jobs.json";
export const DEFAULT_COMPLETED_JOBS_PATH = "completed_jobs.json";
export const DEFAULT_STATE_PATH = "state.json";
export const DEFAULT_RUNTIME_DIR = ".runtime";
export const DEFAULT_LOGS_DIR = "logs";
export const DEFAULT_CRON_LOCK_NAME = "cron.lock";
export const DEFAULT_CRON_LOG_NAME = "cron.log";
export const DEFAULT_TELEGRAM_LOG_NAME = "telegram_bot.log";
export const DEFAULT_RUNTIME_STATUS_NAME = "runtime_status.json";
export const DEFAULT_RUNTIME_ERRORS_NAME = "runtime_errors.json";
export const DEFAULT_RUNTIME_ERRORS_LIMIT = 20;
export const TEMP_ROOT = "./temp";
export const SUPPORTED_VIDEO_EXTENSIONS = new Set([".mkv", ".mp4"]);

export const REQUIRED_ENV_VARS = [
  "S3_ENDPOINT",
  "S3_ACCESS_KEY_ID",
  "S3_SECRET_ACCESS_KEY",
  "S3_REGION",
  "S3_BUCKET_NAME",
] as const;

export const VK_REQUIRED_ENV_VARS = [
  "VK_ACCESS_TOKEN",
  "VK_API_VERSION",
] as const;

export const VK_PUBLIC_ENV_VARS = [
  "VK_PUBLIC_GROUP_ID",
] as const;

export const VK_PRIVATE_ENV_VARS = [
  "VK_PRIVATE_GROUP_ID",
] as const;

export const BASE_REQUIRED_TOOLS = ["ffmpeg", "ffprobe"] as const;

export const DEFAULT_TIMING_DETECTION: ConfigTimingDetection = {
  enabled: false,
  mode: "audio_fingerprint",
  search_head_seconds: 300,
  search_tail_seconds: 210,
  min_support_episodes: 3,
  frame_step_seconds: 0.25,
  min_segment_seconds: 45,
  max_segment_seconds: 150,
  feature_sample_rate: 16000,
  feature_hop_length: 0,
  consensus_min_similarity: 0.78,
  pair_match_min_seconds: 35,
  cache_enabled: true,
  cache_dir: null,
  detector_version: "v2",
  auto_cut_min_confidence: "high",
};

export const DEFAULT_AUTOMATION: ConfigAutomation = {
  enabled: true,
  provider: "aniliberty",
  jobs_path: "./jobs.json",
  completed_jobs_path: "./completed_jobs.json",
  state_path: "./state.json",
  poll_limit: 50,
  download_root: "./downloads",
  default_source_type: "magnet",
};

export const DEFAULT_TELEGRAM_STATE_PATH = "telegram_state.json";

export const DEFAULT_DELIVERY: ConfigDelivery = {
  s3_enabled: true,
  s3_upload_video: false,
  s3_upload_timestamps: false,
  s3_upload_manifest: true,
  vk_enabled: true,
  vk_wall_post_enabled: true,
  vk_comment_enabled: true,
  vk_privacy_view: 0,
  vk_comment_banner_path: "./assets/banner.png",
  vk_comment_template: "",
};

export const VALID_PRIVACY_VALUES = new Set([0, 1, 2, 3, 5]);
