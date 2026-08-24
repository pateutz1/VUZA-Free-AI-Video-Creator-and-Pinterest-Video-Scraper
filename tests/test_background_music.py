import asyncio
import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import BackgroundTasks, HTTPException

from app import (
    ALLOWED_UPLOAD_SUFFIXES,
    ApiKeys,
    MIXKIT_MUSIC_DOWNLOAD_DIR,
    MIXKIT_MUSIC_MOODS,
    ScrapeRequest,
    UPLOAD_DIR,
    VALID_SOURCES,
    VideoSettings,
    app as fastapi_app,
    group_scenes_to_clip_budget,
    is_fatal_scene_media_error,
    local_script_segments,
    mixkit_music_catalog,
    normalized_script_inputs,
    resolve_path_within_directory,
    set_status,
    run_scrape,
    scraping_status,
    resolve_background_music,
    sanitize_upload_filename,
    start_scrape,
    validate_request_api_dependencies,
    validate_scrape_request_options,
    validate_script_keyword_key,
)
import app as app_module
from aesthetic_scraper import CoverrScraper, PiAPIScraper, LLMProcessor, LLM_PROVIDER_PRESETS, is_hd_resolution, matches_video_aspect

ROOT = Path(__file__).resolve().parents[1]


async def post_json(path, payload):
    body = json.dumps(payload).encode("utf-8")
    sent_body = False
    messages = []

    async def receive():
        nonlocal sent_body
        if sent_body:
            return {"type": "http.disconnect"}
        sent_body = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    await fastapi_app(scope, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    text = response_body.decode("utf-8") if response_body else "{}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {"raw": text}
    return status, data


async def get_json(path, query_string=b""):
    messages = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query_string,
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    await fastapi_app(scope, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, json.loads(response_body.decode("utf-8"))


class LocalScriptSegmentTests(unittest.TestCase):
    def test_chinese_script_splits_into_stable_scene_rows(self):
        script = "凌晨两点，我收到一条陌生短信。短信里只有五个字：别回头看。\n窗外的雨声突然停了。"

        segments = local_script_segments(script)

        self.assertEqual(
            segments,
            [
                {"sentence": "凌晨两点，我收到一条陌生短信。", "keyword": "scene_001"},
                {"sentence": "短信里只有五个字：别回头看。", "keyword": "scene_002"},
                {"sentence": "窗外的雨声突然停了。", "keyword": "scene_003"},
            ],
        )

    def test_empty_script_produces_no_segments(self):
        self.assertEqual(local_script_segments(" \n\t "), [])


class SceneBudgetTests(unittest.TestCase):
    def test_short_sentences_merge_to_assets_times_clip_length(self):
        rows = [
            {"sentence": "One two three four five.", "keyword": f"k{i}"}
            for i in range(12)
        ]
        grouped = group_scenes_to_clip_budget(rows, count=3, clip_duration=5)
        self.assertLess(len(grouped), 12)
        self.assertGreaterEqual(len(grouped), 1)

    def test_long_sentence_stays_one_scene(self):
        sentence = " ".join(["word"] * 80)
        grouped = group_scenes_to_clip_budget(
            [{"sentence": sentence, "keyword": "gym"}],
            count=3,
            clip_duration=5,
        )
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["keyword"], "gym")


class StatusProgressTests(unittest.TestCase):
    def test_set_status_clamps_progress_to_api_range(self):
        set_status(progress=-25)
        self.assertEqual(scraping_status["progress"], 0)

        set_status(progress=150.7)
        self.assertEqual(scraping_status["progress"], 100)

    def test_set_status_uses_zero_for_invalid_progress(self):
        set_status(progress="not-a-number")
        self.assertEqual(scraping_status["progress"], 0)


class BackgroundMusicResolutionTests(unittest.TestCase):
    def test_none_disables_background_music(self):
        settings = VideoSettings(music="none")

        self.assertIsNone(resolve_background_music(settings))

    def test_blank_music_uses_default_no_music(self):
        settings = VideoSettings(music="")

        self.assertIsNone(resolve_background_music(settings))

    def test_none_music_is_case_insensitive(self):
        settings = VideoSettings(music=" NONE ")

        self.assertIsNone(resolve_background_music(settings))

    def test_existing_music_file_resolves_to_static_music_path(self):
        settings = VideoSettings(music="cinematic.mp3")

        music_path = resolve_background_music(settings)

        self.assertTrue(music_path.endswith("static\\music\\cinematic.mp3") or music_path.endswith("static/music/cinematic.mp3"))

    def test_missing_music_file_raises_clear_error(self):
        settings = VideoSettings(music="missing.mp3")

        with self.assertRaisesRegex(RuntimeError, "Background music file is missing or empty"):
            resolve_background_music(settings)

    def test_nested_music_path_is_rejected(self):
        settings = VideoSettings(music="../cinematic.mp3")

        with self.assertRaisesRegex(RuntimeError, "Background music filename is invalid"):
            resolve_background_music(settings)


class MixkitMusicTests(unittest.TestCase):
    def setUp(self):
        app_module._mixkit_music_cache["tracks"] = []
        app_module._mixkit_music_cache["fetched_at"] = 0.0

    tearDown = setUp

    def test_catalog_dedupes_across_moods_and_caches(self):
        track = {
            "id": "mixkit-1", "title": "Track", "artist": "A",
            "genre": "Ambient", "duration": 60.0, "url": "https://assets.mixkit.co/music/1/1.mp3",
        }
        with patch("app.mixkit_music_tracks", return_value=[track]) as fake_fetch:
            tracks = mixkit_music_catalog()
        self.assertEqual(tracks, [track])
        self.assertEqual(fake_fetch.call_count, len(MIXKIT_MUSIC_MOODS))

        with patch("app.mixkit_music_tracks") as fake_fetch_again:
            tracks_again = mixkit_music_catalog()
        fake_fetch_again.assert_not_called()
        self.assertEqual(tracks_again, [track])

    def test_resolve_mixkit_music_downloads_and_caches_locally(self):
        track = {
            "id": "mixkit-999", "title": "Test Track", "artist": "A",
            "genre": "Ambient", "duration": 60.0, "url": "https://assets.mixkit.co/music/999/999.mp3",
        }
        app_module._mixkit_music_cache["tracks"] = [track]
        app_module._mixkit_music_cache["fetched_at"] = time.time()
        cache_path = MIXKIT_MUSIC_DOWNLOAD_DIR / "999.mp3"
        self.addCleanup(lambda: cache_path.unlink(missing_ok=True))

        def fake_download(url, path, timeout, min_bytes, verify):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"x" * 30000)
            return True

        with patch("app.download_http", side_effect=fake_download):
            resolved = resolve_background_music(VideoSettings(music="mixkit-999"))

        self.assertEqual(resolved, str(cache_path))
        self.assertTrue(cache_path.exists())

    def test_resolve_mixkit_music_missing_track_raises(self):
        with patch("app.mixkit_music_catalog", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "no longer available"):
                resolve_background_music(VideoSettings(music="mixkit-doesnotexist"))

    def test_resolve_mixkit_music_download_failure_raises(self):
        track = {
            "id": "mixkit-777", "title": "Broken Track", "artist": "A",
            "genre": "Ambient", "duration": 60.0, "url": "https://assets.mixkit.co/music/777/777.mp3",
        }
        app_module._mixkit_music_cache["tracks"] = [track]
        app_module._mixkit_music_cache["fetched_at"] = time.time()
        cache_path = MIXKIT_MUSIC_DOWNLOAD_DIR / "777.mp3"
        self.addCleanup(lambda: cache_path.unlink(missing_ok=True))

        with patch("app.download_http", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "Could not download Mixkit music track"):
                resolve_background_music(VideoSettings(music="mixkit-777"))

    def test_api_music_includes_mixkit_tracks(self):
        track = {
            "id": "mixkit-42", "title": "Chill", "artist": "A",
            "genre": "Lo-fi", "duration": 90.0, "url": "https://assets.mixkit.co/music/42/42.mp3",
        }
        with patch("app.mixkit_music_catalog", return_value=[track]):
            status, data = asyncio.run(get_json("/api/music"))
        self.assertEqual(status, 200)
        self.assertIn("mixkit-42", data["files"])
        self.assertEqual(data["mixkit_tracks"], [track])


class SourceContractTests(unittest.TestCase):
    def test_default_source_is_pinterest(self):
        self.assertEqual(ScrapeRequest().source, "pinterest")

    def test_ai_source_is_rejected(self):
        request = ScrapeRequest(source="ai", query="rain alley")
        with self.assertRaisesRegex(RuntimeError, "Invalid media source"):
            validate_scrape_request_options(request)

    def test_api_keys_have_no_seedream_fields(self):
        self.assertNotIn("seedream_key", ApiKeys.model_fields)
        self.assertNotIn("seedream_url", ApiKeys.model_fields)
        self.assertNotIn("seedream_model", ApiKeys.model_fields)
        self.assertIn("coverr_key", ApiKeys.model_fields)
        self.assertIn("piapi_key", ApiKeys.model_fields)
        self.assertIn("piapi_model", ApiKeys.model_fields)

    def test_valid_sources_exclude_ai(self):
        self.assertEqual(VALID_SOURCES, {"pinterest", "pexels", "pixabay", "coverr", "mixkit", "piapi", "local", "round_robin"})
        self.assertNotIn("ai", VALID_SOURCES)

    def test_api_scrape_rejects_ai_source_with_english_detail(self):
        scraping_status["is_running"] = False
        status, data = asyncio.run(post_json(
            "/api/scrape",
            {"source": "ai", "mode": "single", "query": "rain alley", "auto_video": False},
        ))
        self.assertEqual(status, 400)
        self.assertIn("Invalid media source", data["detail"])
        self.assertFalse(scraping_status["is_running"])

    def test_html_js_contract_has_script_id_and_no_seedream(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "static" / "script.js").read_text(encoding="utf-8")
        self.assertIn('id="script"', html)
        self.assertIn('id="src-mixkit"', html)
        self.assertIn("checked", html.split('id="src-mixkit"', 1)[1][:80])
        self.assertNotIn('id="src-pinterest"', html)
        self.assertNotIn("src-ai", html)
        self.assertNotIn("seedream", html.lower())
        self.assertIn("getElementById('script')", js)
        self.assertNotIn("src-ai", js)
        self.assertIn("src-mixkit", js)
        self.assertNotIn("src-pinterest", js)
        self.assertIn('id="src-piapi"', html)
        self.assertIn("PiAPI is paid", html)
        self.assertIn("window.confirm", js)
        self.assertIn("piapi_confirmed: piapiConfirmed", js)
        self.assertIn('id="keywords-input"', html)
        self.assertIn('id="regenerate-keywords-btn"', html)
        self.assertIn("/api/generate_keywords", js)
        self.assertIn("keywords: readKeywords()", js)
        self.assertNotIn("volces.com", js)


class LlmEndpointValidationTests(unittest.TestCase):
    def test_api_analyze_rejects_missing_llm_key_before_processor(self):
        with patch("app.LLMProcessor") as processor:
            status, data = asyncio.run(post_json(
                "/api/analyze",
                {
                    "script": "凌晨两点，我收到一条陌生短信。",
                    "api_keys": {"llm_key": ""},
                },
            ))

        self.assertEqual(status, 400)
        self.assertIn("requires an AI text API key", data["detail"])
        processor.assert_not_called()

    def test_api_generate_script_rejects_missing_llm_key_before_processor(self):
        with patch("app.LLMProcessor") as processor:
            status, data = asyncio.run(post_json(
                "/api/generate_script",
                {
                    "topic": "雨夜收到陌生短信",
                    "vibe": "suspense_cn",
                    "api_keys": {"llm_key": " "},
                },
            ))

        self.assertEqual(status, 400)
        self.assertIn("requires an AI text API key", data["detail"])
        processor.assert_not_called()

    def test_api_generate_keywords_rejects_missing_llm_key_before_processor(self):
        with patch("app.LLMProcessor") as processor:
            status, data = asyncio.run(post_json(
                "/api/generate_keywords",
                {
                    "topic": "gym, fitness, motivation",
                    "script": "Feel the burn and lace up those shoes.",
                    "api_keys": {"llm_key": " "},
                },
            ))

        self.assertEqual(status, 400)
        self.assertIn("requires an AI text API key", data["detail"])
        processor.assert_not_called()

    def test_api_scrape_url_rejects_missing_llm_key_before_scraping(self):
        with patch("app.WebScraper") as web_scraper, patch("app.LLMProcessor") as processor:
            status, data = asyncio.run(post_json(
                "/api/scrape_url",
                {
                    "url": "https://example.com/story",
                    "api_keys": {"llm_key": ""},
                },
            ))

        self.assertEqual(status, 400)
        self.assertIn("requires an AI text API key", data["detail"])
        web_scraper.assert_not_called()
        processor.assert_not_called()

    def test_llm_presets_exclude_volcengine(self):
        ids = {item["id"] for item in LLM_PROVIDER_PRESETS}
        self.assertTrue({"openrouter", "openai", "deepseek", "groq", "ollama", "oneapi"} <= ids)
        self.assertNotIn("volcengine", ids)
        status, data = asyncio.run(get_json("/api/llm/presets"))
        self.assertEqual(status, 200)
        self.assertEqual(len(data["presets"]), len(LLM_PROVIDER_PRESETS))
        openai = next(item for item in data["presets"] if item["id"] == "openai")
        self.assertIn("gpt-4o-mini", openai["models"])

    def test_llm_test_requires_key_for_openai(self):
        status, data = asyncio.run(post_json("/api/llm/test", {"provider": "openai", "api_key": "", "model": "gpt-4o-mini"}))
        self.assertEqual(status, 400)
        self.assertIn("API key", data["detail"])

    def test_llm_test_success_uses_selected_model(self):
        class FakeResponse:
            status_code = 200
            def json(self):
                return {"choices": [{"message": {"content": "ok"}}]}

        with patch("requests.post", return_value=FakeResponse()):
            status, data = asyncio.run(post_json("/api/llm/test", {
                "provider": "openai",
                "api_key": "sk-test",
                "model": "gpt-4o-mini",
            }))
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["model"], "gpt-4o-mini")


class ScrapeRequestValidationTests(unittest.TestCase):
    def test_request_options_are_normalized_before_validation(self):
        request = ScrapeRequest(
            source=" PEXELS ",
            media_type=" PHOTO ",
            mode=" SCRIPT ",
            script="凌晨两点，我听见门外有人低声喊我的名字。",
            auto_video=False,
            api_keys=ApiKeys(llm_key="sk-test"),
        )

        validate_scrape_request_options(request)
        validate_request_api_dependencies(request)

        self.assertEqual(request.source, "pexels")
        self.assertEqual(request.media_type, "photo")
        self.assertEqual(request.mode, "script")

    def test_invalid_source_is_rejected_before_background_work(self):
        request = ScrapeRequest(source="unknown", query="雨夜小巷")

        with self.assertRaisesRegex(RuntimeError, "Invalid media source"):
            validate_scrape_request_options(request)

    def test_invalid_media_type_is_rejected(self):
        request = ScrapeRequest(media_type="gif", query="雨夜小巷")

        with self.assertRaisesRegex(RuntimeError, "Invalid media type"):
            validate_scrape_request_options(request)

    def test_single_mode_requires_query(self):
        request = ScrapeRequest(source="pexels", query="  ")

        with self.assertRaisesRegex(RuntimeError, "Single search requires a topic query"):
            validate_scrape_request_options(request)

    def test_count_must_stay_inside_ui_range(self):
        request = ScrapeRequest(source="pexels", query="雨夜小巷", count=16)

        with self.assertRaisesRegex(RuntimeError, "1 and 15"):
            validate_scrape_request_options(request)

    def test_single_stock_search_rejects_auto_video(self):
        request = ScrapeRequest(source="pexels", query="雨夜小巷", auto_video=True)

        with self.assertRaisesRegex(RuntimeError, "Single stock search does not assemble a video"):
            validate_scrape_request_options(request)

    def test_auto_video_rejects_disabled_voice(self):
        request = ScrapeRequest(
            source="pexels",
            query="雨夜小巷",
            auto_video=True,
            video_settings=VideoSettings(voice=" NONE "),
        )

        with self.assertRaisesRegex(RuntimeError, "Auto video requires a TTS voice"):
            validate_scrape_request_options(request)

    def test_asset_only_mode_allows_disabled_voice(self):
        request = ScrapeRequest(
            source="pexels",
            query="雨夜小巷",
            auto_video=False,
            video_settings=VideoSettings(voice="none"),
        )

        validate_scrape_request_options(request)

    def test_youtube_upload_requires_confirmation(self):
        request = ScrapeRequest(
            source="pexels",
            mode="script",
            script="Hello there.",
            auto_video=True,
            yt_upload=True,
            publish_confirmed=False,
            api_keys=ApiKeys(llm_key="sk-test"),
        )
        with self.assertRaisesRegex(RuntimeError, "explicit confirmation"):
            validate_scrape_request_options(request)

    def test_start_scrape_rejects_invalid_request_before_queuing_task(self):
        request = ScrapeRequest(source="pexels", query="  ")
        background_tasks = BackgroundTasks()
        scraping_status["is_running"] = False
        scraping_status["status"] = "success"
        scraping_status["message"] = "old success"
        scraping_status["error"] = None
        scraping_status["final_video"] = "/downloads/old/final.mp4"
        scraping_status["results"] = [{"keyword": "old", "files": ["/downloads/old/final.mp4"]}]

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(start_scrape(request, background_tasks))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Single search requires a topic query", raised.exception.detail)
        self.assertEqual(background_tasks.tasks, [])
        self.assertEqual(scraping_status["status"], "error")
        self.assertEqual(scraping_status["error"], raised.exception.detail)
        self.assertIn("Single search requires a topic query", scraping_status["message"])
        self.assertEqual(scraping_status["progress"], 100)
        self.assertIsNone(scraping_status["final_video"])
        self.assertEqual(scraping_status["results"], [])

    def test_start_scrape_rejects_empty_script_mode_before_queuing_task(self):
        request = ScrapeRequest(mode="script", script=" ", scripts=["", "  "])
        background_tasks = BackgroundTasks()
        scraping_status["is_running"] = False

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(start_scrape(request, background_tasks))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("at least one narration script", raised.exception.detail)
        self.assertEqual(background_tasks.tasks, [])

    def test_script_mode_ignores_blank_batch_entries(self):
        request = ScrapeRequest(
            mode="script",
            script="   备用脚本不会重复使用   ",
            scripts=["", "  第一段旁白  ", "\t", "第二段旁白\n"],
        )

        self.assertEqual(normalized_script_inputs(request), ["第一段旁白", "第二段旁白"])
        validate_scrape_request_options(request)

    def test_script_mode_uses_single_script_when_batch_is_empty(self):
        request = ScrapeRequest(mode="script", script="   只有一段旁白   ", scripts=[" ", ""])

        self.assertEqual(normalized_script_inputs(request), ["只有一段旁白"])
        validate_scrape_request_options(request)

    def test_stock_script_mode_requires_llm_key_for_keyword_analysis(self):
        request = ScrapeRequest(
            source="pexels",
            mode="script",
            script="凌晨两点，我听见门外有人低声喊我的名字。",
            auto_video=False,
            api_keys=ApiKeys(llm_key=" "),
        )

        with self.assertRaisesRegex(RuntimeError, "AI text API key"):
            validate_script_keyword_key(request)

    def test_stock_script_mode_api_dependency_rejects_missing_llm_key(self):
        request = ScrapeRequest(
            source="pixabay",
            mode="script",
            script="凌晨两点，我听见门外有人低声喊我的名字。",
            auto_video=False,
            api_keys=ApiKeys(llm_key=""),
        )

        with self.assertRaisesRegex(RuntimeError, "search keywords"):
            validate_request_api_dependencies(request)

    def test_stock_script_mode_accepts_llm_key_without_coverr_key(self):
        request = ScrapeRequest(
            source="pexels",
            mode="script",
            script="凌晨两点，我听见门外有人低声喊我的名字。",
            auto_video=False,
            api_keys=ApiKeys(llm_key="sk-test", coverr_key=""),
        )

        validate_request_api_dependencies(request)

    def test_start_scrape_rejects_single_stock_auto_video_before_queuing_task(self):
        request = ScrapeRequest(source="pexels", query="雨夜小巷", auto_video=True)
        background_tasks = BackgroundTasks()
        scraping_status["is_running"] = False

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(start_scrape(request, background_tasks))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Single stock search does not assemble a video", raised.exception.detail)
        self.assertEqual(background_tasks.tasks, [])

    def test_start_scrape_returns_task_id(self):
        request = ScrapeRequest(source="pexels", query="rain alley", auto_video=False)
        background_tasks = BackgroundTasks()
        scraping_status["is_running"] = False

        result = asyncio.run(start_scrape(request, background_tasks))

        self.assertIn("task_id", result)
        self.assertTrue(result["task_id"])
        self.assertEqual(scraping_status["task_id"], result["task_id"])
        self.assertEqual(len(background_tasks.tasks), 1)

    def test_start_scrape_rejects_stock_script_without_llm_key_before_queuing_task(self):
        request = ScrapeRequest(
            source="pexels",
            mode="script",
            script="凌晨两点，我听见门外有人低声喊我的名字。",
            auto_video=False,
            api_keys=ApiKeys(llm_key=""),
        )
        background_tasks = BackgroundTasks()
        scraping_status["is_running"] = False

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(start_scrape(request, background_tasks))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("AI text API key", raised.exception.detail)
        self.assertEqual(background_tasks.tasks, [])

    def test_start_scrape_rejects_missing_music_before_queuing_task(self):
        request = ScrapeRequest(
            source="pexels",
            mode="script",
            script="Hello there.",
            auto_video=True,
            video_settings=VideoSettings(music="missing.mp3"),
            api_keys=ApiKeys(llm_key="sk-test"),
        )
        background_tasks = BackgroundTasks()
        scraping_status["is_running"] = False

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(start_scrape(request, background_tasks))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Background music file is missing or empty", raised.exception.detail)
        self.assertEqual(background_tasks.tasks, [])

    def test_api_scrape_rejects_disabled_voice_auto_video_with_detail(self):
        scraping_status["is_running"] = False

        status, data = asyncio.run(post_json(
            "/api/scrape",
            {
                "source": "pexels",
                "mode": "script",
                "script": "凌晨两点，我听见门外有人低声喊我的名字。",
                "auto_video": True,
                "video_settings": {"voice": "none"},
                "api_keys": {"llm_key": "sk-test"},
            },
        ))

        self.assertEqual(status, 400)
        self.assertIn("Auto video requires a TTS voice", data["detail"])
        self.assertFalse(scraping_status["is_running"])

    def test_api_scrape_rejects_stock_script_without_llm_key_with_detail(self):
        scraping_status["is_running"] = False

        status, data = asyncio.run(post_json(
            "/api/scrape",
            {
                "source": "pexels",
                "mode": "script",
                "script": "凌晨两点，我听见门外有人低声喊我的名字。",
                "auto_video": False,
                "api_keys": {"llm_key": ""},
            },
        ))

        self.assertEqual(status, 400)
        self.assertIn("AI text API key", data["detail"])
        self.assertFalse(scraping_status["is_running"])

    def test_single_search_reports_error_when_no_media_is_found(self):
        request = ScrapeRequest(
            source="pexels",
            mode="single",
            query="codex missing media case",
            auto_video=False,
        )
        scraping_status["is_running"] = False
        scraping_status["error"] = None

        with patch("app.universal_search", new=AsyncMock(return_value=[])):
            with contextlib.redirect_stderr(io.StringIO()):
                asyncio.run(run_scrape(request))

        self.assertEqual(scraping_status["status"], "error")
        self.assertIn("No usable media found", scraping_status["error"])
        self.assertFalse(scraping_status["is_running"])

    def test_asset_only_script_reports_error_when_a_scene_has_no_media(self):
        long_a = "这是第一段旁白需要足够的字数才能单独占满一个五秒镜头预算的画面。" * 3
        long_b = "这是第二段旁白同样需要足够的字数才能单独占满下一个镜头预算的画面。" * 3
        request = ScrapeRequest(
            source="pexels",
            mode="script",
            script=f"{long_a}。{long_b}。",
            auto_video=False,
            api_keys=ApiKeys(llm_key="sk-test"),
        )
        processor = Mock()
        processor.extract_keywords.return_value = [
            {"sentence": long_a, "keyword": "scene_001"},
            {"sentence": long_b, "keyword": "scene_002"},
        ]

        async def fake_universal_search(keyword, **kwargs):
            return [Path("README.md")] if keyword == "scene_001" else []

        scraping_status["is_running"] = False
        scraping_status["error"] = None

        with patch.object(Path, "mkdir", return_value=None), \
             patch("app.LLMProcessor", return_value=processor), \
             patch("app.universal_search", new=AsyncMock(side_effect=fake_universal_search)):
            with contextlib.redirect_stderr(io.StringIO()):
                asyncio.run(run_scrape(request))

        self.assertEqual(scraping_status["status"], "error")
        self.assertIn("Scene media is incomplete", scraping_status["error"])
        self.assertIn("scene_002", scraping_status["error"])
        self.assertFalse(scraping_status["is_running"])

    def test_script_batch_search_exception_keeps_scene_context(self):
        long_a = "这是第一段旁白需要足够的字数才能单独占满一个五秒镜头预算的画面。" * 3
        long_b = "这是第二段旁白同样需要足够的字数才能单独占满下一个镜头预算的画面。" * 3
        request = ScrapeRequest(
            source="pexels",
            mode="script",
            script=f"{long_a}。{long_b}。",
            auto_video=False,
            api_keys=ApiKeys(llm_key="sk-test"),
        )
        processor = Mock()
        processor.extract_keywords.return_value = [
            {"sentence": long_a, "keyword": "scene_001"},
            {"sentence": long_b, "keyword": "scene_002"},
        ]

        async def fake_universal_search(keyword, **kwargs):
            if keyword == "scene_002":
                raise RuntimeError("Coverr HTTP 500: quota exhausted")
            return [Path("README.md")]

        scraping_status["is_running"] = False
        scraping_status["error"] = None

        with patch.object(Path, "mkdir", return_value=None), \
             patch("app.LLMProcessor", return_value=processor), \
             patch("app.universal_search", new=AsyncMock(side_effect=fake_universal_search)):
            with contextlib.redirect_stderr(io.StringIO()):
                asyncio.run(run_scrape(request))

        self.assertEqual(scraping_status["status"], "error")
        self.assertIn("Scene media is incomplete", scraping_status["error"])
        self.assertIn("scene_002", scraping_status["error"])
        self.assertIn("Coverr HTTP 500: quota exhausted", scraping_status["error"])
        self.assertFalse(scraping_status["is_running"])

    def test_asset_only_single_search_does_not_load_video_engine(self):
        request = ScrapeRequest(
            source="pexels",
            mode="single",
            query="雨夜小巷",
            auto_video=False,
        )
        scraping_status["is_running"] = False
        scraping_status["error"] = None

        with patch("app.universal_search", new=AsyncMock(return_value=["unused"])), \
             patch("app.require_media_files", return_value=[Path("downloads/fake.jpg")]), \
             patch("app.load_video_engine", side_effect=AssertionError("video deps loaded")):
            asyncio.run(run_scrape(request))

        self.assertEqual(scraping_status["status"], "success")
        self.assertIsNone(scraping_status["error"])
        self.assertFalse(scraping_status["is_running"])


class PathSafetyAndMediaTests(unittest.TestCase):
    def test_path_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            resolve_path_within_directory(str(UPLOAD_DIR), "../app.py")

    def test_sanitize_upload_filename_strips_directories(self):
        self.assertEqual(sanitize_upload_filename("..\\secret.exe"), "secret.exe")
        self.assertIn(Path("photo.PNG").suffix.lower(), ALLOWED_UPLOAD_SUFFIXES)

    def test_local_source_requires_files(self):
        request = ScrapeRequest(source="local", mode="single", auto_video=False, local_files=[])
        with self.assertRaisesRegex(RuntimeError, "Local source requires at least one uploaded media file"):
            validate_scrape_request_options(request)

    def test_coverr_search_is_mocked_http(self):
        payload = {"hits": [{"id": "c1", "duration": 6, "width": 1080, "height": 1920, "urls": {"mp4": "https://example.test/c.mp4"}}]}
        response = Mock()
        response.json.return_value = payload
        with tempfile.TemporaryDirectory() as tmp:
            scraper = CoverrScraper(output_dir=tmp, api_key="ck-test")
            with patch("requests.get", return_value=response), \
                 patch.object(scraper, "download_file", return_value=True) as download:
                files = asyncio.run(scraper.search_videos("rain", num_videos=1, aspect="9:16"))
            download.assert_called()
            self.assertIsInstance(files, list)

    def test_piapi_requires_api_key(self):
        request = ScrapeRequest(source="piapi", mode="single", query="gym squat", auto_video=False)
        with self.assertRaisesRegex(RuntimeError, "PiAPI source requires an API key"):
            validate_request_api_dependencies(request)

    def test_piapi_requires_explicit_paid_confirmation(self):
        request = ScrapeRequest(
            source="piapi", mode="single", query="gym squat", auto_video=False,
            api_keys=ApiKeys(piapi_key="pi_test"),
        )
        with self.assertRaisesRegex(RuntimeError, "paid.*explicit confirmation"):
            validate_request_api_dependencies(request)

        request.piapi_confirmed = True
        validate_request_api_dependencies(request)

    def test_piapi_search_is_mocked_http(self):
        created = Mock()
        created.status_code = 200
        created.json.return_value = {
            "code": 200,
            "data": {"task_id": "t1", "status": "pending"},
        }
        done = Mock()
        done.status_code = 200
        done.json.return_value = {
            "code": 200,
            "data": {
                "task_id": "t1",
                "status": "Completed",
                "output": {"video_url": "https://example.test/gen.mp4"},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            scraper = PiAPIScraper(output_dir=tmp, api_key="pi_test", model="kling-2.5")
            with patch("requests.post", return_value=created), \
                 patch("requests.get", return_value=done), \
                 patch("time.sleep"):
                def fake_download(url, dest):
                    Path(dest).parent.mkdir(parents=True, exist_ok=True)
                    Path(dest).write_bytes(b"x" * 50000)
                    return True
                with patch.object(scraper, "download_file", side_effect=fake_download):
                    files = asyncio.run(scraper.search_videos("gym squat", num_videos=1, aspect="9:16"))
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].endswith(".mp4"))

    def test_piapi_kling_spec_and_retry(self):
        scraper = PiAPIScraper(output_dir=".", api_key="pi_test", model="kling-2.1-pro")
        self.assertEqual(scraper._kling_spec(), ("2.1", "pro"))
        hailuo = PiAPIScraper(output_dir=".", api_key="pi_test", model="hailuo-2.3-fast")
        body = hailuo._task_body("gym squat", "9:16", "video")
        self.assertEqual(body["model"], "hailuo")
        self.assertEqual(body["input"]["model"], "v2.3-fast")
        self.assertEqual(body["input"]["duration"], 6)
        self.assertEqual(body["input"]["resolution"], 768)
        self.assertTrue(body["input"]["expand_prompt"])
        self.assertEqual(body["config"], {"service_mode": "public"})
        self.assertEqual(hailuo._output_urls({"video": "https://example.test/h.mp4"}), ["https://example.test/h.mp4"])
        limited = Mock()
        limited.status_code = 429
        limited.headers = {"Retry-After": "1"}
        limited.json.return_value = {"message": "throttled"}
        ok = Mock()
        ok.status_code = 200
        ok.json.return_value = {"code": 200, "data": {"task_id": "t1", "status": "pending"}}
        with patch("requests.post", side_effect=[limited, ok]), patch("time.sleep"):
            data = scraper._create_task({"model": "kling", "task_type": "video_generation", "input": {}})
        self.assertEqual(data["task_id"], "t1")

    def test_piapi_failed_task_preserves_upstream_diagnostics(self):
        scraper = PiAPIScraper(output_dir=".", api_key="pi_test", model="hailuo-2.3-fast")
        failed = {
            "task_id": "t-failed",
            "status": "failed",
            "error": {"message": "invalid request", "raw_message": "unsupported resolution"},
            "detail": "upstream rejected input",
        }
        with self.assertRaisesRegex(RuntimeError, "unsupported resolution"):
            scraper._wait(failed)

    def test_generated_script_removes_model_chatter(self):
        content = "I can’t create videos, but I can help you craft a script for one!\nHere’s a high-retention spoken video script:\nNeon rain covers the city."
        self.assertEqual(
            LLMProcessor._clean_script_output(content),
            "Neon rain covers the city.",
        )

    def test_piapi_402_is_fatal(self):
        self.assertTrue(is_fatal_scene_media_error("PiAPI HTTP 402: no credit."))
        self.assertTrue(is_fatal_scene_media_error("PiAPI HTTP 401: API key rejected."))
        self.assertFalse(is_fatal_scene_media_error("PiAPI HTTP 429: throttled"))
        self.assertEqual(PiAPIScraper._clean_key('  Bearer abc  '), "abc")
        request = ScrapeRequest(
            source="piapi", mode="single", query="gym squat", auto_video=False,
            api_keys=ApiKeys(piapi_key="r8_old_replicate"),
        )
        with self.assertRaisesRegex(RuntimeError, "Replicate token"):
            validate_request_api_dependencies(request)

    def test_aspect_and_hd_helpers(self):
        self.assertTrue(matches_video_aspect(1080, 1920, "9:16"))
        self.assertFalse(matches_video_aspect(1920, 1080, "9:16"))
        self.assertTrue(is_hd_resolution(1920, 1080))
        self.assertFalse(is_hd_resolution(640, 360))

    def test_clip_duration_and_bgm_volume_bounds(self):
        settings = VideoSettings(clip_duration=8, bgm_volume=0.4, video_count=2, transition="fade")
        self.assertEqual(settings.clip_duration, 8)
        self.assertEqual(settings.bgm_volume, 0.4)
        self.assertEqual(settings.video_count, 2)
        with self.assertRaises(Exception):
            VideoSettings(clip_duration=99)

    def test_tts_preview_rejects_blank_text(self):
        status, data = asyncio.run(post_json("/api/tts/preview", {"text": "  "}))
        self.assertEqual(status, 400)
        self.assertIn("Enter preview text", data["detail"])

    def test_music_list_includes_none(self):
        status, data = asyncio.run(get_json("/api/music"))
        self.assertEqual(status, 200)
        self.assertIn("none", data["files"])


class CliTests(unittest.TestCase):
    def test_cli_usage_error_is_exit_2(self):
        from cli import main
        self.assertEqual(main(["--mode", "single", "--source", "pexels", "--no-auto-video"]), 2)

    def test_cli_help_exits_zero(self):
        from cli import main
        with self.assertRaises(SystemExit) as raised:
            main(["--help"])
        self.assertEqual(raised.exception.code, 0)

    def test_cli_piapi_requires_and_forwards_explicit_confirmation(self):
        from cli import build_request, parse_args
        args = parse_args([
            "--source", "piapi",
            "--media-type", "video",
            "--script", "A runner crosses a neon city.",
            "--llm-key", "sk-test",
            "--piapi-key", "pi-test",
            "--piapi-model", "hailuo-2.3",
            "--piapi-confirmed",
        ])
        request = build_request(args)
        self.assertTrue(request.piapi_confirmed)
        self.assertEqual(request.api_keys.piapi_key, "pi-test")
        self.assertEqual(request.api_keys.piapi_model, "hailuo-2.3")


if __name__ == "__main__":
    unittest.main()
