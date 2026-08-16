import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from lib import reset_test_db  # noqa: F401 - initializes src on sys.path
from core.torrent import (
    SOURCE_MARKER_NAME,
    download_selected_episodes,
    download_torrent_episode,
    prepare_torrent_episode_downloads,
    select_torrent_external_audio_files,
    select_torrent_episode_files,
)


class TorrentSelectionTests(unittest.TestCase):
    def make_temp_dir(self):
        root = Path(".test_tmp") / f"torrent_{uuid4().hex}"
        root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root

    @patch("core.pipeline.find_episode_files", return_value=([(1, Path("episode-001.mkv"))], []))
    @patch("core.pipeline.download_selected_episodes")
    def test_normal_pipeline_uses_shared_selective_downloader(self, mock_download, _mock_find):
        from core.pipeline import collect_episode_files

        collect_episode_files(
            {"type": "magnet", "magnet": "magnet:?xt=urn:btih:test", "download_dir": "downloads/test"},
            "test",
            {1, 2, 3},
            processing={"source_path_contains": "HEVC"},
            download_timeout=60,
        )

        mock_download.assert_called_once_with(
            "magnet:?xt=urn:btih:test",
            Path("downloads/test"),
            {1, 2, 3},
            path_filter="HEVC",
            timeout=60,
        )

    def test_selects_only_requested_indices(self):
        files = [
            {"index": episode, "path": f"Release/Show [{episode:03d}] [1080p].mkv"}
            for episode in range(1, 171)
        ]

        selected = select_torrent_episode_files(files, {1, 2, 3})

        self.assertEqual([item["index"] for item in selected], [1, 2, 3])

    def test_external_audio_selection_uses_episode_number_and_supported_extensions(self):
        selected = select_torrent_external_audio_files([
            {"index": 10, "path": "RUS Sound/Show - 001.mka"},
            {"index": 11, "path": "RUS Sound/Show - 002.flac"},
            {"index": 12, "path": "RUS Subs/Show - 001.ass"},
            {"index": 13, "path": "RUS Sound/Show - 003.mka"},
        ], {1, 2})

        self.assertEqual([item["index"] for item in selected], [10, 11])

    @patch("core.torrent.subprocess.check_output")
    @patch("core.torrent.run")
    def test_payload_download_includes_external_audio_for_requested_episodes(self, mock_run, mock_show):
        download_dir = self.make_temp_dir() / "downloads"

        def fake_run(command, timeout=None):
            if "--bt-metadata-only=true" in command:
                (download_dir / "release.torrent").write_bytes(b"metadata")

        mock_run.side_effect = fake_run
        mock_show.return_value = "\n".join([
            " 1|Show - 001.mkv",
            " 2|Show - 002.mkv",
            " 10|RUS Sound/Show - 001.mka",
            " 11|RUS Sound/Show - 002.mka",
            " 12|RUS Subs/Show - 001.ass",
        ])

        download_selected_episodes("magnet:?xt=urn:btih:audio", download_dir, {1})

        payload_command = mock_run.call_args_list[-1].args[0]
        self.assertIn("--select-file=1,10", payload_command)

    def test_selects_episode_from_of_total_filename(self):
        selected = select_torrent_episode_files([
            {"index": 7, "path": "Release/Show [02 of 25] [720p].mkv"},
            {"index": 3, "path": "Release/Show [01 of 25] [720p].mkv"},
        ], {1, 2})

        self.assertEqual(
            [(item["episode"], item["index"]) for item in selected],
            [(1, 3), (2, 7)],
        )

    def test_selects_four_digit_episode_number(self):
        selected = select_torrent_episode_files([
            {"index": 1, "path": "One Piece - 1156.mkv"},
        ], {1156})

        self.assertEqual(selected[0]["episode"], 1156)

    def test_selects_legacy_avi_episode(self):
        selected = select_torrent_episode_files([{
            "index": 1,
            "path": "One.Piece.001.IPTVRip.2x2.XviD.640x480.RUS.avi",
        }], {1})

        self.assertEqual(selected[0]["episode"], 1)

    def test_unique_1080p_candidate_wins_over_720p(self):
        selected = select_torrent_episode_files([
            {"index": 1, "path": "720p/Show [001] [720p].mkv"},
            {"index": 2, "path": "1080p/Show [001] [1080p].mkv"},
        ], {1})

        self.assertEqual(selected[0]["index"], 2)

    def test_episode_directory_wins_over_opening_and_ending(self):
        selected = select_torrent_episode_files([
            {"index": 3, "path": "Release [1080p]/Episodes/Show - 003.mkv"},
            {"index": 174, "path": "Release [1080p]/Openings & Endings/Show - NCED 03.mkv"},
            {"index": 189, "path": "Release [1080p]/Openings & Endings/Show - NCOP 03.mkv"},
        ], {3})

        self.assertEqual(selected[0]["index"], 3)

    def test_ambiguous_1080p_candidates_fail_before_download(self):
        with self.assertRaisesRegex(RuntimeError, "Multiple torrent files.*AVC.*HEVC"):
            select_torrent_episode_files([
                {"index": 1, "path": "AVC/Show [001] [1080p].mkv"},
                {"index": 2, "path": "HEVC/Show [001] [1080p].mkv"},
            ], {1})

    def test_literal_case_insensitive_path_filter_resolves_ambiguity(self):
        selected = select_torrent_episode_files([
            {"index": 1, "path": "TV/AVC/Show [001] [1080p].mkv"},
            {"index": 2, "path": "Special/HEVC/Show [001] [1080p].mkv"},
        ], {1}, path_filter="special/hevc")

        self.assertEqual(selected[0]["index"], 2)

    def test_missing_episode_and_unsupported_extension_fail_preflight(self):
        with self.assertRaisesRegex(RuntimeError, r"episodes: \[2\]"):
            select_torrent_episode_files([
                {"index": 1, "path": "Show [001].mkv"},
                {"index": 2, "path": "Show [002].wmv"},
            ], {1, 2})

    @patch("core.torrent.subprocess.check_output")
    @patch("core.torrent.run")
    def test_metadata_precedes_payload_and_payload_selects_only_range(self, mock_run, mock_show):
        download_dir = self.make_temp_dir() / "downloads"
        calls = []

        def fake_run(command, timeout=None):
            calls.append(command)
            if "--bt-metadata-only=true" in command:
                (download_dir / "release.torrent").write_bytes(b"metadata")

        mock_run.side_effect = fake_run
        mock_show.return_value = "\n".join(
            f" {episode}|Release/Show [{episode:03d}] [1080p].mkv"
            for episode in range(1, 171)
        )

        download_selected_episodes("magnet:?xt=urn:btih:first", download_dir, {1, 2, 3})

        self.assertIn("--bt-metadata-only=true", calls[0])
        self.assertIn("--select-file=1,2,3", calls[1])
        self.assertIn("--bt-remove-unselected-file=true", calls[1])
        self.assertTrue(str(calls[1][-1]).endswith("release.torrent"))

    @patch("core.torrent.subprocess.check_output")
    @patch("core.torrent.run")
    def test_prepare_validates_full_range_without_payload_download(self, mock_run, mock_show):
        download_dir = self.make_temp_dir() / "downloads"

        def fake_run(command, timeout=None):
            (download_dir / "release.torrent").write_bytes(b"metadata")

        mock_run.side_effect = fake_run
        mock_show.return_value = "\n".join([
            " 1|Release/Show [001] [1080p].mkv",
            " 2|Release/Show [002] [1080p].mkv",
        ])

        torrent_path, selected = prepare_torrent_episode_downloads(
            "magnet:?xt=urn:btih:first",
            download_dir,
            {1, 2},
        )

        self.assertEqual(torrent_path, download_dir / "release.torrent")
        self.assertEqual([item["index"] for item in selected], [1, 2])
        self.assertEqual(mock_run.call_count, 1)
        self.assertIn("--bt-metadata-only=true", mock_run.call_args.args[0])

    @patch("core.torrent.run")
    def test_single_episode_download_uses_isolated_slot_and_one_index(self, mock_run):
        root = self.make_temp_dir()
        torrent_path = root / "release.torrent"
        torrent_path.write_bytes(b"metadata")
        slot_dir = root / "episode_002"

        download_torrent_episode(
            torrent_path,
            slot_dir,
            {"episode": 2, "index": 42, "path": "Release/Show [002].mkv"},
        )

        command = mock_run.call_args.args[0]
        self.assertEqual(command[command.index("--dir") + 1], str(slot_dir))
        self.assertIn("--select-file=42", command)
        self.assertIn("--bt-remove-unselected-file=true", command)

    @patch("core.torrent.subprocess.check_output")
    @patch("core.torrent.run")
    def test_retry_reuses_metadata_and_range_extension_preserves_downloads(self, mock_run, mock_show):
        download_dir = self.make_temp_dir() / "downloads"

        def fake_run(command, timeout=None):
            if "--bt-metadata-only=true" in command:
                (download_dir / "release.torrent").write_bytes(b"metadata")

        mock_run.side_effect = fake_run
        mock_show.return_value = "\n".join([
            " 1|Release/Show [001] [1080p].mkv",
            " 2|Release/Show [002] [1080p].mkv",
        ])

        magnet = "magnet:?xt=urn:btih:first"
        download_selected_episodes(magnet, download_dir, {1})
        sentinel = download_dir / "Release" / "Show [001] [1080p].mkv"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(b"partial-or-complete")
        mock_run.reset_mock()

        download_selected_episodes(magnet, download_dir, {1, 2})

        self.assertTrue(sentinel.exists())
        commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertFalse(any("--bt-metadata-only=true" in command for command in commands))
        self.assertIn("--select-file=1,2", commands[0])

    @patch("core.torrent.subprocess.check_output")
    @patch("core.torrent.run")
    def test_new_magnet_or_filter_clears_old_downloads(self, mock_run, mock_show):
        download_dir = self.make_temp_dir() / "downloads"

        def fake_run(command, timeout=None):
            if "--bt-metadata-only=true" in command:
                (download_dir / "release.torrent").write_bytes(b"metadata")

        mock_run.side_effect = fake_run
        mock_show.return_value = "\n".join([
            " 1|HEVC/Show [001] [1080p].mkv",
            " 2|AVC/Show [001] [1080p].mkv",
        ])

        download_selected_episodes("magnet:?xt=urn:btih:first", download_dir, {1}, path_filter="hevc")
        sentinel = download_dir / "old.mkv"
        sentinel.write_bytes(b"old")
        download_selected_episodes("magnet:?xt=urn:btih:second", download_dir, {1}, path_filter="hevc")
        self.assertFalse(sentinel.exists())

        sentinel.write_bytes(b"old-again")
        download_selected_episodes("magnet:?xt=urn:btih:second", download_dir, {1}, path_filter="avc")
        self.assertFalse(sentinel.exists())
        marker = json.loads((download_dir / SOURCE_MARKER_NAME).read_text(encoding="utf-8"))
        self.assertEqual(marker["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
