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
)
from semantic_media import MediaCandidate
from video_engine import best_crop_box, scene_visual_plan


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
    def setUp(self):
        self.client = TestClient(fastapi_app)
        self.project_dir = Path(tempfile.mkdtemp(dir=DOWNLOAD_DIR))
        self.addCleanup(shutil.rmtree, self.project_dir, ignore_errors=True)
        self.addCleanup(pending_assembly.clear)
        self.addCleanup(lambda: app_module.set_status(
            "idle", message="Ready", progress=0, error=None,
            results=[], candidates=[], review=None, final_video=None,
        ))

    def _touch(self, name):
        path = self.project_dir / name
        path.write_bytes(b"0" * 10)
        return str(path)

    def _seed_pending(self, task_id, cap=2):
        path_a, path_b, path_c = (self._touch(f"{n}.mp4") for n in "abc")
        keyword_data = [{"keyword": "k", "sentence": "s", "_files": [path_a]}]
        pending_assembly[task_id] = {
            "keyword_data": keyword_data,
            "project_path": self.project_dir,
            "project_name": "proj",
            "media_type": "video",
            "settings": VideoSettings(),
            "api_keys": ApiKeys(),
            "vibe": "aesthetic",
            "yt_upload": False,
            "publish_confirmed": False,
            "scene_pools": {0: {path_a, path_b, path_c}},
            "count": cap,
        }
        return keyword_data, [path_a, path_b, path_c]

    def test_assemble_missing_task_returns_404(self):
        response = self.client.post("/api/assemble", json={"task_id": "missing"})
        self.assertEqual(response.status_code, 404)

    @patch("app.run_assemble_phase", new_callable=AsyncMock)
    def test_assemble_uses_default_when_no_selections(self, mock_phase):
        keyword_data, paths = self._seed_pending("t1")
        response = self.client.post("/api/assemble", json={"task_id": "t1", "use_default": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(keyword_data[0]["_files"], [paths[0]])
        self.assertNotIn("t1", pending_assembly)

    @patch("app.run_assemble_phase", new_callable=AsyncMock)
    def test_assemble_applies_valid_swap_selection(self, mock_phase):
        keyword_data, paths = self._seed_pending("t2")
        response = self.client.post(
            "/api/assemble", json={"task_id": "t2", "selections": [[paths[1], paths[2]]]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(keyword_data[0]["_files"], [paths[1], paths[2]])

    @patch("app.run_assemble_phase", new_callable=AsyncMock)
    def test_assemble_rejects_path_outside_scene_pool(self, mock_phase):
        keyword_data, paths = self._seed_pending("t3")
        response = self.client.post(
            "/api/assemble", json={"task_id": "t3", "selections": [["/etc/passwd"]]}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("t3", pending_assembly)
        self.assertEqual(keyword_data[0]["_files"], [paths[0]])

    @patch("app.run_assemble_phase", new_callable=AsyncMock)
    def test_assemble_caps_selection_to_assets_per_scene(self, mock_phase):
        keyword_data, paths = self._seed_pending("t4", cap=2)
        response = self.client.post("/api/assemble", json={"task_id": "t4", "selections": [paths]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(keyword_data[0]["_files"]), 2)

    @patch("app.run_assemble_phase", new_callable=AsyncMock)
    def test_assemble_rejects_scene_count_mismatch(self, mock_phase):
        keyword_data, paths = self._seed_pending("t5")
        response = self.client.post(
            "/api/assemble",
            json={"task_id": "t5", "selections": [[paths[0]], [paths[1]]]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("t5", pending_assembly)

    @patch("app.run_assemble_phase", new_callable=AsyncMock)
    def test_assemble_swap_refreshes_gallery_results(self, mock_phase):
        keyword_data, paths = self._seed_pending("t6")
        app_module.set_status(results=[{"keyword": "old", "files": ["/downloads/old.mp4"]}])
        response = self.client.post(
            "/api/assemble", json={"task_id": "t6", "selections": [[paths[1], paths[2]]]}
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        gallery = scraping_status["results"]
        self.assertEqual(payload["results"], gallery)
        self.assertEqual(len(gallery), 1)
        names = [Path(url).name for url in gallery[0]["files"]]
        self.assertEqual(names, [Path(paths[1]).name, Path(paths[2]).name])
        self.assertNotIn("old.mp4", names)


class SceneVisualPlanTests(unittest.TestCase):
    def test_short_speech_still_shows_every_selected_clip(self):
        n, slot, visual = scene_visual_plan(3.0, 3, 5)
        self.assertEqual(n, 3)
        self.assertEqual(slot, 5.0)
        self.assertEqual(visual, 15.0)

    def test_long_speech_extends_visuals_past_clip_budget(self):
        n, slot, visual = scene_visual_plan(20.0, 3, 5)
        self.assertEqual(n, 3)
        self.assertGreaterEqual(visual, 20.0)
        self.assertAlmostEqual(n * slot, visual)


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
