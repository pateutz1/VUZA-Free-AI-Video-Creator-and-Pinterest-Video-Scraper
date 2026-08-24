import asyncio
import contextlib
import os
import re
import requests
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse, unquote
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

PIN_HREF_RE = re.compile(r"/pin/([^/?#]+)", re.IGNORECASE)
PROMO_PIN_RE = re.compile(
    r"\b(follow|subscribe|shop now|link in bio|promo code|discount code|#ad|click here|tiktok|instagram)\b",
    re.I,
)
CAPTION_PIN_RE = re.compile(
    r"("
    r"\d+\s*(min(?:ute)?s?|sec(?:ond)?s?)"
    r"|\d+\s*x\s*\d+"
    r"|full body"
    r"|fat burner"
    r"|tone\s*\+"
    r"|save this"
    r"|try this"
    r"|workout plan"
    r"|workout routine"
    r"|don.?t do this"
    r"|hammer curls?"
    r"|wanna get"
    r"|want to"
    r"|physique like"
    r"|your move"
    r")",
    re.I,
)
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000FE0F"
    "]+"
)


def is_text_heavy_pin(item):
    from semantic_media import UNREALISTIC_PIN_RE
    title = str((item or {}).get("title") or "")
    blob = f"{title} {str((item or {}).get('description') or '')}"
    if not blob.strip():
        return False
    if PROMO_PIN_RE.search(blob) or UNREALISTIC_PIN_RE.search(blob) or CAPTION_PIN_RE.search(blob):
        return True
    if EMOJI_RE.search(blob):
        return True
    return any(mark in title for mark in ('"', "“", "”"))


def parse_pinterest_pin_hrefs(hrefs):
    pins = []
    seen = set()
    for href in hrefs or []:
        match = PIN_HREF_RE.search(str(href or ""))
        if not match:
            continue
        pin_id = unquote(match.group(1)).strip().strip("/")
        if not pin_id or pin_id in seen:
            continue
        seen.add(pin_id)
        pins.append(f"https://www.pinterest.com/pin/{pin_id}/")
    return pins


def is_pinterest_media_url(url):
    lowered = str(url or "").lower().split("?", 1)[0]
    return lowered.endswith((".mp4", ".m4v", ".mov", ".webm", ".m3u8")) or "pinimg.com/videos" in lowered


def is_pinterest_direct_file_url(url):
    lowered = str(url or "").lower().split("?", 1)[0]
    return lowered.endswith((".mp4", ".m4v", ".mov", ".webm"))


def pinterest_mp4_urls(url):
    raw = str(url or "").split("?", 1)[0].strip()
    if not raw:
        return []
    urls = []

    def add(item):
        if item and item not in urls and is_pinterest_direct_file_url(item):
            urls.append(item)

    add(raw)
    lowered = raw.lower()
    if ".m3u8" in lowered or "/hls/" in lowered or "/ihls/" in lowered:
        converted = re.sub(r"(?i)/ihls/", "/720p/", raw)
        converted = re.sub(r"(?i)/hls/", "/720p/", converted)
        converted = re.sub(r"(?i)\.m3u8$", ".mp4", converted)
        add(converted)
        add(re.sub(r"(?i)_(t\d+|v\d+)\.mp4$", ".mp4", converted))
        for quality in ("720p", "480p", "360p"):
            add(re.sub(r"(?i)/(720p|480p|360p)/", f"/{quality}/", converted))
    # Hotlink-blocked /iht/ progressive files often also exist under /mc/ or /ml/.
    if "/iht/" in lowered or "/videos/" in lowered:
        for folder in ("mc", "ml", "iht"):
            alt = re.sub(r"(?i)/videos/(iht|mc|ml)/", f"/videos/{folder}/", raw)
            add(alt)
            for quality in ("720p", "480p", "360p"):
                add(re.sub(r"(?i)/(720p|480p|360p)/", f"/{quality}/", alt))
            add(re.sub(r"(?i)_(t\d+|v\d+)\.mp4$", ".mp4", alt))
    return urls


def pinterest_pin_page(pin_id="", source_page="", url=""):
    for raw in (source_page, url):
        text = str(raw or "").strip()
        lowered = text.lower()
        if "pinterest." in lowered and "/pin/" in lowered:
            return text.split("?", 1)[0].rstrip("/") + "/"
    pid = str(pin_id or "").strip()
    if pid and pid.lower() not in {"x", "pin", ""}:
        return f"https://www.pinterest.com/pin/{pid}/"
    return ""


def download_pinterest_with_ytdlp(pin_page, dest, min_bytes=None):
    """Download a Pinterest pin page to dest using yt-dlp (HLS/real rendition)."""
    from media_quality import MIN_VIDEO_BYTES, download_headers_for, set_download_error

    min_bytes = int(min_bytes or MIN_VIDEO_BYTES)
    dest = Path(dest)
    pin_page = str(pin_page or "").strip()
    if not pin_page:
        set_download_error("no pin page")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = download_headers_for(pin_page)
    opts = {
        "format": "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
        "outtmpl": str(dest.with_name(dest.stem + ".%(ext)s")),
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "retries": 2,
        "socket_timeout": 30,
        "http_headers": headers,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(pin_page, download=True)
            if not info:
                set_download_error("yt-dlp no info")
                return False
            produced = Path(ydl.prepare_filename(info))
    except Exception as exc:
        set_download_error(f"yt-dlp {type(exc).__name__}")
        return False
    picked = None
    for candidate in [dest, produced, *dest.parent.glob(dest.stem + ".*")]:
        try:
            if candidate.is_file() and candidate.stat().st_size >= min_bytes:
                if picked is None or candidate.stat().st_size > picked.stat().st_size:
                    picked = candidate
        except OSError:
            continue
    if picked is None:
        set_download_error("yt-dlp empty file")
        return False
    if picked.resolve() != dest.resolve():
        if dest.exists():
            dest.unlink()
        picked.replace(dest)
    if dest.suffix.lower() != ".mp4" or dest.stat().st_size < min_bytes:
        set_download_error("yt-dlp not mp4")
        return False
    return True


def collect_pinterest_video_specs(node, found=None):
    found = found if found is not None else []
    if isinstance(node, dict):
        url = str(node.get("url") or "")
        if is_pinterest_media_url(url):
            found.append(node)
        for value in node.values():
            collect_pinterest_video_specs(value, found)
    elif isinstance(node, list):
        for value in node:
            collect_pinterest_video_specs(value, found)
    return found


def pinterest_video_rendition(item):
    best_file = None
    best_key = (-1, -1)
    for spec in collect_pinterest_video_specs(item):
        if not isinstance(spec, dict):
            continue
        url = str(spec.get("url") or "")
        width = int(spec.get("width") or 0)
        height = int(spec.get("height") or 0)
        native = 1 if is_pinterest_direct_file_url(url) else 0
        cand = {
            "url": url,
            "width": width,
            "height": height,
            "duration": float(spec.get("duration") or 0),
        }
        if not native:
            converted = pinterest_mp4_urls(url)
            if not converted:
                continue
            cand["url"] = converted[0]
        key = (native, width * height)
        if key >= best_key:
            best_key = key
            best_file = cand
    return best_file


def parse_pinterest_pin_payload(data):
    payload = data.get("resource_response") if isinstance(data, dict) else None
    inner = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(inner, dict) and inner.get("id") and not (inner.get("results") or inner.get("pins")):
        return [inner]
    if isinstance(inner, dict):
        return inner.get("results") or inner.get("pins") or []
    if isinstance(inner, list):
        return inner
    return []


def parse_pinterest_resource_results(data, want_video=False):
    items = []
    for item in parse_pinterest_pin_payload(data):
        if not isinstance(item, dict):
            continue
        pin_id = str(item.get("id") or "").strip()
        if not pin_id:
            continue
        rendition = pinterest_video_rendition(item)
        if want_video and not rendition:
            continue
        source_page = f"https://www.pinterest.com/pin/{pin_id}/"
        items.append({
            "provider": "pinterest",
            "asset_id": pin_id,
            "url": (rendition or {}).get("url") or source_page,
            "source_page": source_page,
            "title": str(item.get("grid_title") or item.get("title") or "")[:200],
            "creator": ((item.get("pinner") or {}).get("username") if isinstance(item.get("pinner"), dict) else "") or "",
            "duration": float((rendition or {}).get("duration") or 0),
            "width": int((rendition or {}).get("width") or 0),
            "height": int((rendition or {}).get("height") or 0),
            "rendition": {"id": "pinterest_video" if rendition else "pin"},
        })
    return items


class PinterestScraper:
    def __init__(self, output_dir="downloads/pinterest"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        self.seen_ids = set()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._warmed = False

    def _get_folder(self, query):
        safe_query = re.sub(r'[^\w\-]', '_', query)[:25]
        folder = self.output_dir / safe_query
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    async def _ensure_page(self):
        if self._page:
            return self._page
        playwright = get_async_playwright()
        self._playwright = await playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )
        await self._context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self._page = await self._context.new_page()
        return self._page

    async def aclose(self):
        page, context, browser, playwright = self._page, self._context, self._browser, self._playwright
        self._page = self._context = self._browser = self._playwright = None
        self._warmed = False
        for closer in (page, context, browser):
            if closer is None:
                continue
            with contextlib.suppress(Exception):
                await closer.close()
        if playwright is not None:
            with contextlib.suppress(Exception):
                await playwright.stop()

    async def _warm_session(self):
        page = await self._ensure_page()
        if self._warmed:
            return page
        try:
            await page.goto("https://www.pinterest.com/", wait_until="commit", timeout=20000)
        except Exception as exc:
            print(f"⚠️ Pinterest warmup failed: {exc}")
        self._warmed = True
        return page

    async def _fetch_search_resource(self, page, query, scope, csrftoken):
        source_path = f"/search/{scope}/?q={quote(query)}"
        payload = {
            "source_url": source_path,
            "data": json.dumps({
                "options": {"query": query, "scope": scope, "page_size": 25},
                "context": {},
            }),
        }
        return await asyncio.wait_for(
            page.evaluate(
                """async ([apiUrl, fetchPayload, token]) => {
                    const headers = {
                        'accept': 'application/json, text/javascript, */*; q=0.01',
                        'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
                        'x-requested-with': 'XMLHttpRequest',
                    };
                    if (token) headers['x-csrftoken'] = token;
                    const r = await fetch(apiUrl, {
                        method: 'POST',
                        credentials: 'include',
                        headers,
                        body: new URLSearchParams(fetchPayload).toString(),
                    });
                    return [r.status, await r.text()];
                }""",
                ["https://www.pinterest.com/resource/BaseSearchResource/get/", payload, csrftoken],
            ),
            timeout=20,
        )

    async def _resource_search(self, query, scope="pins"):
        try:
            page = await self._warm_session()
            cookies = await self._context.cookies("https://www.pinterest.com")
            csrftoken = next((cookie["value"] for cookie in cookies if cookie["name"] == "csrftoken"), "")
            status, body = await self._fetch_search_resource(page, query, scope, csrftoken)
            if int(status or 0) != 200:
                print(f"⚠️ Pinterest API HTTP {status} for scope={scope}; retrying after search page")
                source_path = f"/search/{scope}/?q={quote(query)}"
                with contextlib.suppress(Exception):
                    await page.goto(f"https://www.pinterest.com{source_path}", wait_until="commit", timeout=15000)
                cookies = await self._context.cookies("https://www.pinterest.com")
                csrftoken = next((cookie["value"] for cookie in cookies if cookie["name"] == "csrftoken"), "")
                status, body = await self._fetch_search_resource(page, query, scope, csrftoken)
            if int(status or 0) != 200:
                print(f"⚠️ Pinterest API HTTP {status} for scope={scope}")
                return []
            try:
                data = json.loads(body)
            except Exception:
                print("⚠️ Pinterest API returned non-JSON")
                return []
            return parse_pinterest_resource_results(data, want_video=False)
        except Exception as exc:
            print(f"⚠️ Pinterest search failed ({scope} {query!r}): {exc}")
            return []

    async def _hydrate_pin_videos(self, pin_ids):
        pin_ids = [str(pid) for pid in pin_ids if pid][:12]
        if not pin_ids:
            return []
        try:
            page = await self._warm_session()
            cookies = await self._context.cookies("https://www.pinterest.com")
            csrftoken = next((cookie["value"] for cookie in cookies if cookie["name"] == "csrftoken"), "")
            rows = await asyncio.wait_for(
                page.evaluate(
                    """async ([ids, token]) => {
                        const out = [];
                        for (const id of ids) {
                            const payload = {
                                source_url: `/pin/${id}/`,
                                data: JSON.stringify({options: {id, field_set_key: 'unauth_react_main_pin'}, context: {}}),
                            };
                            const headers = {
                                'accept': 'application/json, text/javascript, */*; q=0.01',
                                'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
                                'x-requested-with': 'XMLHttpRequest',
                            };
                            if (token) headers['x-csrftoken'] = token;
                            try {
                                const r = await fetch('https://www.pinterest.com/resource/PinResource/get/', {
                                    method: 'POST',
                                    credentials: 'include',
                                    headers,
                                    body: new URLSearchParams(payload).toString(),
                                });
                                out.push([id, r.status, await r.text()]);
                            } catch (e) {
                                out.push([id, 0, '']);
                            }
                        }
                        return out;
                    }""",
                    [pin_ids, csrftoken],
                ),
                timeout=45,
            )
        except Exception as exc:
            print(f"⚠️ Pinterest pin hydrate failed: {exc}")
            return []
        items = []
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                continue
            status, body = row[1], row[2]
            if int(status or 0) != 200:
                continue
            try:
                data = json.loads(body)
            except Exception:
                continue
            items.extend(parse_pinterest_resource_results(data, want_video=True))
        return self._video_candidates(items)

    async def _dom_pin_urls(self, query, media_type="videos", scroll_count=5):
        page = await self._ensure_page()
        search_url = f"https://www.pinterest.com/search/{media_type}/?q={quote(query)}"
        try:
            await page.goto(search_url, wait_until="commit", timeout=15000)
            try:
                await page.wait_for_selector('a[href*="/pin/"]', timeout=15000)
            except Exception:
                pass
            for _ in range(scroll_count):
                await page.evaluate("window.scrollBy(0, 1500)")
                await asyncio.sleep(1)
            hrefs = await page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href*="/pin/"]')).map(a => a.href)"""
            )
            return parse_pinterest_pin_hrefs(hrefs)
        except Exception as exc:
            print(f"⚠️ Pinterest DOM search failed: {exc}")
            return []

    async def get_pin_urls(self, query, media_type="videos", scroll_count=5):
        print(f"🔍 Searching Pinterest {media_type}: {query}")
        scope = "videos" if media_type == "videos" else "pins"
        items = await self._resource_search(query, scope=scope)
        if not items and scope == "videos":
            items = await self._resource_search(query, scope="pins")
        pins = [item.get("source_page") for item in items if item.get("source_page")]
        if not pins:
            pins = await self._dom_pin_urls(query, media_type=media_type, scroll_count=scroll_count)
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

    def _video_candidates(self, items):
        videos = []
        for item in items or []:
            url = item.get("url") or ""
            if not is_pinterest_direct_file_url(url):
                continue
            if is_text_heavy_pin(item):
                continue
            videos.append(item)
        return videos

    async def find_videos(self, query, aspect="9:16", min_duration=2, limit=20):
        print(f"🔍 Searching Pinterest videos: {query}")
        limit = max(1, int(limit or 20))
        raw = await self._resource_search(query, scope="videos")
        items = self._video_candidates(raw)
        if len(items) < limit:
            seen = {str(item.get("asset_id") or "") for item in items}
            missing = [item.get("asset_id") for item in raw if str(item.get("asset_id") or "") not in seen]
            items.extend(await self._hydrate_pin_videos(missing))
        if len(items) < limit:
            items.extend(self._video_candidates(await self._resource_search(query, scope="pins")))
        candidates = []
        seen = set()
        for item in items:
            pin_id = str(item.get("asset_id") or "")
            if not pin_id or pin_id in seen or pin_id in self.seen_ids:
                continue
            if not is_pinterest_direct_file_url(item.get("url") or ""):
                continue
            seen.add(pin_id)
            self.seen_ids.add(pin_id)
            item = dict(item)
            item["query"] = query
            candidates.append(item)
            if len(candidates) >= limit:
                break
        print(f"📌 Pinterest candidates: {len(candidates)}")
        return candidates

    async def search_videos(self, query, num_videos=3, aspect="9:16"):
        urls = await self.get_pin_urls(query, media_type="videos", scroll_count=3)
        if not urls: return []
        folder = self._get_folder(query)
        print(f"📌 Found {len(urls)} pins, downloading via yt-dlp...")
        downloader = VideoDownloader(output_dir=folder)
        return await downloader.download_parallel(urls, max_count=num_videos)

    def download_file(self, url, path):
        try:
            from media_quality import download_headers_for, MIN_VIDEO_BYTES
            r = requests.get(url, timeout=15, headers=download_headers_for(url))
            if r.status_code == 200 and len(r.content or b"") >= MIN_VIDEO_BYTES:
                path.write_bytes(r.content)
                return True
        except Exception:
            pass
        return False

    async def download_bytes(self, url, path, min_bytes=None):
        """Fetch a pinimg URL using the warmed Playwright session cookies."""
        from media_quality import MIN_VIDEO_BYTES, download_headers_for, set_download_error

        min_bytes = int(min_bytes or MIN_VIDEO_BYTES)
        path = Path(path)
        await self._warm_session()
        if self._context is None:
            set_download_error("no pinterest session")
            return False
        try:
            resp = await self._context.request.get(
                str(url),
                headers=download_headers_for(url),
                timeout=60000,
            )
            status = int(resp.status or 0)
            if status != 200:
                set_download_error(f"HTTP {status}")
                return False
            body = await resp.body()
            if len(body or b"") < min_bytes:
                set_download_error(f"too small {len(body or b'')}B (need {min_bytes})")
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            ok = path.exists() and path.stat().st_size >= min_bytes
            if not ok:
                set_download_error("write failed")
            return ok
        except Exception as exc:
            set_download_error(f"{type(exc).__name__}")
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

    def find_videos(self, query, aspect="9:16", min_duration=2, limit=20):
        if not self.api_key:
            print("⚠️ Pexels API key not set")
            return []
        print(f"🎬 Searching Pexels: {query}")
        try:
            params = {
                "query": query,
                "per_page": 20,
                "orientation": pexels_orientation(aspect),
            }
            url = f"https://api.pexels.com/videos/search?{urlencode(params)}"
            data = requests.get(url, headers=self.headers, timeout=(30, 60), verify=True).json()
            candidates = []
            for video in data.get("videos") or []:
                vid_id = video.get("id")
                if not vid_id or vid_id in self.seen_ids:
                    continue
                duration = float(video.get("duration") or 0)
                if duration < float(min_duration or 0):
                    continue
                best = pick_pexels_rendition(video.get("video_files") or [], aspect)
                if not best or not best.get("link"):
                    continue
                width, height = int(best.get("width") or 0), int(best.get("height") or 0)
                if aspect != "1:1" and not matches_orientation(width, height, aspect):
                    continue
                self.seen_ids.add(vid_id)
                user = video.get("user") or {}
                candidates.append({
                    "provider": "pexels",
                    "asset_id": str(vid_id),
                    "url": best["link"],
                    "source_page": video.get("url") or "",
                    "creator": user.get("name") or "",
                    "duration": duration,
                    "width": width,
                    "height": height,
                    "query": query,
                    "rendition": {"id": str(best.get("id") or ""), "width": width, "height": height},
                })
                if len(candidates) >= max(1, int(limit or 20)):
                    break
            print(f"  Pexels candidates: {len(candidates)}")
            return candidates
        except Exception as exc:
            print(f"⚠️ Pexels video search failed: {exc}")
            return []

    async def search_videos(self, query, num_videos=3, aspect="9:16", min_duration=2):
        if not self.api_key: print("⚠️ Pexels API key not set"); return []
        folder = self._get_folder(query)
        items = await asyncio.to_thread(self.find_videos, query, aspect, min_duration, max(20, num_videos))
        valid_vids = [(item["url"], item["asset_id"]) for item in items[:num_videos]]
        return await download_id_files(self.download_file, valid_vids, folder, "vid", "mp4", MIN_VIDEO_BYTES)

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

    def find_videos(self, query, aspect="9:16", min_duration=2, limit=50):
        if not self.api_key:
            print("⚠️ Pixabay API key not set")
            return []
        original = (query or "").strip()
        min_seconds = float(min_duration or 0)
        cap = max(1, int(limit or 50))
        try:
            candidates = []
            for variant in short_query_fallbacks(original) or [original]:
                params = {"q": variant, "video_type": "all", "per_page": 50, "key": self.api_key}
                url = f"https://pixabay.com/api/videos/?{urlencode(params)}"
                data = requests.get(url, timeout=(30, 60), verify=True).json()
                before = len(candidates)
                for hit in data.get("hits") or []:
                    vid_id = hit.get("id")
                    if not vid_id or vid_id in self.seen_ids:
                        continue
                    duration = float(hit.get("duration") or 0)
                    if duration and duration < min_seconds:
                        continue
                    rendition, rendition_id = pick_pixabay_rendition(hit.get("videos") or {}, aspect)
                    if not rendition:
                        continue
                    width, height = int(rendition.get("width") or 0), int(rendition.get("height") or 0)
                    if aspect != "1:1" and width and height and not matches_orientation(width, height, aspect):
                        continue
                    self.seen_ids.add(vid_id)
                    candidates.append({
                        "provider": "pixabay",
                        "asset_id": str(vid_id),
                        "url": rendition.get("url") or "",
                        "source_page": hit.get("pageURL") or "",
                        "creator": hit.get("user") or "",
                        "duration": duration,
                        "width": width,
                        "height": height,
                        "query": original or variant,
                        "rendition": {"id": rendition_id, "width": width, "height": height},
                    })
                    if len(candidates) >= cap:
                        break
                if len(candidates) > before:
                    break
            print(f"  Pixabay candidates: {len(candidates)}")
            return candidates
        except Exception as exc:
            print(f"⚠️ Pixabay video search failed: {exc}")
            return []

    async def search_videos(self, query, num_videos=3, aspect="9:16", min_duration=2):
        if not self.api_key: print("⚠️ Pixabay API key not set"); return []
        folder = self._get_folder(query)
        items = await asyncio.to_thread(self.find_videos, query, aspect, min_duration, 50)
        valid = [(item["url"], item["asset_id"]) for item in items[:max(num_videos, 8)]]
        return await download_id_files(self.download_file, valid, folder, "v", "mp4", MIN_VIDEO_BYTES)

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


def matches_orientation(width, height, aspect="9:16"):
    try:
        w, h = int(float(width or 0)), int(float(height or 0))
    except (TypeError, ValueError):
        return False
    if w <= 0 or h <= 0:
        return False
    if aspect == "16:9":
        return w > h
    if aspect == "1:1":
        return True
    return h > w


def is_hd_resolution(width, height):
    try:
        w, h = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return False
    return max(w, h) >= 1080 and min(w, h) >= 720


def target_resolution(aspect="9:16"):
    if aspect == "16:9":
        return 1920, 1080
    if aspect == "1:1":
        return 1080, 1080
    return 1080, 1920


def pick_pexels_rendition(video_files, aspect="9:16"):
    files = [vf for vf in (video_files or []) if vf.get("link") and vf.get("width")]
    if not files:
        return None
    target_w, target_h = target_resolution(aspect)
    oriented = [vf for vf in files if matches_orientation(vf.get("width"), vf.get("height"), aspect)]
    pool = oriented or files
    exact = [
        vf for vf in pool
        if int(vf.get("width") or 0) == target_w and int(vf.get("height") or 0) == target_h
    ]
    if exact:
        return exact[0]
    hd = [
        vf for vf in pool
        if is_hd_resolution(vf.get("width"), vf.get("height")) and max(int(vf.get("width") or 0), int(vf.get("height") or 0)) <= 1920
    ]
    chosen = hd or [vf for vf in pool if max(int(vf.get("width") or 0), int(vf.get("height") or 0)) <= 1920] or pool
    return max(chosen, key=lambda vf: int(vf.get("width") or 0) * int(vf.get("height") or 0))


def pick_pixabay_rendition(videos, aspect="9:16"):
    if not isinstance(videos, dict):
        return None, None
    target_w, _target_h = target_resolution(aspect)
    for name in ("large", "medium", "small", "tiny"):
        item = videos.get(name)
        if not item or not item.get("url"):
            continue
        width, height = item.get("width") or 0, item.get("height") or 0
        if aspect != "1:1" and not matches_orientation(width, height, aspect):
            continue
        if int(width or 0) >= target_w or is_hd_resolution(width, height):
            return item, name
    for name, item in videos.items():
        if item and item.get("url") and (aspect == "1:1" or matches_orientation(item.get("width"), item.get("height"), aspect)):
            return item, name
    return None, None


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


def short_query_fallbacks(query):
    text = (query or "").strip()
    words = text.split()
    ordered = []
    for item in (text, " ".join(words[:2]) if len(words) > 2 else "", words[0] if len(words) > 1 else ""):
        item = (item or "").strip()
        if item and item not in ordered:
            ordered.append(item)
    return ordered


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

    @staticmethod
    def _hits_from_payload(data):
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        hits = data.get("hits") or data.get("videos")
        if hits is None and isinstance(data.get("data"), dict):
            hits = data["data"].get("hits") or data["data"].get("videos")
        return hits if isinstance(hits, list) else []

    @staticmethod
    def _clip_url(item):
        urls = item.get("urls") or {}
        if isinstance(urls, str) and urls:
            return urls
        if isinstance(urls, dict):
            for key in ("mp4_download", "mp4", "mp4_preview", "download"):
                if urls.get(key):
                    return urls[key]
        return item.get("mp4") or item.get("mp4_url") or item.get("download_url") or ""

    def _fetch_hits(self, query):
        params = {
            "query": query,
            "page_size": 20,
            "urls": "true",
            "sort": "popular",
        }
        response = requests.get(
            f"https://api.coverr.co/videos?{urlencode(params)}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=(30, 60),
            verify=True,
        )
        try:
            status = int(getattr(response, "status_code", 200) or 200)
        except (TypeError, ValueError):
            status = 200
        if status != 200:
            print(f"⚠️ Coverr HTTP {status} for {query!r}: {(getattr(response, 'text', '') or '')[:180]}")
            return []
        try:
            payload = response.json()
        except Exception:
            print(f"⚠️ Coverr returned non-JSON for {query!r}")
            return []
        hits = self._hits_from_payload(payload)
        print(f"  Coverr API {query!r}: {len(hits)} raw")
        return hits

    def find_videos(self, query, aspect="9:16", min_duration=2, limit=20):
        if not self.api_key:
            print("⚠️ Coverr API key not set")
            return []
        try:
            hits = []
            used_query = query
            for variant in short_query_fallbacks(query):
                hits = self._fetch_hits(variant)
                if hits:
                    used_query = variant
                    break
            candidates = []
            min_seconds = float(min_duration or 0)

            def vertical_rank(item):
                flag = item.get("is_vertical")
                width = item.get("max_width") or item.get("width") or 0
                height = item.get("max_height") or item.get("height") or 0
                if isinstance(flag, bool):
                    return 0 if flag else 1
                try:
                    return 0 if int(height) > int(width) > 0 else 1
                except (TypeError, ValueError):
                    return 1

            for item in sorted(hits, key=vertical_rank):
                vid_id = item.get("id") or item.get("_id") or item.get("objectID")
                if not vid_id or vid_id in self.seen_ids:
                    continue
                try:
                    duration = float(item.get("duration") or item.get("length") or item.get("video_length") or 0)
                except (TypeError, ValueError):
                    continue
                if duration and duration < min_seconds:
                    continue
                link = self._clip_url(item)
                if not link:
                    continue
                width = item.get("max_width") or item.get("width") or 0
                height = item.get("max_height") or item.get("height") or 0
                self.seen_ids.add(vid_id)
                creator = item.get("creator") or item.get("author") or {}
                creator_name = creator.get("name") if isinstance(creator, dict) else (creator or "")
                candidates.append({
                    "provider": "coverr",
                    "asset_id": str(vid_id),
                    "url": link,
                    "source_page": item.get("canonical_url") or item.get("url") or "",
                    "creator": creator_name or "",
                    "duration": duration,
                    "width": int(width or 0),
                    "height": int(height or 0),
                    "query": used_query,
                    "rendition": {"id": "mp4_download", "width": width, "height": height},
                })
                if len(candidates) >= max(1, int(limit or 20)):
                    break
            print(f"  Coverr candidates: {len(candidates)}")
            return candidates
        except Exception as exc:
            print(f"⚠️ Coverr search failed: {exc}")
            return []

    async def search_videos(self, query, num_videos=3, aspect="9:16", min_duration=2):
        if not self.api_key:
            print("⚠️ Coverr API key not set")
            return []
        folder = self._get_folder(query)
        items = await asyncio.to_thread(self.find_videos, query, aspect, min_duration, 20)
        valid = [(item["url"], item["asset_id"]) for item in items[:max(num_videos, 8)]]
        return await download_id_files(self.download_file, valid, folder, "c", "mp4", MIN_VIDEO_BYTES)

    def download_file(self, url, path):
        try:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            r = requests.get(url, headers=headers, timeout=60)
            if r.status_code == 200 and r.content and len(r.content) >= MIN_VIDEO_BYTES:
                path.write_bytes(r.content)
                return True
        except Exception:
            pass
        return False

    async def download_bytes(self, url, path, min_bytes=None):
        return await asyncio.to_thread(self.download_file, url, path)


_MIXKIT_LD_JSON_RE = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S)
_MIXKIT_ISO8601_DURATION_RE = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?')
_MIXKIT_VIDEO_ID_RE = re.compile(r'/videos/(\d+)/')
_MIXKIT_DETAIL_HREF_RE = re.compile(r'href="(/free-stock-video/[a-z0-9\-]+-\d+/)"')


def mixkit_iso8601_duration_seconds(value):
    match = _MIXKIT_ISO8601_DURATION_RE.fullmatch(value or "")
    if not match:
        return 0.0
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return float(hours * 3600 + minutes * 60 + seconds)


def mixkit_ld_json_graph(html):
    """Mixkit embeds one schema.org JSON-LD <script> per page with an @graph array
    (VideoObject / MusicRecording / Thing entries). No official API exists."""
    match = _MIXKIT_LD_JSON_RE.search(html or "")
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except (ValueError, TypeError):
        return []
    graph = data.get("@graph") if isinstance(data, dict) else data
    return graph if isinstance(graph, list) else []


class MixkitScraper:
    """Mixkit (mixkit.co) free stock video search. No API key, no login, no
    attribution required. No official API — this scrapes the schema.org
    JSON-LD block that Mixkit embeds on search and detail pages, which
    exposes a direct, hotlinkable CDN mp4 URL per clip (assets.mixkit.co)."""

    SEARCH_URL = "https://mixkit.co/free-stock-video/{slug}/"
    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VUZA/1.0"}

    def __init__(self, output_dir="downloads/mixkit", api_key=None):
        self.output_dir = Path(output_dir)
        self.seen_ids = set()

    def _get_folder(self, query):
        folder = self.output_dir / re.sub(r'[^\w\-]', '_', query)[:25]
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _fetch(self, url):
        resp = requests.get(url, timeout=(30, 60), headers=self.HEADERS)
        resp.raise_for_status()
        return resp.text

    async def search_images(self, query, num_images=5):
        return []  # Mixkit has no free stock photo library, video + music only

    def find_videos(self, query, aspect="9:16", min_duration=2, limit=20):
        limit = max(1, int(limit or 20))
        original = (query or "").strip()
        min_seconds = float(min_duration or 0)
        candidates = []

        def add_from_graph(graph, search_url):
            for item in graph:
                if len(candidates) >= limit:
                    return
                if not isinstance(item, dict) or item.get("@type") != "VideoObject":
                    continue
                content_url = item.get("contentUrl") or ""
                if not content_url:
                    continue
                id_match = _MIXKIT_VIDEO_ID_RE.search(content_url)
                vid_id = id_match.group(1) if id_match else content_url
                if vid_id in self.seen_ids:
                    continue
                duration = mixkit_iso8601_duration_seconds(item.get("duration"))
                if duration and duration < min_seconds:
                    continue
                self.seen_ids.add(vid_id)
                candidates.append({
                    "provider": "mixkit",
                    "asset_id": str(vid_id),
                    "url": content_url,
                    "source_page": item.get("@id") or search_url,
                    "creator": "Mixkit",
                    "title": item.get("name") or "",
                    "duration": duration,
                    "width": 0,
                    "height": 0,
                    "query": original or query,
                    "rendition": {"id": "720p"},
                })

        for variant in short_query_fallbacks(original) or [original]:
            slug = re.sub(r'[^\w]+', '-', variant.lower()).strip('-') or "nature"
            search_url = self.SEARCH_URL.format(slug=slug)
            try:
                html = self._fetch(search_url)
            except Exception as exc:
                print(f"⚠️ Mixkit search failed ({variant!r}): {exc}")
                continue
            before = len(candidates)
            add_from_graph(mixkit_ld_json_graph(html), search_url)
            if len(candidates) < limit:
                for href in dict.fromkeys(_MIXKIT_DETAIL_HREF_RE.findall(html)):
                    if len(candidates) >= limit:
                        break
                    try:
                        detail_html = self._fetch(f"https://mixkit.co{href}")
                    except Exception:
                        continue
                    add_from_graph(mixkit_ld_json_graph(detail_html), search_url)
            if len(candidates) > before:
                break

        print(f"  Mixkit candidates: {len(candidates)}")
        return candidates

    async def search_videos(self, query, num_videos=3, aspect="9:16", min_duration=2):
        folder = self._get_folder(query)
        items = await asyncio.to_thread(self.find_videos, query, aspect, min_duration, max(20, num_videos))
        valid = [(item["url"], item["asset_id"]) for item in items[:num_videos]]
        return await download_id_files(self.download_file, valid, folder, "mk", "mp4", MIN_VIDEO_BYTES)

    def download_file(self, url, path):
        try:
            r = requests.get(url, timeout=60, headers=self.HEADERS)
            if r.status_code == 200 and r.content and len(r.content) >= MIN_VIDEO_BYTES:
                path.write_bytes(r.content)
                return True
        except Exception:
            pass
        return False


_MIXKIT_MUSIC_ID_RE = re.compile(r'/music/(\d+)/')
MIXKIT_MUSIC_MOODS = ("cinematic", "ambient", "upbeat", "corporate", "inspirational", "lo-fi")


def mixkit_music_tracks(mood, limit=6):
    """Scrape one Mixkit free-stock-music mood page (schema.org MusicRecording
    JSON-LD, same @graph convention as the video pages) for direct mp3 URLs."""
    url = f"https://mixkit.co/free-stock-music/{mood}/"
    try:
        resp = requests.get(
            url, timeout=(30, 60),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VUZA/1.0"},
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        print(f"⚠️ Mixkit music fetch failed ({mood}): {exc}")
        return []

    tracks = []
    for item in mixkit_ld_json_graph(html):
        if not isinstance(item, dict) or item.get("@type") != "MusicRecording":
            continue
        track_url = item.get("url") or ""
        if not track_url:
            continue
        id_match = _MIXKIT_MUSIC_ID_RE.search(track_url)
        track_id = id_match.group(1) if id_match else track_url
        tracks.append({
            "id": f"mixkit-{track_id}",
            "title": item.get("name") or f"Mixkit track {track_id}",
            "artist": item.get("byArtist") or "",
            "genre": item.get("genre") or mood,
            "duration": mixkit_iso8601_duration_seconds(item.get("duration")),
            "url": track_url,
        })
        if len(tracks) >= limit:
            break
    return tracks


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

    async def download_url(self, url, media_id="pin"):
        safe_id = re.sub(r"[^\w\-]", "_", str(media_id))[:48] or "pin"
        ydl_opts = {
            'format': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best',
            'outtmpl': str(self.output_dir / f'vid_{safe_id}.%(ext)s'),
            'quiet': True, 'ignoreerrors': True
        }
        try:
            return await asyncio.to_thread(self._run_ydl, url, ydl_opts)
        except Exception:
            return None

    async def download_parallel(self, urls, max_count=3):
        print(f"🚀 Downloading {max_count} videos in parallel...")
        tasks = [self._dl_one(url, i) for i, url in enumerate(urls[:max_count])]
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

    def extract_keywords(self, script, vibe="aesthetic", language="", topic="", scenes=None, word_counts=None):
        if not self.api_key:
            self.last_error = "No AI API key was provided. Add one in API settings."
            print("⚠️ LLM API key not set! Please add your AI API key in settings.")
            return []

        stock_rules = """Keywords are Pinterest/Pexels/Pixabay/Coverr SEARCH QUERIES in narration order.
Rules:
- Follow the exact word count written in [N words] for each line.
- Mix 1-word, 2-word, 3-word, and 4-word queries. The 1-word query must be the topic's main visible subject (gym, not motivation).
- Every query must be footage of the VIDEO TOPIC setting plus a visible action from the narration.
- Never use motivation, inspiration, regret, or slogans as search words.
- No body-only closeups, on-screen text, hashtags, or vibe suffixes.
Return format strictly: Sentence → keyword"""
        if scenes:
            stock_rules = """You will receive numbered narration scenes.
Write exactly one stock-footage query per scene, same order.
Rules:
- Follow the exact word count in [N words] for that scene.
- Mix 1-word, 2-word, 3-word, and 4-word queries. The 1-word query must be the topic's main visible subject.
- Every query must show the VIDEO TOPIC setting (gym/workout if the topic is gym/fitness) plus a visible action from that scene.
- Never use motivation, inspiration, regret, or slogans as search words.
- No slogans, "person feeling", on-screen text, hashtags, or vibe suffixes.
Return format strictly: <scene text> → keyword"""
        prompts = {
            "aesthetic": f"Give concrete stock-footage queries. {stock_rules}",
            "lofi": f"Give concrete stock-footage queries naming a real scene. {stock_rules}",
            "general": f"Give concrete English stock queries. {stock_rules}",
            "suspense_cn": """把旁白场景按顺序处理。
对每一场生成 1 个英文素材搜索关键词，必须是 Pexels/Pixabay/Coverr 容易搜到的具体画面。
规则:
- 左边保留原旁白。
- 右边只写英文关键词，3-5 个词，不要中文，不要抽象词，不要 hashtag，不要 cinematic/aesthetic 后缀。
- 关键词要包含主题里的可见主体，并描述当前场景能拍到的主体、动作、环境或物体。
- 不要输出解释、编号、镜头指令或角色名。
返回格式严格为: 中文句子 → english keyword""",
            "futuristic": f"Give concrete stock queries of a visible futuristic scene. {stock_rules}",
            "black_and_white": f"Give concrete stock queries of a visible noir/vintage scene. {stock_rules}",
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
        topic = (topic or "").strip()
        if scenes:
            counts = list(word_counts or [])
            numbered = []
            for idx, block in enumerate(scenes):
                n = counts[idx] if idx < len(counts) else ((idx % 4) + 1)
                numbered.append(f"{idx + 1}. [{n} words] {block}")
            user_content = f"Video topic: {topic or script[:160]}\n\nScenes:\n" + "\n".join(numbered)
        else:
            user_content = script
            if topic:
                user_content = f"Video topic: {topic}\n\nScript:\n{script}"
        from semantic_media import fallback_stock_query, ground_query, ensure_topic_anchor, fit_keyword_length
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
                if scenes:
                    out = []
                    for idx, sentence in enumerate(scenes):
                        raw = parsed[idx]["keyword"] if parsed and idx < len(parsed) else ""
                        keyword = ensure_topic_anchor(ground_query(raw, sentence, topic), topic)
                        n = (word_counts[idx] if word_counts and idx < len(word_counts) else None)
                        if n:
                            keyword = fit_keyword_length(keyword, n, topic)
                        if not keyword:
                            keyword = fallback_stock_query(sentence, topic)
                        if keyword:
                            out.append({"sentence": sentence, "keyword": keyword})
                    if out:
                        return out
                elif parsed:
                    return parsed
                self.last_error = f"AI returned content, but it was not in “sentence → keyword” format: {content[:200]}"
        if scenes:
            out = []
            for idx, sentence in enumerate(scenes):
                n = word_counts[idx] if word_counts and idx < len(word_counts) else 3
                keyword = fit_keyword_length(
                    ensure_topic_anchor(fallback_stock_query(sentence, topic), topic),
                    n,
                    topic,
                )
                if keyword:
                    out.append({"sentence": sentence, "keyword": keyword})
            return out
        return []

    def suggest_visual_query(self, sentence, topic="", failed_queries=None):
        if not self.api_key:
            return ""
        failed = ", ".join(failed_queries or [])
        prompt = (
            "Give ONE 2-4 word English stock-footage search query for the narration sentence. "
            "Concrete visible subject/action/environment only. Include the topic's main visual anchor. "
            "No hashtags, slogans, emotions, camera instructions, or vibe suffixes."
        )
        user = f"Topic: {topic}\nSentence: {sentence}\nFailed: {failed}\nNew query:"
        for model in self.models:
            content = self._chat(
                model,
                [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
                timeout=20,
                max_tokens=40,
            )
            if content:
                from semantic_media import ensure_topic_anchor, ground_query, is_concrete_query
                query = ensure_topic_anchor(
                    ground_query(
                        content.split("\n")[0].split("→")[-1].split("->")[-1],
                        sentence,
                        topic,
                    ),
                    topic,
                )
                if is_concrete_query(query):
                    return query
        return ""

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

    def generate_full_script(self, topic, vibe="general", language="", assets_per_scene=3, clip_duration=5):
        if not self.api_key:
            self.last_error = "No AI API key was provided. Add one in API settings."
            return None
        lang = (language or "").strip() or "en-US"
        chinese = lang.lower().startswith("zh")
        count = max(1, min(15, int(assets_per_scene or 3)))
        slot = max(2, min(12, int(clip_duration or 5)))
        beat_seconds = count * slot
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
            extra = (
                f"Length: 3-6 spoken scenes. Each scene is about {beat_seconds} seconds of narration "
                f"so it covers {count} clips of {slot} seconds. Fold any CTA into the last full scene. "
                "Never put a one-line CTA on its own as the final scene."
            )
            if is_long_source:
                extra = (
                    f"Keep the source plot. 45-80 spoken sentences, one line per sentence. "
                    f"Group ideas into beats of about {beat_seconds} seconds ({count} clips × {slot}s). "
                    "Fold any CTA into the last full beat."
                )
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
        from semantic_media import parse_sentence_queries
        return parse_sentence_queries(text)

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
