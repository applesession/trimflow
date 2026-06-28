import { existsSync } from "node:fs";
import type { VkSummary } from "../shared/types";

const VK_API_BASE = "https://api.vk.com/method";

const PRIVACY_VIEW_MAP: Record<number, string> = { 0: "all", 1: "members", 2: "editors", 3: "by_link", 5: "donut" };

async function vkRequest(method: string, params: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  const body = new URLSearchParams();
  body.set("access_token", Bun.env.VK_ACCESS_TOKEN ?? "");
  body.set("v", Bun.env.VK_API_VERSION ?? "5.199");
  for (const [k, v] of Object.entries(params)) {
    body.set(k, String(v));
  }

  const response = await fetch(`${VK_API_BASE}/${method}`, {
    method: "POST",
    body,
    signal: AbortSignal.timeout(60_000),
  });

  if (!response.ok) throw new Error(`VK API ${method} HTTP ${response.status}`);

  const data = (await response.json()) as {
    error?: { error_code: number; error_msg: string; request_params: unknown[] };
    response?: Record<string, unknown>;
  };

  if (data.error) {
    const e = data.error;
    throw new Error(
      `VK API ${method} failed: ${e.error_code} ${e.error_msg}\nrequest_params: ${JSON.stringify(e.request_params, null, 2)}`,
    );
  }

  return data.response ?? {};
}

// ============================================================================
// Helpers
// ============================================================================

function getVkPublicGroupId(): number {
  const v = Bun.env.VK_PUBLIC_GROUP_ID ?? Bun.env.VK_GROUP_ID;
  if (!v) throw new Error("VK_PUBLIC_GROUP_ID is not configured");
  return Number(v);
}

function getVkPrivateGroupId(): number {
  const v = Bun.env.VK_PRIVATE_GROUP_ID;
  if (!v) throw new Error("VK_PRIVATE_GROUP_ID is not configured");
  return Number(v);
}

function getVkDonutLevelId(): number {
  const v = Bun.env.VK_DONUT_LEVEL_ID;
  if (!v) throw new Error("VK_DONUT_LEVEL_ID is not configured");
  return Number(v);
}

function buildVkVideoUrl(ownerId: number | undefined, videoId: number | undefined): string | undefined {
  if (ownerId == null || videoId == null) return undefined;
  return `https://vk.ru/video${ownerId}_${videoId}`;
}

function buildVideoAttachment(ownerId: number | undefined, videoId: number | undefined): string | undefined {
  if (ownerId == null || videoId == null) return undefined;
  return `video${ownerId}_${videoId}`;
}

function buildPhotoAttachment(photo: Record<string, unknown>): string {
  const ownerId = photo.owner_id as number;
  const photoId = photo.id as number;
  if (ownerId == null || photoId == null) throw new Error("VK photo response missing owner_id or id");
  return `photo${ownerId}_${photoId}`;
}

// ============================================================================
// Video upload (2-step flow)
// ============================================================================

async function requestVideoUpload(
  title: string,
  description: string,
  privacyView: number,
  groupId: number,
): Promise<Record<string, unknown>> {
  const params: Record<string, unknown> = {
    group_id: groupId,
    name: title,
    description,
    wallpost: 0,
  };
  const privacyValue = PRIVACY_VIEW_MAP[privacyView] ?? "all";
  params.privacy_view = privacyValue;
  if (privacyValue === "donut") {
    params.donut_level_id = getVkDonutLevelId();
  }
  const response = await vkRequest("video.save", params);
  if (!response.upload_url) throw new Error("VK video.save did not return upload_url");
  return response;
}

async function uploadVideoFile(uploadUrl: string, localPath: string): Promise<Record<string, unknown>> {
  const file = Bun.file(localPath);
  const form = new FormData();
  form.set("video_file", file);
  const response = await fetch(uploadUrl, {
    method: "POST",
    body: form,
    signal: AbortSignal.timeout(3_600_000),
  });
  if (!response.ok) throw new Error(`VK video upload HTTP ${response.status}`);
  return (await response.json()) as Record<string, unknown>;
}

// ============================================================================
// Photo upload for comment banner (3-step flow)
// ============================================================================

async function uploadCommentBanner(
  bannerPath: string,
  groupId: number,
): Promise<{ attachment: string | undefined; error: string | undefined }> {
  if (!existsSync(bannerPath)) return { attachment: undefined, error: `banner_not_found:${bannerPath}` };

  try {
    // Step 1: get upload server
    const serverResp = await vkRequest("photos.getWallUploadServer", { group_id: groupId });
    const uploadUrl = serverResp.upload_url as string;
    if (!uploadUrl) throw new Error("photos.getWallUploadServer did not return upload_url");

    // Step 2: upload photo
    const photo = Bun.file(bannerPath);
    const form = new FormData();
    form.set("photo", photo);
    const uploadResp = await fetch(uploadUrl, {
      method: "POST",
      body: form,
      signal: AbortSignal.timeout(300_000),
    });
    if (!uploadResp.ok) throw new Error(`VK photo upload HTTP ${uploadResp.status}`);
    const uploadedPhoto = (await uploadResp.json()) as Record<string, unknown>;

    // Step 3: save wall photo
    const savedResp = await vkRequest("photos.saveWallPhoto", {
      group_id: groupId,
      photo: uploadedPhoto.photo,
      server: uploadedPhoto.server,
      hash: uploadedPhoto.hash,
    });
    if (!Array.isArray(savedResp) || !savedResp[0]) throw new Error("photos.saveWallPhoto did not return saved photo");

    return { attachment: buildPhotoAttachment(savedResp[0] as Record<string, unknown>), error: undefined };
  } catch (err) {
    return { attachment: undefined, error: String(err) };
  }
}

// ============================================================================
// Public API
// ============================================================================

export async function publishVideoToVk(
  localPath: string,
  title: string,
  description: string,
  options: {
    wallPostText?: string | null;
    commentText?: string | null;
    commentBannerPath?: string;
    privacyView?: number;
  } = {},
): Promise<VkSummary> {
  const publicGroupId = getVkPublicGroupId();
  const privacyView = options.privacyView ?? 0;

  // Step 1-2: Upload video
  try {
    const saveResp = await requestVideoUpload(title, description, privacyView, publicGroupId);
    const uploadResp = await uploadVideoFile(saveResp.upload_url as string, localPath);

    const ownerId = (saveResp.owner_id ?? uploadResp.owner_id) as number | undefined;
    const videoId = (saveResp.video_id ?? uploadResp.video_id) as number | undefined;

    const result: VkSummary = {
      enabled: true,
      uploaded: true,
      video_uploaded: true,
      post_created: false,
      comment_created: false,
      error: null,
      video_title: title,
      video_description: description,
      video_id,
      owner_id: ownerId,
      video_url: (saveResp.player ?? uploadResp.video_url ?? uploadResp.link ?? buildVkVideoUrl(ownerId, videoId)) as string | undefined,
      video_group_id: publicGroupId,
      wall_group_id: publicGroupId,
      post_id: undefined,
      comment_id: undefined,
      comment_attachment: undefined,
      errors_by_stage: {},
    };

    // Step 3: Wall post
    if (options.wallPostText) {
      try {
        const attachments = buildVideoAttachment(ownerId, videoId);
        const postParams: Record<string, unknown> = {
          owner_id: -publicGroupId,
          from_group: 1,
          message: options.wallPostText,
          attachments,
        };
        const postResp = await vkRequest("wall.post", postParams);
        result.post_created = true;
        result.post_id = postResp.post_id as number | undefined;
      } catch (err) {
        result.errors_by_stage["wall_post"] = String(err);
        result.error = Object.values(result.errors_by_stage).join("; ");
        return result;
      }
    }

    // Step 4: Comment with banner
    const commentText = (options.commentText ?? "").trim();
    if (result.post_created && commentText) {
      const commentAttachments: string[] = [];
      if (options.commentBannerPath) {
        const { attachment, error } = await uploadCommentBanner(options.commentBannerPath, publicGroupId);
        if (attachment) {
          commentAttachments.push(attachment);
          result.comment_attachment = attachment;
        } else if (error) {
          result.errors_by_stage["comment_photo"] = error;
        }
      }

      try {
        const commentResp = await vkRequest("wall.createComment", {
          owner_id: -publicGroupId,
          post_id: result.post_id,
          message: commentText,
          from_group: publicGroupId,
          ...(commentAttachments.length > 0 ? { attachments: commentAttachments.join(",") } : {}),
        });
        result.comment_created = true;
        result.comment_id = commentResp.comment_id as number | undefined;
      } catch (err) {
        result.errors_by_stage["wall_comment"] = String(err);
      }
    }

    result.error = Object.values(result.errors_by_stage).join("; ") || null;
    return result;
  } catch (err) {
    return {
      enabled: true,
      uploaded: false,
      video_uploaded: false,
      post_created: false,
      comment_created: false,
      error: String(err),
      video_title: title,
      video_description: description,
      errors_by_stage: { video_upload: String(err) },
    };
  }
}

export async function publishPrivateVideoLinkToVk(
  localPath: string,
  title: string,
  description: string,
  options: { wallPostText?: string | null } = {},
): Promise<VkSummary> {
  const privateGroupId = getVkPrivateGroupId();
  const publicGroupId = getVkPublicGroupId();

  try {
    const saveResp = await requestVideoUpload(title, description, 3, privateGroupId);
    const uploadResp = await uploadVideoFile(saveResp.upload_url as string, localPath);

    const ownerId = (saveResp.owner_id ?? uploadResp.owner_id ?? -privateGroupId) as number;
    const videoId = (saveResp.video_id ?? uploadResp.video_id) as number | undefined;
    const videoUrl = (
      saveResp.player ??
      uploadResp.video_url ??
      uploadResp.link ??
      buildVkVideoUrl(ownerId, videoId)
    ) as string | undefined;

    const result: VkSummary = {
      enabled: true,
      uploaded: true,
      video_uploaded: true,
      post_created: false,
      comment_created: false,
      error: null,
      video_title: title,
      video_description: description,
      video_id: videoId,
      owner_id: ownerId,
      video_url: videoUrl,
      video_group_id: privateGroupId,
      wall_group_id: publicGroupId,
      post_mode: "private_donut_link",
      post_message: undefined,
      errors_by_stage: {},
    };

    if (!videoUrl) {
      result.errors_by_stage["video_upload"] = "private_video_url_missing";
      result.error = "private_video_url_missing";
      return result;
    }

    const postMessage = (options.wallPostText ?? "").trim();
    result.post_message = postMessage;
    const videoAttachment = buildVideoAttachment(ownerId, videoId);

    if (postMessage || videoUrl) {
      try {
        const postResp = await vkRequest("wall.post", {
          owner_id: -publicGroupId,
          from_group: 1,
          message: postMessage,
          attachments: videoAttachment,
          donut_paid_duration: -1,
        });
        result.post_created = true;
        result.post_id = postResp.post_id as number | undefined;
      } catch (err) {
        result.errors_by_stage["wall_post"] = String(err);
        result.error = Object.values(result.errors_by_stage).join("; ");
        return result;
      }
    }

    return result;
  } catch (err) {
    return {
      enabled: true,
      uploaded: false,
      video_uploaded: false,
      post_created: false,
      comment_created: false,
      error: String(err),
      video_title: title,
      video_description: description,
      errors_by_stage: { video_upload: String(err) },
    };
  }
}
