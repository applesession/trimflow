import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.media import (
    ffprobe_duration,
    ffprobe_media_signature,
    normalize_interstitial_promo,
    render_concat,
    validate_interstitial_promo,
)
from lib.pipeline import (
    RENDER_PIPELINE_VERSION,
    build_branding_banner_render_signature,
    build_episode_fingerprint,
    build_interstitial_manifest,
    build_interstitial_plan,
    build_interstitial_pool_signature,
    build_interstitial_positions,
    build_timestamps_from_episodes,
    discover_interstitial_promo_pool,
    load_or_create_interstitial_plan,
    load_render_checkpoint,
    normalize_branding_banner_config,
    normalize_interstitial_promos_config,
    prepare_interstitial_concat_items,
    slice_interstitial_plan,
)
from lib.helpers import create_concat_file


TARGET_SIGNATURE = {
    "video": {
        "codec_name": "h264",
        "width": 1920,
        "height": 1080,
        "pix_fmt": "yuv420p",
        "r_frame_rate": "30/1",
        "time_base": "1/1000",
    },
    "audio": {
        "codec_name": "aac",
        "sample_fmt": "fltp",
        "sample_rate": "48000",
        "channels": 2,
        "channel_layout": "stereo",
        "time_base": "1/1000",
    },
}


class FirstChoice:
    def choice(self, values):
        return values[0]


class InterstitialPromoTests(unittest.TestCase):
    def make_temp_dir(self):
        root = Path(".test_tmp")
        root.mkdir(exist_ok=True)
        path = Path(tempfile.mkdtemp(dir=root))
        self.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
        return path

    def make_pool(self, directory, count=3):
        pool = []
        for index in range(count):
            path = directory / f"promo-{index + 1}.mp4"
            path.write_bytes(f"promo-{index + 1}".encode())
            pool.append({
                "name": path.name,
                "path": str(path.resolve()),
                "identity": {
                    "path": str(path.resolve()),
                    "size": path.stat().st_size,
                    "mtime_ns": path.stat().st_mtime_ns,
                    "sha256": f"hash-{index + 1}",
                },
            })
        return pool

    def make_config(self, directory):
        return normalize_interstitial_promos_config({
            "processing_mode": "compilation",
            "interstitial_promos": {
                "enabled": True,
                "directory": str(directory),
                "interval_episodes": 4,
            },
        })

    def test_schedule_requires_a_following_episode(self):
        self.assertEqual(build_interstitial_positions(3, 4), [])
        self.assertEqual(build_interstitial_positions(4, 4), [])
        self.assertEqual(build_interstitial_positions(5, 4), [4])
        self.assertEqual(build_interstitial_positions(9, 4), [4, 8])
        self.assertEqual(build_interstitial_positions(12, 4), [4, 8])

    def test_plan_avoids_adjacent_repeats_unless_pool_has_one_file(self):
        directory = self.make_temp_dir()
        config = self.make_config(directory)
        multiple = build_interstitial_plan(
            config,
            self.make_pool(directory, 3),
            13,
            chooser=FirstChoice(),
        )
        self.assertEqual(
            [item["source_file"] for item in multiple],
            ["promo-1.mp4", "promo-2.mp4", "promo-1.mp4"],
        )
        single = build_interstitial_plan(
            config,
            self.make_pool(directory, 1),
            9,
            chooser=FirstChoice(),
        )
        self.assertEqual(
            [item["source_file"] for item in single],
            ["promo-1.mp4", "promo-1.mp4"],
        )

    def test_saved_plan_is_stable_and_pool_change_reselects(self):
        directory = self.make_temp_dir()
        checkpoint = directory / "checkpoint.json"
        config = self.make_config(directory)
        pool = self.make_pool(directory, 2)
        first = load_or_create_interstitial_plan(checkpoint, config, pool, 9)
        second = load_or_create_interstitial_plan(checkpoint, config, pool, 9)
        self.assertEqual(first, second)

        changed_pool = json.loads(json.dumps(pool))
        changed_pool[0]["identity"]["sha256"] = "replacement"
        load_or_create_interstitial_plan(checkpoint, config, changed_pool, 9)
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["pool_signature"],
            build_interstitial_pool_signature(config, changed_pool),
        )

    def test_multi_season_plan_slices_keep_global_positions(self):
        selections = [
            {"after_episode_position": 4},
            {"after_episode_position": 8},
        ]
        self.assertEqual(slice_interstitial_plan(selections, 0, 4), [selections[0]])
        self.assertEqual(slice_interstitial_plan(selections, 4, 3), [])
        self.assertEqual(slice_interstitial_plan(selections, 7, 5), [selections[1]])

    def test_pool_scan_ignores_hidden_and_unsupported_files(self):
        directory = self.make_temp_dir()
        visible = directory / "promo.MP4"
        visible.write_bytes(b"video")
        (directory / ".hidden.mp4").write_bytes(b"hidden")
        (directory / "readme.txt").write_text("ignored", encoding="utf-8")
        config = self.make_config(directory)
        with patch("lib.pipeline.validate_interstitial_promo"):
            pool = discover_interstitial_promo_pool(config)
        self.assertEqual([item["name"] for item in pool], ["promo.MP4"])

    def test_missing_empty_and_corrupt_pool_errors_include_path(self):
        root = self.make_temp_dir()
        missing = root / "missing"
        with self.assertRaisesRegex(RuntimeError, str(missing.resolve()).replace("\\", "\\\\")):
            discover_interstitial_promo_pool(self.make_config(missing))
        empty = root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(RuntimeError, "contains no supported files"):
            discover_interstitial_promo_pool(self.make_config(empty))
        corrupt = root / "corrupt.mp4"
        corrupt.write_bytes(b"bad")
        with patch("lib.media.ffprobe_media_signature", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "corrupt.mp4"):
                validate_interstitial_promo(corrupt)

    def test_concat_items_normalize_unique_file_once_and_shift_timestamps(self):
        directory = self.make_temp_dir()
        config = self.make_config(directory)
        pool = self.make_pool(directory, 1)
        selections = build_interstitial_plan(
            config,
            pool,
            9,
            chooser=FirstChoice(),
        )
        episodes = [directory / f"episode-{index}.mkv" for index in range(1, 10)]
        manifest_episodes = [
            {"episode": index, "cleaned_duration": 10.0}
            for index in range(1, 10)
        ]
        normalized_result = {
            "duration": 3.5,
            "media_signature": TARGET_SIGNATURE,
            "has_audio": True,
        }
        with patch(
            "lib.pipeline.normalize_interstitial_promo",
            return_value=normalized_result,
        ) as normalize_mock:
            items, durations, insertions = prepare_interstitial_concat_items(
                config=config,
                selections=selections,
                episode_outputs=episodes,
                episode_durations=[10.0] * 9,
                manifest_episodes=manifest_episodes,
                target_signature=TARGET_SIGNATURE,
                normalized_dir=directory / "normalized",
                season=1,
            )
        self.assertEqual(normalize_mock.call_count, 1)
        self.assertEqual(len(items), 11)
        self.assertEqual(sum(durations), 97.0)
        self.assertEqual(
            build_timestamps_from_episodes(manifest_episodes, insertions)[4],
            "00:00:43 - 5 серия",
        )
        self.assertEqual(
            build_timestamps_from_episodes(manifest_episodes, insertions)[8],
            "00:01:27 - 9 серия",
        )
        manifest = build_interstitial_manifest(config, pool, selections, insertions)
        self.assertTrue(manifest["enabled"])
        self.assertEqual(manifest["insertions"][0]["after_episode"], 4)
        self.assertEqual(manifest["insertions"][0]["duration_seconds"], 3.5)

    def test_absent_config_and_4k_mode_are_disabled(self):
        self.assertFalse(normalize_interstitial_promos_config({})["active"])
        self.assertFalse(normalize_interstitial_promos_config({
            "processing_mode": "upscale_4k",
            "interstitial_promos": {"enabled": True},
        })["active"])

    def test_pool_change_invalidates_final_not_episode_fingerprint(self):
        directory = self.make_temp_dir()
        source = directory / "episode.mkv"
        branding = directory / "branding.mp4"
        source.write_bytes(b"episode")
        branding.write_bytes(b"branding")
        job = {
            "title": "Test",
            "season": 1,
            "episodes_range": "001-005",
            "processing_mode": "compilation",
            "source": {"type": "local", "input_dir": str(directory)},
            "branding_banner": {"path": str(branding)},
            "timing_detection": {"enabled": False},
            "timing_providers": {},
            "encoding": {"video_codec": "libx264", "audio_codec": "aac"},
            "interstitial_promos": {
                "enabled": True,
                "directory": str(directory),
                "interval_episodes": 4,
            },
        }
        episode_infos = [{
            "episode": 1,
            "path": str(source),
            "duration": 10.0,
            "frame_rate": "30/1",
            "width": 1920,
            "height": 1080,
        }]
        fingerprint = build_episode_fingerprint(
            job,
            episode_infos,
            branding_banner=normalize_branding_banner_config(job),
            timing_detection=job["timing_detection"],
            preferred_language="rus",
        )
        config = self.make_config(directory)
        pool = self.make_pool(directory, 1)
        selections = build_interstitial_plan(config, pool, 5, chooser=FirstChoice())
        promo_manifest = build_interstitial_manifest(config, pool, selections, [{
            "after_episode_position": 4,
            "after_season": 1,
            "after_episode": 4,
            "source_file": pool[0]["name"],
            "duration_seconds": 3.0,
        }])
        output = directory / "output.mkv"
        timestamps = directory / "output.txt"
        manifest_path = directory / "output_manifest.json"
        output.write_bytes(b"output")
        timestamps.write_text("00:00:00 - 1 серия\n", encoding="utf-8")
        manifest_path.write_text(json.dumps({
            "render_pipeline_version": RENDER_PIPELINE_VERSION,
            "render_complete": True,
            "branding_banner": build_branding_banner_render_signature(job),
            "title": job["title"],
            "season": "01",
            "episodes_range": job["episodes_range"],
            "output_video": output.name,
            "output_timestamps": timestamps.name,
            "episodes": [],
            "interstitial_promos": promo_manifest,
        }), encoding="utf-8")
        artifacts = {
            "output_video": output,
            "output_txt": timestamps,
            "output_manifest": manifest_path,
        }
        with patch("lib.pipeline.ffprobe_duration", return_value=10.0):
            self.assertIsNotNone(load_render_checkpoint(
                job,
                artifacts,
                {"config": config, "pool": pool},
            ))
            changed_pool = json.loads(json.dumps(pool))
            changed_pool[0]["identity"]["sha256"] = "replacement"
            self.assertIsNone(load_render_checkpoint(
                job,
                artifacts,
                {"config": config, "pool": changed_pool},
            ))
        self.assertEqual(fingerprint, build_episode_fingerprint(
            job,
            episode_infos,
            branding_banner=normalize_branding_banner_config(job),
            timing_detection=job["timing_detection"],
            preferred_language="rus",
        ))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
class InterstitialPromoFfmpegSmokeTests(unittest.TestCase):
    def test_normalized_silent_promo_stream_copy_concat(self):
        root = Path(".test_tmp")
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            directory = Path(temporary)
            episode = directory / "episode.mkv"
            promo = directory / "promo.mp4"
            normalized = directory / "normalized.mkv"
            output = directory / "output.mkv"
            concat_file = directory / "concat.txt"
            subprocess.check_call([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=black:s=320x180:r=24:d=1",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(episode),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.check_call([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=red:s=100x100:r=30:d=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(promo),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            target_signature = ffprobe_media_signature(episode)
            result = normalize_interstitial_promo(promo, normalized, target_signature)
            self.assertEqual(result["media_signature"], target_signature)
            create_concat_file(
                [episode, normalized, episode],
                concat_file,
                durations=[
                    ffprobe_duration(episode),
                    ffprobe_duration(normalized),
                    ffprobe_duration(episode),
                ],
            )
            render_concat(concat_file, output, allow_reencode=False)
            self.assertGreater(ffprobe_duration(output), 2.5)


if __name__ == "__main__":
    unittest.main()
