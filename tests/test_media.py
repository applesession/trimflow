import unittest
from unittest.mock import patch

from lib.media import get_preferred_audio_stream


class MediaAudioSelectionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
