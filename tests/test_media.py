import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.media import (
    get_nvenc_fallback_codec,
    get_preferred_audio_stream,
    render_episode,
    render_segment_copy,
    validate_episode_render,
)


class MediaAudioSelectionTests(unittest.TestCase):
    @patch("lib.media.run")
    def test_episode_render_uses_normalized_filter_graph_without_subtitles(self, mock_run):
        render_episode(
            "episode.mkv",
            "rendered.mkv",
            [(0.0, 10.0), (100.0, 200.0)],
            "watermark.png",
            {"video_codec": "libx264", "preset": "fast", "cq": 23},
            audio_stream_index=1,
        )

        command = mock_run.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("trim=start=0.000000:end=10.000000", graph)
        self.assertIn("setpts=PTS-STARTPTS", graph)
        self.assertIn("atrim=start=100.000000:end=200.000000", graph)
        self.assertIn("asetpts=PTS-STARTPTS", graph)
        self.assertIn("concat=n=2:v=1:a=1", graph)
        self.assertIn("overlay=W-w-20:20", graph)
        self.assertNotIn("0:s", command)

    @patch("lib.media.run")
    def test_episode_render_supports_video_without_audio(self, mock_run):
        render_episode(
            "episode.mkv",
            "rendered.mkv",
            [(0.0, 10.0)],
            "watermark.png",
            {"video_codec": "libx264"},
            audio_stream_index=None,
        )

        command = mock_run.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("setpts=PTS-STARTPTS", graph)
        self.assertNotIn("atrim", graph)
        self.assertNotIn("-c:a", command)

    @patch("lib.media.run")
    def test_copy_segment_normalizes_audio_without_reencoding_video(self, mock_run):
        render_segment_copy("episode.mkv", "segment.mkv", 10, 20)

        command = mock_run.call_args.args[0]
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertEqual(command[command.index("-ar") + 1], "48000")
        self.assertEqual(command[command.index("-ac") + 1], "2")

    def test_nvenc_fallback_keeps_codec_family(self):
        self.assertEqual(get_nvenc_fallback_codec("h264_nvenc"), "libx264")
        self.assertEqual(get_nvenc_fallback_codec("hevc_nvenc"), "libx265")

    @patch("lib.media.detect_audio_streams")
    def test_prefers_russian_audio_stream_position_not_absolute_stream_index(self, mock_detect_audio_streams):
        mock_detect_audio_streams.return_value = [
            {
                "audio_index": 0,
                "stream_index": 1,
                "language": "jpn",
                "title": "Japanese",
                "handler_name": None,
                "is_default": True,
            },
            {
                "audio_index": 1,
                "stream_index": 2,
                "language": "rus",
                "title": "Russian Dub",
                "handler_name": None,
                "is_default": False,
            },
        ]

        result = get_preferred_audio_stream("episode.mkv", "rus")

        self.assertEqual(result, 1)

    @patch("lib.media.detect_audio_streams")
    def test_prefers_russian_track_from_title_when_language_tag_missing(self, mock_detect_audio_streams):
        mock_detect_audio_streams.return_value = [
            {
                "audio_index": 0,
                "stream_index": 1,
                "language": None,
                "title": "Japanese 2.0",
                "handler_name": None,
                "is_default": True,
            },
            {
                "audio_index": 1,
                "stream_index": 2,
                "language": None,
                "title": "Русский дубляж",
                "handler_name": None,
                "is_default": False,
            },
        ]

        result = get_preferred_audio_stream("episode.mkv", "rus")

        self.assertEqual(result, 1)

    @patch("lib.media.detect_audio_streams")
    def test_falls_back_to_first_audio_stream_when_no_russian_match(self, mock_detect_audio_streams):
        mock_detect_audio_streams.return_value = [
            {
                "audio_index": 0,
                "stream_index": 1,
                "language": "jpn",
                "title": "Japanese",
                "handler_name": None,
                "is_default": True,
            },
            {
                "audio_index": 1,
                "stream_index": 2,
                "language": "eng",
                "title": "English",
                "handler_name": None,
                "is_default": False,
            },
        ]

        result = get_preferred_audio_stream("episode.mkv", "rus")

        self.assertEqual(result, 0)

    @patch("lib.media.ffprobe_media_signature", return_value={"video": {}, "audio": {}})
    @patch("lib.media.ffprobe_duration", return_value=10.0)
    def test_episode_validation_rejects_start_gap_and_av_mismatch(
        self,
        mock_duration,
        mock_signature,
    ):
        invalid_timelines = [
            (
                {
                    "video": {"start": 0.2, "duration": 10.0, "max_packet_gap": 0.04},
                    "audio": {"start": 0.0, "duration": 10.0, "max_packet_gap": 0.03},
                },
                "starts at",
            ),
            (
                {
                    "video": {"start": 0.0, "duration": 10.0, "max_packet_gap": 0.6},
                    "audio": {"start": 0.0, "duration": 10.0, "max_packet_gap": 0.03},
                },
                "packet gap",
            ),
            (
                {
                    "video": {"start": 0.0, "duration": 10.0, "max_packet_gap": 0.04},
                    "audio": {"start": 0.0, "duration": 9.5, "max_packet_gap": 0.03},
                },
                "A/V duration mismatch",
            ),
        ]
        for timeline, error in invalid_timelines:
            with self.subTest(error=error), patch(
                "lib.media.ffprobe_episode_timeline",
                return_value=timeline,
            ):
                with self.assertRaisesRegex(RuntimeError, error):
                    validate_episode_render("episode.mkv")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
class MediaEpisodeIntegrationTests(unittest.TestCase):
    def test_cut_episode_has_continuous_pts_and_drops_soft_subtitles(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_dir = Path(raw_tmp)
            subtitle = tmp_dir / "subtitle.srt"
            subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:04,000\nLong packet\n",
                encoding="utf-8",
            )
            source = tmp_dir / "source.mkv"
            watermark = tmp_dir / "watermark.png"
            output = tmp_dir / "rendered.mkv"
            try:
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=4",
                    "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=4.1",
                    "-i", str(subtitle),
                    "-map", "0:v", "-map", "1:a", "-map", "2:s",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-c:s", "srt",
                    str(source),
                ], check=True)
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=white:size=32x16",
                    "-frames:v", "1", str(watermark),
                ], check=True)
            except subprocess.CalledProcessError as exc:
                self.skipTest(f"local ffmpeg cannot build fixture: {exc}")

            render_episode(
                source,
                output,
                [(0.0, 1.0), (2.0, 4.0)],
                watermark,
                {
                    "video_codec": "libx264",
                    "preset": "ultrafast",
                    "cq": 28,
                    "audio_codec": "aac",
                },
                audio_stream_index=0,
            )
            validation = validate_episode_render(output)
            probe = json.loads(subprocess.check_output([
                "ffprobe", "-v", "error",
                "-show_entries", "stream=codec_type",
                "-of", "json", str(output),
            ], encoding="utf-8"))

            self.assertLessEqual(validation["timeline"]["video"]["max_packet_gap"], 0.5)
            self.assertLessEqual(validation["timeline"]["audio"]["max_packet_gap"], 0.5)
            self.assertLessEqual(
                abs(
                    validation["timeline"]["video"]["duration"]
                    - validation["timeline"]["audio"]["duration"]
                ),
                0.25,
            )
            self.assertNotIn("subtitle", [item["codec_type"] for item in probe["streams"]])


if __name__ == "__main__":
    unittest.main()
