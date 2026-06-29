import os
import shutil
from pathlib import Path

from shared.constants import (
    BASE_REQUIRED_TOOLS,
    REQUIRED_ENV_VARS,
    TEMP_ROOT,
    VK_PRIVATE_ENV_VARS,
    VK_PUBLIC_ENV_VARS,
    VK_REQUIRED_ENV_VARS,
)


def validate_required_env(config, jobs):
    delivery_enabled = {"s3": False, "vk": False}
    requires_private_vk_delivery = False
    for job in jobs:
        delivery = job.get("delivery", {})
        if delivery.get("s3_enabled", True):
            delivery_enabled["s3"] = True
        if delivery.get("vk_enabled", False):
            delivery_enabled["vk"] = True
            if int(delivery.get("vk_privacy_view", 0)) == 5:
                requires_private_vk_delivery = True

    required_vars = []
    if delivery_enabled["s3"]:
        required_vars.extend(REQUIRED_ENV_VARS)
    if delivery_enabled["vk"]:
        required_vars.extend(VK_REQUIRED_ENV_VARS)
        required_vars.extend(VK_PUBLIC_ENV_VARS)
        if requires_private_vk_delivery:
            required_vars.extend(VK_PRIVATE_ENV_VARS)

    missing = [name for name in required_vars if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )


def validate_required_tools(config, jobs):
    required_tools = list(BASE_REQUIRED_TOOLS)

    if any(job.get("source", {}).get("type") == "magnet" for job in jobs):
        required_tools.append("aria2c")

    missing = []

    for tool in required_tools:
        if shutil.which(tool) is None:
            missing.append(tool)

    if missing:
        raise RuntimeError("Missing required tools in PATH: " + ", ".join(missing))


def validate_required_files(config):
    watermark_path = Path(config.get("defaults", {}).get("watermark_path", ""))
    if not watermark_path.is_file():
        raise RuntimeError(f"Watermark file not found: {watermark_path}")


def reset_temp_dir(title_slug: str):
    temp_root = TEMP_ROOT.resolve()
    temp_dir = (TEMP_ROOT / title_slug).resolve()

    if temp_dir == temp_root:
        raise RuntimeError("Refusing to clear the temp root directly")

    shutil.rmtree(temp_dir, ignore_errors=True)
    Path(temp_dir).mkdir(parents=True, exist_ok=True)
    return Path(temp_dir)
