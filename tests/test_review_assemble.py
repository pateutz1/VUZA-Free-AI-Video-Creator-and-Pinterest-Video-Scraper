import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app as app_module
from app import (
    ApiKeys,
    DOWNLOAD_DIR,
    VideoSettings,
    app as fastapi_app,
    build_scene_pools,
    build_scene_review,
    group_scenes_to_clip_budget,
    pending_assembly,
    scraping_status,
    azure_voice_options,
)
from semantic_media import MediaCandidate
from video_engine import best_crop_box, scene_visual_plan, subtitle_top, resolve_font_path, parse_azure_voice_name, azure_v2_synthesis_name


def _candidate(provider, asset_id, path):
    return MediaCandidate(provider=provider, asset_id=asset_id, local_path=str(path))


class ReviewPayloadTests(unittest.TestCase):
    def setUp(self):
        self.project_dir = Path(tempfile.mkdtemp(dir=DOWNLOAD_DIR))
        self.addCleanup(shutil.rmtree, self.project_dir, ignore_errors=True)

    def _touch(self, name):
        path = self.project_dir / name
        path.write_bytes(b"0" * 10)
        return path

    def test_build_scene_review_lists_selected_and_alternates(self):
        selected = [_candidate("pexels", "a1", self._touch("a1.mp4"))]
        alternates = [
            _candidate("pixabay", "a2", self._touch("a2.mp4")),
            _candidate("coverr", "a3", self._touch("a3.mp4")),
        ]
        keyword_data = [{
            "keyword": "gym workout sweat",
            "sentence": "A rower pulls hard.",
            "_candidates": selected,
            "_alternates": alternates,
        }]

        review = build_scene_review(keyword_data, count=1)

        self.assertEqual(len(review["scenes"]), 1)
        scene = review["scenes"][0]
        self.assertEqual(scene["keyword"], "gym workout sweat")
        self.assertEqual(len(scene["selected"]), 1)
        self.assertEqual(len(scene["alternates"]), 2)
        self.assertEqual(scene["selected"][0]["provider"], "pexels")
        self.assertTrue(scene["selected"][0]["url"].startswith("/downloads/"))
        self.assertEqual(review["count"], 1)

    def test_build_scene_review_falls_back_to_files_without_candidates(self):
        path = self._touch("plain.mp4")
        keyword_data = [{"keyword": "k", "sentence": "s", "_files": [str(path)]}]

        review = build_scene_review(keyword_data, count=2)

        scene = review["scenes"][0]
        self.assertEqual(len(scene["selected"]), 1)
        self.assertEqual(scene["selected"][0]["path"], str(path))
        self.assertEqual(scene["alternates"], [])

    def test_build_scene_pools_includes_selected_and_alternates(self):
        selected = [_candidate("pexels", "a1", self._touch("a1.mp4"))]
        alternates = [_candidate("pixabay", "a2", self._touch("a2.mp4"))]
        keyword_data = [{"_candidates": selected, "_alternates": alternates}]

        pools = build_scene_pools(keyword_data)

        self.assertEqual(pools[0], {selected[0].local_path, alternates[0].local_path})


class AssembleEndpointTests(unittest.TestCase):
    def test_assemble_endpoint_removed(self):
        client = TestClient(fastapi_app)
        response = client.post('/api/assemble', json={'task_id': 'missing'})
        self.assertEqual(response.status_code, 404)


class SceneVisualPlanTests(unittest.TestCase):
    def test_short_speech_still_shows_every_selected_clip(self):
        n, slot, visual = scene_visual_plan(3.0, 3, 5)
        self.assertEqual(n, 3)
        self.assertEqual(slot, 5.0)
        self.assertEqual(visual, 15.0)

    def test_long_speech_cycles_selected_assets_to_cover_narration(self):
        n, slot, visual = scene_visual_plan(20.0, 3, 5)
        self.assertEqual(n, 4)
        self.assertEqual(slot, 5.0)
        self.assertEqual(visual, 20.0)

    def test_partial_last_slot_matches_exact_narration_duration(self):
        n, slot, visual = scene_visual_plan(15.54, 2, 3)
        self.assertEqual(n, 6)
        self.assertEqual(slot, 3.0)
        self.assertAlmostEqual(visual, 15.54)


class ShortLastSceneMergeTests(unittest.TestCase):
    def test_short_cta_merges_into_previous_scene(self):
        rows = [
            {"sentence": " ".join(["word"] * 40), "keyword": "gym"},
            {"sentence": "Smash that like button if you are ready to transform!", "keyword": "cta"},
        ]
        grouped = group_scenes_to_clip_budget(rows, count=3, clip_duration=5)
        self.assertEqual(len(grouped), 1)
        self.assertIn("Smash that like button", grouped[0]["sentence"])


class CropFrameTests(unittest.TestCase):
    def test_landscape_to_portrait_follows_subject_on_the_right(self):
        import numpy as np
        frame = np.full((108, 192, 3), 40, dtype="uint8")
        frame[:, 140:190] = (200, 160, 120)
        frame[:, 150:180:2] = (240, 220, 200)
        x, y, w, h = best_crop_box([frame], 192, 108, 9, 16)
        self.assertEqual(y, 0)
        self.assertGreater(x, 70)

    def test_matching_aspect_keeps_full_frame(self):
        x, y, w, h = best_crop_box([], 1080, 1920, 1080, 1920)
        self.assertEqual((x, y, w, h), (0, 0, 1080, 1920))


class CaptionLayoutTests(unittest.TestCase):
    def test_custom_position_is_percent_from_top(self):
        y = subtitle_top(1000, 100, "custom", 70)
        self.assertAlmostEqual(y, 650, delta=1)

    def test_bottom_sits_in_lower_third(self):
        y = subtitle_top(1920, 120, "bottom", 70)
        self.assertGreater(y, 1920 * 0.8)
        self.assertLess(y + 120, 1920)
        self.assertGreater(y + 120, 1920 * 0.88)

    def test_default_caption_font_resolves_from_project_static_fonts(self):
        path = resolve_font_path("BeVietnamPro-Bold.ttf")
        self.assertTrue(path)
        self.assertIn("static", path.replace("\\", "/").lower())
        self.assertTrue(path.endswith("BeVietnamPro-Bold.ttf"))


class AzureVoiceTests(unittest.TestCase):
    def test_parse_strips_gender_suffix(self):
        self.assertEqual(parse_azure_voice_name("en-US-ChristopherNeural-Male"), "en-US-ChristopherNeural")

    def test_v2_synthesis_name_strips_marker(self):
        self.assertEqual(
            azure_v2_synthesis_name("en-US-AndrewMultilingualNeural-V2-Male"),
            "en-US-AndrewMultilingualNeural",
        )
        self.assertEqual(azure_v2_synthesis_name("en-US-ChristopherNeural-Male"), "")

    def test_v1_list_matches_language_and_excludes_v2(self):
        rows = azure_voice_options("azure-tts-v1", "en-US")
        self.assertTrue(rows)
        self.assertTrue(all(row["name"].startswith("en-US") for row in rows))
        self.assertFalse(any("-V2" in row["name"] for row in rows))
        self.assertTrue(any("Christopher" in row["value"] for row in rows))

    def test_v2_includes_locale_neural_and_multilingual(self):
        rows = azure_voice_options("azure-tts-v2", "en-US")
        names = [row["name"] for row in rows]
        self.assertTrue(any("Christopher" in name for name in names))
        self.assertTrue(any("-V2" in name for name in names))
        self.assertTrue(any("Multilingual" in name for name in names))
        self.assertTrue(any(name.startswith("en-US") for name in names))
        spanish = azure_voice_options("azure-tts-v2", "es-ES")
        self.assertTrue(any(row["name"].startswith("es-ES") for row in spanish))
        self.assertTrue(any("Multilingual" in row["name"] for row in spanish))


class StatusReviewStateTests(unittest.TestCase):
    def tearDown(self):
        app_module.set_status(
            "idle", message="Ready", progress=0, error=None,
            results=[], candidates=[], review=None, final_video=None,
        )

    def test_awaiting_review_keeps_is_running_true(self):
        app_module.set_status("awaiting_review", message="Review", progress=100, review={"scenes": []})
        self.assertTrue(scraping_status["is_running"])

    def test_running_then_success_flips_is_running(self):
        app_module.set_status("running")
        self.assertTrue(scraping_status["is_running"])
        app_module.set_status("success")
        self.assertFalse(scraping_status["is_running"])


if __name__ == "__main__":
    unittest.main()
