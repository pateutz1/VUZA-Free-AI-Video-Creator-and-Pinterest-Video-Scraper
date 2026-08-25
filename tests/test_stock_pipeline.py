import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from aesthetic_scraper import (
    CoverrScraper,
    LLMProcessor,
    MixkitScraper,
    PexelsScraper,
    PixabayScraper,
    is_text_heavy_pin,
    parse_pinterest_pin_hrefs,
    parse_pinterest_resource_results,
    pick_pexels_rendition,
)
from app import (
    ApiKeys,
    apply_user_keywords,
    collect_stock_videos,
    group_scenes_to_clip_budget,
    keep_visually_clean_media,
    split_script_sentences,
    split_script_to_n,
    terms_amount_for_script,
    terms_to_scene_rows,
)
from semantic_media import (
    CoverageError,
    MediaCandidate,
    dedupe_candidates,
    pinterest_query_variants,
    round_robin_order,
    interleave_candidates_by_provider,
    stock_query_plan,
    unique_usable_duration,
)


def _video(asset_id, duration, width, height, link="https://example.com/a.mp4"):
    return {
        "id": asset_id,
        "duration": duration,
        "url": f"https://www.pexels.com/video/{asset_id}",
        "user": {"name": "Ada"},
        "video_files": [
            {"id": 1, "link": link, "width": width, "height": height},
        ],
    }


class ProviderFilterTests(unittest.TestCase):
    def test_pexels_duration_aspect_and_rendition(self):
        scraper = PexelsScraper(output_dir=tempfile.mkdtemp(), api_key="px-test")
        payload = {
            "videos": [
                _video(1, 1.5, 1080, 1920),
                _video(2, 8, 1920, 1080, "https://example.com/land.mp4"),
                {
                    "id": 3,
                    "duration": 6,
                    "url": "https://www.pexels.com/video/3",
                    "user": {"name": "Ada"},
                    "video_files": [
                        {"id": 10, "link": "https://example.com/hd.mp4", "width": 720, "height": 1280},
                        {"id": 11, "link": "https://example.com/target.mp4", "width": 1080, "height": 1920},
                    ],
                },
            ]
        }

        class FakeResponse:
            def json(self):
                return payload

        with patch("aesthetic_scraper.requests.get", return_value=FakeResponse()):
            items = scraper.find_videos("astronaut red sand", aspect="9:16", min_duration=2, limit=20)
        ids = [item["asset_id"] for item in items]
        self.assertNotIn("1", ids)
        self.assertNotIn("2", ids)
        self.assertEqual(ids, ["3"])
        self.assertEqual(items[0]["url"], "https://example.com/target.mp4")
        self.assertEqual(items[0]["rendition"]["width"], 1080)

    def test_pixabay_requires_orientation_and_video_type_all(self):
        scraper = PixabayScraper(output_dir=tempfile.mkdtemp(), api_key="pb-test")
        captured = {}

        class FakeResponse:
            def json(self):
                return {
                    "hits": [
                        {
                            "id": 9,
                            "duration": 1,
                            "pageURL": "https://pixabay.com/9",
                            "user": "Ada",
                            "videos": {"large": {"url": "https://example.com/short.mp4", "width": 1080, "height": 1920}},
                        },
                        {
                            "id": 10,
                            "duration": 8,
                            "pageURL": "https://pixabay.com/10",
                            "user": "Ada",
                            "videos": {
                                "large": {"url": "https://example.com/ok.mp4", "width": 1080, "height": 1920},
                                "small": {"url": "https://example.com/tiny.mp4", "width": 240, "height": 426},
                            },
                        },
                        {
                            "id": 11,
                            "duration": 8,
                            "pageURL": "https://pixabay.com/11",
                            "user": "Ada",
                            "videos": {"large": {"url": "https://example.com/wide.mp4", "width": 1920, "height": 1080}},
                        },
                    ]
                }

        def fake_get(url, **kwargs):
            captured["url"] = url
            return FakeResponse()

        with patch("aesthetic_scraper.requests.get", side_effect=fake_get):
            items = scraper.find_videos("red planet rover", aspect="9:16", min_duration=2)
        self.assertIn("video_type=all", captured["url"])
        self.assertIn("per_page=50", captured["url"])
        self.assertIn("orientation=vertical", captured["url"])
        self.assertEqual([item["asset_id"] for item in items], ["10"])
        self.assertEqual(items[0]["rendition"]["id"], "large")

    def test_mixkit_retries_shorter_slug_and_keeps_original_query(self):
        scraper = MixkitScraper(output_dir=tempfile.mkdtemp())
        calls = []
        empty = '<script type="application/ld+json">{"@graph":[]}</script>'
        hit = '<script type="application/ld+json">' + json.dumps({
            "@graph": [{
                "@type": "VideoObject",
                "contentUrl": "https://assets.mixkit.co/videos/123/123-720.mp4",
                "duration": "PT8S",
                "name": "Gym",
            }]
        }) + "</script>"

        def fake_fetch(url):
            calls.append(url)
            if "gym-transformation" in url:
                return empty
            return hit

        with patch.object(scraper, "_fetch", side_effect=fake_fetch):
            items = scraper.find_videos("gym transformation", min_duration=2)
        self.assertTrue(any("gym-transformation" in url for url in calls))
        self.assertTrue(any(url.rstrip("/").endswith("/gym") for url in calls))
        self.assertEqual(items[0]["query"], "gym transformation")
        self.assertEqual(items[0]["asset_id"], "123")

    def test_mixkit_skips_landscape_when_portrait_requested(self):
        scraper = MixkitScraper(output_dir=tempfile.mkdtemp())
        hit = '<script type="application/ld+json">' + json.dumps({
            "@graph": [
                {
                    "@type": "VideoObject",
                    "contentUrl": "https://assets.mixkit.co/videos/111/111-720.mp4",
                    "duration": "PT8S",
                    "name": "Wide lift",
                    "width": 1920,
                    "height": 1080,
                },
                {
                    "@type": "VideoObject",
                    "contentUrl": "https://assets.mixkit.co/videos/222/222-720.mp4",
                    "duration": "PT8S",
                    "name": "Portrait lift",
                    "width": 1080,
                    "height": 1920,
                },
            ]
        }) + "</script>"

        with patch.object(scraper, "_fetch", return_value=hit):
            items = scraper.find_videos("gym lifting weights", aspect="9:16", min_duration=2)
        self.assertEqual([item["asset_id"] for item in items], ["222"])

    def test_coverr_uses_popular_sort_and_keeps_matching_aspect(self):
        scraper = CoverrScraper(output_dir=tempfile.mkdtemp(), api_key="cv-test")
        captured = {}

        class FakeResponse:
            status_code = 200
            text = ""
            def json(self):
                return {
                    "hits": [
                        {"id": "", "duration": 8, "urls": {"mp4_download": "https://cdn.coverr.co/a.mp4"}, "max_width": 1080, "max_height": 1920, "is_vertical": True},
                        {"id": "ok", "duration": 3.23, "urls": {"mp4_download": "https://cdn.coverr.co/b.mp4?jwt=aaa"}, "canonical_url": "https://coverr.co/videos/ok", "max_width": 1080, "max_height": 1920, "is_vertical": True},
                        {"id": "wide", "duration": 9, "urls": {"mp4_download": "https://cdn.coverr.co/c.mp4"}, "max_width": 1920, "max_height": 1080, "is_vertical": False},
                    ]
                }

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            return FakeResponse()

        with patch("aesthetic_scraper.requests.get", side_effect=fake_get):
            items = scraper.find_videos("astronaut red sand", aspect="9:16", min_duration=2)
        self.assertIn("sort=popular", captured["url"])
        self.assertIn("urls=true", captured["url"])
        self.assertNotIn("is_vertical", captured["url"])
        self.assertEqual([item["asset_id"] for item in items], ["ok"])
        self.assertTrue(items[0]["url"])

    def test_coverr_retries_shorter_query_when_empty(self):
        scraper = CoverrScraper(output_dir=tempfile.mkdtemp(), api_key="cv-test")
        calls = []

        class FakeResponse:
            def __init__(self, hits):
                self.status_code = 200
                self.text = ""
                self._hits = hits
            def json(self):
                return {"hits": self._hits}

        def fake_get(url, **kwargs):
            calls.append(url)
            if "gym+transformation" in url or "gym%20transformation" in url:
                return FakeResponse([])
            return FakeResponse([{
                "id": "gym1",
                "duration": 8,
                "urls": {"mp4_download": "https://cdn.coverr.co/gym.mp4"},
                "is_vertical": True,
            }])

        with patch("aesthetic_scraper.requests.get", side_effect=fake_get):
            items = scraper.find_videos("gym transformation", aspect="9:16", min_duration=5)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual([item["asset_id"] for item in items], ["gym1"])

    def test_pexels_rendition_prefers_target(self):
        best = pick_pexels_rendition(
            [
                {"link": "a", "width": 720, "height": 1280},
                {"link": "b", "width": 1080, "height": 1920},
            ],
            "9:16",
        )
        self.assertEqual(best["link"], "b")


class SelectionTests(unittest.TestCase):
    def test_dedupe_by_asset_url_and_fingerprint(self):
        items = [
            MediaCandidate(provider="coverr", asset_id="1", url="https://cdn.example/a.mp4", fingerprint="aaa"),
            MediaCandidate(provider="coverr", asset_id="1", url="https://cdn.example/b.mp4"),
            MediaCandidate(provider="coverr", asset_id="2", url="https://cdn.example/a.mp4?jwt=x"),
            MediaCandidate(provider="coverr", asset_id="3", url="https://cdn.example/c.mp4", fingerprint="aaa"),
            MediaCandidate(provider="coverr", asset_id="4", url="https://cdn.example/d.mp4", fingerprint="bbb"),
        ]
        unique = dedupe_candidates(items)
        self.assertEqual([item.asset_id for item in unique], ["1", "4"])

    def test_round_robin_does_not_let_first_query_monopolize(self):
        groups = [
            [MediaCandidate(asset_id="a1"), MediaCandidate(asset_id="a2"), MediaCandidate(asset_id="a3")],
            [MediaCandidate(asset_id="b1"), MediaCandidate(asset_id="b2")],
        ]
        order = [(scene, cand.asset_id) for scene, cand in round_robin_order(groups)]
        self.assertEqual(order[:2], [(0, "a1"), (1, "b1")])
        self.assertEqual(order[2], (0, "a2"))

    def test_interleave_by_provider_rotates_before_second_clip(self):
        items = [
            MediaCandidate(provider="pexels", asset_id="p1"),
            MediaCandidate(provider="pexels", asset_id="p2"),
            MediaCandidate(provider="mixkit", asset_id="m1"),
            MediaCandidate(provider="pixabay", asset_id="x1"),
        ]
        mixed = interleave_candidates_by_provider(items, ["mixkit", "pexels", "pixabay", "coverr"])
        self.assertEqual([item.asset_id for item in mixed], ["m1", "p1", "x1", "p2"])

    def test_pinterest_query_variants_never_shrink_to_one_generic_word(self):
        variants = pinterest_query_variants("gym", sentence="Sweat drips as she grinds through squats.", topic="gym workout motivation")
        self.assertTrue(variants)
        self.assertTrue(all(len(v.split()) >= 2 for v in variants))
        self.assertIn("gym sweat", variants)

    def test_pinterest_query_variants_add_sentence_detail_first(self):
        variants = pinterest_query_variants("gym workout sweat", sentence="A rower pulls hard on the machine.", topic="gym workout")
        self.assertEqual(variants[0], "gym workout sweat")
        self.assertIn("gym workout sweat rower", variants)

    def test_pinterest_query_variants_truncate_chain_stays_multi_word(self):
        variants = pinterest_query_variants("intense gym floor workout session", topic="gym")
        self.assertNotIn("intense", variants)
        self.assertTrue(all(len(v.split()) >= 2 for v in variants))

    def test_keep_visually_clean_media_skips_tiny_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.mp4"
            tiny = Path(tmp) / "tiny.mp4"
            good.write_bytes(b"0" * 50000)
            tiny.write_bytes(b"0" * 10)
            picked = keep_visually_clean_media([tiny, good], set(), sentence="mars rover", keyword="mars rover", limit=2)
            self.assertEqual(picked, [str(good)])


class StockPipelineTests(unittest.TestCase):
    def test_failed_download_uses_next_candidate_same_scene(self):
        rows = [{"sentence": "Astronauts walk the dune.", "keyword": "astronaut red sand"}]
        attempts = []

        async def search_fn(query, scene_index):
            return [
                {"provider": "coverr", "asset_id": "bad", "url": "https://cdn.example/bad.mp4", "duration": 5, "width": 1080, "height": 1920},
                {"provider": "coverr", "asset_id": "good", "url": "https://cdn.example/good.mp4", "duration": 5, "width": 1080, "height": 1920},
            ]

        async def download_fn(candidate, project_path, source, probe=None):
            attempts.append(candidate.asset_id)
            if candidate.asset_id == "bad":
                return None, "download failed"
            candidate.local_path = str(Path(project_path) / "good.mp4")
            Path(candidate.local_path).write_bytes(b"0" * 50000)
            return candidate.local_path, "ok"

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(collect_stock_videos(
                rows,
                source="coverr",
                project_path=tmp,
                clip_duration=2,
                count=1,
                narration_duration=2,
                search_fn=search_fn,
                download_fn=download_fn,
            ))
        self.assertEqual(attempts, ["bad", "good"])
        self.assertEqual(rows[0]["_files"][0].endswith("good.mp4"), True)

    def test_round_robin_download_order_across_scenes(self):
        rows = [
            {"sentence": "A rover climbs.", "keyword": "mars rover dust"},
            {"sentence": "An astronaut plants a flag.", "keyword": "astronaut flag moon"},
        ]
        order = []

        async def search_fn(query, scene_index):
            prefix = "r" if "rover" in query else "a"
            return [
                {"provider": "pexels", "asset_id": f"{prefix}1", "url": f"https://example.com/{prefix}1.mp4", "duration": 5, "width": 1080, "height": 1920},
                {"provider": "pexels", "asset_id": f"{prefix}2", "url": f"https://example.com/{prefix}2.mp4", "duration": 5, "width": 1080, "height": 1920},
            ]

        async def download_fn(candidate, project_path, source, probe=None):
            order.append(candidate.asset_id)
            candidate.local_path = str(Path(project_path) / f"{candidate.asset_id}.mp4")
            Path(candidate.local_path).write_bytes(b"0" * 50000)
            return candidate.local_path, "ok"

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(collect_stock_videos(
                rows,
                source="pexels",
                project_path=tmp,
                clip_duration=2,
                count=2,
                narration_duration=4,
                search_fn=search_fn,
                download_fn=download_fn,
            ))
        self.assertEqual(order[:2], ["r1", "a1"])

    def test_astronaut_single_clip_fails_with_coverage_report(self):
        queries = [
            "astronaut helmet visor",
            "mars canyon vista",
            "rover dust trail",
            "astronaut red sand",
            "habitat dome night",
            "launch pad steam",
            "orbital station window",
            "red planet sunrise",
        ]
        rows = [{"sentence": f"Narration beat {idx + 1} about {query}.", "keyword": query} for idx, query in enumerate(queries)]
        alt_seen = []
        search_calls = []

        async def search_fn(query, scene_index):
            search_calls.append(query)
            if query == "astronaut red sand":
                return [{
                    "provider": "coverr",
                    "asset_id": "sand-1",
                    "url": "https://cdn.coverr.co/sand.mp4?jwt=secret",
                    "source_page": "https://coverr.co/videos/sand-1",
                    "duration": 3.23,
                    "width": 1080,
                    "height": 1920,
                    "rendition": {"id": "mp4_download", "width": 1080, "height": 1920},
                }]
            return []

        async def download_fn(candidate, project_path, source, probe=None):
            candidate.local_path = str(Path(project_path) / "sand-1.mp4")
            Path(candidate.local_path).write_bytes(b"0" * 50000)
            return candidate.local_path, "ok"

        llm = Mock()
        llm.suggest_visual_query.side_effect = lambda sentence, topic="", failed_queries=None: (
            alt_seen.append(failed_queries or []) or "astronaut walking dunes"
        )

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "video"
            project.mkdir()
            with self.assertRaises(CoverageError) as raised:
                asyncio.run(collect_stock_videos(
                    rows,
                    source="coverr",
                    project_path=str(project),
                    clip_duration=2,
                    count=1,
                    llm=llm,
                    topic="astronauts on mars",
                    narration_duration=31.66,
                    search_fn=search_fn,
                    download_fn=download_fn,
                ))
            message = str(raised.exception)
            self.assertIn("scene", message.lower())
            self.assertIn("coverr", message.lower())
            self.assertIn("required_duration", message)
            self.assertIn("available_duration", message)
            self.assertNotIn("jwt=secret", message)
            manifest = Path(tmp) / "source_manifest.json"
            self.assertTrue(manifest.exists())
            body = manifest.read_text(encoding="utf-8")
            self.assertNotIn("jwt=secret", body)
            self.assertIn("astronaut red sand", body)
        self.assertGreaterEqual(llm.suggest_visual_query.call_count, 1)
        self.assertIn("astronaut walking dunes", search_calls)
        self.assertEqual(unique_usable_duration([[MediaCandidate(provider="coverr", asset_id="sand-1", duration=3.23)]], 2), 2.0)

    def test_selected_provider_only_and_pinterest_not_replaced(self):
        created = []

        def fake_make(src, *args, **kwargs):
            created.append(src)
            scraper = Mock()
            scraper.find_videos = Mock(return_value=[])
            return scraper

        rows = [{"sentence": "A pin of neon rain.", "keyword": "neon rain alley"}]
        with tempfile.TemporaryDirectory() as tmp:
            with patch("app.make_scraper", side_effect=fake_make):
                with self.assertRaises(CoverageError):
                    asyncio.run(collect_stock_videos(
                        rows,
                        source="pinterest",
                        project_path=tmp,
                        clip_duration=2,
                        narration_duration=8,
                    ))
        self.assertEqual(created, ["pinterest"])
        self.assertNotIn("pexels", created)
        self.assertNotIn("pixabay", created)
        self.assertNotIn("coverr", created)

    def test_round_robin_blends_all_ready_providers(self):
        rows = [{"sentence": "A rover climbs red dunes.", "keyword": "mars rover dust"}]
        created = []

        def fake_make(provider, *args, **kwargs):
            created.append(provider)
            scraper = Mock()
            scraper.find_videos = Mock(return_value=[
                {
                    "provider": provider,
                    "asset_id": f"{provider}-1",
                    "url": f"https://example.com/{provider}.mp4",
                    "duration": 5,
                    "width": 1080,
                    "height": 1920,
                },
            ])
            return scraper

        downloaded_providers = []

        async def download_fn(candidate, project_path, source, probe=None):
            downloaded_providers.append(source)
            candidate.local_path = str(Path(project_path) / f"{candidate.asset_id}.mp4")
            Path(candidate.local_path).write_bytes(b"0" * 50000)
            return candidate.local_path, "ok"

        api_keys = ApiKeys(pexels_key="px", pixabay_key="pb", coverr_key="cv")

        with tempfile.TemporaryDirectory() as tmp:
            with patch("app.make_scraper", side_effect=fake_make):
                asyncio.run(collect_stock_videos(
                    rows,
                    source="round_robin",
                    project_path=tmp,
                    api_keys=api_keys,
                    clip_duration=2,
                    count=4,
                    narration_duration=8,
                    download_fn=download_fn,
                ))
        self.assertEqual(set(created), {"mixkit", "pexels", "pixabay", "coverr"})
        self.assertEqual(set(downloaded_providers), {"mixkit", "pexels", "pixabay", "coverr"})

    def test_spares_download_two_extra_without_affecting_selection(self):
        rows = [{"sentence": "A rower pulls hard on the machine.", "keyword": "gym workout sweat"}]

        async def search_fn(query, scene_index):
            return [
                {
                    "provider": "pexels",
                    "asset_id": f"c{i}",
                    "url": f"https://example.com/c{i}.mp4",
                    "duration": 5,
                    "width": 1080,
                    "height": 1920,
                }
                for i in range(6)
            ]

        downloaded = []

        async def download_fn(candidate, project_path, source, probe=None):
            downloaded.append(candidate.asset_id)
            candidate.local_path = str(Path(project_path) / f"{candidate.asset_id}.mp4")
            Path(candidate.local_path).write_bytes(b"0" * 50000)
            return candidate.local_path, "ok"

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(collect_stock_videos(
                rows,
                source="pexels",
                project_path=tmp,
                clip_duration=2,
                count=2,
                narration_duration=4,
                search_fn=search_fn,
                download_fn=download_fn,
            ))
            manifest = json.loads((Path(tmp) / "source_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(rows[0]["_files"]), 2)
        self.assertEqual(len(rows[0]["_alternates"]), 2)
        self.assertEqual(len(downloaded), 4)
        self.assertEqual(len(manifest["scenes"][0]["selected"]), 2)
        self.assertEqual(len(manifest["scenes"][0]["spares"]), 2)

    def test_spares_are_skipped_when_coverage_fails(self):
        rows = [{
            "sentence": "A rower pulls hard on the rowing machine through every single grueling rep of this workout.",
            "keyword": "gym workout sweat",
        }]

        async def search_fn(query, scene_index):
            return [
                {
                    "provider": "pexels",
                    "asset_id": "only",
                    "url": "https://example.com/only.mp4",
                    "duration": 5,
                    "width": 1080,
                    "height": 1920,
                },
            ]

        async def download_fn(candidate, project_path, source, probe=None):
            candidate.local_path = str(Path(project_path) / f"{candidate.asset_id}.mp4")
            Path(candidate.local_path).write_bytes(b"0" * 50000)
            return candidate.local_path, "ok"

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CoverageError):
                asyncio.run(collect_stock_videos(
                    rows,
                    source="pexels",
                    project_path=tmp,
                    clip_duration=2,
                    count=2,
                    narration_duration=4,
                    search_fn=search_fn,
                    download_fn=download_fn,
                ))
        self.assertNotIn("_alternates", rows[0])

    def test_pinterest_infographic_titles_are_hard_filtered(self):
        rows = [{"sentence": "A rower pulls hard on the machine.", "keyword": "gym workout sweat"}]

        async def search_fn(query, scene_index):
            return [
                {
                    "provider": "pinterest",
                    "asset_id": "infographic",
                    "url": "https://example.com/infographic.mp4",
                    "title": "3D anatomical muscle diagram how to",
                    "duration": 5,
                    "width": 1080,
                    "height": 1920,
                },
                {
                    "provider": "pinterest",
                    "asset_id": "real",
                    "url": "https://example.com/real.mp4",
                    "title": "gym workout sweat",
                    "duration": 5,
                    "width": 1080,
                    "height": 1920,
                },
            ]

        async def download_fn(candidate, project_path, source, probe=None):
            candidate.local_path = str(Path(project_path) / f"{candidate.asset_id}.mp4")
            Path(candidate.local_path).write_bytes(b"0" * 50000)
            return candidate.local_path, "ok"

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(collect_stock_videos(
                rows,
                source="pinterest",
                project_path=tmp,
                clip_duration=2,
                count=1,
                narration_duration=2,
                search_fn=search_fn,
                download_fn=download_fn,
            ))
        self.assertTrue(rows[0]["_files"][0].endswith("real.mp4"))

    def test_round_robin_skips_providers_without_api_keys(self):
        rows = [{"sentence": "A rover climbs red dunes.", "keyword": "mars rover dust"}]
        created = []

        def fake_make(provider, *args, **kwargs):
            created.append(provider)
            scraper = Mock()
            scraper.find_videos = Mock(return_value=[
                {
                    "provider": provider,
                    "asset_id": f"{provider}-1",
                    "url": f"https://example.com/{provider}.mp4",
                    "duration": 5,
                    "width": 1080,
                    "height": 1920,
                },
            ])
            return scraper

        async def download_fn(candidate, project_path, source, probe=None):
            candidate.local_path = str(Path(project_path) / f"{candidate.asset_id}.mp4")
            Path(candidate.local_path).write_bytes(b"0" * 50000)
            return candidate.local_path, "ok"

        with tempfile.TemporaryDirectory() as tmp:
            with patch("app.make_scraper", side_effect=fake_make):
                asyncio.run(collect_stock_videos(
                    rows,
                    source="round_robin",
                    project_path=tmp,
                    api_keys=ApiKeys(),
                    clip_duration=2,
                    count=1,
                    narration_duration=2,
                    download_fn=download_fn,
                ))
        self.assertEqual(created, ["mixkit"])


class PinterestParseTests(unittest.TestCase):
    def test_resource_results_extract_mp4_and_pin_page(self):
        data = {
            "resource_response": {
                "data": {
                    "results": [
                        {
                            "id": "111",
                            "videos": {
                                "video_list": {
                                    "V_720P": {
                                        "url": "https://v1.pinimg.com/videos/mc/720p/clip.mp4",
                                        "width": 720,
                                        "height": 1280,
                                        "duration": 8.5,
                                    }
                                }
                            },
                        },
                        {"id": "222", "images": {"orig": {"url": "https://i.pinimg.com/photo.jpg"}}},
                    ]
                }
            }
        }
        videos = parse_pinterest_resource_results(data, want_video=True)
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["asset_id"], "111")
        self.assertTrue(videos[0]["url"].endswith(".mp4"))
        self.assertEqual(videos[0]["source_page"], "https://www.pinterest.com/pin/111/")
        all_items = parse_pinterest_resource_results(data, want_video=False)
        self.assertEqual([item["asset_id"] for item in all_items], ["111", "222"])

    def test_story_pin_and_hls_prefer_mp4(self):
        from aesthetic_scraper import pinterest_video_rendition

        story = {
            "id": "333",
            "story_pin_data": {
                "pages": [{
                    "blocks": [{
                        "video": {
                            "video_list": {
                                "V_HLS": {
                                    "url": "https://v1.pinimg.com/videos/mc/hls/clip.m3u8",
                                    "width": 1080,
                                    "height": 1920,
                                },
                                "V_720P": {
                                    "url": "https://v1.pinimg.com/videos/mc/720p/clip.mp4",
                                    "width": 720,
                                    "height": 1280,
                                },
                            }
                        }
                    }]
                }]
            },
        }
        rendition = pinterest_video_rendition(story)
        self.assertTrue(rendition["url"].endswith(".mp4"))
        videos = parse_pinterest_resource_results(
            {"resource_response": {"data": story}},
            want_video=True,
        )
        self.assertEqual(len(videos), 1)
        self.assertTrue(videos[0]["url"].endswith(".mp4"))
        photo_only = parse_pinterest_resource_results(
            {"resource_response": {"data": {"id": "444", "is_video": True}}},
            want_video=True,
        )
        self.assertEqual(photo_only, [])

    def test_skips_quote_and_promo_titles(self):
        self.assertTrue(is_text_heavy_pin({"title": 'Follow for more "never quit"'}))
        self.assertTrue(is_text_heavy_pin({"title": "Shop now gym program"}))
        self.assertFalse(is_text_heavy_pin({"title": "barbell squat gym"}))
        self.assertTrue(is_text_heavy_pin({"title": "3D anatomical deadlift muscles"}))
        self.assertTrue(is_text_heavy_pin({"title": "Deadlift tutorial correct form"}))
        self.assertTrue(is_text_heavy_pin({"title": "30 MINUTE BARBELL BURNER WORKOUT"}))
        self.assertTrue(is_text_heavy_pin({"title": "full body kettlebell workout"}))
        self.assertTrue(is_text_heavy_pin({"title": "Reverse lunge 3 x 10"}))
        self.assertTrue(is_text_heavy_pin({"title": "gym session \U0001F525"}))
        self.assertTrue(is_text_heavy_pin({"title": "Don't do this DB Hammer Curls"}))
        self.assertTrue(is_text_heavy_pin({"title": "Wanna get buff physique like"}))

    def test_hls_only_converts_to_mp4(self):
        from aesthetic_scraper import pinterest_mp4_urls, pinterest_video_rendition

        hls = "https://v1.pinimg.com/videos/mc/hls/ab/cd/ef/clip_v2.m3u8"
        urls = pinterest_mp4_urls(hls)
        self.assertIn("https://v1.pinimg.com/videos/mc/720p/ab/cd/ef/clip_v2.mp4", urls)
        self.assertIn("https://v1.pinimg.com/videos/mc/720p/ab/cd/ef/clip.mp4", urls)
        iht = "https://v1.pinimg.com/videos/iht/720p/3d/7c/cb/clip.mp4"
        iht_urls = pinterest_mp4_urls(iht)
        self.assertIn("https://v1.pinimg.com/videos/mc/720p/3d/7c/cb/clip.mp4", iht_urls)
        self.assertIn("https://v1.pinimg.com/videos/ml/480p/3d/7c/cb/clip.mp4", iht_urls)
        rendition = pinterest_video_rendition({
            "id": "555",
            "videos": {"video_list": {"V_HLS": {"url": hls, "width": 720, "height": 1280}}},
        })
        self.assertTrue(rendition["url"].endswith(".mp4"))
        self.assertIn("/720p/", rendition["url"])

    def test_pinterest_pin_page_from_id(self):
        from aesthetic_scraper import pinterest_pin_page

        self.assertEqual(
            pinterest_pin_page(pin_id="769411917630087421"),
            "https://www.pinterest.com/pin/769411917630087421/",
        )
        self.assertEqual(
            pinterest_pin_page(source_page="https://www.pinterest.com/pin/abc/?utm=1"),
            "https://www.pinterest.com/pin/abc/",
        )

    def test_ytdlp_download_writes_mp4(self):
        from aesthetic_scraper import download_pinterest_with_ytdlp

        class FakeYDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=True):
                dest = Path(self.opts["outtmpl"].replace(".%(ext)s", ".mp4"))
                dest.write_bytes(b"0" * 50000)
                self._name = str(dest)
                return {"id": "1", "ext": "mp4"}

            def prepare_filename(self, info):
                return self._name

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "vid_1.mp4"
            with patch("aesthetic_scraper.yt_dlp.YoutubeDL", FakeYDL):
                self.assertTrue(download_pinterest_with_ytdlp("https://www.pinterest.com/pin/1/", dest))
            self.assertTrue(dest.is_file())
            self.assertGreaterEqual(dest.stat().st_size, 40000)

    def test_href_parser_accepts_non_numeric_pin_ids(self):
        pins = parse_pinterest_pin_hrefs([
            "https://www.pinterest.com/pin/123456789/",
            "https://www.pinterest.com/pin/abc-xyz/?utm=1",
            "https://example.com/not-a-pin",
        ])
        self.assertEqual(pins, [
            "https://www.pinterest.com/pin/123456789/",
            "https://www.pinterest.com/pin/abc-xyz/",
        ])


class KeywordParseHookTests(unittest.TestCase):
    def test_extract_keywords_parser_keeps_order(self):
        processor = LLMProcessor(api_key="x")
        parsed = processor._parse("One. → mars rover dust\nTwo. → astronaut red sand")
        self.assertEqual([row["keyword"] for row in parsed], ["mars rover dust", "astronaut red sand"])

    def test_generate_full_script_prompt_includes_asset_budget(self):
        processor = LLMProcessor(api_key="x")
        captured = {}

        def fake_chat(model, messages, **kwargs):
            captured["prompt"] = messages[0]["content"]
            return "Hook line about the gym.\nSecond beat about consistency."

        with patch.object(processor, "_chat", side_effect=fake_chat):
            script = processor.generate_full_script(
                "gym", vibe="motivational", assets_per_scene=3, clip_duration=5
            )
        self.assertIn("15 seconds", captured["prompt"])
        self.assertIn("3 clips", captured["prompt"])
        self.assertIn("Hook line about the gym", script)

    def test_assets_per_scene_compacts_narration_phrases(self):
        script = (
            "Ever wonder why the gym feels like a battlefield and you’re the only soldier? "
            "Because every rep is a tiny war against the version of yourself that wants to quit. "
            "Drop the excuses—those dumbbells don’t care about your schedule, they care about consistency. "
            "Feel the burn? That’s not pain, that’s progress screaming, “I’m getting stronger!” "
            "When the weights feel heavy, remember: your mind is the strongest muscle you’ll ever train. "
            "Turn sweat into confidence and watch the world start to notice your unstoppable vibe. "
            "One more set, one more push, and you’re rewriting your story in real time. "
            "So lace up, crank the music, and make today the day you become your own legend."
        )
        sentences = split_script_sentences(script)
        grouped = group_scenes_to_clip_budget(
            [{"sentence": part, "keyword": "scene"} for part in sentences],
            count=3,
            clip_duration=5,
        )
        self.assertGreaterEqual(len(sentences), 7)
        self.assertLess(len(grouped), len(sentences))

    def test_apply_user_keywords_maps_onto_scenes(self):
        grouped = [
            {"sentence": "Feel the burn.", "keyword": "scene"},
            {"sentence": "Lace up those shoes.", "keyword": "scene"},
        ]
        mapped = apply_user_keywords(
            grouped,
            ["gym motivation workout", "gym dedication challenge", "gym fitness", "gym"],
            "gym, fitness, motivation",
        )
        self.assertEqual(mapped[0]["keyword"], "gym motivation workout")
        self.assertEqual(mapped[1]["keyword"], "gym dedication challenge")
        self.assertEqual(
            mapped[0]["_alts"],
            ["gym dedication challenge", "gym fitness", "gym"],
        )
        self.assertEqual(
            mapped[1]["_alts"],
            ["gym motivation workout", "gym fitness", "gym"],
        )
        self.assertEqual(
            stock_query_plan(mapped),
            ["gym motivation workout", "gym dedication challenge", "gym fitness", "gym"],
        )

    def test_apply_user_keywords_matches_scene_words_not_first_keyword(self):
        grouped = [
            {"sentence": "Every time you step into the gym, you're not just lifting weights."},
            {"sentence": "Every drop of sweat is a step closer to the best version of you."},
        ]
        mapped = apply_user_keywords(
            grouped,
            ["gym weights lifting", "gym fitness", "gym sweat dropping", "gym"],
            "gym",
        )
        self.assertEqual(mapped[0]["keyword"], "gym weights lifting")
        self.assertEqual(mapped[1]["keyword"], "gym sweat dropping")

    def test_terms_to_scene_rows_chunks_script_in_term_order(self):
        rows = terms_to_scene_rows(
            ["gym workout", "barbell squat", "victory pose"],
            "Hook the viewer. Then squat heavy. Finish proud.",
        )
        self.assertEqual([item["keyword"] for item in rows], ["gym workout", "barbell squat", "victory pose"])
        self.assertTrue(all(item["sentence"] for item in rows))
        self.assertEqual(split_script_to_n("A. B. C.", 3), ["A.", "B.", "C."])

    def test_split_script_to_n_does_not_repeat_one_paragraph(self):
        script = (
            "From Patagonia cliffs to Maldives atolls; wander Machu Picchu; "
            "explore Iceland beach; and the Great Barrier Reef."
        )
        chunks = split_script_to_n(script, 5)
        self.assertEqual(len(chunks), 5)
        self.assertEqual(len(set(c.lower() for c in chunks)), 5)
        self.assertIn("Patagonia", chunks[0])
        self.assertIn("Reef", chunks[-1])

    def test_terms_amount_matches_mpt_five_or_eight(self):
        short = "One two three four five six seven eight nine ten."
        self.assertEqual(terms_amount_for_script(short, match_script_order=False), 5)
        self.assertEqual(terms_amount_for_script(short, match_script_order=True), 8)
        long = " ".join(["word"] * 400)
        self.assertEqual(terms_amount_for_script(long, count=3, clip_duration=5), 5)
        self.assertEqual(terms_amount_for_script(long, match_script_order=True, count=3, clip_duration=5), 8)


if __name__ == "__main__":
    unittest.main()
