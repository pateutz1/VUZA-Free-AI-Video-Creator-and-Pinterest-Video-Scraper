import asyncio
import os
import re
import requests
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse, unquote
import yt_dlp
from tqdm import tqdm
from PIL import Image

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

# ═══════════════════════════════════════════════════════════════
# VUZA — Video Utility for Zero-cost Automation
# Built by Ali R. | github.com/AliRash3ed
# ═══════════════════════════════════════════════════════════════

LLM_PROVIDER_PRESETS = [
    {"id": "openrouter", "label": "OpenRouter", "url": "https://openrouter.ai/api/v1/chat/completions", "model": "deepseek/deepseek-v4-pro"},
    {"id": "openai", "label": "OpenAI", "url": "https://api.openai.com/v1/chat/completions", "model": "gpt-4o-mini"},
    {"id": "deepseek", "label": "DeepSeek", "url": "https://api.deepseek.com/v1/chat/completions", "model": "deepseek-v4-pro"},
    {"id": "groq", "label": "Groq", "url": "https://api.groq.com/openai/v1/chat/completions", "model": "llama-3.3-70b-versatile"},
    {"id": "ollama", "label": "Ollama", "url": "http://127.0.0.1:11434/v1/chat/completions", "model": "llama3.2"},
    {"id": "oneapi", "label": "OneAPI / LiteLLM gateway", "url": "http://127.0.0.1:3000/v1/chat/completions", "model": "gpt-4o-mini"},
]

def get_async_playwright():
    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError("Pinterest and URL scraping require Playwright: pip install playwright && playwright install chromium") from exc
    return async_playwright

class PinterestScraper:
    def __init__(self, output_dir="downloads/pinterest"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.seen_ids = set()

    def _get_folder(self, query):
        safe_query = re.sub(r'[^\w\-]', '_', query)[:25]
        folder = self.output_dir / safe_query
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    async def get_pin_urls(self, query, media_type="videos", scroll_count=5):
        search_url = f"https://www.pinterest.com/search/{media_type}/?q={quote(query)}"
        print(f"🔍 Searching Pinterest {media_type}: {query}")
        pins = []
        async with get_async_playwright()() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(user_agent=self.user_agent)
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
                try:
                    await page.wait_for_selector('a[href*="/pin/"]', timeout=15000)
                except Exception:
                    pass
                for _ in range(scroll_count):
                    await page.evaluate("window.scrollBy(0, 1500)")
                    await asyncio.sleep(1)
                hrefs = await page.evaluate('() => Array.from(document.querySelectorAll(\'a[href*="/pin/"]\')).map(a => a.href)')
                seen = set()
                for href in hrefs:
                    match = re.search(r'/pin/(\d+)/?', href)
                    if match and match.group(1) not in seen:
                        pins.append(f"https://www.pinterest.com/pin/{match.group(1)}/")
                        seen.add(match.group(1))
            except Exception as exc:
                print(f"⚠️ Pinterest search failed: {exc}")
            finally: await browser.close()
        print(f"📌 Found {len(pins)} pins")
        return pins

    async def search_images(self, query, num_images=5):
        urls = await self.get_pin_urls(query, media_type="pins", scroll_count=3)
        folder = self._get_folder(query)
        results = []
        for i, pin_url in enumerate(urls[:num_images*2]):
            try:
                async with get_async_playwright()() as p:
                    browser = await p.chromium.launch(headless=True)
                    page = await browser.new_page(user_agent=self.user_agent)
                    await page.goto(pin_url, wait_until="networkidle", timeout=30000)
                    img_url = await page.evaluate('() => { const img = document.querySelector(\'img[srcset]\'); return img ? img.src : null; }')
                    await browser.close()
                    if img_url:
                        path = folder / f"pin_{i}.jpg"
                        if not path.exists():
                            r = requests.get(img_url, timeout=15)
                            if r.status_code == 200: path.write_bytes(r.content); results.append(str(path))
                        else: results.append(str(path))
            except: continue
            if len(results) >= num_images: break
        return results[:num_images]

    async def search_videos(self, query, num_videos=3, aspect="9:16"):
        urls = await self.get_pin_urls(query, media_type="videos", scroll_count=3)
        if not urls: return []
        folder = self._get_folder(query)
        print(f"📌 Found {len(urls)} pins, downloading via yt-dlp...")
        downloader = VideoDownloader(output_dir=folder)
        return await downloader.download_parallel(urls, max_count=num_videos)

    def download_file(self, url, path):
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                path.write_bytes(r.content); return True
        except: pass
        return False

# ═══════════════════════════════════════════════════════════════
# PEXELS SCRAPER (PARALLEL)
# ═══════════════════════════════════════════════════════════════

class PexelsScraper:
    def __init__(self, output_dir="downloads/pexels", api_key=None):
        self.output_dir = Path(output_dir)
        self.api_key = api_key or os.environ.get("PEXELS_API_KEY", "")
        self.headers = {"Authorization": self.api_key}
        self.seen_ids = set()

    def _get_folder(self, query):
        folder = self.output_dir / re.sub(r'[^\w\-]', '_', query)[:25]
        folder.mkdir(parents=True, exist_ok=True); return folder

    async def search_images(self, query, num_images=5):
        if not self.api_key: print("⚠️ Pexels API key not set"); return []
        folder = self._get_folder(query)
        try:
            url = f"https://api.pexels.com/v1/search?query={quote(query)}&per_page={min(80, max(num_images, 15))}"
            data = requests.get(url, headers=self.headers, timeout=15).json()
            items = []
            for photo in data.get("photos", []):
                pid = photo.get("id")
                src = (photo.get("src") or {}).get("large2x")
                if pid and src and pid not in self.seen_ids:
                    items.append((src, pid))
                    self.seen_ids.add(pid)
                if len(items) >= num_images:
                    break
            return await download_id_files(self.download_file, items, folder, "p", "jpg", MIN_IMAGE_BYTES)
        except Exception as exc:
            print(f"⚠️ Pexels image search failed: {exc}")
            return []

    async def search_videos(self, query, num_videos=3, aspect="9:16"):
        if not self.api_key: print("⚠️ Pexels API key not set"); return []
        print(f"🎬 Searching Pexels: {query}")
        folder = self._get_folder(query)
        try:
            per_page = min(80, max(20, num_videos * 10))
            url = f"https://api.pexels.com/videos/search?query={quote(query)}&per_page={per_page}&orientation={pexels_orientation(aspect)}"
            data = requests.get(url, headers=self.headers, timeout=15).json()
            valid_vids = []
            for v in data.get("videos", []):
                vid_id = v.get("id")
                if vid_id in self.seen_ids:
                    continue
                duration = v.get("duration") or 0
                if 2 <= duration <= 25:
                    files = [vf for vf in v.get("video_files", []) if vf.get("link") and vf.get("width")]
                    files.sort(key=lambda vf: abs((vf.get("width") or 0) - 1920))
                    best = next((vf for vf in files if matches_video_aspect(vf.get("width"), vf.get("height"), aspect) and is_hd_resolution(vf.get("width"), vf.get("height"))), None)
                    if not best:
                        best = next((vf for vf in files if vf.get("width") and vf["width"] <= 1920), None)
                    if best:
                        valid_vids.append((best["link"], vid_id))
                        self.seen_ids.add(vid_id)
                if len(valid_vids) >= max(num_videos, 8):
                    break
            return await download_id_files(self.download_file, valid_vids, folder, "vid", "mp4", MIN_VIDEO_BYTES)
        except Exception as exc:
            print(f"⚠️ Pexels video search failed: {exc}")
            return []

    def download_file(self, url, path):
        min_bytes = MIN_VIDEO_BYTES if str(path).lower().endswith(".mp4") else MIN_IMAGE_BYTES
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200 and r.content and len(r.content) >= min_bytes:
                path.write_bytes(r.content)
                return True
        except Exception:
            pass
        return False

# ═══════════════════════════════════════════════════════════════
# PIXABAY SCRAPER (PARALLEL)
# ═══════════════════════════════════════════════════════════════

class PixabayScraper:
    def __init__(self, output_dir="downloads/pixabay", api_key=None):
        self.output_dir = Path(output_dir)
        self.api_key = api_key or os.environ.get("PIXABAY_API_KEY", "")
        self.seen_ids = set()

    def _get_folder(self, query):
        folder = self.output_dir / re.sub(r'[^\w\-]', '_', query)[:25]
        folder.mkdir(parents=True, exist_ok=True); return folder

    async def search_images(self, query, num_images=5):
        if not self.api_key: print("⚠️ Pixabay API key not set"); return []
        folder = self._get_folder(query)
        try:
            url = f"https://pixabay.com/api/?key={self.api_key}&q={quote(query)}&per_page={min(80, max(num_images, 15))}"
            data = requests.get(url, timeout=15).json()
            items = []
            for hit in data.get("hits", []):
                hid = hit.get("id")
                src = hit.get("largeImageURL")
                if hid and src and hid not in self.seen_ids:
                    items.append((src, hid))
                    self.seen_ids.add(hid)
                if len(items) >= num_images:
                    break
            return await download_id_files(self.download_file, items, folder, "pix", "jpg", MIN_IMAGE_BYTES)
        except Exception as exc:
            print(f"⚠️ Pixabay image search failed: {exc}")
            return []

    async def search_videos(self, query, num_videos=3, aspect="9:16"):
        if not self.api_key: print("⚠️ Pixabay API key not set"); return []
        folder = self._get_folder(query)
        try:
            url = f"https://pixabay.com/api/videos/?key={self.api_key}&q={quote(query)}&per_page={min(80, max(20, num_videos * 10))}"
            data = requests.get(url, timeout=15).json()
            valid = []
            for h in data.get("hits", []):
                vid_id = h.get("id")
                if vid_id in self.seen_ids:
                    continue
                if 2 <= h.get("duration", 0) <= 25:
                    v = h["videos"].get("medium") or h["videos"].get("small")
                    if v and v.get("url"):
                        valid.append((v["url"], vid_id))
                        self.seen_ids.add(vid_id)
                if len(valid) >= max(num_videos, 8):
                    break
            return await download_id_files(self.download_file, valid, folder, "v", "mp4", MIN_VIDEO_BYTES)
        except Exception as exc:
            print(f"⚠️ Pixabay video search failed: {exc}")
            return []

    def download_file(self, url, path):
        min_bytes = MIN_VIDEO_BYTES if str(path).lower().endswith(".mp4") else MIN_IMAGE_BYTES
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200 and r.content and len(r.content) >= min_bytes:
                path.write_bytes(r.content)
                return True
        except Exception:
            pass
        return False

def matches_video_aspect(width, height, aspect="9:16"):
    try:
        w, h = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return False
    if w <= 0 or h <= 0:
        return False
    ratio = w / h
    if aspect == "16:9":
        return ratio >= 1.2 and w >= 1280 and h >= 720
    if aspect == "1:1":
        return 0.8 <= ratio <= 1.25 and min(w, h) >= 720
    return ratio <= 0.85 and h >= 720


def is_hd_resolution(width, height):
    try:
        w, h = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return False
    return max(w, h) >= 1080 and min(w, h) >= 720


MIN_VIDEO_BYTES = 40000
MIN_IMAGE_BYTES = 8000


async def download_id_files(download_fn, items, folder, prefix, ext, min_bytes):
    """Download (url, id) pairs to id-based filenames. Return only those files, never a folder glob."""
    async def one(url, media_id):
        safe_id = re.sub(r"[^\w\-]", "_", str(media_id))[:48] or "x"
        path = Path(folder) / f"{prefix}_{safe_id}.{ext}"
        if path.exists() and path.stat().st_size >= min_bytes:
            return str(path)
        ok = await asyncio.to_thread(download_fn, url, path)
        if ok and path.exists() and path.stat().st_size >= min_bytes:
            return str(path)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        return None
    results = await asyncio.gather(*[one(url, media_id) for url, media_id in items])
    return [path for path in results if path]


def pexels_orientation(aspect="9:16"):
    if aspect == "16:9":
        return "landscape"
    if aspect == "1:1":
        return "square"
    return "portrait"


class CoverrScraper:
    """Coverr stock video search. Pattern adapted from MoneyPrinterTurbo (MIT)."""

    def __init__(self, output_dir="downloads/coverr", api_key=None):
        self.output_dir = Path(output_dir)
        self.api_key = api_key or os.environ.get("COVERR_API_KEY", "")
        self.seen_ids = set()

    def _get_folder(self, query):
        folder = self.output_dir / re.sub(r'[^\w\-]', '_', query)[:25]
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    async def search_images(self, query, num_images=5):
        return []

    async def search_videos(self, query, num_videos=3, aspect="9:16"):
        if not self.api_key:
            print("⚠️ Coverr API key not set")
            return []
        folder = self._get_folder(query)
        try:
            url = f"https://api.coverr.co/videos?query={quote(query)}&page_size={num_videos * 5}&urls=true"
            data = requests.get(url, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=20).json()
            hits = data.get("hits") or data.get("videos") or []
            valid = []
            for item in hits:
                vid_id = item.get("id") or item.get("_id")
                if vid_id in self.seen_ids:
                    continue
                duration = item.get("duration") or 0
                if duration and not (2 <= float(duration) <= 25):
                    continue
                urls = item.get("urls") or {}
                link = urls.get("mp4_preview") or urls.get("mp4") or item.get("mp4")
                width = item.get("width") or (item.get("max_width") if isinstance(item.get("max_width"), int) else 0)
                height = item.get("height") or 0
                if link and (not width or matches_video_aspect(width, height, aspect) or is_hd_resolution(width, height)):
                    valid.append((link, vid_id))
                    self.seen_ids.add(vid_id)
                if len(valid) >= max(num_videos, 8):
                    break
            return await download_id_files(self.download_file, valid, folder, "c", "mp4", MIN_VIDEO_BYTES)
        except Exception as exc:
            print(f"⚠️ Coverr search failed: {exc}")
            return []

    def download_file(self, url, path):
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 200 and r.content and len(r.content) >= MIN_VIDEO_BYTES:
                path.write_bytes(r.content)
                return True
        except Exception:
            pass
        return False


class PiAPIScraper:
    """Generate clips via PiAPI Unified API. Docs: https://piapi.ai/docs/unified-api-schema"""

    BASE_URL = "https://api.piapi.ai/api/v1/task"
    DEFAULT_VIDEO_MODEL = "hailuo-2.3-fast"
    DEFAULT_IMAGE_MODEL = "Qubico/flux1-schnell"

    def __init__(self, output_dir="downloads/piapi", api_key=None, model=None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = self._clean_key(api_key or os.environ.get("PIAPI_API_KEY", "") or os.environ.get("PIAPI_KEY", ""))
        self.model = (model or "").strip() or self.DEFAULT_VIDEO_MODEL

    @staticmethod
    def _clean_key(api_key):
        key = (api_key or "").strip().strip('"').strip("'")
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        return key

    def _get_folder(self, query):
        folder = self.output_dir / re.sub(r"[^\w\-]", "_", query)[:25]
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _headers(self):
        return {"x-api-key": self.api_key, "Content-Type": "application/json"}

    def _kling_spec(self):
        raw = (self.model or "").lower()
        version = "2.5"
        mode = "pro" if "pro" in raw or "master" in raw else "std"
        if "master" in raw:
            version = "2.1-master"
        else:
            match = re.search(r"(1\.5|1\.6|2\.1|2\.5|2\.6)", raw)
            if match:
                version = match.group(1)
        return version, mode

    def _is_hailuo(self):
        raw = (self.model or "").lower()
        return "hailuo" in raw or "v2.3" in raw or "2.3-fast" in raw

    def _hailuo_variant(self):
        raw = (self.model or "").lower()
        return "v2.3-fast" if "fast" in raw else "v2.3"

    def _task_body(self, prompt, aspect, kind):
        limit = 2000 if self._is_hailuo() and kind != "image" else 2500
        prompt = (prompt or "")[:limit]
        if kind == "image" or "flux" in (self.model or "").lower():
            model = self.model if "flux" in (self.model or "").lower() else self.DEFAULT_IMAGE_MODEL
            if "/" not in model:
                model = f"Qubico/{model}"
            size = {"9:16": (768, 1344), "16:9": (1344, 768)}.get(aspect or "9:16", (1024, 1024))
            return {
                "model": model,
                "task_type": "txt2img",
                "input": {"prompt": prompt, "width": size[0], "height": size[1]},
            }
        if self._is_hailuo():
            return {
                "model": "hailuo",
                "task_type": "video_generation",
                "input": {
                    "prompt": prompt,
                    "model": self._hailuo_variant(),
                    "expand_prompt": True,
                    "duration": 6,
                    "resolution": 768,
                },
                "config": {"service_mode": "public"},
            }
        version, mode = self._kling_spec()
        ratio = aspect if aspect in {"16:9", "9:16", "1:1"} else "9:16"
        return {
            "model": "kling",
            "task_type": "video_generation",
            "input": {
                "prompt": prompt,
                "negative_prompt": "subtitles, captions, text overlay, watermark, logo",
                "cfg_scale": "0.5",
                "duration": 5,
                "aspect_ratio": ratio,
                "mode": mode,
                "version": version,
            },
        }

    def _retry_after_seconds(self, response):
        header = response.headers.get("Retry-After") or response.headers.get("retry-after")
        if header:
            try:
                return max(1, min(int(float(header)), 60))
            except ValueError:
                pass
        return 12

    def _payload_message(self, payload):
        if not isinstance(payload, dict):
            return str(payload)[:300]
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("raw_message") or err)[:300]
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        nested = data.get("error") if isinstance(data, dict) else None
        if isinstance(nested, dict):
            return str(nested.get("message") or nested.get("raw_message") or nested)[:300]
        return str(payload.get("message") or payload.get("detail") or payload.get("title") or "")[:300]

    def _format_error(self, response):
        try:
            payload = response.json()
        except Exception:
            payload = {}
        detail = self._payload_message(payload) or (response.text or "")[:300]
        text = f"PiAPI HTTP {response.status_code}: {detail}".strip(": ")
        lowered = text.lower()
        if response.status_code == 401 or "failed to verify api key" in lowered or "unauthorized" in lowered:
            return (
                "PiAPI HTTP 401: API key rejected. Create a PiAPI key at https://app.piapi.ai/ "
                "and paste it in API settings. Do not use a Replicate r8_ token."
            )
        if response.status_code == 402 or "credit" in lowered or "balance" in lowered:
            return (
                "PiAPI HTTP 402: no credit. Top up at https://app.piapi.ai/ "
                "or switch Source to Pexels."
            )
        return text if detail else f"PiAPI HTTP {response.status_code}"

    def _create_task(self, body):
        last_error = None
        for attempt in range(5):
            response = requests.post(self.BASE_URL, headers=self._headers(), json=body, timeout=60)
            if response.status_code == 429:
                wait = self._retry_after_seconds(response)
                last_error = self._format_error(response)
                print(f"⏳ PiAPI rate limit, waiting {wait}s (attempt {attempt + 1}/5)")
                time.sleep(wait)
                continue
            if response.status_code in (500, 502, 503, 504) and attempt < 4:
                last_error = self._format_error(response)
                wait = 5 * (attempt + 1)
                print(f"⏳ PiAPI upstream {response.status_code}, retrying in {wait}s (attempt {attempt + 1}/5)")
                time.sleep(wait)
                continue
            if response.status_code >= 400:
                raise RuntimeError(self._format_error(response))
            payload = response.json() if response.content else {}
            code = payload.get("code")
            if code not in (None, 200, 0):
                raise RuntimeError(f"PiAPI error {code}: {self._payload_message(payload) or 'create task failed'}")
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            if not (data or {}).get("task_id"):
                raise RuntimeError(self._payload_message(payload) or "PiAPI did not return a task_id.")
            return data
        raise RuntimeError(last_error or "PiAPI HTTP 429: rate limited")

    def _wait(self, data):
        task_id = data.get("task_id")
        if not task_id:
            raise RuntimeError("PiAPI did not return a task_id.")
        url = f"{self.BASE_URL}/{task_id}"
        last_status = None
        for _ in range(72):
            status = str(data.get("status") or "").lower()
            if status != last_status:
                print(f"⏳ PiAPI task {task_id}: {status or 'pending'}")
                last_status = status
            if status == "completed":
                return data
            if status in {"failed", "error", "cancelled", "canceled"}:
                msg = self._task_failure_message(data)
                raise RuntimeError(f"PiAPI failed: {msg}")
            time.sleep(5)
            response = requests.get(url, headers=self._headers(), timeout=30)
            if response.status_code == 429:
                time.sleep(self._retry_after_seconds(response))
                continue
            if response.status_code >= 400:
                raise RuntimeError(self._format_error(response))
            payload = response.json() if response.content else {}
            code = payload.get("code") if isinstance(payload, dict) else None
            if code not in (None, 200, 0):
                raise RuntimeError(f"PiAPI error {code}: {self._payload_message(payload) or 'task polling failed'}")
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        raise RuntimeError("PiAPI task timed out after 6 minutes.")

    def _task_failure_message(self, data):
        error = data.get("error") if isinstance(data, dict) else None
        parts = []
        if isinstance(error, dict):
            for key in ("message", "raw_message", "detail", "code"):
                value = error.get(key)
                if value not in (None, "", 0) and str(value) not in parts:
                    parts.append(str(value))
        elif error:
            parts.append(str(error))
        detail = data.get("detail") if isinstance(data, dict) else None
        if detail and str(detail) not in parts:
            parts.append(str(detail))
        logs = data.get("logs") if isinstance(data, dict) else None
        if logs:
            log_text = logs[-1] if isinstance(logs, list) else logs
            if str(log_text) not in parts:
                parts.append(str(log_text))
        return " | ".join(parts)[:1000] or "task failed without an upstream error message"

    def _output_urls(self, output):
        if not output:
            return []
        if isinstance(output, str) and output.startswith("http"):
            return [output]
        if isinstance(output, list):
            urls = []
            for item in output:
                urls.extend(self._output_urls(item))
            return urls
        if isinstance(output, dict):
            for key in ("video_url", "image_url", "url", "href", "video", "image", "resource"):
                if output.get(key):
                    found = self._output_urls(output[key])
                    if found:
                        return found
            for key in ("works", "images", "videos"):
                if output.get(key):
                    found = self._output_urls(output[key])
                    if found:
                        return found
        return []

    def download_file(self, url, path):
        min_bytes = MIN_VIDEO_BYTES if str(path).lower().endswith(".mp4") else MIN_IMAGE_BYTES
        try:
            response = requests.get(url, timeout=120)
            if response.status_code == 200 and response.content and len(response.content) >= min_bytes:
                path.write_bytes(response.content)
                return True
        except Exception:
            pass
        return False

    async def _generate(self, prompt, kind, aspect, limit=1):
        if not self.api_key:
            print("⚠️ PiAPI key not set")
            return []
        if self.api_key.lower().startswith("r8_"):
            raise RuntimeError(
                "PiAPI HTTP 401: this looks like a Replicate token (r8_...). "
                "Create a PiAPI key at https://app.piapi.ai/"
            )
        prompt = (prompt or "").strip()
        if not prompt:
            return []
        folder = self._get_folder(prompt)
        original_model = self.model
        if kind == "image" and "flux" not in original_model.lower():
            self.model = self.DEFAULT_IMAGE_MODEL
        paths = []
        try:
            for idx in range(max(1, min(limit, 1))):
                print(f"🎬 PiAPI {kind}: {self.model} | {prompt[:80]}")
                visual = prompt if "vertical" in prompt.lower() or "9:16" in prompt else f"{prompt}, vertical 9:16, cinematic, no subtitles"
                data = await asyncio.to_thread(self._create_task, self._task_body(visual, aspect, kind))
                data = await asyncio.to_thread(self._wait, data)
                urls = self._output_urls(data.get("output"))
                if not urls:
                    print("⚠️ PiAPI returned no file URL")
                    continue
                ext = "jpg" if kind == "image" else "mp4"
                task_id = data.get("task_id") or f"{idx}_{int(time.time())}"
                path = folder / f"p_{re.sub(r'[^\w\-]', '_', str(task_id))[:40]}.{ext}"
                ok = await asyncio.to_thread(self.download_file, urls[0], path)
                if ok and path.exists() and path.stat().st_size >= (MIN_IMAGE_BYTES if kind == "image" else MIN_VIDEO_BYTES):
                    paths.append(str(path))
        except Exception as exc:
            print(f"⚠️ PiAPI generate failed: {exc}")
            raise
        finally:
            self.model = original_model
        return paths

    async def search_images(self, query, num_images=1):
        return await self._generate(query, "image", "9:16", limit=min(num_images, 1))

    async def search_videos(self, query, num_videos=1, aspect="9:16"):
        return await self._generate(query, "video", aspect or "9:16", limit=min(num_videos, 1))

class VideoDownloader:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def download_parallel(self, urls, max_count=3):
        print(f"🚀 Downloading {max_count} videos in parallel...")
        tasks = [self._dl_one(url, i) for i, url in enumerate(urls[:max_count*2])]
        res = await asyncio.gather(*tasks)
        return [r for r in res if r][:max_count]

    async def _dl_one(self, url, idx):
        ydl_opts = {
            'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best',
            'outtmpl': str(self.output_dir / f'vid_{idx}_%(id)s.%(ext)s'),
            'match_filter': yt_dlp.utils.match_filter_func('duration >= 3 & duration <= 15'),
            'quiet': True, 'ignoreerrors': True
        }
        try:
            return await asyncio.to_thread(self._run_ydl, url, ydl_opts)
        except: return None

    def _run_ydl(self, url, opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info) if info else None

# ═══════════════════════════════════════════════════════════════
# LLM PROCESSOR (Custom AI Brain Support)
# ═══════════════════════════════════════════════════════════════

class LLMProcessor:
    OPENROUTER_MODEL_ALIASES = {
        "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
        "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
        "deepseek-v3.2": "deepseek/deepseek-v3.2",
        "deepseek-v3.2-exp": "deepseek/deepseek-v3.2-exp",
        "deepseek-chat-v3.1": "deepseek/deepseek-chat-v3.1",
        "deepseek-r1": "deepseek/deepseek-r1",
        "deepseek-chat": "deepseek/deepseek-chat",
    }
    DEEPSEEK_MODEL_ALIASES = {
        "deepseek/deepseek-v4-pro": "deepseek-v4-pro",
        "deepseek/deepseek-v4-flash": "deepseek-v4-flash",
        "deepseek/deepseek-chat": "deepseek-chat",
        "deepseek/deepseek-reasoner": "deepseek-reasoner",
    }

    def __init__(self, api_key=None, api_url=None, model=None):
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.api_url = self._normalize_api_url(api_url or os.environ.get("LLM_API_URL", ""))
        self.last_error = ""
        custom_model = self._normalize_model(model or os.environ.get("LLM_MODEL", ""))
        if custom_model:
            self.models = [custom_model]
        elif self._is_deepseek_api():
            self.models = ["deepseek-v4-pro", "deepseek-chat"]
        else:
            self.models = [
                "qwen/qwen3-coder:free",
                "openai/gpt-oss-20b:free",
                "z-ai/glm-4.5-air:free",
                "meta-llama/llama-3.3-70b-instruct:free"
            ]

    def _normalize_api_url(self, api_url):
        url = (api_url or "").strip().rstrip("/")
        default = "https://openrouter.ai/api/v1/chat/completions"
        if not url:
            return default

        if "openrouter.ai" in url and not url.endswith("/chat/completions"):
            return default

        if "api.deepseek.com" in url and not url.endswith("/chat/completions"):
            return f"{url}/chat/completions"

        if url.endswith("/v1"):
            return f"{url}/chat/completions"

        return url

    def _is_deepseek_api(self):
        return "api.deepseek.com" in self.api_url

    def _is_openrouter_api(self):
        return "openrouter.ai" in self.api_url

    def _normalize_model(self, model):
        model = (model or "").strip()
        if not model:
            return ""

        if self._is_deepseek_api():
            if model in self.DEEPSEEK_MODEL_ALIASES:
                return self.DEEPSEEK_MODEL_ALIASES[model]
            if model.startswith("deepseek/"):
                return model.split("/", 1)[1]
            return model

        if self._is_openrouter_api() and model in self.OPENROUTER_MODEL_ALIASES:
            return self.OPENROUTER_MODEL_ALIASES[model]
        if self._is_openrouter_api() and "/" not in model and model.startswith("deepseek-"):
            return f"deepseek/{model}"
        return model

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://vuza.local",
            "X-Title": "VUZA Video Generator"
        }

    def _format_api_error(self, response):
        try:
            data = response.json()
            if isinstance(data, dict):
                err = data.get("error") or data.get("detail") or data
                if isinstance(err, dict):
                    return err.get("message") or json.dumps(err, ensure_ascii=False)[:300]
                return str(err)[:300]
        except Exception:
            pass
        return response.text[:300] if response.text else response.reason

    def _request_timeout(self, timeout):
        read_timeout = timeout or 40
        if self._is_deepseek_api():
            read_timeout = max(read_timeout, 180)
        return (20, read_timeout)

    def _uses_max_completion_tokens(self, model):
        name = (model or "").lower()
        return bool(re.search(r"(?:^|/)(?:gpt-5|o1|o3|o4)|terra|codex", name))

    def _chat(self, model, messages, timeout=40, max_tokens=None):
        payload = {"model": model, "messages": messages}
        if max_tokens:
            token_key = "max_completion_tokens" if self._uses_max_completion_tokens(model) else "max_tokens"
            payload[token_key] = max_tokens

        attempts = 3 if self._is_deepseek_api() else 2
        response = None
        for attempt in range(1, attempts + 1):
            try:
                response = requests.post(
                    self.api_url,
                    headers=self._headers(),
                    json=payload,
                    timeout=self._request_timeout(timeout)
                )
            except requests.Timeout as exc:
                self.last_error = (
                    f"AI API timed out (attempt {attempt}/{attempts}): {exc}. "
                    "If this keeps happening, try deepseek-chat, retry later, or switch to OpenRouter."
                )
                print(f"❌ {self.last_error}")
                if attempt < attempts:
                    time.sleep(4 * attempt)
                    continue
                return None
            except requests.ConnectionError as exc:
                self.last_error = (
                    f"Could not reach the AI API (attempt {attempt}/{attempts}): {exc}. "
                    "Check the network or proxy, then retry."
                )
                print(f"❌ {self.last_error}")
                if attempt < attempts:
                    time.sleep(4 * attempt)
                    continue
                return None
            except requests.RequestException as exc:
                self.last_error = f"Could not reach the AI API: {exc}"
                print(f"❌ LLM request failed: {exc}")
                return None

            if response.status_code in {429, 500, 502, 503, 504} and attempt < attempts:
                self.last_error = f"AI API is busy (HTTP {response.status_code}), retrying {attempt}/{attempts}..."
                print(f"⚠️ {self.last_error}")
                time.sleep(4 * attempt)
                continue
            break

        if response.status_code == 400 and max_tokens and "max_tokens" in payload:
            err_text = self._format_api_error(response)
            if "max_completion_tokens" in err_text:
                payload.pop("max_tokens", None)
                payload["max_completion_tokens"] = max_tokens
                try:
                    response = requests.post(
                        self.api_url,
                        headers=self._headers(),
                        json=payload,
                        timeout=self._request_timeout(timeout),
                    )
                except requests.RequestException as exc:
                    self.last_error = f"Could not reach the AI API: {exc}"
                    print(f"❌ {self.last_error}")
                    return None

        if response.status_code != 200:
            if response.status_code == 404:
                if self._is_deepseek_api():
                    self.last_error = (
                        f"DeepSeek URL or model was not found (HTTP 404). URL: {self.api_url}; "
                        f"model: {model}. Official DeepSeek URL: https://api.deepseek.com, "
                        "example model: deepseek-v4-pro."
                    )
                else:
                    self.last_error = (
                        f"AI URL or model was not found (HTTP 404). URL: {self.api_url}; "
                        f"model: {model}. OpenRouter URL should be https://openrouter.ai/api/v1/chat/completions, "
                        "example model: deepseek/deepseek-v4-pro."
                    )
            else:
                self.last_error = f"Model {model} failed (HTTP {response.status_code}): {self._format_api_error(response)}"
            print(f"❌ {self.last_error}")
            return None

        try:
            return response.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            self.last_error = f"AI returned an unexpected payload: {exc}"
            print(f"❌ {self.last_error}")
            return None

    def extract_keywords(self, script, vibe="aesthetic", language="", topic=""):
        if not self.api_key:
            self.last_error = "No AI API key was provided. Add one in API settings."
            print("⚠️ LLM API key not set! Please add your AI API key in settings.")
            return []

        stock_rules = """Keywords are Pinterest/Pexels/Pixabay SEARCH QUERIES.
Rules:
- Each keyword must be a concrete object, place, or action visible on camera.
- Match the VIDEO TOPIC and script subject. Gym/fitness script → gym workout, barbell squat, treadmill running, dumbbells, athlete training. Cooking script → kitchen, chef, food prep.
- NEVER use abstract mood words: resilience, warrior spirit, aspirational, transformative, empowerment, disciplined ambition, self-commitment.
- NEVER use generic 1-word dumps: exercise, fitness, athlete, people, motivation, success, sport, training.
- Keep one setting when the topic is one place (gym stays gym: no park, child, empty wall, stretching class).
- 2-4 common English words. No hashtags. No poetry.
Return format: Sentence → keyword"""
        prompts = {
            "aesthetic": f"Break the script into sentences. For each, give 1 concrete stock-footage keyword (2-4 words). You may append 'aesthetic' ONLY if the words still name a real scene (e.g. 'gym workout aesthetic'). {stock_rules}",
            "lofi": f"""Break script into sentences. For each, give 1 concrete keyword then append 'lofi art'.
Still name a real scene matching the topic (gym, rain window, coffee desk) — not abstract moods. {stock_rules}""",
            "general": f"""Break this script into sentences. For each sentence, give 1 simple stock keyword (1-3 words).
{stock_rules}""",
            "suspense_cn": """把中文悬疑短视频旁白拆成适合配画面的短句。
对每一句生成 1 个英文素材搜索关键词，必须是 Pexels/Pixabay 容易搜到的具体画面。
规则:
- 左边保留原中文旁白句子。
- 右边只写英文关键词，1-4 个词，不要中文，不要抽象词。
- 关键词要偏悬疑、夜晚、空房间、走廊、手机、门、窗、影子、雨、监控、脚步、老照片等可视化元素。
- 不要输出解释、编号、场景描述或角色名。
返回格式严格为: 中文句子 → english keyword""",
            "futuristic": "Break script into sentences. For each, give 1 futuristic/cyberpunk keyword (2-4 words, end with 'futuristic'). Return: Sentence → keyword",
            "black_and_white": "Break script into sentences. For each, give 1 noir/vintage keyword (2-4 words, end with 'black and white'). Return: Sentence → keyword"
        }
        prompt = prompts.get(vibe, prompts["aesthetic"])
        lang = (language or "").strip()
        chinese = lang.lower().startswith("zh")
        if vibe == "suspense_cn" and not chinese:
            prompt = prompts["aesthetic"] + "\nPrefer suspense visuals: night hallway, door, phone, shadow, rain, empty room."
        if lang:
            prompt += (
                f"\nNarration language: {lang}. Keep the left-side sentences in that language. "
                "Do not translate them into Chinese unless the language is Chinese."
            )
        user_content = script
        topic = (topic or "").strip()
        if topic:
            user_content = f"Video topic: {topic}\n\nScript:\n{script}"
        for m in self.models:
            print(f"🤖 LLM ({m}) | Vibe: {vibe}")
            content = self._chat(
                m,
                [{"role": "system", "content": prompt}, {"role": "user", "content": user_content}],
                timeout=120 if len(script) >= 1000 else 40,
                max_tokens=6000 if len(script) >= 1000 else 1500
            )
            if content:
                parsed = self._parse(content)
                if parsed:
                    return parsed
                self.last_error = f"AI returned content, but it was not in “sentence → keyword” format: {content[:200]}"
        return []

    def generate_viral_metadata(self, script):
        if not self.api_key:
            self.last_error = "No AI API key was provided. Add one in API settings."
            return None
        prompt = """Analyze the following video script and act as a viral YouTube expert.
Generate:
1. A viral, high-click-through-rate Title.
2. An engaging Description including a summary and relevant keywords.
3. 5-10 trending Hashtags.
4. A detailed AI Image Generation Prompt for a high-CTR thumbnail (for Midjourney/DALL-E).

Format your response exactly like this:
TITLE: [Your Title]
DESCRIPTION: [Your Description]
HASHTAGS: [Your Hashtags]
THUMBNAIL_PROMPT: [Your AI Image Prompt]"""
        for m in self.models:
            try:
                r = requests.post(self.api_url,
                                  headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                                  data=json.dumps({"model": m, "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": script}]}), timeout=30)
                if r.status_code == 200:
                    content = r.json()["choices"][0]["message"]["content"]
                    return self._parse_youtube(content)
            except: continue
        return None

    def generate_full_script(self, topic, vibe="general", language=""):
        if not self.api_key:
            self.last_error = "No AI API key was provided. Add one in API settings."
            return None
        lang = (language or "").strip() or "en-US"
        chinese = lang.lower().startswith("zh")
        if vibe == "suspense_cn" and chinese:
            is_long_source = len(topic) >= 600
            if is_long_source:
                prompt = """你是抖音中文悬疑剧情解说编剧，擅长把长篇故事改写成高留存旁白。
用户会给你一篇完整故事。请把它改写成适合自动配画面的中文旁白脚本。
目标:
- 保留原文主线，不要压缩成简介或梗概。
- 必须覆盖关键剧情节点、转折、危机场景、解法和结尾反转。
- 适合 3-6 分钟竖屏悬疑解说视频。
结构:
- 开头 1-2 句必须是强钩子。
- 中段按原文事件顺序推进，保持紧张感。
- 每个重要危机场景至少写 4-8 句，不要一句带过。
- 结尾保留原故事的余味或悬念。
格式规则:
- 输出 45-80 句中文旁白，每句独立一行。
- 每句 10-26 个汉字左右，方便一句配一个画面。
- 只输出可以直接念出来的旁白。
- 不要标题、分集标题、镜头说明、编号、项目符号、角色名标签。
- 不要写“第一章”“下一幕”“画面出现”等说明。
- 不要添加原文没有的关键设定。"""
                max_tokens = 5000
            else:
                prompt = """你是抖音中文原创悬疑剧情解说编剧。
用户会给你一个主题或悬疑点子。请写一段 30-60 秒的原创悬疑短视频旁白。
结构必须是: 3 秒钩子 -> 异常细节 -> 反转或疑点 -> 悬念结尾。
规则:
- 输出 8-12 句中文旁白，每句独立一行。
- 每句 10-24 个汉字左右，适合一句话配一个画面。
- 只写可以直接念出来的旁白，不要标题、镜头说明、角色名标签、编号。
- 氛围要克制、紧张、有画面感，避免血腥暴力和真实案件指认。
- 最后一行留下悬念，适合引导观众看下一集。"""
                max_tokens = 1200

            for m in self.models:
                content = self._chat(
                    m,
                    [{"role": "system", "content": prompt}, {"role": "user", "content": topic}],
                    timeout=90 if is_long_source else 40,
                    max_tokens=max_tokens
                )
                if content:
                    return content
            return None

        is_long_source = len(topic) >= 600
        language_line = (
            f"Write the spoken narration in {lang} only. "
            "Do not use Chinese unless the selected language is Chinese."
        )
        if vibe == "suspense_cn":
            vibe_instr = "suspenseful and tense, with a mystery hook"
            extra = "Structure: 3-second hook -> strange detail -> twist -> cliffhanger. 8-12 spoken sentences."
            if is_long_source:
                extra = "Keep the source plot. 45-80 spoken sentences. Strong hook, then scene-by-scene tension, then a cliffhanger."
        else:
            vibe_instr = "educational and informative" if vibe == "educational" else "inspiring and fast-paced" if vibe == "motivational" else "poetic and slow" if vibe == "lofi" else "engaging and viral"
            extra = "Length: 5-10 punchy sentences."
            if is_long_source:
                extra = "Keep the source plot. 45-80 spoken sentences, one scene per line."
        prompt = f"""Act as a professional viral script writer for TikTok/Reels/Shorts.
Write a complete, high-retention spoken video script about the user's topic.
The vibe should be {vibe_instr}.
{language_line}
Rules:
- {extra}
- Each sentence should be on a NEW line.
- Do NOT include scene descriptions or speaker names. ONLY the text to be spoken.
- Make it highly engaging with a strong hook at the beginning."""
        for m in self.models:
            content = self._chat(
                m,
                [{"role": "system", "content": prompt}, {"role": "user", "content": topic}],
                timeout=90 if is_long_source else 40,
                max_tokens=5000 if is_long_source else 1200,
            )
            if content:
                cleaned = self._clean_script_output(content)
                if cleaned:
                    return cleaned
        return None

    @staticmethod
    def _clean_script_output(content):
        """Remove model chatter that must never become narration or a paid media scene."""
        lines = [line.strip() for line in (content or "").replace("```text", "").replace("```", "").splitlines()]
        meta_prefixes = (
            "i can't create videos",
            "i cannot create videos",
            "i can help you craft",
            "here's a high-retention spoken video script",
            "here is a high-retention spoken video script",
        )
        while lines:
            normalized = lines[0].lower().replace("’", "'") if lines[0] else ""
            if normalized and not normalized.startswith(meta_prefixes):
                break
            lines.pop(0)
        return "\n".join(line for line in lines if line).strip()

    def _parse_youtube(self, text):
        data = {"title": "", "description": "", "hashtags": "", "thumbnail_prompt": ""}
        title_match = re.search(r'TITLE:\s*(.*)', text, re.IGNORECASE)
        desc_match = re.search(r'DESCRIPTION:\s*([\s\S]*?)(?=HASHTAGS:|$)', text, re.IGNORECASE)
        hash_match = re.search(r'HASHTAGS:\s*([\s\S]*?)(?=THUMBNAIL_PROMPT:|$)', text, re.IGNORECASE)
        thumb_match = re.search(r'THUMBNAIL_PROMPT:\s*(.*)', text, re.IGNORECASE)

        if title_match: data["title"] = title_match.group(1).strip()
        if desc_match: data["description"] = desc_match.group(1).strip()
        if hash_match: data["hashtags"] = hash_match.group(1).strip()
        if thumb_match: data["thumbnail_prompt"] = thumb_match.group(1).strip()
        return data

    def _parse(self, text):
        res = []
        for line in text.split('\n'):
            if '→' in line or '->' in line:
                arrow = '→' if '→' in line else '->'
                p = line.split(arrow, 1)
                sentence = re.sub(r'^\s*[\-\*\d\.\)\uff08\uff09、]+\s*', '', p[0]).strip()
                keyword = p[1].strip().strip('"').strip("'")
                if sentence and keyword:
                    res.append({"sentence": sentence, "keyword": keyword})
        return res

    def summarize_url(self, content):
        """Summarizes scraped web content into a video script."""
        if not self.api_key: return None
        prompt = "Act as a viral script writer. Summarize the following web content into a 5-10 sentence punchy video script for TikTok/Shorts. Return ONLY the script sentences, one per line. No scene descriptions."
        for m in self.models:
            try:
                r = requests.post(self.api_url,
                                  headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                                  data=json.dumps({"model": m, "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": content[:10000]}]}), timeout=30)
                if r.status_code == 200:
                    return r.json()["choices"][0]["message"]["content"].strip()
            except: continue
        return None

        return None

# ═══════════════════════════════════════════════════════════════
# WEB SCRAPER (FOR URL TO VIDEO)
# ═══════════════════════════════════════════════════════════════

class WebScraper:
    def __init__(self):
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    async def scrape_url(self, url):
        """Extracts text content from a URL using Playwright."""
        print(f"🌐 Scraping URL: {url}")
        content = ""
        async with get_async_playwright()() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(user_agent=self.user_agent)
            try:
                await page.goto(url, wait_until="networkidle", timeout=60000)
                # Remove script/style tags
                await page.evaluate('''() => {
                    const elements = document.querySelectorAll("script, style, nav, footer, header");
                    for (const el of elements) el.remove();
                }''')
                content = await page.evaluate('() => document.body.innerText')
                # Clean up whitespace
                content = re.sub(r'\s+', ' ', content).strip()
            except Exception as e:
                print(f"❌ Scrape Error: {e}")
            finally:
                await browser.close()
        return content
