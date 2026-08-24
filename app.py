import asyncio
import contextlib
import os
import re
import inspect
import json
import random
import sys
import time
import uuid
from fastapi import FastAPI, HTTPException, BackgroundTasks, File, Form, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from typing import List, Optional
from pathlib import Path
import uvicorn

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

# ═══════════════════════════════════════════════════════════════
# VUZA — Video Utility for Zero-cost Automation
# Built by Ali R. | github.com/AliRash3ed
# ═══════════════════════════════════════════════════════════════

import base64
from aesthetic_scraper import PinterestScraper, PexelsScraper, PixabayScraper, CoverrScraper, MixkitScraper, LLMProcessor, WebScraper, LLM_PROVIDER_PRESETS, pinterest_mp4_urls, pinterest_pin_page, download_pinterest_with_ytdlp, mixkit_music_tracks, mixkit_music_search, coverr_music_tracks, pixabay_music_tracks, music_match_query, MIXKIT_MUSIC_MOODS, VIBE_TO_MUSIC_MOOD
from media_quality import content_fingerprint, delete_rejected_file, download_http, last_download_error, redact_secret, reset_download_fail_logs, validate_downloaded_video, MIN_VIDEO_BYTES
from semantic_media import (
    CoverageError,
    MAX_ALT_QUERIES_PER_SCENE,
    MediaCandidate,
    SearchCache,
    coverage_failures,
    dedupe_candidates,
    ensure_topic_anchor,
    format_coverage_error,
    keyword_length_plan,
    fit_keyword_length,
    fallback_stock_query,
    query_broaden_chain,
    pinterest_query_variants,
    stock_query_plan,
    visual_query_plan,
    normalize_stock_query,
    rank_scene_candidates,
    interleave_candidates_by_query,
    interleave_candidates_by_provider,
    matches_orientation,
    search_with_cache,
    selected_record,
    unique_usable_duration,
    write_source_manifest,
    UNREALISTIC_PIN_RE,
)

app = FastAPI(title="VUZA — Free AI Video Creator")

BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

static_path = BASE_DIR / "static"
static_path.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_path)), name="static")
app.mount("/downloads", StaticFiles(directory=str(DOWNLOAD_DIR)), name="downloads")

UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

scraping_status = {
    "is_running": False, "progress": 0,
    "message": "Ready", "mode": "single", "results": [],
    "status": "idle", "final_video": None, "error": None,
    "task_id": None, "candidates": [], "review": None,
}

# Holds phase-2 inputs (keyword_data, project paths, settings) while a job is
# paused for review. Keyed by task_id; single-job app, so at most one entry.
pending_assembly = {}

# ── Models ──
class VideoSettings(BaseModel):
    ratio: str = "9:16"
    voice: str = "en-US-ChristopherNeural"
    tts_server: str = "azure-tts-v1"
    subtitles: bool = True
    language: str = "en-US"
    subtitle_style: str = "high_retention"
    music: str = "none"
    music_style: str = ""
    custom_music: str = ""
    music_query: str = ""
    filter: str = "none"
    vibe: str = "aesthetic"
    emoji_subtitles: bool = False
    watermark: bool = False
    logo_path: str = "static/logo.png"
    clip_duration: int = Field(default=5, ge=2, le=12)
    bgm_volume: float = Field(default=0.2, ge=0.0, le=1.0)
    voice_volume: float = Field(default=1.0, ge=0.0, le=2.0)
    voice_rate: float = Field(default=1.0, ge=0.5, le=2.0)
    video_count: int = Field(default=1, ge=1, le=5)
    subtitle_position: str = "bottom"
    font_size: int = Field(default=52, ge=24, le=160)
    font_name: str = "BeVietnamPro-Bold.ttf"
    text_fore_color: str = "#FFFFFF"
    stroke_color: str = "#000000"
    stroke_width: float = Field(default=3.0, ge=0.0, le=12.0)
    subtitle_background: str = "none"
    subtitle_custom_position: float = Field(default=70.0, ge=0.0, le=100.0)
    transition: str = "fade"

class ApiKeys(BaseModel):
    llm_key: str = ""
    llm_url: str = "https://openrouter.ai/api/v1/chat/completions"
    llm_model: str = ""
    pexels_key: str = ""
    pixabay_key: str = ""
    coverr_key: str = ""
    yt_client_id: str = ""
    yt_client_secret: str = ""
    eleven_key: str = ""
    azure_speech_key: str = ""
    azure_speech_region: str = ""
    sonilo_key: str = ""

class ScrapeRequest(BaseModel):
    query: Optional[str] = None
    script: Optional[str] = None
    scripts: Optional[List[str]] = None
    source: str = "pinterest"
    media_type: str = "photo"
    count: int = 3
    mode: str = "single"
    vibe: str = "aesthetic"
    video_settings: Optional[VideoSettings] = None
    auto_video: bool = True
    yt_upload: bool = False
    publish_confirmed: bool = False
    local_files: Optional[List[str]] = None
    api_keys: Optional[ApiKeys] = None
    keywords: Optional[List[str]] = None
    provider_fallback: bool = False

class AssembleRequest(BaseModel):
    task_id: str
    use_default: bool = False
    selections: Optional[List[List[str]]] = None  # per-scene ordered local file paths, len <= assets/scene

class VoicePreviewRequest(BaseModel):
    text: str = "This is a VUZA voice preview."
    voice: str = "en-US-ChristopherNeural"
    language: str = "en-US"
    voice_rate: float = 1.0
    voice_volume: float = 1.0
    eleven_key: str = ""
    tts_server: str = "azure-tts-v1"
    azure_speech_key: str = ""
    azure_speech_region: str = ""

class LlmTestRequest(BaseModel):
    provider: str = ""
    api_key: str = ""
    api_url: str = ""
    model: str = ""

class StockTestRequest(BaseModel):
    provider: str = ""
    api_key: str = ""

class AzureTtsTestRequest(BaseModel):
    api_key: str = ""
    region: str = ""

class SoniloTestRequest(BaseModel):
    api_key: str = ""

LLM_PROVIDER_MODELS = {
    "openrouter": [
        "deepseek/deepseek-v4-pro",
        "openai/gpt-4o-mini",
        "qwen/qwen3-coder:free",
        "openai/gpt-oss-20b:free",
        "meta-llama/llama-3.3-70b-instruct:free",
    ],
    "openai": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
    "deepseek": ["deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
    "groq": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    "ollama": ["llama3.2", "qwen2.5", "mistral"],
    "oneapi": ["gpt-4o-mini", "deepseek-chat"],
}

VALID_SOURCES = {"pinterest", "pexels", "pixabay", "coverr", "mixkit", "local", "round_robin"}
VALID_MEDIA_TYPES = {"photo", "video"}
VALID_MODES = {"single", "script"}
VALID_TRANSITIONS = {"none", "fade", "zoom_in", "zoom_out", "slide"}
ALLOWED_UPLOAD_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v", ".webm"}
ALLOWED_MUSIC_SUFFIXES = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"}
MUSIC_SOURCES = ("none", "random", "custom", "coverr", "mixkit", "pixabay", "sonilo")

# ── Routes ──
@app.get("/")
async def read_index():
    return FileResponse(static_path / "index.html")

@app.get("/api/status")
async def get_status(task_id: Optional[str] = None):
    if task_id and scraping_status.get("task_id") and task_id != scraping_status["task_id"]:
        return {**scraping_status, "message": "Showing the latest job; requested task_id is no longer active."}
    return scraping_status

@app.get("/api/llm/presets")
async def llm_presets():
    presets = []
    for item in LLM_PROVIDER_PRESETS:
        models = list(LLM_PROVIDER_MODELS.get(item["id"], [item["model"]]))
        if item["model"] and item["model"] not in models:
            models.insert(0, item["model"])
        presets.append({**item, "models": models})
    return {"presets": presets}

@app.post("/api/llm/test")
async def test_llm(request: LlmTestRequest):
    import requests as req
    provider = (request.provider or "").strip().lower()
    preset = next((item for item in LLM_PROVIDER_PRESETS if item["id"] == provider), None)
    api_url = (request.api_url or "").strip() or ((preset or {}).get("url") or "")
    model = (request.model or "").strip() or ((preset or {}).get("model") or "")
    api_key = (request.api_key or "").strip()
    local_url = "127.0.0.1" in api_url or "localhost" in api_url
    if not api_url or not model:
        raise HTTPException(status_code=400, detail="Provider URL and model are required.")
    if not api_key and not local_url and provider != "ollama":
        raise HTTPException(status_code=400, detail=f"Enter an API key for {preset['label'] if preset else 'this provider'}.")
    llm = LLMProcessor(api_key=api_key or "ollama", api_url=api_url, model=model)
    model_name = llm.models[0]
    payload = {"model": model_name, "messages": [{"role": "user", "content": "Reply with exactly: ok"}]}
    if llm._uses_max_completion_tokens(model_name):
        payload["max_completion_tokens"] = 8
    else:
        payload["max_tokens"] = 8

    def ping():
        return req.post(llm.api_url, headers=llm._headers(), json=payload, timeout=(8, 20))

    try:
        response = await asyncio.to_thread(ping)
    except req.RequestException as exc:
        label = (preset or {}).get("label") or provider or "API"
        raise HTTPException(status_code=400, detail=f"Could not reach {label}: {exc}") from exc

    if response.status_code != 200:
        detail = llm._format_api_error(response) or f"HTTP {response.status_code}"
        raise HTTPException(status_code=400, detail=f"{(preset or {}).get('label') or 'API'} / {model_name}: {detail[:300]}")

    reply = "ok"
    with contextlib.suppress(Exception):
        reply = str(response.json()["choices"][0]["message"]["content"]).strip() or "ok"
    print(f"✅ LLM test ok: {provider or 'custom'} / {model_name}")
    return {"ok": True, "provider": provider or "custom", "model": model_name, "reply": reply[:80]}

STOCK_TEST_LABELS = {"pexels": "Pexels", "pixabay": "Pixabay", "coverr": "Coverr"}

def stock_test_call(provider, api_key):
    if provider == "pexels":
        return (
            "https://api.pexels.com/videos/search",
            {"query": "nature", "per_page": 1},
            {"Authorization": api_key},
            "videos",
        )
    if provider == "pixabay":
        return (
            "https://pixabay.com/api/videos/",
            {"key": api_key, "q": "nature", "per_page": 3},
            {},
            "hits",
        )
    if provider == "coverr":
        return (
            "https://api.coverr.co/videos",
            {"query": "nature", "page_size": 1, "urls": "true"},
            {"Authorization": f"Bearer {api_key}"},
            "hits",
        )
    raise ValueError(provider)

def stock_test_error_text(payload):
    if not isinstance(payload, dict):
        return ""
    err = payload.get("error") or payload.get("message") or payload.get("detail")
    if isinstance(err, dict):
        err = err.get("message") or err.get("error") or ""
    return str(err or "").strip()

@app.post("/api/stock/test")
async def test_stock(request: StockTestRequest):
    import requests as req
    provider = (request.provider or "").strip().lower()
    api_key = (request.api_key or "").strip()
    label = STOCK_TEST_LABELS.get(provider)
    if not label:
        raise HTTPException(status_code=400, detail="Use Pexels, Pixabay, or Coverr.")
    if not api_key:
        raise HTTPException(status_code=400, detail=f"Enter a {label} API key first.")
    url, params, headers, count_key = stock_test_call(provider, api_key)

    def ping():
        return req.get(url, params=params, headers=headers or None, timeout=(8, 15))

    try:
        response = await asyncio.to_thread(ping)
    except req.RequestException as exc:
        raise HTTPException(status_code=400, detail=f"Could not reach {label}: {exc}") from exc

    payload = {}
    with contextlib.suppress(Exception):
        payload = response.json()
    err = stock_test_error_text(payload)
    auth_failed = response.status_code in (401, 403) or (
        "invalid" in err.lower() and "key" in err.lower()
    )
    if auth_failed:
        raise HTTPException(status_code=400, detail=f"{label} rejected this API key.")
    if response.status_code == 429:
        return {"ok": True, "provider": provider, "hits": 0, "note": "rate limited, key accepted"}
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail=f"{label}: {(err or f'HTTP {response.status_code}')[:300]}")
    hits = payload.get(count_key) if isinstance(payload, dict) else None
    if hits is None:
        raise HTTPException(status_code=400, detail=f"{label} returned an unexpected response.")
    count = len(hits) if isinstance(hits, list) else 0
    print(f"✅ Stock test ok: {provider} hits={count}")
    return {"ok": True, "provider": provider, "hits": count}

@app.post("/api/tts/azure/test")
async def test_azure_tts(request: AzureTtsTestRequest):
    import requests as req
    api_key = (request.api_key or "").strip()
    region = (request.region or "").strip()
    if not region:
        raise HTTPException(status_code=400, detail="Enter an Azure Speech region first.")
    if not api_key:
        raise HTTPException(status_code=400, detail="Enter an Azure Speech API key first.")
    url = f"https://{region}.api.cognitive.microsoft.com/sts/v1.0/issueToken"

    def ping():
        return req.post(
            url,
            headers={"Ocp-Apim-Subscription-Key": api_key, "Content-Length": "0"},
            timeout=(8, 15),
        )

    try:
        response = await asyncio.to_thread(ping)
    except req.RequestException as exc:
        raise HTTPException(status_code=400, detail=f"Could not reach Azure Speech: {exc}") from exc
    if response.status_code in (401, 403):
        raise HTTPException(status_code=400, detail="Azure Speech rejected this region or API key.")
    if response.status_code != 200 or not (response.text or "").strip():
        raise HTTPException(status_code=400, detail=f"Azure Speech: HTTP {response.status_code}")
    print(f"✅ Azure TTS V2 test ok: region={region}")
    return {"ok": True, "note": f"token issued · {region}"}

@app.post("/api/music/sonilo/test")
async def test_sonilo(request: SoniloTestRequest):
    import requests as req
    api_key = (request.api_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="Enter a Sonilo API key first.")

    def ping():
        return req.get(
            "https://api.sonilo.com/v1/account/services",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=(8, 15),
        )

    try:
        response = await asyncio.to_thread(ping)
    except req.RequestException as exc:
        raise HTTPException(status_code=400, detail=f"Could not reach Sonilo: {exc}") from exc
    if response.status_code in (401, 403):
        raise HTTPException(status_code=400, detail="Sonilo rejected this API key.")
    if response.status_code == 402:
        raise HTTPException(status_code=400, detail="Sonilo: add credits before generating music.")
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Sonilo: HTTP {response.status_code}")
    print("✅ Sonilo test ok")
    return {"ok": True, "note": "account services reachable"}

@app.get("/api/music")
async def list_music():
    files = list_local_music_files()
    return {"files": ["none"] + files, "local_files": files, "sources": list(MUSIC_SOURCES)}

AZURE_VOICES_PATH = BASE_DIR / "data" / "azure_voices.json"
_azure_voices_cache = None

def load_azure_voices():
    global _azure_voices_cache
    if _azure_voices_cache is None:
        with open(AZURE_VOICES_PATH, encoding="utf-8") as handle:
            _azure_voices_cache = json.load(handle)
    return _azure_voices_cache

def azure_voice_options(server="azure-tts-v1", language="en-US"):
    want_v2 = (server or "") == "azure-tts-v2"
    lang = (language or "en-US").strip().lower()
    rows = []
    for item in load_azure_voices():
        name = item.get("name") or ""
        gender = item.get("gender") or ""
        is_v2 = name.endswith("-V2") or "-V2" in name
        is_multi = "Multilingual" in name
        locale_ok = bool(lang) and name.lower().startswith(lang)
        if want_v2:
            if not (locale_ok or is_multi or is_v2):
                continue
        else:
            if is_v2:
                continue
            if lang and not locale_ok:
                continue
        value = f"{name}-{gender}" if gender else name
        label = f"{name.replace('Neural', '')}-{gender}".replace("--", "-") if gender else name.replace("Neural", "")
        rows.append({"value": value, "label": label, "name": name, "gender": gender})
    rows.sort(key=lambda row: (0 if row["name"].lower().startswith(lang) else 1, row["label"]))
    return rows

@app.get("/api/voices")
async def list_voices(server: str = "azure-tts-v1", language: str = "en-US"):
    if server == "elevenlabs":
        return {"voices": []}
    return {"voices": azure_voice_options(server, language)}

@app.post("/api/analyze")
async def analyze_script(request: ScrapeRequest):
    if not request.script:
        raise HTTPException(status_code=400, detail="Enter a script first.")

    api_keys = request.api_keys or ApiKeys()
    require_llm_key(api_keys, "AI title analysis")
    llm = LLMProcessor(api_key=api_keys.llm_key, api_url=api_keys.llm_url, model=api_keys.llm_model)
    analysis = llm.generate_viral_metadata(request.script)

    if not analysis:
        raise HTTPException(status_code=500, detail=llm.last_error or "Analysis failed. Check your AI API key.")

    return analysis

class GenerateScriptRequest(BaseModel):
    topic: str
    vibe: str = "aesthetic"
    language: str = "en-US"
    count: int = 3
    clip_duration: int = 5
    api_keys: Optional[ApiKeys] = None

class GenerateKeywordsRequest(BaseModel):
    topic: str
    script: str
    vibe: str = "aesthetic"
    language: str = "en-US"
    count: int = 3
    clip_duration: int = 5
    api_keys: Optional[ApiKeys] = None

def apply_user_keywords(grouped, keywords, topic=""):
    cleaned = []
    seen = set()
    for raw in keywords or []:
        keyword = ensure_topic_anchor(normalize_stock_query(raw), topic)
        key = keyword.lower()
        if not keyword or key in seen:
            continue
        seen.add(key)
        cleaned.append(keyword)
    if not cleaned or not grouped:
        return None

    def overlap(keyword, sentence):
        words = set(re.findall(r"[a-zA-Z]{3,}", (sentence or "").lower()))
        keys = set(re.findall(r"[a-zA-Z]{3,}", (keyword or "").lower()))
        return len(words & keys)

    used = set()
    mapped = []
    for item in grouped:
        sentence = item.get("sentence") or ""
        unused = [keyword for keyword in cleaned if keyword.lower() not in used]
        pool = unused or cleaned
        primary = max(pool, key=lambda keyword: (overlap(keyword, sentence), -cleaned.index(keyword)))
        used.add(primary.lower())
        mapped.append({
            "sentence": sentence,
            "keyword": primary,
            "_alts": [keyword for keyword in cleaned if keyword.lower() != primary.lower()],
        })
    return mapped


def keywords_from_topic_and_script(llm, topic, script, vibe, language, count, clip_duration):
    sentence_rows = [{"sentence": part, "keyword": "scene"} for part in split_script_sentences(script)]
    grouped = group_scenes_to_clip_budget(sentence_rows, count, clip_duration)
    scenes = [item["sentence"] for item in grouped]
    n = 4
    lengths = keyword_length_plan(n)
    padded = list(scenes)
    while len(padded) < n:
        padded.append(script)
    filled = llm.extract_keywords(
        script,
        vibe=vibe,
        language=language,
        topic=topic,
        scenes=padded,
        word_counts=lengths,
    ) if padded else []
    keywords = []
    for index in range(n):
        raw = ""
        if filled and index < len(filled):
            raw = filled[index].get("keyword") or ""
        if not raw:
            raw = fallback_stock_query(padded[index] if index < len(padded) else script, topic)
        keywords.append(fit_keyword_length(ensure_topic_anchor(raw, topic), lengths[index], topic))
    return [item for item in keywords if item]


@app.post("/api/generate_script")
async def generate_script(request: GenerateScriptRequest):
    if not request.topic:
        raise HTTPException(status_code=400, detail="Enter a topic first.")

    api_keys = request.api_keys or ApiKeys()
    require_llm_key(api_keys, "Script generation")
    llm = LLMProcessor(api_key=api_keys.llm_key, api_url=api_keys.llm_url, model=api_keys.llm_model)
    count = max(1, min(15, int(request.count or 3)))
    clip_duration = max(2, min(12, int(request.clip_duration or 5)))
    script = llm.generate_full_script(
        request.topic,
        vibe=request.vibe,
        language=request.language,
        assets_per_scene=count,
        clip_duration=clip_duration,
    )

    if not script:
        raise HTTPException(status_code=500, detail=llm.last_error or "Script generation failed. Check your AI API key.")
    keywords = keywords_from_topic_and_script(
        llm, request.topic, script, request.vibe, request.language, count, clip_duration
    )
    return {"script": script, "keywords": keywords}


@app.post("/api/generate_keywords")
async def generate_keywords(request: GenerateKeywordsRequest):
    topic = (request.topic or "").strip()
    script = (request.script or "").strip()
    if not topic:
        raise HTTPException(status_code=400, detail="Enter a topic first.")
    if not script:
        raise HTTPException(status_code=400, detail="Generate or paste a narration script first.")

    api_keys = request.api_keys or ApiKeys()
    require_llm_key(api_keys, "Keyword generation")
    llm = LLMProcessor(api_key=api_keys.llm_key, api_url=api_keys.llm_url, model=api_keys.llm_model)
    count = max(1, min(15, int(request.count or 3)))
    clip_duration = max(2, min(12, int(request.clip_duration or 5)))
    keywords = keywords_from_topic_and_script(
        llm, topic, script, request.vibe, request.language, count, clip_duration
    )
    if not keywords:
        raise HTTPException(status_code=500, detail=llm.last_error or "Keyword generation failed. Check your AI API key.")
    return {"keywords": keywords}

class ScrapeUrlRequest(BaseModel):
    url: str
    api_keys: Optional[ApiKeys] = None

@app.post("/api/scrape_url")
async def scrape_url_endpoint(request: ScrapeUrlRequest):
    if not request.url:
        raise HTTPException(status_code=400, detail="Paste a URL first.")

    api_keys = request.api_keys or ApiKeys()
    require_llm_key(api_keys, "URL summarization")

    scraper = WebScraper()
    content = await scraper.scrape_url(request.url)
    if not content:
        raise HTTPException(status_code=500, detail="Could not extract text from that URL.")

    llm = LLMProcessor(api_key=api_keys.llm_key, api_url=api_keys.llm_url, model=api_keys.llm_model)
    script = llm.summarize_url(content)

    if not script:
        raise HTTPException(status_code=500, detail="Could not summarize the URL into a script.")

    return {"script": script}

@app.post("/api/upload/material")
async def upload_material(file: UploadFile = File(...)):
    filename = sanitize_upload_filename(file.filename or "upload.bin")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {suffix or 'unknown'}.")
    dest = UPLOAD_DIR / f"{uuid.uuid4().hex}_{filename}"
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    dest.write_bytes(content)
    return {"path": dest.name, "url": "/uploads/" + dest.name}

@app.post("/api/upload/music")
async def upload_music(file: UploadFile = File(...)):
    filename = sanitize_upload_filename(file.filename or "track.mp3")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_MUSIC_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Unsupported music type: {suffix or 'unknown'}. Use MP3, M4A, AAC, WAV, FLAC, or OGG.")
    CUSTOM_MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    dest = CUSTOM_MUSIC_DIR / f"{uuid.uuid4().hex}_{filename}"
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded music file is empty.")
    dest.write_bytes(content)
    return {"path": dest.name, "name": filename}

@app.post("/api/tts/preview")
async def tts_preview(request: VoicePreviewRequest):
    text = (request.text or "").strip()[:180]
    if not text:
        raise HTTPException(status_code=400, detail="Enter preview text.")
    engine = load_video_engine()(output_dir=DOWNLOAD_DIR / "_preview")
    if request.eleven_key:
        engine.set_eleven_key(request.eleven_key)
    engine.tts_server = request.tts_server or "azure-tts-v1"
    engine.set_azure_speech(request.azure_speech_key, request.azure_speech_region)
    path = await engine.generate_voiceover(
        text,
        0,
        voice=request.voice,
        language=request.language,
        voice_rate=request.voice_rate,
        voice_volume=request.voice_volume,
        timeout_seconds=20,
    )
    if not path or not Path(path).exists():
        raise HTTPException(status_code=500, detail="Voice preview failed.")
    data = Path(path).read_bytes()
    with contextlib.suppress(OSError):
        Path(path).unlink()
    return Response(content=data, media_type="audio/mpeg")

# ── Helpers ──
def resolve_path_within_directory(base_dir, unsafe_path, require_file=True):
    if not unsafe_path:
        raise ValueError("empty path is not allowed")
    base_dir_real = os.path.realpath(base_dir)
    candidate_path = unsafe_path
    if not os.path.isabs(candidate_path):
        candidate_path = os.path.join(base_dir_real, candidate_path)
    resolved_path = os.path.realpath(candidate_path)
    try:
        common_path = os.path.commonpath([base_dir_real, resolved_path])
    except ValueError as exc:
        raise ValueError("path is outside the allowed directory") from exc
    if common_path != base_dir_real:
        raise ValueError("path is outside the allowed directory")
    if require_file and not os.path.isfile(resolved_path):
        raise ValueError("file does not exist")
    return resolved_path

def sanitize_upload_filename(filename):
    name = Path(filename or "upload.bin").name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:80] or "upload.bin"

def make_scraper(src, output_dir, api_keys=None):
    keys = api_keys or ApiKeys()
    if src == "pinterest": return PinterestScraper(output_dir=output_dir)
    if src == "mixkit": return MixkitScraper(output_dir=output_dir)
    if src == "pexels": return PexelsScraper(output_dir=output_dir, api_key=keys.pexels_key)
    if src == "pixabay": return PixabayScraper(output_dir=output_dir, api_key=keys.pixabay_key)
    if src == "coverr": return CoverrScraper(output_dir=output_dir, api_key=keys.coverr_key)
    return None

def load_video_engine():
    try:
        from video_engine import VideoEngine
    except ModuleNotFoundError as exc:
        missing = exc.name or "video dependencies"
        raise RuntimeError(f"Video assembly dependencies are missing ({missing}). Run pip install -r requirements.txt and retry.") from exc
    return VideoEngine

def load_youtube_uploader():
    try:
        from youtube_utils import YouTubeUploader
    except ModuleNotFoundError as exc:
        missing = exc.name or "YouTube upload dependencies"
        raise RuntimeError(f"YouTube upload dependencies are missing ({missing}). Run pip install -r requirements.txt and retry.") from exc
    return YouTubeUploader

def require_llm_key(api_keys, action):
    if not (api_keys.llm_key or "").strip():
        raise HTTPException(status_code=400, detail=f"{action} requires an AI text API key.")

def normalized_script_inputs(request):
    scripts = [(script or "").strip() for script in (request.scripts or [])]
    scripts = [script for script in scripts if script]
    if scripts:
        return scripts

    script = (request.script or "").strip()
    return [script] if script else []

def normalize_scrape_request_options(request):
    request.source = (request.source or "").strip().lower()
    request.media_type = (request.media_type or "").strip().lower()
    request.mode = (request.mode or "").strip().lower()

def validate_scrape_request_options(request):
    normalize_scrape_request_options(request)
    if request.source not in VALID_SOURCES:
        raise RuntimeError(f"Invalid media source: {request.source}. Choose mixkit, pexels, pixabay, coverr, pinterest, local, or round_robin.")
    if request.source == "round_robin" and not (request.mode == "script" and request.media_type == "video"):
        raise RuntimeError("Round-Robin requires script mode and video media type (it searches Mixkit, Pexels, Pixabay, and Coverr together per scene).")
    if request.media_type not in VALID_MEDIA_TYPES:
        raise RuntimeError(f"Invalid media type: {request.media_type}. Choose photo or video.")
    if request.mode not in VALID_MODES:
        raise RuntimeError(f"Invalid mode: {request.mode}. Choose single or script.")
    if request.count < 1 or request.count > 15:
        raise RuntimeError("Assets per scene must be between 1 and 15.")
    if request.source == "local":
        if not resolved_local_files(request):
            raise RuntimeError("Local source requires at least one uploaded media file.")
    elif request.mode != "script" and not (request.query or "").strip():
        raise RuntimeError("Single search requires a topic query.")
    if request.mode == "script":
        if not normalized_script_inputs(request):
            raise RuntimeError("Script mode requires at least one narration script.")
    if request.auto_video:
        settings = request.video_settings or VideoSettings()
        if (settings.voice or "").strip().lower() == "none":
            raise RuntimeError("Auto video requires a TTS voice. Turn off auto video for asset-only mode.")
        if (settings.transition or "fade") not in VALID_TRANSITIONS:
            raise RuntimeError("Invalid clip transition. Choose none, fade, zoom_in, zoom_out, or slide.")
        validate_background_music(settings, request.api_keys)
        if request.mode == "single" and request.source != "local":
            raise RuntimeError("Single stock search does not assemble a video. Switch to script mode, or turn off auto video.")
        if request.yt_upload and not request.publish_confirmed:
            raise RuntimeError("YouTube publishing requires explicit confirmation.")

def validate_script_keyword_key(request):
    if request.mode != "script" or request.source == "local":
        return
    api_keys = request.api_keys or ApiKeys()
    if not (api_keys.llm_key or "").strip():
        raise RuntimeError("Script mode with Pinterest/Pexels/Pixabay/Coverr requires an AI text API key to split narration into search keywords.")

def validate_request_api_dependencies(request):
    validate_script_keyword_key(request)

def local_script_segments(script):
    """Split a Chinese narration script into stable scene rows without calling an LLM."""
    cleaned = (script or "").replace("\r", "\n").strip()
    rows = []
    for raw_line in cleaned.split("\n"):
        line = re.sub(r'^\s*[\-\*\d\.\)\uff08\uff09、]+\s*', '', raw_line).strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r'(?<=[。！？!?；;])\s*', line) if p.strip()]
        if len(parts) > 1:
            rows.extend(parts)
            continue
        if len(line) <= 42:
            rows.append(line)
            continue
        rows.extend(parts)

    if not rows:
        rows = [p.strip() for p in re.split(r'(?<=[。！？!?；;])\s*', cleaned) if p.strip()]

    return [
        {"sentence": sentence, "keyword": f"scene_{idx + 1:03d}"}
        for idx, sentence in enumerate(rows)
    ]

SPEECH_WORDS_PER_SEC = 2.5
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def split_script_sentences(script):
    text = re.sub(r"\s+", " ", (script or "").strip())
    if not text:
        return []
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]


def estimated_speech_seconds(text):
    words = re.findall(r"[A-Za-z0-9']+", text or "")
    cjk = re.findall(r"[\u4e00-\u9fff]", text or "")
    return (len(words) / SPEECH_WORDS_PER_SEC) + (len(cjk) / 4.5)


def group_scenes_to_clip_budget(keyword_data, count, clip_duration):
    """Merge consecutive sentences so one scene fills assets × clip length."""
    budget = max(2.0, float(count or 1) * float(clip_duration or 5))
    grouped = []
    bucket = None
    spoken = 0.0
    for item in keyword_data or []:
        sentence = (item.get("sentence") or "").strip()
        keyword = (item.get("keyword") or "").strip()
        if not sentence:
            continue
        extra = estimated_speech_seconds(sentence)
        incoming_files = list(item.get("_files") or [])
        if bucket is None:
            bucket = {"sentence": sentence, "keyword": keyword or "scene"}
            if incoming_files:
                bucket["_files"] = incoming_files
            spoken = extra
            continue
        if spoken < budget:
            bucket["sentence"] = f"{bucket['sentence']} {sentence}".strip()
            if incoming_files:
                bucket["_files"] = list(bucket.get("_files") or []) + incoming_files
            spoken += extra
            continue
        grouped.append(bucket)
        bucket = {"sentence": sentence, "keyword": keyword or "scene"}
        spoken = extra
    if bucket:
        grouped.append(bucket)
    if len(grouped) >= 2:
        last_spoken = estimated_speech_seconds(grouped[-1].get("sentence") or "")
        if last_spoken < budget * 0.5:
            last = grouped.pop()
            prev = grouped[-1]
            prev["sentence"] = f"{prev['sentence']} {last['sentence']}".strip()
            if last.get("_files"):
                prev["_files"] = list(prev.get("_files") or []) + list(last["_files"])
    return grouped


def resolved_local_files(request):
    names = [name for name in (request.local_files or []) if (name or "").strip()]
    resolved = []
    for name in names:
        try:
            resolved.append(resolve_path_within_directory(str(UPLOAD_DIR), Path(name).name))
        except ValueError:
            raise RuntimeError(f"Local upload is invalid or outside the uploads folder: {Path(name).name}.")
    return resolved

_UNSET = object()

def normalize_status_progress(progress):
    try:
        value = round(float(progress))
    except (TypeError, ValueError):
        return 0
    return min(100, max(0, int(value)))

def set_status(status=None, message=None, progress=None, error=_UNSET, final_video=_UNSET, **extra):
    if status:
        scraping_status["status"] = status
        # awaiting_review pauses the job (not finished, not actively working) — keep
        # is_running true so the UI keeps polling and a new job can't clobber it.
        scraping_status["is_running"] = status in ("running", "awaiting_review")
    if message is not None:
        scraping_status["message"] = message
    if progress is not None:
        scraping_status["progress"] = normalize_status_progress(progress)
    if error is not _UNSET:
        scraping_status["error"] = error
    if final_video is not _UNSET:
        scraping_status["final_video"] = final_video
    if extra:
        scraping_status.update(extra)

def relative_download_path(path):
    return "/" + str(Path(path).relative_to(BASE_DIR)).replace("\\", "/")

def safe_scene_folder(project_path, keyword):
    safe_keyword = re.sub(r'[^\w\-]', '_', keyword)[:40] or "scene"
    return project_path / safe_keyword

def describe_scene_media_error(error):
    if not error:
        return "no media was generated or downloaded"
    return str(error) or error.__class__.__name__

def is_fatal_scene_media_error(message):
    text = (message or "").lower()
    return (
        "http 401" in text
        or "http 402" in text
        or "failed to verify" in text
        or "unauthorized" in text
        or "insufficient credit" in text
        or "no credit" in text
        or "replicate token" in text
    )

def describe_empty_media_result(source, media_type):
    media_label = "video" if media_type == "video" else "image"
    return f"{source} found no usable {media_label} assets"

def validate_scene_images(keyword_data, project_path):
    missing = []
    for idx, item in enumerate(keyword_data, start=1):
        explicit_files = [
            Path(f) for f in item.get("_files", [])
            if Path(f).exists() and Path(f).stat().st_size > 0
        ]
        folder = safe_scene_folder(project_path, item["keyword"])
        folder_files = [
            f for f in folder.glob("*")
            if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v", ".webm"} and f.stat().st_size > 0
        ] if folder.exists() else []
        files = explicit_files or folder_files
        if not files:
            reason = describe_scene_media_error(item.get("_error"))
            missing.append(f"scene {idx} ({item['keyword']}): {reason}")
    if missing:
        raise RuntimeError(f"Scene media is incomplete: expected {len(keyword_data)} scenes, missing {len(missing)}: {'; '.join(missing[:5])}")

def validate_tts_files(engine, scene_count):
    missing = []
    for idx in range(scene_count):
        path = engine.temp_dir / f"speech_{idx}.mp3"
        if not path.exists() or path.stat().st_size <= 0:
            missing.append(idx + 1)
    if missing:
        raise RuntimeError(f"TTS files are incomplete: expected {scene_count}, missing or empty {len(missing)}: {missing[:8]}")

def validate_final_video(video_file):
    if not video_file:
        raise RuntimeError("Video assembly failed: create_video did not return an mp4 path.")
    video_path = Path(video_file)
    if video_path.suffix.lower() != ".mp4" or not video_path.exists() or video_path.stat().st_size <= 0:
        raise RuntimeError(f"Video assembly failed: returned mp4 is missing or empty: {video_file}")
    return video_path

BROAD_STOCK_TERMS = {
    "exercise", "fitness", "athlete", "sport", "sports", "training", "people",
    "person", "man", "woman", "action", "power", "motivation", "success",
    "health", "body", "life", "strong", "strength", "energy", "workout",
}

def file_fingerprint(path):
    return content_fingerprint(path)

def keep_visually_clean_media(files, seen_hashes, sentence="", keyword="", limit=3):
    return pick_unique_media(files, seen_hashes, sentence=sentence, keyword=keyword, limit=limit)

def stock_keyword_variants(keyword):
    variants = []
    raw = (keyword or "").strip()
    if raw:
        variants.append(raw)
    simple = raw.replace(" aesthetic", "").replace(" lofi art", "").replace(" futuristic", "").replace(" black and white", "").strip()
    if simple and simple not in variants:
        variants.append(simple)
    words = [w for w in simple.split() if w]
    if len(words) >= 2:
        pair = " ".join(words[:2])
        if pair not in variants:
            variants.append(pair)
        if words[0].lower() not in BROAD_STOCK_TERMS and words[0] not in variants:
            variants.append(words[0])
    return variants

def sentence_clip_score(path, sentence, keyword=""):
    stop = {"the", "and", "for", "you", "your", "are", "this", "that", "with", "from", "have", "will", "just"}
    words = {w.lower() for w in re.findall(r"[a-zA-Z]{3,}", f"{sentence} {keyword}") if w.lower() not in stop}
    name = f"{Path(path).stem} {Path(path).parent.name} {keyword}".lower().replace("_", " ")
    overlap = sum(1 for w in words if w in name)
    score = overlap * 3
    try:
        size = Path(path).stat().st_size
    except OSError:
        return -1
    suffix = Path(path).suffix.lower()
    min_bytes = 40000 if suffix in {".mp4", ".mov", ".m4v", ".webm"} else 8000
    if size < min_bytes:
        return -1
    score += min(size / 500000, 4)
    return score

def pick_unique_media(files, seen_hashes, sentence="", keyword="", limit=3):
    ranked = []
    for file in files or []:
        path = Path(file)
        suffix = path.suffix.lower()
        min_bytes = 40000 if suffix in {".mp4", ".mov", ".m4v", ".webm"} else 8000
        try:
            if not path.is_file() or path.stat().st_size < min_bytes:
                continue
            fingerprint = file_fingerprint(path)
        except OSError:
            continue
        if fingerprint in seen_hashes:
            continue
        score = sentence_clip_score(path, sentence, keyword)
        if score < 0:
            continue
        ranked.append((score, fingerprint, str(path)))
    ranked.sort(key=lambda item: item[0], reverse=True)
    chosen = []
    for score, fingerprint, path in ranked[:limit]:
        seen_hashes.add(fingerprint)
        chosen.append(path)
    return chosen

def existing_media_paths(files):
    valid = []
    for file in files or []:
        if not file:
            continue
        path = Path(file)
        try:
            if path.is_file() and path.stat().st_size > 0:
                valid.append(path)
        except OSError:
            continue
    return valid


def keyword_data_result_rows(keyword_data):
    rows = []
    for item in keyword_data or []:
        rel_paths = []
        for path in existing_media_paths(item.get("_files")):
            try:
                rel_paths.append(relative_download_path(path))
            except Exception:
                rel_paths.append(str(path).replace("\\", "/"))
        if rel_paths:
            rows.append({
                "keyword": item.get("keyword") or "scene",
                "sentence": item.get("sentence") or "",
                "files": rel_paths,
            })
    return rows

def require_media_files(files, label):
    valid = existing_media_paths(files)
    if not valid:
        raise RuntimeError(f"No usable media found for: {label}. Try another keyword or source, or check the stock API key/network.")
    return valid

MIXKIT_MUSIC_CACHE_TTL = 6 * 3600  # seconds
MIXKIT_MUSIC_TRACKS_PER_MOOD = 6
MIXKIT_MUSIC_DOWNLOAD_DIR = DOWNLOAD_DIR / "_mixkit_music"
COVERR_MUSIC_DOWNLOAD_DIR = DOWNLOAD_DIR / "_coverr_music"
PIXABAY_MUSIC_DOWNLOAD_DIR = DOWNLOAD_DIR / "_pixabay_music"
SONILO_MUSIC_DOWNLOAD_DIR = DOWNLOAD_DIR / "_sonilo_music"
CUSTOM_MUSIC_DIR = DOWNLOAD_DIR / "_custom_music"
_mixkit_music_cache = {"tracks": [], "fetched_at": 0.0}


def list_local_music_files():
    music_dir = BASE_DIR / "static" / "music"
    if not music_dir.exists():
        return []
    return sorted(
        p.name for p in music_dir.iterdir()
        if p.suffix.lower() in ALLOWED_MUSIC_SUFFIXES and p.stat().st_size > 0
    )


def music_source_kind(settings):
    music = (settings.music or "none").strip()
    lower = music.lower()
    if not lower or lower == "none":
        return "none"
    if lower in MUSIC_SOURCES:
        return lower
    if music.startswith("mixkit-"):
        return "mixkit-track"
    return "file"


def mixkit_music_catalog(force=False):
    """In-memory cached catalog of Mixkit free-stock-music tracks (video+music
    only provider; scraped, no API key). Refetched at most every MIXKIT_MUSIC_CACHE_TTL."""
    now = time.time()
    if not force and _mixkit_music_cache["tracks"] and (now - _mixkit_music_cache["fetched_at"]) < MIXKIT_MUSIC_CACHE_TTL:
        return _mixkit_music_cache["tracks"]
    tracks = []
    seen_ids = set()
    for mood in MIXKIT_MUSIC_MOODS:
        for track in mixkit_music_tracks(mood, limit=MIXKIT_MUSIC_TRACKS_PER_MOOD):
            if track["id"] in seen_ids:
                continue
            seen_ids.add(track["id"])
            tracks.append(track)
    if tracks:
        _mixkit_music_cache["tracks"] = tracks
        _mixkit_music_cache["fetched_at"] = now
    return tracks or _mixkit_music_cache["tracks"]


def resolve_mixkit_music_file(track_id):
    numeric_id = track_id.split("-", 1)[1] if "-" in track_id else track_id
    safe_id = re.sub(r"[^\w\-]", "_", numeric_id) or "track"
    cache_path = MIXKIT_MUSIC_DOWNLOAD_DIR / f"{safe_id}.mp3"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return str(cache_path)

    track = next((t for t in mixkit_music_catalog() if t["id"] == track_id), None)
    if not track:
        raise RuntimeError(f"Mixkit music track '{track_id}' is no longer available. Choose another track or “No music”.")
    MIXKIT_MUSIC_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ok = download_http(track["url"], cache_path, 60, 20000, True)
    if not ok or not cache_path.exists() or cache_path.stat().st_size <= 0:
        raise RuntimeError(f"Could not download Mixkit music track '{track.get('title') or track_id}'. Choose another track or “No music”.")
    return str(cache_path)


def resolve_local_music_file(filename):
    if Path(filename).name != filename:
        raise RuntimeError("Background music filename is invalid. Choose a track from the dropdown.")
    music_path = BASE_DIR / "static" / "music" / filename
    if not music_path.exists() or music_path.stat().st_size <= 0:
        raise RuntimeError(f"Background music file is missing or empty: static/music/{filename}. Choose “No music” or add the file.")
    return str(music_path)


def resolve_custom_music_file(value):
    raw = (value or "").strip().strip('"')
    if not raw:
        raise RuntimeError("Custom background music needs an uploaded file or a local path.")
    suffix = Path(raw).suffix.lower()
    if suffix not in ALLOWED_MUSIC_SUFFIXES:
        raise RuntimeError("Custom background music must be MP3, M4A, AAC, WAV, FLAC, or OGG.")
    name = Path(raw).name
    candidates = [CUSTOM_MUSIC_DIR / name, BASE_DIR / "static" / "music" / name, Path(raw)]
    for candidate in candidates:
        try:
            resolved = Path(os.path.realpath(str(candidate)))
        except OSError:
            continue
        if resolved.is_file() and resolved.stat().st_size > 0 and resolved.suffix.lower() in ALLOWED_MUSIC_SUFFIXES:
            return str(resolved)
    raise RuntimeError("Custom background music file is missing or empty. Upload a track or enter a valid path.")


def download_matched_track(tracks, cache_dir, prefix, label):
    if not tracks:
        raise RuntimeError(f"No {label} tracks matched this video. Try another style or source.")
    shuffled = list(tracks)
    random.shuffle(shuffled)
    cache_dir.mkdir(parents=True, exist_ok=True)
    for track in shuffled[:8]:
        url = track.get("url") or ""
        if not url:
            continue
        tid = re.sub(r"[^\w\-]", "_", str(track.get("id") or "track"))[:48] or "track"
        ext = Path(url.split("?", 1)[0]).suffix.lower()
        if ext not in ALLOWED_MUSIC_SUFFIXES:
            ext = ".mp3"
        dest = cache_dir / f"{prefix}_{tid}{ext}"
        if dest.exists() and dest.stat().st_size > 0:
            return str(dest)
        ok = download_http(url, dest, 60, 8000, True)
        if ok and dest.exists() and dest.stat().st_size > 0:
            return str(dest)
    raise RuntimeError(f"Could not download {label} music. Try another style or source.")


def resolve_sonilo_music(settings, api_keys, duration_seconds):
    import requests as req
    key = ((api_keys.sonilo_key if api_keys else "") or "").strip()
    if not key:
        raise RuntimeError("Sonilo AI needs an API key. Add it in API settings.")
    duration = max(15, min(120, int(duration_seconds or 45)))
    query = music_match_query(
        vibe=getattr(settings, "vibe", "") or "",
        style=getattr(settings, "music_style", "") or "",
        topic=getattr(settings, "music_query", "") or "",
    )
    prompt = (
        f"{duration} seconds of instrumental background music, no vocals. "
        f"Style: {query}. Licensed bed for a short video."
    )
    digest = hashlib_sha1(f"{prompt}|{duration}")
    dest = SONILO_MUSIC_DOWNLOAD_DIR / f"sonilo_{digest}.m4a"
    if dest.exists() and dest.stat().st_size > 0:
        return str(dest)
    SONILO_MUSIC_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {key}"}
    files = {"prompt": (None, prompt), "duration": (None, str(duration))}
    response = req.post(
        "https://api.sonilo.com/v1/text-to-music",
        headers=headers,
        files=files,
        stream=True,
        timeout=300,
    )
    if response.status_code in (401, 403):
        raise RuntimeError("Sonilo rejected this API key.")
    if response.status_code == 402:
        raise RuntimeError("Sonilo: add credits before generating music.")
    if response.status_code != 200:
        detail = (response.text or "")[:180]
        raise RuntimeError(f"Sonilo music failed: HTTP {response.status_code} {detail}".strip())
    chunks = []
    complete = False
    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue
        event = json.loads(line)
        kind = event.get("type")
        if kind == "audio_chunk":
            chunks.append(base64.b64decode(event.get("data") or ""))
        elif kind == "complete":
            complete = True
            break
        elif kind == "error":
            raise RuntimeError(event.get("message") or "Sonilo generation failed.")
    if not complete or not chunks:
        raise RuntimeError("Sonilo returned no audio. Try again or pick another music source.")
    dest.write_bytes(b"".join(chunks))
    return str(dest)


def hashlib_sha1(text):
    import hashlib
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def validate_background_music(settings, api_keys=None):
    kind = music_source_kind(settings)
    keys = api_keys or ApiKeys()
    if kind == "none":
        return
    if kind == "random":
        if not list_local_music_files():
            raise RuntimeError("Random background music needs at least one file in static/music.")
        return
    if kind == "custom":
        resolve_custom_music_file(getattr(settings, "custom_music", "") or "")
        return
    if kind == "pixabay" and not (keys.pixabay_key or "").strip():
        raise RuntimeError("Pixabay music needs a Pixabay API key. Add it in API settings.")
    if kind == "sonilo" and not (keys.sonilo_key or "").strip():
        raise RuntimeError("Sonilo AI needs an API key. Add it in API settings.")
    if kind == "file":
        resolve_local_music_file((settings.music or "").strip())
        return
    if kind == "mixkit-track":
        return
    if kind not in MUSIC_SOURCES:
        raise RuntimeError("Invalid background music source.")


def resolve_background_music(settings, api_keys=None, vibe="", duration_seconds=45):
    kind = music_source_kind(settings)
    keys = api_keys or ApiKeys()
    if vibe:
        settings.vibe = vibe
    if kind == "none":
        return None
    if kind == "random":
        files = list_local_music_files()
        if not files:
            raise RuntimeError("Random background music needs at least one file in static/music.")
        return str(BASE_DIR / "static" / "music" / random.choice(files))
    if kind == "custom":
        return resolve_custom_music_file(getattr(settings, "custom_music", "") or "")
    if kind == "mixkit-track":
        return resolve_mixkit_music_file((settings.music or "").strip())
    if kind == "file":
        return resolve_local_music_file((settings.music or "").strip())
    query = music_match_query(
        vibe=vibe or getattr(settings, "vibe", "") or "",
        style=getattr(settings, "music_style", "") or "",
        topic=getattr(settings, "music_query", "") or "",
    )
    if kind == "mixkit":
        style = (getattr(settings, "music_style", "") or "").strip()
        mood = VIBE_TO_MUSIC_MOOD.get((vibe or getattr(settings, "vibe", "") or "").strip(), "cinematic")
        tracks = mixkit_music_search(style or mood, limit=8)
        return download_matched_track(tracks, MIXKIT_MUSIC_DOWNLOAD_DIR, "mk", "Mixkit")
    if kind == "coverr":
        tracks = coverr_music_tracks(query, api_key=(keys.coverr_key or "").strip(), limit=8)
        return download_matched_track(tracks, COVERR_MUSIC_DOWNLOAD_DIR, "cv", "Coverr")
    if kind == "pixabay":
        if not (keys.pixabay_key or "").strip():
            raise RuntimeError("Pixabay music needs a Pixabay API key. Add it in API settings.")
        tracks = pixabay_music_tracks(query, keys.pixabay_key, limit=8)
        return download_matched_track(tracks, PIXABAY_MUSIC_DOWNLOAD_DIR, "px", "Pixabay")
    if kind == "sonilo":
        return resolve_sonilo_music(settings, keys, duration_seconds)
    raise RuntimeError("Invalid background music source.")


async def try_search(scraper, keyword, media_type, count, aspect="9:16"):
    if not scraper:
        return []
    try:
        if media_type == "video":
            if hasattr(scraper, "search_videos"):
                return await scraper.search_videos(keyword, num_videos=count, aspect=aspect)
            return []
        if hasattr(scraper, "search_images"):
            return await scraper.search_images(keyword, num_images=count)
        return []
    except Exception:
        return []

async def universal_search(keyword, media_type, count, primary_source, project_path, api_keys=None, vibe="aesthetic", sentence="", llm=None, aspect="9:16", local_files=None, seen_hashes=None, enable_fallback=False):
    if primary_source == "local":
        return [str(path) for path in (local_files or [])]

    keywords = stock_keyword_variants(keyword)
    seen_hashes = seen_hashes if seen_hashes is not None else set()
    providers = stock_fallback_providers(primary_source, enable_fallback)
    ready = []
    for provider in providers:
        if provider == "coverr" and media_type != "video":
            continue
        if stock_provider_ready(provider, api_keys):
            ready.append(provider)
        elif enable_fallback:
            print(f"  ⏭️ skip fallback {provider}: missing API key")
    if not ready:
        ready = [primary_source]

    collected = []
    for provider in ready:
        if provider == "coverr" and media_type != "video":
            continue
        scraper = make_scraper(provider, project_path, api_keys)
        if scraper is None:
            continue
        if collected and provider != primary_source:
            print(f"↪️ Fallback search → {provider}")
        for k in keywords:
            remaining = max(0, count - len(collected))
            if remaining == 0:
                return collected[:count]
            res = await try_search(scraper, k, media_type, remaining, aspect=aspect)
            picked = keep_visually_clean_media(res, seen_hashes, sentence=sentence, keyword=k, limit=remaining)
            if picked:
                print(f"  ✅ [{provider}:{k}] kept {len(picked)} unique clips")
                collected.extend(picked)
                if len(collected) >= count:
                    return collected[:count]

        if len(collected) >= count:
            return collected[:count]

        if provider == ready[-1] and llm and sentence and llm.api_key and len(collected) < count:
            print(f"  🧠 AI Re-Ask for '{keyword}'...")
            try:
                new_kw = llm.suggest_visual_query(sentence, topic=keyword, failed_queries=keywords)
                if not new_kw:
                    import requests as req
                    r = req.post(llm.api_url,
                        headers={"Authorization": f"Bearer {llm.api_key}", "Content-Type": "application/json"},
                        data=json.dumps({
                            "model": llm.models[0],
                            "messages": [
                                {"role": "system", "content": "Give ONE 2-3 word stock-footage search query that matches the sentence visually. Concrete objects/actions only."},
                                {"role": "user", "content": f"Sentence: {sentence}\nFailed: {', '.join(keywords)}\nNew keyword:"}
                            ]
                        }), timeout=15)
                    if r.status_code == 200:
                        new_kw = r.json()["choices"][0]["message"]["content"].strip().strip('"').strip("'").lower()
                if new_kw:
                    print(f"  🆕 AI suggested: '{new_kw}'")
                    remaining = max(1, count - len(collected))
                    res = await try_search(scraper, new_kw, media_type, remaining, aspect=aspect)
                    picked = keep_visually_clean_media(res, seen_hashes, sentence=sentence, keyword=new_kw, limit=remaining)
                    if picked:
                        collected.extend(picked)
                        return collected[:count]
            except Exception as e:
                print(f"  ⚠️ AI Re-Ask failed: {redact_secret(e)}")

    return collected

STOCK_VIDEO_SOURCES = {"pexels", "pixabay", "coverr", "mixkit", "pinterest", "round_robin"}
STOCK_ALL_PROVIDERS = ("mixkit", "pexels", "pixabay", "coverr")
SPARE_CLIPS_PER_SCENE = 2
STOCK_FALLBACK_CHAINS = {
    "mixkit": ["pexels", "pixabay", "coverr"],
    "pinterest": ["pexels", "pixabay", "coverr"],
    "pexels": ["mixkit", "pixabay", "coverr"],
    "pixabay": ["pexels", "mixkit", "coverr"],
    "coverr": ["pexels", "pixabay", "mixkit"],
}
_SEARCH_CACHE = None


def stock_fallback_providers(primary, enable_fallback=False):
    primary = (primary or "").strip().lower()
    if primary not in STOCK_VIDEO_SOURCES:
        return [primary] if primary else []
    if not enable_fallback:
        return [primary]
    return [primary] + [p for p in STOCK_FALLBACK_CHAINS.get(primary, []) if p != primary]


def stock_provider_ready(provider, api_keys=None):
    keys = api_keys or ApiKeys()
    provider = (provider or "").strip().lower()
    if provider == "round_robin":
        return True
    if provider == "pinterest":
        return True
    if provider == "mixkit":
        return True
    if provider == "pexels":
        return bool((keys.pexels_key or "").strip())
    if provider == "pixabay":
        return bool((keys.pixabay_key or "").strip())
    if provider == "coverr":
        return bool((keys.coverr_key or "").strip())
    return False


def resolve_stock_providers(source, api_keys=None):
    """Round-Robin blends every ready stock provider; otherwise just the one picked source."""
    if source == "round_robin":
        ready = [p for p in STOCK_ALL_PROVIDERS if stock_provider_ready(p, api_keys)]
        return ready or list(STOCK_ALL_PROVIDERS)
    return [source]


def seed_selected_from_keyword_data(keyword_data, clip_duration):
    selected = [[] for _ in keyword_data]
    taken_keys = set()
    for idx, item in enumerate(keyword_data or []):
        for candidate in item.get("_candidates") or []:
            if not isinstance(candidate, MediaCandidate) or not candidate.local_path:
                continue
            if not Path(candidate.local_path).is_file():
                continue
            selected[idx].append(candidate)
            for key in candidate.identity_keys():
                taken_keys.add(key)
        if selected[idx]:
            continue
        for path in existing_media_paths(item.get("_files")):
            stub = MediaCandidate(
                provider=str(getattr(item, "provider", "") or path.parent.name),
                asset_id=path.stem,
                url="",
                local_path=str(path),
                duration=float(clip_duration or 5),
                query=item.get("keyword") or "",
                scene_index=idx,
            )
            selected[idx].append(stub)
            for key in stub.identity_keys():
                taken_keys.add(key)
    return selected, taken_keys


def scene_review_entry(candidate_or_path, fallback_id=""):
    if isinstance(candidate_or_path, MediaCandidate):
        path = candidate_or_path.local_path
        return {
            "path": path,
            "url": relative_download_path(path) if path else "",
            "provider": candidate_or_path.provider,
            "asset_id": candidate_or_path.asset_id,
        }
    path = str(candidate_or_path)
    return {"path": path, "url": relative_download_path(path), "provider": "", "asset_id": fallback_id or Path(path).stem}


def build_scene_review(keyword_data, count):
    scenes = []
    for idx, item in enumerate(keyword_data):
        selected = [scene_review_entry(c) for c in (item.get("_candidates") or [])]
        if not selected:
            selected = [scene_review_entry(p) for p in existing_media_paths(item.get("_files"))]
        alternates = [scene_review_entry(c) for c in (item.get("_alternates") or [])]
        scenes.append({
            "index": idx,
            "keyword": item.get("keyword"),
            "sentence": item.get("sentence"),
            "selected": selected,
            "alternates": alternates,
        })
    return {"scenes": scenes, "count": count}


def build_scene_pools(keyword_data):
    pools = {}
    for idx, item in enumerate(keyword_data):
        pool = set()
        for candidate in (item.get("_candidates") or []) + (item.get("_alternates") or []):
            if isinstance(candidate, MediaCandidate) and candidate.local_path:
                pool.add(candidate.local_path)
        if not pool:
            pool = {str(p) for p in existing_media_paths(item.get("_files"))}
        pools[idx] = pool
    return pools


def get_search_cache():
    global _SEARCH_CACHE
    if _SEARCH_CACHE is None:
        _SEARCH_CACHE = SearchCache(directory=BASE_DIR / "storage" / "cache_material_search")
    return _SEARCH_CACHE


def candidate_from_dict(item, scene_index, query):
    return MediaCandidate(
        provider=str(item.get("provider") or ""),
        asset_id=str(item.get("asset_id") or ""),
        url=str(item.get("url") or ""),
        source_page=str(item.get("source_page") or ""),
        creator=str(item.get("creator") or ""),
        title=str(item.get("title") or ""),
        query=query,
        scene_index=scene_index,
        duration=float(item.get("duration") or 0),
        width=int(item.get("width") or 0),
        height=int(item.get("height") or 0),
        rendition=dict(item.get("rendition") or {}),
        relevance=float(item.get("relevance") or 0),
    )


async def download_stock_candidate(candidate, project_path, source, probe=None, fetcher=None, aspect="9:16"):
    folder = Path(project_path) / source / re.sub(r"[^\w\-]", "_", candidate.query or "scene")[:25]
    folder.mkdir(parents=True, exist_ok=True)
    prefix = {"pexels": "vid", "pixabay": "v", "coverr": "c", "mixkit": "mk"}.get(source, "vid")
    safe_id = re.sub(r"[^\w\-]", "_", str(candidate.asset_id or "x"))[:48] or "x"
    path = folder / f"{prefix}_{safe_id}.mp4"
    existed = path.exists() and path.stat().st_size >= MIN_VIDEO_BYTES
    try:
        if source == "pinterest":
            newly = not existed
            if not existed:
                pin_page = pinterest_pin_page(
                    pin_id=candidate.asset_id,
                    source_page=candidate.source_page,
                    url=candidate.url,
                )
                ok = False
                if pin_page:
                    ok = await asyncio.to_thread(download_pinterest_with_ytdlp, pin_page, path)
                if not ok:
                    urls = pinterest_mp4_urls(candidate.url or "")[:2]
                    for media_url in urls:
                        if fetcher:
                            ok = await fetcher(media_url, path)
                        else:
                            ok = await asyncio.to_thread(download_http, media_url, path, 60, MIN_VIDEO_BYTES, True)
                        if ok:
                            break
                if not ok:
                    delete_rejected_file(path, newly_downloaded=True)
                    return None, last_download_error() or "download failed"
        else:
            newly = not existed
            if not existed:
                if fetcher:
                    ok = await fetcher(candidate.url, path)
                else:
                    ok = await asyncio.to_thread(download_http, candidate.url, path, 60, MIN_VIDEO_BYTES, True)
                if not ok:
                    delete_rejected_file(path, newly_downloaded=True)
                    return None, last_download_error() or "download failed"
        valid, info = await asyncio.to_thread(validate_downloaded_video, path, probe)
        if not valid:
            delete_rejected_file(path, newly_downloaded=not existed)
            return None, info if isinstance(info, str) else "invalid video"
        if isinstance(info, dict):
            candidate.duration = candidate.duration or float(info.get("duration") or 0)
            candidate.width = candidate.width or int(info.get("width") or 0)
            candidate.height = candidate.height or int(info.get("height") or 0)
            candidate.quality = f"ok fps={info.get('fps')}"
        if aspect != "1:1" and candidate.width and candidate.height and not matches_orientation(candidate.width, candidate.height, aspect):
            delete_rejected_file(path, newly_downloaded=not existed)
            return None, "wrong aspect"
        try:
            candidate.fingerprint = content_fingerprint(path)
        except OSError:
            pass
        candidate.local_path = str(path)
        return str(path), "ok"
    except Exception as exc:
        print(f"  ⚠️ Download failed: {redact_secret(exc)}")
        return None, f"download failed: {type(exc).__name__}"


async def collect_stock_videos(
    keyword_data,
    source,
    project_path,
    api_keys=None,
    aspect="9:16",
    clip_duration=5,
    count=1,
    llm=None,
    topic="",
    narration_duration=None,
    cache=None,
    probe=None,
    search_fn=None,
    download_fn=None,
):
    """Search selected provider, round-robin download, then enforce unique duration coverage."""
    reset_download_fail_logs()
    clip_duration = max(2, min(12, float(clip_duration or 5)))
    needed = max(1, int(count or 1))
    search_limit = max(12, min(24, needed * 6))
    cache = cache if cache is not None else get_search_cache()
    providers = resolve_stock_providers(source, api_keys)
    scrapers = {provider: make_scraper(provider, project_path, api_keys) for provider in providers}
    scrapers = {provider: s for provider, s in scrapers.items() if s is not None}
    if not scrapers and providers:
        scrapers = {providers[0]: make_scraper(providers[0], project_path, api_keys)}
    if len(providers) > 1:
        print(f"🔀 Round-Robin providers: {', '.join(scrapers.keys())}")
    rejection_log = {idx: [] for idx in range(len(keyword_data))}
    alt_queries = {idx: [] for idx in range(len(keyword_data))}
    seen_keys = set()

    plan = visual_query_plan(keyword_data, topic)
    print("🧭 Query plan:")
    for idx, query in enumerate(plan or stock_query_plan(keyword_data)):
        print(f"  {idx + 1}. {query} ({len(query.split())} words)")

    per_provider_limit = max(4, search_limit // max(1, len(scrapers)))

    async def search_query(query, scene_index, broaden=True):
        if search_fn:
            raw_items = await search_fn(query, scene_index)
            items = [
                item if isinstance(item, MediaCandidate) else candidate_from_dict(item, scene_index, query)
                for item in (raw_items or [])
            ]
            return items, "live"

        merged = []
        origin = "live"
        scene_sentence = ""
        if 0 <= scene_index < len(keyword_data):
            scene_sentence = keyword_data[scene_index].get("sentence") or ""
        for provider, provider_scraper in scrapers.items():
            finder = getattr(provider_scraper, "find_videos", None)
            if finder is None:
                continue
            if not broaden:
                chain = [normalize_stock_query(query)]
            elif provider == "pinterest":
                chain = pinterest_query_variants(query, scene_sentence, topic)
            else:
                chain = query_broaden_chain(query, topic)
            for variant in chain:
                if not variant:
                    continue
                if inspect.iscoroutinefunction(finder):
                    try:
                        raw = await finder(variant, aspect=aspect, min_duration=clip_duration, limit=per_provider_limit)
                    except Exception as exc:
                        print(f"⚠️ Provider search failed ({provider} {variant!r}): {redact_secret(exc)}")
                        origin = "error"
                        continue
                    print(f"  🔎 [{provider}] {variant!r} → {len(raw or [])} hits")
                    for item in raw or []:
                        merged.append(candidate_from_dict(item, scene_index, variant))
                else:
                    def live_search(variant=variant, provider_scraper=provider_scraper):
                        inner = getattr(provider_scraper, "find_videos", None)
                        if inner is None:
                            return []
                        return [
                            candidate_from_dict(raw, scene_index, variant)
                            for raw in (inner(variant, aspect=aspect, min_duration=clip_duration, limit=per_provider_limit) or [])
                        ]

                    items, item_origin = await asyncio.to_thread(
                        search_with_cache,
                        cache,
                        provider,
                        variant,
                        int(clip_duration),
                        aspect,
                        live_search,
                    )
                    if item_origin == "error":
                        origin = "error"
                    print(f"  🔎 [{provider}] {variant!r} → {len(items)} hits")
                    for item in items:
                        item.scene_index = scene_index
                        item.query = variant
                    merged.extend(items)
        return merged, origin

    def keep_candidate(candidate, scene_index):
        if not candidate.url or not candidate.asset_id:
            rejection_log[scene_index].append({"asset_id": candidate.asset_id, "reason": "missing url or id"})
            return False
        if candidate.provider == "pinterest" and UNREALISTIC_PIN_RE.search(f"{candidate.title} {candidate.query}"):
            rejection_log[scene_index].append({"asset_id": candidate.asset_id, "reason": "unrealistic pin"})
            return False
        if candidate.provider != "pinterest" and candidate.duration and candidate.duration < clip_duration:
            rejection_log[scene_index].append({"asset_id": candidate.asset_id, "reason": "too short"})
            return False
        if (
            aspect != "1:1"
            and candidate.width
            and candidate.height
            and not matches_orientation(candidate.width, candidate.height, aspect)
        ):
            rejection_log[scene_index].append({"asset_id": candidate.asset_id, "reason": "wrong aspect"})
            return False
        return True

    groups = []
    if search_fn:
        for idx, item in enumerate(keyword_data):
            query = item.get("keyword") or ""
            found, origin = await search_query(query, idx)
            ranked = rank_scene_candidates(found, sentence=item.get("sentence") or "", keyword=query)
            unique = [candidate for candidate in ranked if keep_candidate(candidate, idx)]
            unique = dedupe_candidates(unique, seen_keys)
            print(f"  provider={source} query={query!r} origin={origin} candidates={len(unique)}")
            groups.append(unique)
    else:
        pool = []
        origin = "live"
        plan_queries = plan or stock_query_plan(keyword_data)
        query_total = max(1, len(plan_queries))
        for q_idx, query in enumerate(plan_queries):
            set_status(progress=5 + 20 * (q_idx / query_total), message=f"Searching stock ({q_idx + 1}/{query_total}): {query}")
            found, origin = await search_query(query, 0, broaden=False)
            pool.extend(found)
        pool = [candidate for candidate in pool if keep_candidate(candidate, 0)]
        pool = dedupe_candidates(pool)
        print(f"  provider={source} origin={origin} pool={len(pool)} queries={len(plan_queries)}")
        for idx, item in enumerate(keyword_data):
            ranked = rank_scene_candidates(
                pool,
                sentence=item.get("sentence") or "",
                keyword=f"{item.get('keyword') or ''} {topic}",
            )
            mixed = interleave_candidates_by_query(ranked, plan_queries)
            mixed = interleave_candidates_by_provider(mixed, list(scrapers.keys()))
            print(f"  mix scene {idx + 1}: " + ", ".join(f"{query!r}" for query in plan_queries))
            groups.append(mixed)

    selected, taken_keys = seed_selected_from_keyword_data(keyword_data, clip_duration)
    pointers = [0] * len(keyword_data)
    download_fail_count = 0
    abort_provider = False
    fetchers = {
        provider: provider_scraper.download_bytes
        for provider, provider_scraper in scrapers.items()
        if hasattr(provider_scraper, "download_bytes")
    }
    if any(selected):
        print(
            f"  📎 seeded {sum(len(group) for group in selected)} clip(s) from prior providers"
        )

    async def take_next(scene_index):
        nonlocal download_fail_count, abort_provider
        if abort_provider:
            return None
        group = groups[scene_index]
        while pointers[scene_index] < len(group):
            candidate = group[pointers[scene_index]]
            pointers[scene_index] += 1
            keys = candidate.identity_keys()
            if any(key in taken_keys for key in keys):
                continue
            downloader = download_fn or download_stock_candidate
            effective_provider = candidate.provider or source
            print(f"  ⬇️ scene {scene_index + 1} [{effective_provider}] {candidate.query!r} id={candidate.asset_id}")
            if download_fn:
                path, reason = await downloader(candidate, project_path, effective_provider, probe)
            else:
                path, reason = await download_stock_candidate(
                    candidate, project_path, effective_provider, probe, fetcher=fetchers.get(effective_provider), aspect=aspect
                )
            if path:
                print(f"  ✅ kept {Path(path).name}")
                for key in keys:
                    taken_keys.add(key)
                return candidate
            print(f"  🚫 skip {candidate.asset_id}: {reason}")
            rejection_log[scene_index].append({"asset_id": candidate.asset_id, "reason": reason})
            if reason not in {"text overlay", "dark or frozen"} and not str(reason).startswith("undecodable"):
                download_fail_count += 1
                kept = sum(len(group) for group in selected)
                if download_fail_count >= 20 and kept < max(1, needed):
                    print(
                        f"  ⚠️ aborting {source} after {download_fail_count} download fails "
                        f"(kept {kept})"
                    )
                    abort_provider = True
                    return None
        return None

    def report_download_progress(phase="required"):
        kept = sum(len(group) for group in selected)
        scene_n = max(1, len(keyword_data))
        required_total = needed * scene_n
        if phase == "spare":
            extra = max(0, kept - required_total)
            spare_total = SPARE_CLIPS_PER_SCENE * scene_n
            frac = extra / max(1, spare_total)
            set_status(progress=80 + 15 * frac, message=f"Downloading spare clips {extra}/{spare_total}...")
            return
        frac = min(1.0, kept / max(1, required_total))
        set_status(progress=10 + 70 * frac, message=f"Downloading clips {kept}/{required_total}...")

    round_idx = 0
    while round_idx < needed and not abort_provider:
        progressed = False
        for scene_index, _group in enumerate(groups):
            if len(selected[scene_index]) > round_idx:
                continue
            chosen = await take_next(scene_index)
            if chosen:
                selected[scene_index].append(chosen)
                progressed = True
                report_download_progress()
            if abort_provider:
                break
        if not progressed:
            break
        round_idx += 1

    async def fill_missing_with_alts():
        if not llm or abort_provider:
            return
        for scene_index, item in enumerate(keyword_data):
            if len(selected[scene_index]) >= needed:
                continue
            if len(alt_queries[scene_index]) >= MAX_ALT_QUERIES_PER_SCENE:
                continue
            alt = llm.suggest_visual_query(
                item.get("sentence") or "",
                topic=topic,
                failed_queries=[item.get("keyword")] + alt_queries[scene_index],
            )
            if not alt or alt == item.get("keyword"):
                continue
            alt_queries[scene_index].append(alt)
            print(f"  🆕 alt query scene {scene_index + 1}: {alt}")
            found, _origin = await search_query(alt, scene_index)
            ranked = rank_scene_candidates(found, sentence=item.get("sentence") or "", keyword=alt)
            extra = dedupe_candidates(ranked, seen_keys)
            groups[scene_index].extend(extra)
            while len(selected[scene_index]) < needed:
                chosen = await take_next(scene_index)
                if not chosen:
                    break
                selected[scene_index].append(chosen)
                report_download_progress()
            if abort_provider:
                return

    await fill_missing_with_alts()

    async def collect_spares():
        """Best-effort: download up to SPARE_CLIPS_PER_SCENE extra clips per scene
        (same keyword pool) so the review step has something to swap in. Never
        counted toward coverage — run only after the required count is met."""
        spare_target = needed + SPARE_CLIPS_PER_SCENE
        for scene_index in range(len(keyword_data)):
            if abort_provider:
                return
            while len(selected[scene_index]) < spare_target:
                chosen = await take_next(scene_index)
                if not chosen:
                    break
                selected[scene_index].append(chosen)
                report_download_progress("spare")

    visual_cap = needed * clip_duration
    scenes_for_coverage = []
    for item in keyword_data:
        speech = estimated_speech_seconds(item.get("sentence") or "")
        required = min(max(clip_duration, speech), visual_cap)
        scenes_for_coverage.append({
            "keyword": item.get("keyword"),
            "required_duration": required,
        })
    estimated_narration = narration_duration
    if estimated_narration is None:
        estimated_narration = sum(estimated_speech_seconds(item.get("sentence") or "") for item in keyword_data)

    unique_total = unique_usable_duration(selected, clip_duration)
    failures, unique_total = coverage_failures(
        selected, scenes_for_coverage, clip_duration, estimated_narration, source
    )
    print(
        f"📏 unique usable {unique_total:.2f}s / narration {estimated_narration:.2f}s "
        f"clip={clip_duration}s rejections={sum(len(v) for v in rejection_log.values())}"
    )

    if not failures:
        await collect_spares()
        spare_count = sum(max(0, len(group) - needed) for group in selected)
        if spare_count:
            print(f"  🎞️ downloaded {spare_count} spare clip(s) for review/swap")

    manifest_scenes = []
    for idx, item in enumerate(keyword_data):
        chosen_all = selected[idx]
        chosen = chosen_all[:needed]
        alternates = chosen_all[needed:]
        paths = [c.local_path for c in chosen if c.local_path]
        if paths:
            item["_files"] = paths
            item["_candidates"] = chosen
        else:
            item["_error"] = describe_empty_media_result(source, "video")
        if alternates:
            item["_alternates"] = alternates
        manifest_scenes.append({
            "index": idx,
            "sentence": item.get("sentence"),
            "query": item.get("keyword"),
            "alternatives": alt_queries[idx],
            "provider": source,
            "selected": [selected_record(c) for c in chosen],
            "spares": [selected_record(c) for c in alternates],
            "rejections": rejection_log[idx],
            "reused": False,
        })
    write_source_manifest(Path(project_path).parent if Path(project_path).name in {"video", "photo"} else project_path, {
        "provider": source,
        "clip_duration": clip_duration,
        "narration_duration": estimated_narration,
        "unique_usable_duration": unique_total,
        "scenes": manifest_scenes,
    })

    async def close_scrapers():
        for provider_scraper in scrapers.values():
            closer = getattr(provider_scraper, "aclose", None)
            if inspect.iscoroutinefunction(closer):
                await closer()

    if failures:
        await close_scrapers()
        raise CoverageError(
            format_coverage_error(failures, source, clip_duration, estimated_narration, unique_total)
        )
    await close_scrapers()
    return selected


async def collect_stock_videos_with_fallback(
    keyword_data,
    source,
    project_path,
    api_keys=None,
    aspect="9:16",
    clip_duration=5,
    count=1,
    llm=None,
    topic="",
    narration_duration=None,
    cache=None,
    probe=None,
    search_fn=None,
    download_fn=None,
    enable_fallback=False,
):
    providers = stock_fallback_providers(source, enable_fallback)
    ready = []
    for provider in providers:
        if stock_provider_ready(provider, api_keys):
            ready.append(provider)
        else:
            print(f"  ⏭️ skip fallback {provider}: missing API key")
    if not ready:
        ready = [source]

    last_error = None
    extra = 0
    for index, provider in enumerate(ready):
        target_count = max(1, int(count or 1) + extra)
        if index > 0:
            print(f"↪️ Fallback → {provider} (assets/scene={target_count})")
            scraping_status["message"] = f"Fallback searching {provider}..."
        try:
            return await collect_stock_videos(
                keyword_data,
                source=provider,
                project_path=project_path,
                api_keys=api_keys,
                aspect=aspect,
                clip_duration=clip_duration,
                count=target_count,
                llm=llm,
                topic=topic,
                narration_duration=narration_duration,
                cache=cache,
                probe=probe,
                search_fn=search_fn,
                download_fn=download_fn,
            )
        except CoverageError as exc:
            last_error = exc
            if index + 1 >= len(ready):
                raise
            extra = min(6, extra + 2)
            print(f"  ⚠️ {provider} coverage short → next provider")
    if last_error:
        raise last_error
    return None


async def run_video_assembly(
    keyword_data,
    project_path,
    project_name,
    media_type,
    settings,
    api_keys,
    vibe,
    yt_upload=False,
    publish_confirmed=False,
    progress_label="",
):
    """Voiceover + create_video + thumbnail + optional YouTube upload for one project.
    Shared by the single-pass flow and the post-review /api/assemble phase."""
    label = f" {progress_label}" if progress_label else ""
    validate_scene_images(keyword_data, project_path)
    scraping_status["message"] = f"Generating voiceover{label}..."
    engine = load_video_engine()(output_dir=project_path.parent)
    if api_keys.eleven_key:
        engine.set_eleven_key(api_keys.eleven_key)
    engine.tts_server = getattr(settings, "tts_server", "azure-tts-v1") or "azure-tts-v1"
    engine.set_azure_speech(
        getattr(api_keys, "azure_speech_key", "") or "",
        getattr(api_keys, "azure_speech_region", "") or "",
    )
    voice = settings.voice if settings.voice != "none" else None
    if not voice:
        raise RuntimeError("Auto video requires a TTS voice; voice=none cannot produce one narration file per scene.")

    sem = asyncio.Semaphore(3)

    async def sem_voiceover(text, i):
        async with sem:
            return await engine.generate_voiceover(
                text, i, voice=voice, language=settings.language,
                voice_rate=settings.voice_rate, voice_volume=settings.voice_volume,
            )
    await asyncio.gather(*[sem_voiceover(item["sentence"], idx) for idx, item in enumerate(keyword_data)])
    validate_tts_files(engine, len(keyword_data))

    settings.vibe = vibe
    duration_seconds = max(20, min(120, int(settings.clip_duration or 5) * max(1, len(keyword_data))))
    bg_music = resolve_background_music(settings, api_keys=api_keys, vibe=vibe, duration_seconds=duration_seconds)
    candidate_count = max(1, min(5, int(settings.video_count or 1)))
    candidates = []
    video_path = None
    video_file = None
    for candidate_idx in range(candidate_count):
        scraping_status["message"] = f"Assembling video{label} candidate {candidate_idx+1}/{candidate_count}..."
        video_file = await asyncio.to_thread(
            engine.create_video, keyword_data, project_path, media_type,
            bg_music=bg_music, settings=settings,
            output_name=f"final_aesthetic_video_{candidate_idx+1}.mp4" if candidate_count > 1 else "final_aesthetic_video.mp4",
        )
        video_path = validate_final_video(video_file)
        video_rel = relative_download_path(video_path)
        candidates.append(video_rel)
        scraping_status["results"].append({"keyword": f"Assembled video {candidate_idx+1}", "files": [video_rel]})
        if candidate_idx == 0:
            set_status(final_video=video_rel)
    set_status(candidates=candidates)

    thumb_file = engine.generate_thumbnail(str(video_path), project_name.replace("_", " ").title())
    if thumb_file:
        with contextlib.suppress(Exception):
            thumb_rel = "/" + str(Path(thumb_file).relative_to(BASE_DIR)).replace("\\", "/")
            scraping_status["results"].append({"keyword": "Thumbnail", "files": [thumb_rel]})

    scraping_status["message"] = f"Video ready: {project_name}/final_aesthetic_video.mp4"

    if yt_upload and publish_confirmed and video_file and api_keys.yt_client_id and api_keys.yt_client_secret:
        scraping_status["message"] = "Uploading to YouTube..."
        try:
            uploader = load_youtube_uploader()(api_keys.yt_client_id, api_keys.yt_client_secret)
            title = project_name.replace("_", " ").title()
            await asyncio.to_thread(uploader.upload_video, str(video_path), title, "Automated video created with VUZA.", [])
            scraping_status["message"] += " (uploaded to YouTube)"
        except Exception as e:
            scraping_status["message"] += f" (upload failed: {e})"


async def run_assemble_phase(
    task_id,
    keyword_data,
    project_path,
    project_name,
    media_type,
    settings,
    api_keys,
    vibe,
    yt_upload,
    publish_confirmed,
):
    """Background task for POST /api/assemble: resumes a paused (awaiting_review) job."""
    try:
        await run_video_assembly(
            keyword_data, project_path, project_name, media_type, settings, api_keys, vibe,
            yt_upload=yt_upload, publish_confirmed=publish_confirmed,
        )
        set_status("success", progress=100)
    except Exception as e:
        set_status("error", message=f"Error: {str(e)}", progress=100, error=str(e))
        import traceback
        traceback.print_exc()
    finally:
        scraping_status["is_running"] = False


# ── Main Scraping ──
async def run_scrape(request: ScrapeRequest):
    global scraping_status
    task_id = scraping_status.get("task_id") or uuid.uuid4().hex[:12]
    set_status(
        "running",
        message="Starting...",
        progress=0,
        error=None,
        final_video=None,
        results=[],
        candidates=[],
        review=None,
        mode=request.mode,
        task_id=task_id,
    )
    pending_assembly.pop(task_id, None)

    try:
        validate_scrape_request_options(request)
        validate_request_api_dependencies(request)
        source, media_type, count = request.source, request.media_type, request.count
        api_keys = request.api_keys or ApiKeys()
        settings = request.video_settings or VideoSettings()
        if not (settings.music_query or "").strip():
            settings.music_query = (request.query or "").strip()
        local_files = resolved_local_files(request) if source == "local" else []
        print(
            f"🎛️ Settings: {count} asset(s)/scene | "
            f"{settings.clip_duration}s clip"
        )

        if request.mode == "script":
            scripts = normalized_script_inputs(request)
            if not scripts:
                raise RuntimeError("Script mode requires a script or scripts list.")
            for script_idx, script in enumerate(scripts):
                words = re.findall(r'\w+', script)
                project_name = "_".join(words[:5]).lower() or f"unnamed_{script_idx}"
                project_path = DOWNLOAD_DIR / project_name / media_type
                project_path.mkdir(parents=True, exist_ok=True)

                scraping_status["message"] = f"Analyzing script {script_idx+1}/{len(scripts)}..."
                already_grouped = False
                if source == "local":
                    keyword_data = local_script_segments(script)
                    llm = None
                else:
                    llm = LLMProcessor(api_key=api_keys.llm_key, api_url=api_keys.llm_url, model=api_keys.llm_model)
                    use_stock_pipeline = media_type == "video" and source in STOCK_VIDEO_SOURCES
                    topic = (request.query or "").strip() or script[:160]
                    sentence_rows = [{"sentence": part, "keyword": "scene"} for part in split_script_sentences(script)]
                    grouped = group_scenes_to_clip_budget(
                        sentence_rows, count, settings.clip_duration
                    )
                    mapped = apply_user_keywords(grouped, request.keywords, topic)
                    already_grouped = bool(mapped) or use_stock_pipeline
                    if mapped:
                        keyword_data = mapped
                    elif use_stock_pipeline:
                        keyword_data = llm.extract_keywords(
                            script,
                            vibe=request.vibe,
                            language=settings.language,
                            topic=topic,
                            scenes=[item["sentence"] for item in grouped],
                        )
                    else:
                        keyword_data = llm.extract_keywords(
                            script,
                            vibe=request.vibe,
                            language=settings.language,
                            topic=topic,
                        )

                if not keyword_data:
                    raise RuntimeError((llm.last_error if llm else "") or "No usable scenes were generated. Check that the script is not empty.")

                raw_scenes = len(split_script_sentences(script)) if script else len(keyword_data)
                use_stock_pipeline = media_type == "video" and source in STOCK_VIDEO_SOURCES
                if not already_grouped:
                    keyword_data = group_scenes_to_clip_budget(
                        keyword_data, count, settings.clip_duration
                    )
                    print(
                        f"🎞️ Scenes: {raw_scenes} sentences → {len(keyword_data)} "
                        f"({count}×{settings.clip_duration}s per scene)"
                    )
                elif source != "local":
                    print(
                        f"🎞️ Scenes: {raw_scenes} sentences → {len(keyword_data)} "
                        f"({count}×{settings.clip_duration}s per scene)"
                    )

                total = len(keyword_data)
                seen_hashes = set()
                if use_stock_pipeline:
                    scraping_status["message"] = f"Searching {source} media {script_idx+1}/{len(scripts)}..."
                    await collect_stock_videos_with_fallback(
                        keyword_data,
                        source=source,
                        project_path=project_path,
                        api_keys=api_keys,
                        aspect=settings.ratio,
                        clip_duration=settings.clip_duration,
                        count=count,
                        llm=llm,
                        topic=(request.query or script[:160]).strip(),
                        enable_fallback=bool(request.provider_fallback),
                    )
                    result_rows = keyword_data_result_rows(keyword_data)
                    review_eligible = request.auto_video and use_stock_pipeline and len(scripts) == 1
                    if review_eligible:
                        validate_scene_images(keyword_data, project_path)
                        pending_assembly[task_id] = {
                            "keyword_data": keyword_data,
                            "project_path": project_path,
                            "project_name": project_name,
                            "media_type": media_type,
                            "settings": settings,
                            "api_keys": api_keys,
                            "vibe": request.vibe,
                            "yt_upload": request.yt_upload,
                            "publish_confirmed": request.publish_confirmed,
                            "scene_pools": build_scene_pools(keyword_data),
                            "count": count,
                        }
                        set_status(
                            "awaiting_review",
                            message=f"Scraping finished. Review {len(keyword_data)} scene(s) before voiceover, or continue with the default selection.",
                            progress=100,
                            results=result_rows,
                            review=build_scene_review(keyword_data, count),
                        )
                        return
                    scraping_status["results"] = result_rows
                    set_status(progress=((script_idx + 0.8) / len(scripts)) * 100)
                else:
                    for idx, item in enumerate(keyword_data):
                        scraping_status["message"] = f"Searching media {script_idx+1}/{len(scripts)} | {idx+1}/{total}..."
                        try:
                            res_files = await universal_search(
                                keyword=item["keyword"], media_type=media_type, count=count,
                                primary_source=source, project_path=project_path, api_keys=api_keys,
                                vibe=request.vibe, sentence=item["sentence"], llm=llm,
                                aspect=settings.ratio, local_files=local_files, seen_hashes=seen_hashes,
                                enable_fallback=bool(request.provider_fallback),
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            item["_error"] = describe_scene_media_error(exc)
                            print(f"  ❌ Scene media failed ({item['keyword']}): {item['_error']}")
                            if is_fatal_scene_media_error(item["_error"]):
                                raise RuntimeError(item["_error"])
                            set_status(progress=((script_idx) / len(scripts)) * 100 + ((idx + 1) / total) * (100 / len(scripts)) * 0.8)
                            continue
                        rel_paths = []
                        valid_paths = existing_media_paths(res_files)
                        valid_files = [str(path) for path in valid_paths]
                        for path in valid_paths:
                            try:
                                rel_paths.append("/" + str(path.relative_to(BASE_DIR)).replace("\\", "/"))
                            except Exception:
                                rel_paths.append(str(path))
                        if rel_paths:
                            item["_files"] = valid_files
                            scraping_status["results"].append({"keyword": item["keyword"], "sentence": item["sentence"], "files": rel_paths})
                        else:
                            item["_error"] = describe_empty_media_result(source, media_type)
                        set_status(progress=((script_idx) / len(scripts)) * 100 + ((idx + 1) / total) * (100 / len(scripts)) * 0.8)

                if request.auto_video:
                    await run_video_assembly(
                        keyword_data, project_path, project_name, media_type, settings, api_keys,
                        request.vibe, yt_upload=request.yt_upload, publish_confirmed=request.publish_confirmed,
                        progress_label=f"{script_idx+1}/{len(scripts)}",
                    )
                else:
                    validate_scene_images(keyword_data, project_path)
                    scraping_status["message"] = f"Assets saved to {project_name}/ (video assembly off)"
        else:
            query = request.query
            project_name = re.sub(r'[^\w\-]', '_', query or "local").lower()
            project_path = DOWNLOAD_DIR / project_name / media_type
            project_path.mkdir(parents=True, exist_ok=True)

            scraping_status["message"] = f"Searching “{query or 'local uploads'}”..."
            res_files = await universal_search(
                keyword=query or "local", media_type=media_type, count=count,
                primary_source=source, project_path=project_path, api_keys=api_keys,
                llm=None, sentence=query or "", aspect=settings.ratio, local_files=local_files,
                seen_hashes=set(),
                enable_fallback=bool(request.provider_fallback),
            )
            valid_paths = require_media_files(res_files, query or "local uploads")
            rel_paths = []
            for path in valid_paths:
                try:
                    rel_paths.append("/" + str(path.relative_to(BASE_DIR)).replace("\\", "/"))
                except Exception:
                    rel_paths.append(str(path))
            scraping_status["results"] = [{"keyword": query or "local", "files": rel_paths}]
            scraping_status["message"] = "Done"

        set_status("success", progress=100)
    except Exception as e:
        set_status("error", message=f"Error: {str(e)}", progress=100, error=str(e))
        import traceback
        traceback.print_exc()
    finally:
        # awaiting_review is a deliberate pause, not completion — leave is_running
        # true so /api/scrape keeps rejecting new jobs until /api/assemble resumes it.
        if scraping_status.get("status") != "awaiting_review":
            scraping_status["is_running"] = False

@app.post("/api/scrape")
async def start_scrape(request: ScrapeRequest, background_tasks: BackgroundTasks):
    print(f"VUZA Request: Mode={request.mode}, Source={request.source}, Vibe={request.vibe}")
    if scraping_status["is_running"]:
        return JSONResponse(status_code=400, content={"message": "A job is already running. Wait for it to finish."})
    try:
        validate_scrape_request_options(request)
        validate_request_api_dependencies(request)
    except RuntimeError as exc:
        detail = str(exc)
        set_status(
            "error",
            message=f"Error: {detail}",
            progress=100,
            error=detail,
            final_video=None,
            results=[],
            candidates=[],
            mode=request.mode,
        )
        raise HTTPException(status_code=400, detail=detail) from exc
    task_id = uuid.uuid4().hex[:12]
    set_status(task_id=task_id, status="queued", message="Queued", progress=0, error=None, results=[], candidates=[], final_video=None, review=None, mode=request.mode)
    background_tasks.add_task(run_scrape, request)
    return {"message": "Started", "task_id": task_id}

@app.post("/api/assemble")
async def start_assemble(request: AssembleRequest, background_tasks: BackgroundTasks):
    pending = pending_assembly.get(request.task_id)
    if not pending:
        raise HTTPException(
            status_code=404,
            detail="No pending review for this task. It may have already been assembled or expired.",
        )

    keyword_data = pending["keyword_data"]
    if request.selections:
        if len(request.selections) != len(keyword_data):
            raise HTTPException(
                status_code=400,
                detail=f"Expected {len(keyword_data)} scene selection(s), got {len(request.selections)}.",
            )
        pools = pending["scene_pools"]
        cap = pending["count"]
        for idx, paths in enumerate(request.selections):
            pool = pools.get(idx, set())
            cleaned = [p for p in (paths or []) if p in pool][:cap]
            if not cleaned:
                raise HTTPException(status_code=400, detail=f"Scene {idx + 1} has no valid selection.")
            keyword_data[idx]["_files"] = cleaned

    pending_assembly.pop(request.task_id, None)
    result_rows = keyword_data_result_rows(keyword_data)
    set_status(
        "running",
        message="Resuming after review...",
        task_id=request.task_id,
        error=None,
        review=None,
        results=result_rows,
    )
    background_tasks.add_task(
        run_assemble_phase,
        request.task_id,
        keyword_data,
        pending["project_path"],
        pending["project_name"],
        pending["media_type"],
        pending["settings"],
        pending["api_keys"],
        pending["vibe"],
        pending["yt_upload"],
        pending["publish_confirmed"],
    )
    return {"message": "Assembling", "task_id": request.task_id, "results": result_rows}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
