import os
from pathlib import Path

import requests


VK_API_BASE = "https://api.vk.com/method"


def _vk_request(method, params=None):
    payload = {
        "access_token": os.getenv("VK_ACCESS_TOKEN"),
        "v": os.getenv("VK_API_VERSION"),
    }
    payload.update(params or {})
    response = requests.post(f"{VK_API_BASE}/{method}", data=payload, timeout=60)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        error = data["error"]
        raise RuntimeError(f"VK API {method} failed: {error.get('error_code')} {error.get('error_msg')}")
    return data.get("response")


PRIVACY_VIEW_MAP = {
    0: "all",
    1: "members",
    2: "editors",
    3: "by_link",
    5: "donut",
}


def request_video_upload(title, description, privacy_view=0):
    """Request video upload URL.

    privacy_view controls who can view the uploaded video:
        0 — all users
        1 — group members only
        2 — editors and admins only
        3 — by link
        5 — donut subscribers only
    """
    group_id = int(os.getenv("VK_GROUP_ID"))
    params = {
        "group_id": group_id,
        "name": title,
        "description": description,
        "wallpost": 0,
        "is_private": 0,
        "privacy_view": [PRIVACY_VIEW_MAP.get(privacy_view, "all")],
    }
    response = _vk_request("video.save", params)
    if not isinstance(response, dict) or not response.get("upload_url"):
        raise RuntimeError("VK video.save did not return upload_url")
    return response


def upload_video_file(upload_url, local_path):
    with open(local_path, "rb") as file:
        response = requests.post(upload_url, files={"video_file": file}, timeout=3600)
    response.raise_for_status()
    return response.json()


def request_wall_photo_upload_server():
    group_id = int(os.getenv("VK_GROUP_ID"))
    response = _vk_request(
        "photos.getWallUploadServer",
        {
            "group_id": group_id,
        },
    )
    if not isinstance(response, dict) or not response.get("upload_url"):
        raise RuntimeError("VK photos.getWallUploadServer did not return upload_url")
    return response


def upload_wall_comment_photo(upload_url, local_path):
    with open(local_path, "rb") as file:
        response = requests.post(upload_url, files={"photo": file}, timeout=300)
    response.raise_for_status()
    return response.json()


def save_wall_photo(upload_response):
    group_id = int(os.getenv("VK_GROUP_ID"))
    response = _vk_request(
        "photos.saveWallPhoto",
        {
            "group_id": group_id,
            "photo": upload_response.get("photo"),
            "server": upload_response.get("server"),
            "hash": upload_response.get("hash"),
        },
    )
    if not isinstance(response, list) or not response:
        raise RuntimeError("VK photos.saveWallPhoto did not return saved photo")
    return response[0]


def build_photo_attachment(photo):
    owner_id = photo.get("owner_id")
    photo_id = photo.get("id")
    if owner_id is None or photo_id is None:
        raise RuntimeError("VK photo response is missing owner_id or id")
    return f"photo{owner_id}_{photo_id}"


def create_wall_post(message, video_owner_id, video_id):
    group_id = int(os.getenv("VK_GROUP_ID"))
    response = _vk_request(
        "wall.post",
        {
            "owner_id": -group_id,
            "from_group": 1,
            "message": message,
            "attachments": f"video{video_owner_id}_{video_id}",
        },
    )
    if not isinstance(response, dict) or response.get("post_id") is None:
        raise RuntimeError("VK wall.post did not return post_id")
    return response


def create_wall_comment(post_id, message, attachments=None):
    group_id = int(os.getenv("VK_GROUP_ID"))
    payload = {
        "owner_id": -group_id,
        "post_id": post_id,
        "message": message,
        "from_group": group_id,
    }
    if attachments:
        payload["attachments"] = attachments
    response = _vk_request("wall.createComment", payload)
    if not isinstance(response, dict) or response.get("comment_id") is None:
        raise RuntimeError("VK wall.createComment did not return comment_id")
    return response


def publish_video_to_vk(local_path, title, description, wall_post_text=None, comment_text=None, comment_banner_path=None, privacy_view=0):
    save_response = request_video_upload(title, description, privacy_view=privacy_view)
    upload_response = upload_video_file(save_response["upload_url"], local_path)
    result = {
        "enabled": True,
        "uploaded": True,
        "error": None,
        "video_uploaded": True,
        "post_created": False,
        "comment_created": False,
        "video_title": title,
        "video_description": description,
        "video_id": save_response.get("video_id") or upload_response.get("video_id"),
        "owner_id": save_response.get("owner_id") or upload_response.get("owner_id"),
        "video_url": save_response.get("player") or upload_response.get("video_url") or upload_response.get("link"),
        "post_id": None,
        "comment_id": None,
        "comment_attachment": None,
        "errors_by_stage": {},
    }

    if wall_post_text:
        try:
            post_response = create_wall_post(
                wall_post_text,
                result["owner_id"],
                result["video_id"],
            )
            result["post_created"] = True
            result["post_id"] = post_response.get("post_id")
        except Exception as exc:
            result["errors_by_stage"]["wall_post"] = repr(exc)
            result["error"] = "; ".join(result["errors_by_stage"].values())
            return result

    normalized_comment_text = str(comment_text or "").strip()
    if result["post_created"] and normalized_comment_text:
        attachments = []
        banner_path = Path(comment_banner_path) if comment_banner_path else None
        if banner_path and banner_path.is_file():
            try:
                upload_server = request_wall_photo_upload_server()
                uploaded_photo = upload_wall_comment_photo(upload_server["upload_url"], banner_path)
                saved_photo = save_wall_photo(uploaded_photo)
                attachments.append(build_photo_attachment(saved_photo))
                result["comment_attachment"] = attachments[0]
            except Exception as exc:
                result["errors_by_stage"]["comment_photo"] = repr(exc)
        elif banner_path:
            result["errors_by_stage"]["comment_photo"] = f"banner_not_found:{banner_path}"

        try:
            comment_response = create_wall_comment(
                result["post_id"],
                normalized_comment_text,
                attachments=",".join(attachments) if attachments else None,
            )
            result["comment_created"] = True
            result["comment_id"] = comment_response.get("comment_id")
        except Exception as exc:
            result["errors_by_stage"]["wall_comment"] = repr(exc)

    result["error"] = "; ".join(result["errors_by_stage"].values()) or None
    return result
