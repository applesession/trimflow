import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.anilibria import (
    _build_recent_releases_from_api,
    _build_request_kwargs,
    _extract_release_aliases_from_torrents_page,
    get_anilibria_proxy_url,
    list_recent_releases,
)
from lib.autojobs import collect_release_episode_numbers, discover_jobs, format_episodes_range, merge_episode_ranges
from lib.config import build_default_state, load_completed_jobs, load_jobs, load_state


class ConfigStateTests(unittest.TestCase):
    def make_workspace_temp_dir(self):
        root = Path(".test_tmp")
        root.mkdir(exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(dir=root))
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return temp_dir

    def test_load_jobs_returns_empty_list_when_file_is_missing(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = {
            "automation": {
                "jobs_path": str(tmp_dir / "jobs.json"),
                "state_path": str(tmp_dir / "state.json"),
            }
        }

        self.assertEqual(load_jobs(config), [])

    def test_load_state_returns_default_structure_when_file_is_missing(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = {
            "automation": {
                "jobs_path": str(tmp_dir / "jobs.json"),
                "state_path": str(tmp_dir / "state.json"),
            }
        }

        self.assertEqual(load_state(config), build_default_state())

    def test_load_completed_jobs_returns_empty_list_when_file_is_missing(self):
        tmp_dir = self.make_workspace_temp_dir()
        config = {
            "automation": {
                "jobs_path": str(tmp_dir / "jobs.json"),
                "completed_jobs_path": str(tmp_dir / "completed_jobs.json"),
                "state_path": str(tmp_dir / "state.json"),
            }
        }

        self.assertEqual(load_completed_jobs(config), [])

    def test_merge_episode_ranges_merges_contiguous_ranges(self):
        self.assertEqual(merge_episode_ranges("001-003", [4]), "001-004")
        self.assertEqual(merge_episode_ranges("001-003", [5, 6]), "001-003,005-006")
        self.assertEqual(merge_episode_ranges("005", [7]), "005,007")

    def test_format_episodes_range_compacts_sorted_values(self):
        self.assertEqual(format_episodes_range([6, 5, 7, 9]), "005-007,009")

    def test_extract_release_aliases_from_torrents_page_deduplicates_in_order(self):
        html = """
        <a href="/anime/releases/release/test-one/torrents">A</a>
        <a href="/anime/releases/release/test-two">B</a>
        <a href="/anime/releases/release/test-one">C</a>
        <a href="/anime/releases/test-three">D</a>
        <a href="/anime/releases/test-two">E</a>
        """

        self.assertEqual(
            _extract_release_aliases_from_torrents_page(html),
            ["test-one", "test-two", "test-three"],
        )

    def test_get_anilibria_proxy_url_returns_env_value(self):
        with patch.dict("os.environ", {"ANILIBERTY_PROXY_URL": "socks5h://user:pass@host:1080"}):
            self.assertEqual(get_anilibria_proxy_url(), "socks5h://user:pass@host:1080")

    def test_build_request_kwargs_uses_proxy_when_configured(self):
        with patch.dict("os.environ", {"ANILIBERTY_PROXY_URL": "socks5h://user:pass@host:1080"}):
            kwargs = _build_request_kwargs(timeout=20, params={"query": "test"})

        self.assertEqual(kwargs["timeout"], 20)
        self.assertEqual(kwargs["params"], {"query": "test"})
        self.assertEqual(kwargs["proxies"]["http"], "socks5h://user:pass@host:1080")
        self.assertEqual(kwargs["proxies"]["https"], "socks5h://user:pass@host:1080")

    def test_build_request_kwargs_omits_proxy_when_not_configured(self):
        with patch.dict("os.environ", {}, clear=False):
            if "ANILIBERTY_PROXY_URL" in __import__("os").environ:
                del __import__("os").environ["ANILIBERTY_PROXY_URL"]
            kwargs = _build_request_kwargs(timeout=30)

        self.assertEqual(kwargs["timeout"], 30)
        self.assertNotIn("params", kwargs)
        self.assertNotIn("proxies", kwargs)

    @patch("lib.anilibria._request")
    def test_build_recent_releases_from_api_uses_list_endpoint(self, mock_request):
        mock_request.return_value = (
            {
                "list": [
                    {"id": 2, "alias": "b", "updated_at": "2026-06-12T10:00:00+00:00"},
                    {"id": 1, "alias": "a", "updated_at": "2026-06-13T10:00:00+00:00"},
                ]
            },
            "https://aniliberty.top/api/v1/anime/releases/latest?limit=25",
        )

        urls = []
        errors = []
        releases = _build_recent_releases_from_api(25, urls, errors)

        self.assertEqual(len(releases), 2)
        self.assertEqual(urls, ["https://aniliberty.top/api/v1/anime/releases/latest?limit=25"])
        self.assertEqual(errors, [])
        request_url = mock_request.call_args.args[0]
        request_params = mock_request.call_args.kwargs["params"]
        self.assertEqual(request_url, "https://aniliberty.top/api/v1/anime/releases/latest")
        self.assertEqual(request_params, {"limit": 25})

    @patch("lib.anilibria._build_recent_releases_from_torrents_page")
    @patch("lib.anilibria._request")
    def test_list_recent_releases_prefers_api_results(self, mock_request, mock_torrents):
        mock_request.return_value = (
            {
                "items": [
                    {"id": 1, "alias": "older", "updated_at": "2026-06-12T10:00:00+00:00"},
                    {"id": 2, "alias": "newer", "updated_at": "2026-06-13T10:00:00+00:00"},
                ]
            },
            "https://aniliberty.top/api/v1/anime/releases/latest?limit=25",
        )
        mock_torrents.return_value = []

        result = list_recent_releases(limit=25)

        self.assertEqual([item["alias"] for item in result["releases"]], ["newer", "older"])
        self.assertEqual(result["request_urls"], ["https://aniliberty.top/api/v1/anime/releases/latest?limit=25"])
        mock_torrents.assert_not_called()

    @patch("lib.anilibria._build_recent_releases_from_torrents_page")
    @patch("lib.anilibria._request")
    def test_list_recent_releases_falls_back_to_torrents_page_when_api_is_empty(self, mock_request, mock_torrents):
        mock_request.return_value = (
            {"items": []},
            "https://aniliberty.top/api/v1/anime/releases/latest?limit=25",
        )
        mock_torrents.return_value = [{"id": 3, "alias": "fallback", "updated_at": "2026-06-13T10:00:00+00:00"}]

        result = list_recent_releases(limit=25)

        self.assertEqual([item["alias"] for item in result["releases"]], ["fallback"])
        mock_torrents.assert_called_once()

    def test_collect_release_episode_numbers_ignores_zero_and_negative(self):
        release_payload = {
            "episodes": [
                {"number": 0},
                {"number": -1},
                {"number": 1},
                {"number": "2"},
            ]
        }

        self.assertEqual(collect_release_episode_numbers(release_payload), [1, 2])

    @patch("lib.autojobs.get_release_details")
    @patch("lib.autojobs.list_recent_releases")
    def test_discover_jobs_creates_jobs_and_is_idempotent(self, mock_list_recent_releases, mock_get_release_details):
        mock_list_recent_releases.return_value = {
            "releases": [
                {"id": 10190, "alias": "test-release", "is_ongoing": True},
            ],
            "request_urls": ["https://aniliberty.top/api/v1/anime/releases/list?limit=25"],
        }
        mock_get_release_details.return_value = {
            "release": {
                "id": 10190,
                "alias": "test-release",
                "is_ongoing": True,
                "name": {"english": "Test Release", "main": "Тестовый релиз"},
                "external_ids": {"mal_id": 12345},
                "torrent": {"magnet": "magnet:?xt=urn:btih:testrelease"},
                "episodes": [
                    {"number": 5},
                    {"number": 6},
                ],
            },
            "request_url": "https://aniliberty.top/api/v1/anime/releases/test-release",
        }

        config = {
            "automation": {
                "poll_limit": 25,
                "download_root": "./downloads",
                "default_source_type": "magnet",
            }
        }
        first_result = discover_jobs(config, [], build_default_state())

        self.assertEqual(len(first_result["jobs"]), 1)
        self.assertEqual(first_result["jobs"][0]["episodes_range"], "005-006")
        self.assertEqual(first_result["jobs"][0]["title_ru"], "Тестовый релиз")
        self.assertEqual(first_result["summary"]["created_jobs"], 1)
        self.assertEqual(len(first_result["state"]["seen_release_episodes"]), 2)

        second_result = discover_jobs(config, first_result["jobs"], first_result["state"])
        self.assertEqual(len(second_result["jobs"]), 1)
        self.assertEqual(second_result["summary"]["created_jobs"], 0)
        self.assertEqual(second_result["summary"]["updated_jobs"], 0)

    @patch("lib.autojobs.get_release_details")
    @patch("lib.autojobs.list_recent_releases")
    def test_discover_jobs_expands_existing_job_range(self, mock_list_recent_releases, mock_get_release_details):
        mock_list_recent_releases.return_value = {
            "releases": [
                {"id": 42, "alias": "existing-release", "is_ongoing": True},
            ],
            "request_urls": [],
        }
        magnet = "magnet:?xt=urn:btih:existing"
        mock_get_release_details.return_value = {
            "release": {
                "id": 42,
                "alias": "existing-release",
                "is_ongoing": True,
                "name": {"english": "Existing Release"},
                "external_ids": {"mal_id": 777},
                "torrent": {"magnet": magnet},
                "episodes": [{"number": 4}],
            },
            "request_url": "https://aniliberty.top/api/v1/anime/releases/existing-release",
        }

        jobs = [{
            "title": "Existing Release",
            "mal_id": 777,
            "season": 1,
            "episodes_range": "001-003",
            "source": {
                "type": "magnet",
                "magnet": magnet,
                "download_dir": "./downloads/Existing_Release",
            },
        }]
        result = discover_jobs({"automation": {"download_root": "./downloads"}}, jobs, build_default_state())

        self.assertEqual(result["jobs"][0]["episodes_range"], "001-004")
        self.assertEqual(result["summary"]["created_jobs"], 0)
        self.assertEqual(result["summary"]["updated_jobs"], 1)

    @patch("lib.autojobs.get_release_details")
    @patch("lib.autojobs.list_recent_releases")
    def test_discover_jobs_soft_matches_existing_job_when_magnet_changes(self, mock_list_recent_releases, mock_get_release_details):
        mock_list_recent_releases.return_value = {
            "releases": [
                {"id": 51, "alias": "soft-match-release", "is_ongoing": True},
            ],
            "request_urls": [],
        }
        mock_get_release_details.return_value = {
            "release": {
                "id": 51,
                "alias": "soft-match-release",
                "is_ongoing": True,
                "name": {"english": "Soft Match Release"},
                "external_ids": {"mal_id": 8080},
                "torrent": {"magnet": "magnet:?xt=urn:btih:newmagnet"},
                "episodes": [{"number": 4}],
            },
            "request_url": "https://aniliberty.top/api/v1/anime/releases/soft-match-release",
        }

        jobs = [{
            "title": "Soft Match Release",
            "mal_id": 8080,
            "season": 1,
            "episodes_range": "001-003",
            "source": {
                "type": "magnet",
                "magnet": "magnet:?xt=urn:btih:oldmagnet",
                "download_dir": "./downloads/soft_match_release",
            },
        }]
        result = discover_jobs({"automation": {"download_root": "./downloads"}}, jobs, build_default_state())

        self.assertEqual(len(result["jobs"]), 1)
        self.assertEqual(result["jobs"][0]["episodes_range"], "001-004")

    @patch("lib.autojobs.get_release_details")
    @patch("lib.autojobs.list_recent_releases")
    def test_discover_jobs_allows_release_without_mal_id(self, mock_list_recent_releases, mock_get_release_details):
        mock_list_recent_releases.return_value = {
            "releases": [
                {"id": 90, "alias": "missing-mal", "is_ongoing": True},
            ],
            "request_urls": [],
        }
        mock_get_release_details.return_value = {
            "release": {
                "id": 90,
                "alias": "missing-mal",
                "is_ongoing": True,
                "name": {"english": "Missing Mal"},
                "torrent": {"magnet": "magnet:?xt=urn:btih:missingmal"},
                "episodes": [{"number": 1}],
            },
            "request_url": "https://aniliberty.top/api/v1/anime/releases/missing-mal",
        }

        result = discover_jobs({}, [], build_default_state())

        self.assertEqual(len(result["jobs"]), 1)
        self.assertNotIn("mal_id", result["jobs"][0])
        self.assertEqual(result["jobs"][0]["episodes_range"], "001")
        self.assertEqual(result["summary"]["created_jobs"], 1)

    def test_build_job_from_release_adds_title_ru_when_available(self):
        from lib.autojobs import build_job_from_release

        job = build_job_from_release(
            {
                "name": {"english": "English Title", "main": "Русский тайтл"},
                "torrent": {"magnet": "magnet:?xt=urn:btih:test"},
            },
            [1, 2],
            {"download_root": "./downloads", "default_source_type": "magnet"},
        )

        self.assertEqual(job["title"], "English Title")
        self.assertEqual(job["title_ru"], "Русский тайтл")


if __name__ == "__main__":
    unittest.main()
