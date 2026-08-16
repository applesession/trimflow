import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.media import (
    analyze_audio_recovery,
    ffprobe_episode_timeline,
    get_nvenc_fallback_codec,
    get_preferred_audio_stream,
    render_episode,
    render_final,
    render_segment_copy,
    select_external_audio,
    validate_episode_render,
)


class MediaAudioSelectionTests(unittest.TestCase):
    @patch("lib.media.ffprobe_audio_timeline", return_value={"start_time": 0.0, "duration": 100.4})
    @patch("lib.media.detect_audio_streams")
    def test_external_audio_prefers_unique_language_match(self, mock_streams, _mock_timeline):
        mock_streams.side_effect = lambda path: [{
            "audio_index": 0,
            "stream_index": 0,
            "language": None,
            "title": None,
            "handler_name": None,
            "is_default": True,
        }]

        selected = select_external_audio(
            [Path("JAP Sound/Show - 001.mka"), Path("RUS Sound/Show - 001.mka")],
            100.0,
            "rus",
        )

        self.assertEqual(selected["path"], str(Path("RUS Sound/Show - 001.mka")))
        self.assertAlmostEqual(selected["duration_delta"], 0.4)

    @patch("lib.media.detect_audio_streams", return_value=[{
        "audio_index": 0,
        "stream_index": 0,
        "language": None,
        "title": None,
        "handler_name": None,
        "is_default": True,
    }])
    def test_external_audio_rejects_ambiguous_candidates(self, _mock_streams):
        with self.assertRaisesRegex(RuntimeError, "Ambiguous external audio"):
            select_external_audio(["Audio A/Show - 001.mka", "Audio B/Show - 001.mka"], 100.0)

    @patch("lib.media.ffprobe_audio_timeline", return_value={"start_time": 0.2, "duration": 100.0})
    @patch("lib.media.detect_audio_streams", return_value=[{
        "audio_index": 0,
        "stream_index": 0,
        "language": "rus",
        "title": None,
        "handler_name": None,
        "is_default": True,
    }])
    def test_external_audio_rejects_sync_mismatch(self, _mock_streams, _mock_timeline):
        with self.assertRaisesRegex(RuntimeError, r"start=\+0\.200s"):
            select_external_audio(["RUS/Show - 001.mka"], 100.0)

    @patch("lib.media.run")
    def test_episode_render_uses_external_audio_as_separate_input(self, mock_run):
        render_episode(
            "episode.mkv",
            "rendered.mkv",
            [(0.0, 10.0)],
            "watermark.png",
            {"video_codec": "libx264"},
            audio_stream_index=1,
            external_audio_path="RUS Sound/episode.mka",
        )

        command = mock_run.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertEqual(command[command.index("-filter_complex") - 1], "RUS Sound/episode.mka")
        self.assertIn("[2:a:1]atrim", graph)
        self.assertIn("[acat]apad[aexternal]", graph)

    @patch("lib.media._probe_video_streams", return_value=[{}])
    @patch("lib.media.run")
    def test_single_episode_render_uses_external_audio(self, mock_run, _mock_probe):
        render_final(
            "episode.mkv",
            "watermark.png",
            "rendered.mkv",
            {"video_codec": "libx264", "audio_codec": "aac"},
            audio_stream_index=0,
            external_audio_path="RUS Sound/episode.mka",
        )

        command = mock_run.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("[2:a:0]asetpts=PTS-STARTPTS,apad[aexternal]", graph)
        self.assertIn("[aexternal]", command)
        self.assertIn("-shortest", command)

    @patch("lib.media.ffprobe_episode_timeline")
    def test_audio_recovery_detects_supported_gap_and_short_tail(self, mock_timeline):
        mock_timeline.return_value = {
            "video": {"start": 0.0, "duration": 1440.0, "max_packet_gap": 0.04},
            "audio": {
                "start": 0.0,
                "duration": 1430.3,
                "max_packet_gap": 0.662,
                "total_packet_gap": 0.662,
            },
        }

        result = analyze_audio_recovery("episode.mkv", audio_stream_index=1)

        mock_timeline.assert_called_once_with("episode.mkv", audio_stream_index=1)
        self.assertTrue(result["applied"])
        self.assertEqual(result["reasons"], ["packet_gap", "early_end"])
        self.assertEqual(result["source_audio_end_shortfall"], 9.7)
        self.assertEqual(result["inserted_silence_seconds"], 10.362)

    @patch("lib.media.ffprobe_episode_timeline")
    def test_audio_recovery_rejects_gap_or_total_silence_over_limit(self, mock_timeline):
        mock_timeline.return_value = {
            "video": {"start": 0.0, "duration": 30.0, "max_packet_gap": 0.04},
            "audio": {
                "start": 0.0,
                "duration": 30.0,
                "max_packet_gap": 2.1,
                "total_packet_gap": 2.1,
            },
        }
        with self.assertRaisesRegex(RuntimeError, "packet gap 2.100s exceeds"):
            analyze_audio_recovery("episode.mkv")

        mock_timeline.return_value["audio"].update({
            "max_packet_gap": 1.0,
            "total_packet_gap": 15.1,
        })
        with self.assertRaisesRegex(RuntimeError, "total silence 15.100s exceeds"):
            analyze_audio_recovery("episode.mkv")

    @patch("lib.media.subprocess.check_output")
    def test_episode_timeline_uses_requested_audio_stream_position(self, mock_output):
        mock_output.return_value = json.dumps({
            "streams": [
                {"index": 0, "codec_type": "video"},
                {"index": 1, "codec_type": "audio"},
                {"index": 2, "codec_type": "audio"},
            ],
            "packets": [
                {"stream_index": 0, "pts_time": "0", "duration_time": "1"},
                {"stream_index": 1, "pts_time": "0", "duration_time": "1"},
                {"stream_index": 2, "pts_time": "5", "duration_time": "1"},
            ],
        })

        timeline = ffprobe_episode_timeline("episode.mkv", audio_stream_index=1)

        self.assertEqual(timeline["audio"]["start"], 5.0)

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
        self.assertIn("-shortest", command)
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
    def test_episode_render_applies_audio_recovery_filter_only_when_enabled(self, mock_run):
        render_episode(
            "episode.mkv",
            "rendered.mkv",
            [(0.0, 10.0)],
            "watermark.png",
            {"video_codec": "libx264"},
            audio_stream_index=0,
            audio_recovery=True,
        )

        command = mock_run.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("aresample=async=1:first_pts=0,apad", graph)
        self.assertIn("[arecovered]", command)

    @patch("lib.media._probe_video_streams", return_value=[{}])
    @patch("lib.media.run")
    def test_single_episode_render_supports_audio_recovery(self, mock_run, _mock_probe):
        render_final(
            "episode.mkv",
            "watermark.png",
            "rendered.mkv",
            {"video_codec": "libx264", "audio_codec": "aac"},
            audio_stream_index=1,
            audio_recovery=True,
        )

        command = mock_run.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("[0:a:1]aresample=async=1:first_pts=0,apad[arecovered]", graph)
        self.assertIn("-shortest", command)

    @patch("lib.media.run")
    def test_episode_render_normalizes_frame_rate_and_canvas_before_watermark(self, mock_run):
        render_episode(
            "episode.mkv",
            "rendered.mkv",
            [(0.0, 10.0)],
            "watermark.png",
            {
                "video_codec": "libx264",
                "frame_rate": "30000/1001",
                "frame_width": 1920,
                "frame_height": 1080,
            },
        )

        command = mock_run.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn(
            "[vcat]fps=fps=30000/1001,"
            "scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[base]",
            graph,
        )

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
    def test_audio_recovery_closes_internal_aac_gap(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_dir = Path(raw_tmp)
            source = tmp_dir / "source-gap.mkv"
            watermark = tmp_dir / "watermark.png"
            output = tmp_dir / "recovered.mkv"
            try:
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=4",
                    "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=4",
                    "-filter:a", "aselect=not(between(t\\,1\\,1.7))",
                    "-map", "0:v", "-map", "1:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(source),
                ], check=True)
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=white:size=32x16",
                    "-frames:v", "1", str(watermark),
                ], check=True)
            except subprocess.CalledProcessError as exc:
                self.skipTest(f"local ffmpeg cannot build recovery fixture: {exc}")

            recovery = analyze_audio_recovery(source)
            if not recovery["applied"]:
                self.skipTest("local ffmpeg normalized the artificial AAC gap")

            render_episode(
                source,
                output,
                [(0.0, 4.0)],
                watermark,
                {
                    "video_codec": "libx264",
                    "preset": "ultrafast",
                    "cq": 28,
                    "audio_codec": "aac",
                },
                audio_stream_index=0,
                audio_recovery=True,
            )
            validation = validate_episode_render(output)

            self.assertGreater(recovery["source_audio_max_packet_gap"], 0.5)
            self.assertLessEqual(validation["timeline"]["audio"]["max_packet_gap"], 0.5)
            self.assertLessEqual(
                abs(
                    validation["timeline"]["video"]["duration"]
                    - validation["timeline"]["audio"]["duration"]
                ),
                0.25,
            )

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
                    "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=2.9",
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
