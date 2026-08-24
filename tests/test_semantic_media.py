import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from semantic_media import (
    MediaCandidate,
    SearchCache,
    cache_key,
    coverage_failures,
    ground_query,
    parse_sentence_queries,
    search_with_cache,
    write_source_manifest,
)


class QueryParseTests(unittest.TestCase):
    def test_chronological_topic_anchored_parse_strips_vibe_suffix(self):
        text = "\n".join([
            "Did you know Mars may have hosted life? → mars canyon vista",
            "Astronauts walk the red dunes. → astronaut red sand aesthetic",
            "A rover climbs a dusty ridge. → #rover dust ridge",
        ])
        rows = parse_sentence_queries(text)
        self.assertEqual([row["sentence"] for row in rows], [
            "Did you know Mars may have hosted life?",
            "Astronauts walk the red dunes.",
            "A rover climbs a dusty ridge.",
        ])
        self.assertEqual(rows[0]["keyword"], "mars canyon vista")
        self.assertEqual(rows[1]["keyword"], "astronaut red sand")
        self.assertEqual(rows[2]["keyword"], "rover dust ridge")
        self.assertTrue(all(2 <= len(row["keyword"].split()) <= 5 for row in rows))

    def test_ground_query_keeps_concrete_keyword(self):
        self.assertEqual(
            ground_query(
                "gym barbell lift",
                "Turn sweat into confidence and watch the world start to notice.",
                "Ready to crush the thing standing between you and your goals",
            ),
            "gym barbell lift",
        )
        self.assertIn(
            "dumbbells",
            ground_query("", "Drop the excuses those dumbbells don't care", "gym workout"),
        )


class SearchCacheTests(unittest.TestCase):
    def test_hit_expiry_empty_not_cached_concurrent_and_aspect(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = {"t": 100.0}
            cache = SearchCache(directory=tmp, now=lambda: clock["t"])
            candidate = MediaCandidate(
                provider="pexels",
                asset_id="11",
                url="https://videos.pexels.com/vid.mp4",
                duration=8,
                width=1080,
                height=1920,
                query="mars rover",
            )
            calls = []

            def search():
                calls.append(1)
                return [candidate]

            first, origin = search_with_cache(cache, "pexels", "mars rover", 2, "9:16", search)
            self.assertEqual(origin, "live")
            second, origin = search_with_cache(cache, "pexels", "mars rover", 2, "9:16", search)
            self.assertEqual(origin, "hit")
            self.assertEqual(len(calls), 1)
            self.assertEqual(first[0].asset_id, second[0].asset_id)

            clock["t"] = 100.0 + 25 * 3600
            third, origin = search_with_cache(cache, "pexels", "mars rover", 2, "9:16", search)
            self.assertEqual(origin, "live")
            self.assertEqual(len(calls), 2)

            empty_calls = []

            def empty():
                empty_calls.append(1)
                return []

            search_with_cache(cache, "pexels", "empty dunes", 2, "9:16", empty)
            search_with_cache(cache, "pexels", "empty dunes", 2, "9:16", empty)
            self.assertEqual(len(empty_calls), 2)

            landscape = MediaCandidate(
                provider="pexels",
                asset_id="22",
                url="https://videos.pexels.com/wide.mp4",
                duration=8,
                width=1920,
                height=1080,
                query="wide canyon",
            )
            cache.save("pexels", "wide canyon", 2, "9:16", [landscape])
            live_calls = []

            def live():
                live_calls.append(1)
                return [candidate]

            refreshed, origin = search_with_cache(cache, "pexels", "wide canyon", 2, "9:16", live)
            self.assertEqual(origin, "live")
            self.assertEqual(len(live_calls), 1)
            self.assertEqual(refreshed[0].asset_id, "11")

            started = threading.Event()
            release = threading.Event()
            concurrent_calls = []

            def slow():
                concurrent_calls.append(1)
                started.set()
                release.wait(timeout=2)
                return [candidate]

            key_cache = SearchCache(directory=tmp, now=lambda: 1.0)

            def worker():
                search_with_cache(key_cache, "coverr", "lock test", 2, "9:16", slow)

            first = threading.Thread(target=worker)
            first.start()
            self.assertTrue(started.wait(timeout=2))
            second = threading.Thread(target=worker)
            second.start()
            time.sleep(0.1)
            release.set()
            first.join(timeout=2)
            second.join(timeout=2)
            self.assertEqual(len(concurrent_calls), 1)

    def test_cache_key_ignores_secrets(self):
        self.assertEqual(
            cache_key("pexels", "Mars Rover", 2, "9:16"),
            cache_key("pexels", "mars rover", 2, "9:16"),
        )


class ManifestTests(unittest.TestCase):
    def test_manifest_redacts_signed_urls_and_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_source_manifest(tmp, {
                "provider": "coverr",
                "url": "https://cdn.coverr.co/x.mp4?jwt=secret-token",
                "api_key": "sk-live-should-not-appear",
                "scenes": [{"source_page": "https://coverr.co/videos/abc", "download_url": "https://cdn.coverr.co/x.mp4?jwt=secret-token"}],
            })
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            dumped = json.dumps(data)
            self.assertNotIn("jwt=secret-token", dumped)
            self.assertNotIn("download_url", dumped)
            self.assertEqual(data["scenes"][0]["source_page"], "https://coverr.co/videos/abc")


class CoverageSlackTests(unittest.TestCase):
    def test_three_clips_cover_capped_scene_budget(self):
        selected = [[
            MediaCandidate(provider="pinterest", asset_id="a", duration=8),
            MediaCandidate(provider="pinterest", asset_id="b", duration=8),
            MediaCandidate(provider="pinterest", asset_id="c", duration=8),
        ]]
        scenes = [{"keyword": "gym barbell lift", "required_duration": 15.0}]
        failures, unique_total = coverage_failures(selected, scenes, 5, 15.2, "pinterest")
        self.assertEqual(failures, [])
        self.assertEqual(unique_total, 15.0)


if __name__ == "__main__":
    unittest.main()
