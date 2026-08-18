import shutil
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from lib.helpers import (
    build_compilation_display_name,
    build_single_episode_display_name,
    build_timestamps_description,
    build_vk_comment_text,
    build_vk_wall_post_text,
    format_episodes_label,
    get_navigation_label,
    sanitize_filename,
)
from lib.pipeline import build_compact_manifest, build_delivery_config
from lib.vk import (
    create_wall_comment,
    publish_private_video_link_to_vk,
    publish_video_to_vk,
    upload_video_file,
)


class DeliveryTests(unittest.TestCase):
    def make_workspace_temp_dir(self):
        root = Path(".test_tmp")
        root.mkdir(exist_ok=True)
        temp_dir = root / f"delivery_{uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return temp_dir

    @patch("lib.vk.requests.post")
    def test_upload_video_file_streams_multipart(self, mock_post):
        tmp_dir = self.make_workspace_temp_dir()
        video_path = tmp_dir / "test.mkv"
        video_path.write_bytes(b"video")
        mock_post.return_value.json.return_value = {"ok": 1}

        result = upload_video_file("https://upload.vk.test", video_path)

        kwargs = mock_post.call_args.kwargs
        self.assertNotIn("files", kwargs)
        self.assertNotIsInstance(kwargs["data"], bytes)
        encoder = kwargs["data"].encoder
        self.assertEqual(encoder.fields["video_file"][0], "test.mkv")
        self.assertEqual(encoder.fields["video_file"][2], "video/x-matroska")
        self.assertTrue(kwargs["headers"]["Content-Type"].startswith("multipart/form-data; boundary="))
        self.assertEqual(kwargs["timeout"], 3600)
        self.assertEqual(result, {"ok": 1})
        mock_post.return_value.raise_for_status.assert_called_once_with()

    @patch("lib.vk.requests.post")
    def test_upload_video_file_propagates_http_error(self, mock_post):
        tmp_dir = self.make_workspace_temp_dir()
        video_path = tmp_dir / "test.mkv"
        video_path.write_bytes(b"video")
        mock_post.return_value.raise_for_status.side_effect = RuntimeError("upload failed")

        with self.assertRaisesRegex(RuntimeError, "upload failed"):
            upload_video_file("https://upload.vk.test", video_path)

    def test_build_compilation_display_name_prefers_title_ru(self):
        result = build_compilation_display_name(
            {"title": "Oshi no Ko", "title_ru": "Звёздное дитя"},
            "01",
            "001-011",
        )

        self.assertEqual(result, "Звёздное дитя - 1-11 Серия 1 Сезон [Без OP/ED]")

    def test_build_compilation_display_name_falls_back_to_title(self):
        result = build_compilation_display_name(
            {"title": "Yomi no Tsugai"},
            1,
            "001,003,005-007",
        )

        self.assertEqual(result, "Yomi no Tsugai - 1,3,5-7 Серия 1 Сезон [Без OP/ED]")

    def test_build_single_episode_name_puts_episode_before_season(self):
        result = build_single_episode_display_name(
            {"title": "Test", "season": 1},
            1,
            5,
        )

        self.assertEqual(result, "Test - 5 Серия 1 Сезон")

    def test_navigation_label_requires_confirmed_season_or_release_type(self):
        self.assertEqual(
            get_navigation_label({
                "title": "Seihantai na Kimi to Boku 2nd Season",
                "title_ru": "Ты и я — полные противоположности 2",
                "season": 1,
                "automation": {"release_id": 10237, "release_type": "TV"},
            }),
            "Сезон 2",
        )
        self.assertEqual(
            get_navigation_label({
                "title": "Evangelion: 2.22 You Can (Not) Advance",
                "title_ru": "Евангелион: 2.22 Ты (не) пройдёшь",
                "automation": {"release_id": 5009, "release_type": "MOVIE"},
            }),
            "Фильм",
        )
        self.assertEqual(
            get_navigation_label({
                "title": "86",
                "title_ru": "86",
                "automation": {"release_id": 86, "release_type": "TV"},
            }),
            None,
        )
        self.assertEqual(
            get_navigation_label({
                "title": "Wrong 3rd Season",
                "title_ru": "Несовпадение 2",
                "automation": {"release_id": 3, "release_type": "TV"},
            }),
            None,
        )

    def test_unknown_navigation_label_is_omitted_from_display_name(self):
        result = build_compilation_display_name(
            {
                "title": "86",
                "title_ru": "86",
                "automation": {"release_id": 86, "release_type": "TV"},
            },
            1,
            "001-012",
        )

        self.assertEqual(result, "86 - 1-12 Серия [Без OP/ED]")

    def test_build_timestamps_description_matches_txt_format(self):
        timestamps = [
            "00:00:00 - 1 серия",
            "00:21:57 - 2 серия",
        ]

        self.assertEqual(
            build_timestamps_description(timestamps),
            "00:00:00 - 1 серия\n00:21:57 - 2 серия",
        )

    def test_build_vk_wall_post_text_uses_pretty_name(self):
        result = build_vk_wall_post_text(
            {"title_ru": "Звёздное дитя"},
            "Звёздное дитя - 1 Сезон 1-11 Серия [Без OP/ED]",
        )

        self.assertEqual(result, "Звёздное дитя - 1 Сезон 1-11 Серия [Без OP/ED]")

    def test_build_vk_comment_text_keeps_template_lines(self):
        template = "line1\n\nline2\nline3\n"

        self.assertEqual(build_vk_comment_text(template), "line1\n\nline2\nline3")

    def test_sanitize_filename_keeps_readable_ru_name(self):
        value = sanitize_filename("Звёздное дитя - 1 Сезон 1-11 Серия [Без OP/ED]")

        self.assertEqual(value, "Звёздное дитя - 1 Сезон 1-11 Серия [Без OP-ED]")

    def test_build_delivery_config_defaults_to_manifest_only_s3(self):
        result = build_delivery_config({})

        self.assertTrue(result["s3_enabled"])
        self.assertFalse(result["s3_upload_video"])
        self.assertFalse(result["s3_upload_timestamps"])
        self.assertTrue(result["s3_upload_manifest"])
        self.assertEqual(result["vk_privacy_view"], 0)
        self.assertEqual(result["vk_preview_mode"], "video_thumb")

    def test_build_delivery_config_keeps_private_vk_privacy_view(self):
        result = build_delivery_config({"delivery": {"vk_privacy_view": 5}})

        self.assertEqual(result["vk_privacy_view"], 5)

    def test_build_compact_manifest_omits_heavy_debug_fields(self):
        result = build_compact_manifest(
            job={
                "title": "Test",
                "title_ru": "Тест",
                "source": {"type": "magnet"},
                "skip_types": ["op", "ed"],
            },
            season="01",
            episodes_range="001-002",
            episode_files=[(1, "a.mkv"), (2, "b.mkv")],
            excluded_files=[{"path": "c.mkv", "reason": "out_of_range"}],
            detector_context={"available": True, "reason": "ready"},
            timing_detection={"enabled": True},
            prefetched_anilibria_results={1: {"segments": [{"type": "op"}]}, 2: {"segments": []}},
            prefetched_aniskip_results={1: {"segments": []}, 2: {"segments": []}},
            pretty_base_name="Тест - 1 Сезон 1-2 Серия [Без OP/ED]",
            output_video=Path("test.mkv"),
            output_txt=Path("test.txt"),
            delivery_summary={"s3": {"enabled": True}, "vk": {"enabled": True}},
            quality_summary={"episodes_count": 2},
            manifest_episodes=[{
                "episode": 1,
                "source_file": "/tmp/test_episode_01.mkv",
                "original_duration": 1400.1234,
                "cleaned_duration": 1210.5,
                "segment_cut_mode": "copy",
                "timing_info": {
                    "strategy": "anilibria_only",
                    "confidence": "high",
                    "review_required": False,
                    "used_fallback": False,
                    "request_error": None,
                    "detector_error": None,
                    "reference_episodes": {"op": [], "ed": []},
                    "per_type": {
                        "op": {
                            "source": "anilibria_exact",
                            "confidence": "high",
                            "interval": {"start": 90.0, "end": 180.0},
                            "removed": True,
                            "review_required": False,
                            "match_strategy": "anilibria",
                            "reference_source": "anilibria_exact",
                        },
                        "ed": {
                            "source": "not_found",
                            "confidence": "none",
                            "interval": None,
                            "removed": False,
                            "review_required": True,
                            "reason": "detector_not_found",
                        },
                    },
                    "request_urls": {"anilibria": ["x"], "aniskip": ["y"]},
                },
                "skip_summary": {"op": True, "ed": False, "warnings": []},
                "removed_segments": [{"type": "op"}],
                "kept_segments": [{"start": 0.0, "end": 90.0}],
            }],
        )

        self.assertEqual(result["source_summary"], {
            "selected_episode_count": 2,
            "excluded_file_count": 1,
            "external_audio_episode_count": 0,
        })
        self.assertNotIn("excluded_files", result["source_summary"])
        self.assertEqual(result["episodes"][0]["source_file"], "test_episode_01.mkv")
        self.assertNotIn("removed_segments", result["episodes"][0])
        self.assertNotIn("kept_segments", result["episodes"][0])
        self.assertNotIn("request_urls", result["episodes"][0]["timing_info"])

    @patch.dict("os.environ", {"VK_PUBLIC_GROUP_ID": "236358467"})
    @patch("lib.vk.upload_video_file")
    @patch("lib.vk.request_video_upload")
    def test_publish_video_to_vk_normalizes_response(self, mock_request_upload, mock_upload_file):
        tmp_dir = self.make_workspace_temp_dir()
        video_path = tmp_dir / "test.mkv"
        video_path.write_bytes(b"video")
        mock_request_upload.return_value = {
            "upload_url": "https://upload.vk.test",
            "video_id": 42,
            "owner_id": -100,
            "player": "https://vk.com/video-100_42",
        }
        mock_upload_file.return_value = {"ok": 1}

        result = publish_video_to_vk(video_path, "Test Title", "desc")

        self.assertTrue(result["uploaded"])
        self.assertEqual(result["video_title"], "Test Title")
        self.assertEqual(result["video_description"], "desc")
        self.assertEqual(result["video_id"], 42)
        self.assertEqual(result["owner_id"], -100)
        self.assertEqual(result["video_url"], "https://vk.com/video-100_42")
        self.assertEqual(result["video_group_id"], 236358467)
        self.assertEqual(result["wall_group_id"], 236358467)

    @patch.dict("os.environ", {"VK_PUBLIC_GROUP_ID": "236358467"})
    @patch("lib.vk.create_wall_comment")
    @patch("lib.vk.save_wall_photo")
    @patch("lib.vk.upload_wall_comment_photo")
    @patch("lib.vk.request_wall_photo_upload_server")
    @patch("lib.vk.create_wall_post")
    @patch("lib.vk.upload_video_file")
    @patch("lib.vk.request_video_upload")
    def test_publish_video_to_vk_creates_post_and_comment(
        self,
        mock_request_upload,
        mock_upload_file,
        mock_create_post,
        mock_request_upload_server,
        mock_upload_comment_photo,
        mock_save_wall_photo,
        mock_create_comment,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        video_path = tmp_dir / "test.mkv"
        banner_path = tmp_dir / "banner.png"
        video_path.write_bytes(b"video")
        banner_path.write_bytes(b"image")
        mock_request_upload.return_value = {
            "upload_url": "https://upload.vk.test",
            "video_id": 42,
            "owner_id": -100,
        }
        mock_upload_file.return_value = {"ok": 1}
        mock_create_post.return_value = {"post_id": 77}
        mock_request_upload_server.return_value = {"upload_url": "https://upload.photo.vk.test"}
        mock_upload_comment_photo.return_value = {"photo": "[]", "server": 1, "hash": "hash"}
        mock_save_wall_photo.return_value = {"owner_id": -200, "id": 55}
        mock_create_comment.return_value = {"comment_id": 99}

        result = publish_video_to_vk(
            video_path,
            "Test Title",
            "desc",
            wall_post_text="wall text",
            comment_text="comment text",
            comment_banner_path=banner_path,
        )

        self.assertTrue(result["uploaded"])
        self.assertTrue(result["video_uploaded"])
        self.assertTrue(result["post_created"])
        self.assertTrue(result["comment_created"])
        self.assertEqual(result["post_id"], 77)
        self.assertEqual(result["comment_id"], 99)
        self.assertEqual(result["comment_attachment"], "photo-200_55")
        self.assertEqual(mock_create_post.call_args.kwargs["group_id"], 236358467)
        self.assertEqual(mock_create_post.call_args.kwargs["attachments"], "video-100_42")

    @patch.dict("os.environ", {"VK_PUBLIC_GROUP_ID": "236358467"})
    @patch("lib.vk.create_wall_post")
    @patch("lib.vk.upload_video_file")
    @patch("lib.vk.request_video_upload")
    def test_publish_video_to_vk_keeps_video_publish_when_video_thumb_is_unavailable(
        self,
        mock_request_upload,
        mock_upload_file,
        mock_create_post,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        video_path = tmp_dir / "test.mkv"
        preview_path = tmp_dir / "preview.jpg"
        video_path.write_bytes(b"video")
        preview_path.write_bytes(b"image")
        mock_request_upload.return_value = {
            "upload_url": "https://upload.vk.test",
            "video_id": 42,
            "owner_id": -100,
        }
        mock_upload_file.return_value = {"ok": 1}
        mock_create_post.return_value = {"post_id": 77}

        result = publish_video_to_vk(
            video_path,
            "Test Title",
            "desc",
            wall_post_text="wall text",
            video_thumb_path=preview_path,
            video_thumb_size="1280x720",
        )

        self.assertTrue(result["post_created"])
        self.assertFalse(result["preview_attached"])
        self.assertEqual(result["preview_error"], "video_thumb_upload_url_missing")
        self.assertEqual(result["errors_by_stage"]["video_thumb"], "video_thumb_upload_url_missing")

    @patch.dict("os.environ", {"VK_PUBLIC_GROUP_ID": "236358467"})
    @patch("lib.vk.create_wall_comment")
    @patch("lib.vk.create_wall_post")
    @patch("lib.vk.upload_video_file")
    @patch("lib.vk.request_video_upload")
    def test_publish_video_to_vk_falls_back_to_text_comment_without_banner(
        self,
        mock_request_upload,
        mock_upload_file,
        mock_create_post,
        mock_create_comment,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        video_path = tmp_dir / "test.mkv"
        video_path.write_bytes(b"video")
        mock_request_upload.return_value = {
            "upload_url": "https://upload.vk.test",
            "video_id": 42,
            "owner_id": -100,
        }
        mock_upload_file.return_value = {"ok": 1}
        mock_create_post.return_value = {"post_id": 77}
        mock_create_comment.return_value = {"comment_id": 99}

        result = publish_video_to_vk(
            video_path,
            "Test Title",
            "desc",
            wall_post_text="wall text",
            comment_text="comment text",
            comment_banner_path=tmp_dir / "missing.png",
        )

        self.assertTrue(result["post_created"])
        self.assertTrue(result["comment_created"])
        self.assertIn("comment_photo", result["errors_by_stage"])
        self.assertIsNone(result["comment_attachment"])

    @patch.dict("os.environ", {"VK_PUBLIC_GROUP_ID": "236358467"})
    @patch("lib.vk._vk_request")
    def test_create_wall_comment_uses_group_id_for_from_group(self, mock_vk_request):
        mock_vk_request.return_value = {"comment_id": 99}

        result = create_wall_comment(77, "comment text", attachments="photo-1_2")

        self.assertEqual(result["comment_id"], 99)
        self.assertEqual(mock_vk_request.call_args.args[0], "wall.createComment")
        payload = mock_vk_request.call_args.args[1]
        self.assertEqual(payload["owner_id"], -236358467)
        self.assertEqual(payload["post_id"], 77)
        self.assertEqual(payload["message"], "comment text")
        self.assertEqual(payload["attachments"], "photo-1_2")
        self.assertEqual(payload["from_group"], 236358467)

    @patch.dict("os.environ", {"VK_PUBLIC_GROUP_ID": "236358467", "VK_PRIVATE_GROUP_ID": "236358999"})
    @patch("lib.vk.create_wall_post")
    @patch("lib.vk.upload_video_file")
    @patch("lib.vk.request_video_upload")
    def test_publish_private_video_link_to_vk_uploads_to_private_and_posts_link_in_public(
        self,
        mock_request_upload,
        mock_upload_file,
        mock_create_post,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        video_path = tmp_dir / "private.mkv"
        video_path.write_bytes(b"video")
        mock_request_upload.return_value = {
            "upload_url": "https://upload.vk.test",
            "video_id": 42,
            "owner_id": -236358999,
            "player": "https://vk.com/video-236358999_42",
        }
        mock_upload_file.return_value = {"ok": 1}
        mock_create_post.return_value = {"post_id": 77}

        result = publish_private_video_link_to_vk(
            video_path,
            "Private Title",
            "desc",
            wall_post_text="Private Title",
        )

        self.assertTrue(result["video_uploaded"])
        self.assertTrue(result["post_created"])
        self.assertFalse(result["comment_created"])
        self.assertEqual(result["video_group_id"], 236358999)
        self.assertEqual(result["wall_group_id"], 236358467)
        self.assertEqual(mock_request_upload.call_args.kwargs["group_id"], 236358999)
        self.assertEqual(mock_request_upload.call_args.kwargs["privacy_view"], 3)
        self.assertEqual(mock_create_post.call_args.kwargs["group_id"], 236358467)
        self.assertEqual(mock_create_post.call_args.kwargs["donut_paid_duration"], -1)
        self.assertEqual(mock_create_post.call_args.kwargs["attachments"], "video-236358999_42")
        self.assertEqual(mock_create_post.call_args.args[0], "Private Title")

    @patch.dict("os.environ", {"VK_PUBLIC_GROUP_ID": "236358467", "VK_PRIVATE_GROUP_ID": "239761756"})
    @patch("lib.vk.create_wall_post")
    @patch("lib.vk.upload_video_file")
    @patch("lib.vk.request_video_upload")
    def test_publish_private_video_link_to_vk_builds_fallback_url_from_owner_and_video_id(
        self,
        mock_request_upload,
        mock_upload_file,
        mock_create_post,
    ):
        tmp_dir = self.make_workspace_temp_dir()
        video_path = tmp_dir / "private-no-player.mkv"
        video_path.write_bytes(b"video")
        mock_request_upload.return_value = {
            "upload_url": "https://upload.vk.test",
            "video_id": 456239017,
            "owner_id": -239761756,
        }
        mock_upload_file.return_value = {"ok": 1}
        mock_create_post.return_value = {"post_id": 88}

        result = publish_private_video_link_to_vk(
            video_path,
            "Fallback Title",
            "desc",
            wall_post_text="Fallback Title",
        )

        self.assertEqual(result["video_url"], "https://vk.ru/video-239761756_456239017")
        self.assertTrue(result["post_created"])
        self.assertEqual(mock_create_post.call_args.args[0], "Fallback Title")
        self.assertEqual(
            mock_create_post.call_args.kwargs["attachments"],
            "video-239761756_456239017",
        )


if __name__ == "__main__":
    unittest.main()
