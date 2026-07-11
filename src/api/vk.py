import json
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
        import json
        raise RuntimeError(
            f"VK API {method} failed: {error.get('error_code')} {error.get('error_msg')}\n"
            f"request_params: {json.dumps(error.get('request_params', []), indent=2, ensure_ascii=False)}"
        )
    return data.get("response")


PRIVACY_VIEW_MAP = {
    0: "all",
    1: "members",
    2: "editors",
    3: "by_link",
    5: "donut",
}


def _get_group_id(env_name, fallback_env_name=None):
    value = os.getenv(env_name)
    if not value and fallback_env_name:
        value = os.getenv(fallback_env_name)
    if not value:
        raise RuntimeError(f"{env_name} is not configured")
    return int(value)


def get_vk_public_group_id():
    return _get_group_id("VK_PUBLIC_GROUP_ID", fallback_env_name="VK_GROUP_ID")


def get_vk_private_group_id():
    return _get_group_id("VK_PRIVATE_GROUP_ID")


def get_vk_donut_level_id():
    return _get_group_id("VK_DONUT_LEVEL_ID")


def build_vk_video_url(owner_id, video_id, *, domain="vk.ru"):
    if owner_id is None or video_id is None:
        return None
    try:
        normalized_owner_id = int(owner_id)
        normalized_video_id = int(video_id)
    except (TypeError, ValueError):
        return None
    return f"https://{domain}/video{normalized_owner_id}_{normalized_video_id}"


def build_vk_video_attachment(owner_id, video_id):
    if owner_id is None or video_id is None:
        return None
    try:
        normalized_owner_id = int(owner_id)
        normalized_video_id = int(video_id)
    except (TypeError, ValueError):
        return None
    return f"video{normalized_owner_id}_{normalized_video_id}"


def request_video_upload(title, description, privacy_view=0, *, group_id=None):
    """Request video upload URL.

    privacy_view controls who can view the uploaded video:
        0 — all users
        1 — group members only
        2 — editors and admins only
        3 — by link
        5 — donut subscribers only
    """
    group_id = int(group_id if group_id is not None else get_vk_public_group_id())
    params = {
        "group_id": group_id,
        "name": title,
        "description": description,
        "wallpost": 0,
    }
    privacy_value = PRIVACY_VIEW_MAP.get(privacy_view, "all")
    params["privacy_view"] = privacy_value
    if privacy_value == "donut":
        params["donut_level_id"] = get_vk_donut_level_id()
    response = _vk_request("video.save", params)
    if not isinstance(response, dict) or not response.get("upload_url"):
        raise RuntimeError("VK video.save did not return upload_url")
    return response


def upload_video_file(upload_url, local_path):
    with open(local_path, "rb") as file:
        response = requests.post(upload_url, files={"video_file": file}, timeout=3600)
    response.raise_for_status()
    return response.json()


def request_wall_photo_upload_server(*, group_id=None):
    group_id = int(group_id if group_id is not None else get_vk_public_group_id())
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


def save_wall_photo(upload_response, *, group_id=None):
    group_id = int(group_id if group_id is not None else get_vk_public_group_id())
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


def create_wall_post(message, *, group_id=None, attachments=None, donut_paid_duration=None):
    group_id = int(group_id if group_id is not None else get_vk_public_group_id())
    params = {
        "owner_id": -group_id,
        "from_group": 1,
        "message": message,
    }
    if attachments:
        params["attachments"] = attachments
    if donut_paid_duration is not None:
        params["donut_paid_duration"] = donut_paid_duration
    response = _vk_request("wall.post", params)
    if not isinstance(response, dict) or response.get("post_id") is None:
        raise RuntimeError("VK wall.post did not return post_id")
    return response


def create_wall_comment(post_id, message, attachments=None, *, group_id=None):
    group_id = int(group_id if group_id is not None else get_vk_public_group_id())
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


def _extract_video_thumb_upload_url(*candidates):
    possible_keys = (
        "thumb_upload_url",
        "thumbnail_upload_url",
        "upload_thumb_url",
        "upload_thumbnail_url",
    )
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in possible_keys:
            value = candidate.get(key)
            if value:
                return str(value)
    return None


def upload_video_thumb_file(upload_url, local_path):
    with open(local_path, "rb") as file:
        response = requests.post(
            upload_url,
            files={"file": (Path(local_path).name, file, "image/jpeg")},
            timeout=300,
        )
    response.raise_for_status()
    return response.json()


def save_uploaded_video_thumb(owner_id, video_id, thumb_upload_response, *, thumb_size=None, set_thumb=True):
    params = {
        "owner_id": owner_id,
        "video_id": video_id,
        "thumb_json": json.dumps(thumb_upload_response, ensure_ascii=False),
        "set_thumb": 1 if set_thumb else 0,
    }
    if thumb_size:
        params["thumb_size"] = thumb_size
    response = _vk_request("video.saveUploadedThumb", params)
    if not isinstance(response, dict):
        raise RuntimeError("VK video.saveUploadedThumb did not return response payload")
    return response


def try_apply_video_thumb(
    owner_id,
    video_id,
    video_thumb_path,
    result,
    *,
    save_response=None,
    upload_response=None,
    thumb_size=None,
):
    preview_path = Path(video_thumb_path) if video_thumb_path else None
    if not preview_path:
        return

    result["preview_attempted"] = True

    if not preview_path.is_file():
        result["preview_error"] = f"preview_not_found:{preview_path}"
        result["errors_by_stage"]["video_thumb"] = result["preview_error"]
        return

    upload_url = _extract_video_thumb_upload_url(save_response, upload_response)
    if not upload_url:
        result["preview_error"] = "video_thumb_upload_url_missing"
        result["errors_by_stage"]["video_thumb"] = result["preview_error"]
        return

    try:
        thumb_upload_response = upload_video_thumb_file(upload_url, preview_path)
        thumb_save_response = save_uploaded_video_thumb(
            owner_id,
            video_id,
            thumb_upload_response,
            thumb_size=thumb_size,
            set_thumb=True,
        )
        result["preview_attached"] = True
        result["preview_error"] = None
        result["video_thumb"] = thumb_save_response
    except Exception as exc:
        result["preview_error"] = repr(exc)
        result["errors_by_stage"]["video_thumb"] = result["preview_error"]


def publish_video_to_vk(
    local_path,
    title,
    description,
    wall_post_text=None,
    comment_text=None,
    comment_banner_path=None,
    privacy_view=0,
    video_thumb_path=None,
    video_thumb_size=None,
):
    public_group_id = get_vk_public_group_id()
    save_response = request_video_upload(title, description, privacy_view=privacy_view, group_id=public_group_id)
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
        "video_group_id": public_group_id,
        "wall_group_id": public_group_id,
        "post_id": None,
        "comment_id": None,
        "comment_attachment": None,
        "post_preview_attachment": None,
        "preview_attempted": False,
        "preview_generated": False,
        "preview_attached": False,
        "preview_error": None,
        "video_thumb": None,
        "errors_by_stage": {},
    }

    try_apply_video_thumb(
        result["owner_id"],
        result["video_id"],
        video_thumb_path,
        result,
        save_response=save_response,
        upload_response=upload_response,
        thumb_size=video_thumb_size,
    )

    if wall_post_text:
        try:
            donut_duration = -1 if privacy_view == 5 else None
            post_response = create_wall_post(
                wall_post_text,
                group_id=public_group_id,
                attachments=f"video{result['owner_id']}_{result['video_id']}",
                donut_paid_duration=donut_duration,
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
                upload_server = request_wall_photo_upload_server(group_id=public_group_id)
                uploaded_photo = upload_wall_comment_photo(upload_server["upload_url"], banner_path)
                saved_photo = save_wall_photo(uploaded_photo, group_id=public_group_id)
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
                group_id=public_group_id,
            )
            result["comment_created"] = True
            result["comment_id"] = comment_response.get("comment_id")
        except Exception as exc:
            result["errors_by_stage"]["wall_comment"] = repr(exc)

    result["error"] = "; ".join(result["errors_by_stage"].values()) or None
    return result


def publish_private_video_link_to_vk(
    local_path,
    title,
    description,
    wall_post_text,
    video_thumb_path=None,
    video_thumb_size=None,
):
    private_group_id = get_vk_private_group_id()
    public_group_id = get_vk_public_group_id()

    save_response = request_video_upload(title, description, privacy_view=3, group_id=private_group_id)
    upload_response = upload_video_file(save_response["upload_url"], local_path)
    owner_id = save_response.get("owner_id") or upload_response.get("owner_id") or -private_group_id
    video_id = save_response.get("video_id") or upload_response.get("video_id")
    video_url = (
        save_response.get("player")
        or upload_response.get("video_url")
        or upload_response.get("link")
        or build_vk_video_url(owner_id, video_id)
    )

    result = {
        "enabled": True,
        "uploaded": True,
        "error": None,
        "video_uploaded": True,
        "post_created": False,
        "comment_created": False,
        "video_title": title,
        "video_description": description,
        "video_id": video_id,
        "owner_id": owner_id,
        "video_url": video_url,
        "video_group_id": private_group_id,
        "wall_group_id": public_group_id,
        "post_mode": "private_donut_link",
        "post_id": None,
        "comment_id": None,
        "comment_attachment": None,
        "post_preview_attachment": None,
        "preview_attempted": False,
        "preview_generated": False,
        "preview_attached": False,
        "preview_error": None,
        "video_thumb": None,
        "post_message": None,
        "errors_by_stage": {},
    }

    try_apply_video_thumb(
        owner_id,
        video_id,
        video_thumb_path,
        result,
        save_response=save_response,
        upload_response=upload_response,
        thumb_size=video_thumb_size,
    )

    if not video_url:
        result["errors_by_stage"]["video_upload"] = "private_video_url_missing"
        result["error"] = result["errors_by_stage"]["video_upload"]
        return result

    post_message = str(wall_post_text or "").strip()
    result["post_message"] = post_message
    video_attachment = build_vk_video_attachment(owner_id, video_id)

    if post_message or video_url:
        try:
            post_response = create_wall_post(
                post_message,
                group_id=public_group_id,
                attachments=video_attachment,
                donut_paid_duration=-1,
            )
            result["post_created"] = True
            result["post_id"] = post_response.get("post_id")
        except Exception as exc:
            result["errors_by_stage"]["wall_post"] = repr(exc)
            result["error"] = "; ".join(result["errors_by_stage"].values())
            return result

    result["error"] = "; ".join(result["errors_by_stage"].values()) or None
    return result
