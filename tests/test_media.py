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
    select_audio_stream_by_language,
    select_external_audio,
    validate_episode_render,
)


class MediaAudioSelectionTests(unittest.TestCase):
    def test_selects_japanese_embedded_audio_for_analysis(self):
        streams = [
            {"audio_index": 0, "language": "rus", "title": "Russian", "is_default": True},
            {"audio_index": 1, "language": "jpn", "title": "Japanese", "is_default": False},
        ]

        selected = select_audio_stream_by_language(streams, "jpn", fallback=False)

        self.assertEqual(selected["audio_index"], 1)

    def test_analysis_audio_selection_has_no_implicit_fallback(self):
        streams = [
            {"audio_index": 0, "language": "rus", "title": "Russian", "is_default": True},
        ]

        self.assertIsNone(select_audio_stream_by_language(streams, "jpn", fallback=False))
        self.assertEqual(
            select_audio_stream_by_language(streams, "jpn", fallback=True)["audio_index"],
            0,
        )

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
        self.assertIn("[2:a:1]asetpts=PTS-STARTPTS[anormalized]", graph)
        self.assertIn("[anormalized]atrim", graph)
        self.assertIn("[acat]apad=pad_dur=15[aexternal]", graph)

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
        self.assertIn(
            "[2:a:0]asetpts=PTS-STARTPTS,apad=pad_dur=15[aexternal]",
            graph,
        )
        self.assertIn("[aexternal]", command)
        self.assertIn("-shortest", command)

    @patch("lib.media._probe_video_streams", return_value=[{}])
    @patch("lib.media.run")
    def test_single_episode_render_caps_output_at_video_duration(self, mock_run, _mock_probe):
        render_final(
            "episode.mkv",
            "watermark.png",
            "rendered.mkv",
            {"video_codec": "libx264", "audio_codec": "aac"},
            audio_stream_index=0,
            target_duration=1394.142,
        )

        command = mock_run.call_args.args[0]
        self.assertEqual(command[command.index("-t") + 1], "1394.142000")
        self.assertNotIn("-shortest", command)

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
        self.assertNotIn("-shortest", command)
        self.assertEqual(command[command.index("-t") + 1], "110.000000")
        self.assertIn("overlay=W-w-20:20", graph)
        self.assertNotIn("0:s", command)

    @patch("lib.media.run")
    def test_episode_render_normalizes_negative_input_timestamps_before_trim(self, mock_run):
        render_episode(
            "episode.mkv",
            "rendered.mkv",
            [(0.0, 1422.045)],
            "watermark.png",
            {"video_codec": "libx264"},
            audio_stream_index=0,
        )

        command = mock_run.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        video_normalization = "[0:v:0]setpts=PTS-STARTPTS[vnormalized]"
        audio_normalization = "[0:a:0]asetpts=PTS-STARTPTS[anormalized]"
        video_trim = "[vnormalized]trim=start=0.000000:end=1422.045000"
        audio_trim = "[anormalized]atrim=start=0.000000:end=1422.045000"

        self.assertLess(graph.index(video_normalization), graph.index(video_trim))
        self.assertLess(graph.index(audio_normalization), graph.index(audio_trim))
        self.assertEqual(command[command.index("-t") + 1], "1422.045000")

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
        self.assertIn("aresample=async=1:first_pts=0,apad=pad_dur=15", graph)
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
        self.assertIn(
            "[0:a:1]aresample=async=1:first_pts=0,apad=pad_dur=15[arecovered]",
            graph,
        )
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
    def test_episode_render_adds_animated_support_banner_and_shifts_external_audio(self, mock_run):
        support_banner = {
            "shown": True,
            "path": "support_banner.png",
            "start": 7.0,
            "duration": 6.0,
            "slide_seconds": 0.5,
            "width_px": 596,
            "bottom_margin_px": 40,
        }

        render_episode(
            "episode.mkv",
            "rendered.mkv",
            [(0.0, 20.0)],
            "watermark.png",
            {"video_codec": "libx264"},
            audio_stream_index=1,
            external_audio_path="episode.mka",
            support_banner=support_banner,
        )

        command = mock_run.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertEqual(
            [command[index + 1] for index, value in enumerate(command) if value == "-i"],
            ["episode.mkv", "watermark.png", "support_banner.png", "episode.mka"],
        )
        self.assertIn("[2:v]scale=596:-1,format=rgba[support_banner]", graph)
        self.assertIn("x=(W-w)/2", graph)
        self.assertIn("H-h-40", graph)
        self.assertIn("H-(h+40)", graph)
        self.assertIn("enable='between(t,7.000000,13.000000)'", graph)
        self.assertIn("[3:a:1]", graph)

    @patch("lib.media._probe_video_streams", return_value=[{}])
    @patch("lib.media.run")
    def test_single_episode_render_adds_support_banner(self, mock_run, _mock_probe):
        render_final(
            "episode.mkv",
            "watermark.png",
            "rendered.mkv",
            {"video_codec": "libx264", "audio_codec": "aac"},
            audio_stream_index=0,
            support_banner={
                "shown": True,
                "path": "support_banner.png",
                "start": 2.0,
                "duration": 6.0,
                "slide_seconds": 0.5,
                "width_px": 596,
                "bottom_margin_px": 40,
            },
        )

        command = mock_run.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("[2:v]scale=596:-1,format=rgba[support_banner]", graph)
        self.assertIn("enable='between(t,2.000000,8.000000)'", graph)
        self.assertIn("0:a:0?", command)

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

    @patch("lib.media.run")
    def test_episode_render_falls_back_to_cpu_on_nvenc_code(self, mock_run):
        failure = RuntimeError("ffmpeg exited with code 220")
        failure.__cause__ = subprocess.CalledProcessError(220, ["ffmpeg"])
        mock_run.side_effect = [failure, None]

        support_banner = {
            "shown": True,
            "path": "support_banner.png",
            "start": 2.0,
            "duration": 6.0,
            "slide_seconds": 0.5,
            "width_px": 596,
            "bottom_margin_px": 40,
        }

        render_episode(
            "episode.mkv",
            "rendered.mkv",
            [(0.0, 10.0)],
            "watermark.png",
            {"video_codec": "h264_nvenc", "preset": "fast", "cq": 23},
            audio_stream_index=0,
            audio_recovery=True,
            support_banner=support_banner,
        )

        self.assertEqual(mock_run.call_count, 2)
        first_command, fallback_command = [call.args[0] for call in mock_run.call_args_list]
        self.assertEqual(first_command[first_command.index("-c:v") + 1], "h264_nvenc")
        self.assertEqual(fallback_command[fallback_command.index("-c:v") + 1], "libx264")
        self.assertEqual(
            first_command[first_command.index("-filter_complex") + 1],
            fallback_command[fallback_command.index("-filter_complex") + 1],
        )

    @patch("lib.media.run")
    def test_episode_render_does_not_fallback_on_enospc(self, mock_run):
        failure = RuntimeError("ffmpeg exited with code 228")
        failure.__cause__ = subprocess.CalledProcessError(228, ["ffmpeg"])
        mock_run.side_effect = failure

        with self.assertRaisesRegex(RuntimeError, "code 228"):
            render_episode(
                "episode.mkv",
                "rendered.mkv",
                [(0.0, 10.0)],
                "watermark.png",
                {"video_codec": "h264_nvenc", "preset": "fast", "cq": 23},
                audio_stream_index=0,
                audio_recovery=True,
            )

        mock_run.assert_called_once()

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

    @patch("lib.media.ffprobe_episode_timeline", return_value={
        "video": {"start": 0.0, "duration": 10.0, "max_packet_gap": 0.04},
        "audio": {"start": -0.021, "duration": 11.078, "max_packet_gap": 0.03},
    })
    @patch("lib.media.ffprobe_media_signature", return_value={"video": {}, "audio": {}})
    @patch("lib.media.ffprobe_duration", return_value=11.057)
    def test_episode_validation_reports_signed_end_delta(
        self,
        _mock_duration,
        _mock_signature,
        _mock_timeline,
    ):
        with self.assertRaisesRegex(
            RuntimeError,
            r"video_end=10\.000s, audio_end=11\.057s, delta=\+1\.057s",
        ):
            validate_episode_render("episode.mkv")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
class MediaEpisodeIntegrationTests(unittest.TestCase):
    def test_support_banner_appears_only_in_midpoint_window_and_keeps_audio(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_dir = Path(raw_tmp)
            source = tmp_dir / "source.mkv"
            watermark = tmp_dir / "watermark.png"
            banner = tmp_dir / "support_banner.png"
            output = tmp_dir / "output.mkv"
            try:
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=black:size=320x180:rate=24:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=6",
                    "-map", "0:v", "-map", "1:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(source),
                ], check=True)
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=white:size=16x8",
                    "-frames:v", "1", str(watermark),
                ], check=True)
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=red@0.8:size=200x50,format=rgba",
                    "-frames:v", "1", str(banner),
                ], check=True)
            except subprocess.CalledProcessError as exc:
                self.skipTest(f"local ffmpeg cannot build banner fixture: {exc}")

            render_final(
                source,
                watermark,
                output,
                {
                    "video_codec": "libx264",
                    "preset": "ultrafast",
                    "cq": 28,
                    "audio_codec": "aac",
                },
                audio_stream_index=0,
                target_duration=6.0,
                support_banner={
                    "shown": True,
                    "path": str(banner),
                    "start": 2.0,
                    "duration": 2.0,
                    "slide_seconds": 0.25,
                    "width_px": 200,
                    "bottom_margin_px": 10,
                },
            )
            validation = validate_episode_render(output)
            self.assertIsNotNone(validation["media_signature"]["audio"])
            self.assertAlmostEqual(validation["duration"], 6.0, delta=0.25)

            def sample_rgb(timestamp):
                result = subprocess.run([
                    "ffmpeg", "-v", "error", "-ss", str(timestamp), "-i", str(output),
                    "-vf", "crop=1:1:160:145,format=rgb24",
                    "-frames:v", "1", "-f", "rawvideo", "-",
                ], check=True, capture_output=True)
                return tuple(result.stdout[:3])

            before = sample_rgb(1.0)
            middle = sample_rgb(3.0)
            after = sample_rgb(5.0)
            self.assertLess(max(before), 40)
            self.assertGreater(middle[0], 100)
            self.assertGreater(middle[0], middle[1] + 50)
            self.assertLess(max(after), 40)

    def test_single_episode_caps_long_audio_without_hiding_short_audio(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp_dir = Path(raw_tmp)
            watermark = tmp_dir / "watermark.png"
            long_source = tmp_dir / "long-audio.mkv"
            short_source = tmp_dir / "short-audio.mkv"
            long_output = tmp_dir / "long-output.mkv"
            short_output = tmp_dir / "short-output.mkv"
            try:
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=white:size=32x16",
                    "-frames:v", "1", str(watermark),
                ], check=True)
                for source, audio_duration in ((long_source, 5.0), (short_source, 2.9)):
                    subprocess.run([
                        "ffmpeg", "-v", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=4",
                        "-f", "lavfi", "-i",
                        f"sine=frequency=1000:sample_rate=48000:duration={audio_duration}",
                        "-map", "0:v", "-map", "1:a",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", str(source),
                    ], check=True)
            except subprocess.CalledProcessError as exc:
                self.skipTest(f"local ffmpeg cannot build duration fixture: {exc}")

            encoding = {
                "video_codec": "libx264",
                "preset": "ultrafast",
                "cq": 28,
                "audio_codec": "aac",
            }
            render_final(
                long_source,
                watermark,
                long_output,
                encoding,
                target_duration=4.0,
            )
            validate_episode_render(long_output)

            render_final(
                short_source,
                watermark,
                short_output,
                encoding,
                target_duration=4.0,
            )
            with self.assertRaisesRegex(RuntimeError, "A/V duration mismatch"):
                validate_episode_render(short_output)

            strict_compilation_output = tmp_dir / "short-compilation-output.mkv"
            recovered_compilation_output = tmp_dir / "recovered-compilation-output.mkv"
            render_episode(
                short_source,
                strict_compilation_output,
                [(0.0, 4.0)],
                watermark,
                encoding,
                audio_stream_index=0,
            )
            with self.assertRaisesRegex(RuntimeError, "A/V duration mismatch"):
                validate_episode_render(strict_compilation_output)

            render_episode(
                short_source,
                recovered_compilation_output,
                [(0.0, 4.0)],
                watermark,
                encoding,
                audio_stream_index=0,
                audio_recovery=True,
            )
            validate_episode_render(recovered_compilation_output)

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
                    "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=4",
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
