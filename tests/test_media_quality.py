import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_quality import (
    content_fingerprint,
    delete_rejected_file,
    download_http,
    is_signed_url,
    overlay_text_score,
    redact_secret,
    url_safe_to_cache,
    validate_downloaded_video,
)


class MediaQualityTests(unittest.TestCase):
    def test_redacts_keys_and_signed_query_params(self):
        message = "https://user:hunter2@api.example.com/v1?api_key=sk-secret-123456&q=mars"
        redacted = redact_secret(message, "sk-secret-123456")
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("sk-secret-123456", redacted)

    def test_signed_coverr_url_is_not_cached(self):
        url = "https://cdn.coverr.co/videos/x.mp4?jwt=abc.def.ghi"
        self.assertTrue(is_signed_url(url))
        self.assertIsNone(url_safe_to_cache(url))

    def test_download_requires_http_200_and_non_empty_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"

            class FakeResponse:
                status_code = 403
                content = b"nope"

            with patch("media_quality.requests.get", return_value=FakeResponse()):
                self.assertFalse(download_http("https://example.com/a.mp4", path, min_bytes=10))

            class OkResponse:
                status_code = 200
                content = b"x" * 50000

            with patch("media_quality.requests.get", return_value=OkResponse()):
                self.assertTrue(download_http("https://example.com/a.mp4", path, min_bytes=40000))

    def test_validate_video_rejects_zero_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"0" * 50000)
            ok, info = validate_downloaded_video(
                path,
                probe=lambda _p: {"duration": 3.23, "fps": 24, "width": 1080, "height": 1920},
            )
            self.assertTrue(ok)
            self.assertEqual(info["duration"], 3.23)
            bad, reason = validate_downloaded_video(path, probe=lambda _p: {"duration": 0, "fps": 24})
            self.assertFalse(bad)
            self.assertIn("duration", reason)

    def test_fingerprint_and_rejected_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"0" * 50000)
            self.assertEqual(len(content_fingerprint(path)), 32)
            self.assertTrue(delete_rejected_file(path, newly_downloaded=True))
            self.assertFalse(path.exists())

    def test_overlay_score_flags_dense_caption_edges(self):
        import numpy as np

        clean = np.full((90, 90), 40.0)
        self.assertLess(overlay_text_score(clean), 0.1)
        caption = np.full((90, 90), 40.0)
        for y in range(32, 62):
            for x in range(15, 75):
                caption[y, x] = 220.0 if (x + y) % 2 == 0 else 8.0
        self.assertGreaterEqual(overlay_text_score(caption), 0.38)
        boxed = np.full((90, 90), 40.0)
        boxed[18:48, 4:34] = 220.0
        self.assertGreaterEqual(overlay_text_score(boxed), 0.28)
