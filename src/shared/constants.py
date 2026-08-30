from pathlib import Path


CONFIG_PATH = Path("config.json")
DEFAULT_JOBS_PATH = Path("jobs.json")
DEFAULT_COMPLETED_JOBS_PATH = Path("completed_jobs.json")
DEFAULT_STATE_PATH = Path("state.json")
DEFAULT_RUNTIME_DIR = Path(".runtime")
DEFAULT_LOGS_DIR = Path("logs")
DEFAULT_CRON_LOCK_NAME = "cron.lock"
DEFAULT_DISCOVERY_LOCK_NAME = "discovery.lock"
DEFAULT_CRON_LOG_NAME = "cron.log"
DEFAULT_TELEGRAM_LOG_NAME = "telegram_bot.log"
DEFAULT_RUNTIME_STATUS_NAME = "runtime_status.json"
DEFAULT_RUNTIME_ERRORS_NAME = "runtime_errors.json"
DEFAULT_RUNTIME_ERRORS_LIMIT = 20
TEMP_ROOT = Path("./temp")
SUPPORTED_VIDEO_EXTENSIONS = {".avi", ".mkv", ".mp4"}
SUPPORTED_EXTERNAL_AUDIO_EXTENSIONS = {
    ".mka", ".m4a", ".aac", ".flac", ".ac3", ".eac3", ".wav", ".ogg", ".opus", ".mp3",
}
REQUIRED_ENV_VARS = [
    "S3_ENDPOINT",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_REGION",
    "S3_BUCKET_NAME",
]
VK_REQUIRED_ENV_VARS = [
    "VK_ACCESS_TOKEN",
    "VK_API_VERSION",
]
VK_PUBLIC_ENV_VARS = [
    "VK_PUBLIC_GROUP_ID",
]
VK_PRIVATE_ENV_VARS = [
    "VK_PRIVATE_GROUP_ID",
]
WAVESPEED_REQUIRED_ENV_VARS = [
    "WAVESPEED_API_KEY",
]
BASE_REQUIRED_TOOLS = ["ffmpeg", "ffprobe"]
DEFAULT_DOWNLOAD = {
    "timeout_minutes_per_episode": 20,
    "timeout_minutes_minimum": 30,
    "timeout_minutes_maximum": 1440,
}
DEFAULT_TIMING_DETECTION = {
    "enabled": False,
    "mode": "audio_fingerprint",
    "search_head_seconds": 300,
    "search_tail_seconds": 210,
    "min_support_episodes": 3,
    "frame_step_seconds": 0.25,
    "min_segment_seconds": 45,
    "max_segment_seconds": 150,
    "feature_sample_rate": 16000,
    "feature_hop_length": None,
    "consensus_min_similarity": 0.78,
    "pair_match_min_seconds": 35,
    "high_confidence_boundary_tolerance_seconds": 2.0,
    "cache_enabled": True,
    "cache_dir": None,
    "detector_version": "v2",
    "auto_cut_min_confidence": "high",
}
DEFAULT_AUTOMATION = {
    "enabled": True,
    "provider": "aniliberty",
    "jobs_path": "./jobs.json",
    "completed_jobs_path": "./completed_jobs.json",
    "state_path": "./state.json",
    "poll_limit": 50,
    "download_root": "./downloads",
    "default_source_type": "magnet",
}
DEFAULT_TELEGRAM_STATE_PATH = Path("telegram_state.json")
DEFAULT_DELIVERY = {
    "s3_enabled": True,
    "vk_enabled": True,
}
