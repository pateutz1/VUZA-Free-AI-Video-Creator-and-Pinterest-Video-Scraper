import asyncio
import contextlib
import hashlib
import os
import re
import json
import random
import sys
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

from aesthetic_scraper import PinterestScraper, PexelsScraper, PixabayScraper, CoverrScraper, PiAPIScraper, LLMProcessor, WebScraper, LLM_PROVIDER_PRESETS

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
    "task_id": None, "candidates": [],
}

# ── Models ──
class VideoSettings(BaseModel):
    ratio: str = "9:16"
    voice: str = "en-US-ChristopherNeural"
    subtitles: bool = True
    language: str = "en-US"
    subtitle_style: str = "high_retention"
    music: str = "none"
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
    font_size: int = Field(default=60, ge=24, le=160)
    text_fore_color: str = "#FFFFFF"
    stroke_color: str = "#000000"
    stroke_width: float = Field(default=1.5, ge=0.0, le=12.0)
    subtitle_background: str = "none"
    transition: str = "fade"

class ApiKeys(BaseModel):
    llm_key: str = ""
    llm_url: str = "https://openrouter.ai/api/v1/chat/completions"
    llm_model: str = ""
    pexels_key: str = ""
    pixabay_key: str = ""
    coverr_key: str = ""
    piapi_key: str = ""
    piapi_model: str = "hailuo-2.3-fast"
    yt_client_id: str = ""
    yt_client_secret: str = ""
    eleven_key: str = ""

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
    piapi_confirmed: bool = False
    yt_upload: bool = False
    publish_confirmed: bool = False
    local_files: Optional[List[str]] = None
    api_keys: Optional[ApiKeys] = None

class VoicePreviewRequest(BaseModel):
    text: str = "This is a VUZA voice preview."
    voice: str = "en-US-ChristopherNeural"
    language: str = "en-US"
    voice_rate: float = 1.0
    voice_volume: float = 1.0
    eleven_key: str = ""

class LlmTestRequest(BaseModel):
    provider: str = ""
    api_key: str = ""
    api_url: str = ""
    model: str = ""

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

VALID_SOURCES = {"pinterest", "pexels", "pixabay", "coverr", "piapi", "local"}
VALID_MEDIA_TYPES = {"photo", "video"}
VALID_MODES = {"single", "script"}
VALID_TRANSITIONS = {"none", "fade", "zoom_in", "zoom_out", "slide"}
ALLOWED_UPLOAD_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v", ".webm"}

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

@app.get("/api/music")
async def list_music():
    music_dir = BASE_DIR / "static" / "music"
    files = ["none"]
    if music_dir.exists():
        files.extend(sorted(p.name for p in music_dir.iterdir() if p.suffix.lower() in {".mp3", ".wav", ".m4a"} and p.stat().st_size > 0))
    return {"files": files}

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
    api_keys: Optional[ApiKeys] = None

@app.post("/api/generate_script")
async def generate_script(request: GenerateScriptRequest):
    if not request.topic:
        raise HTTPException(status_code=400, detail="Enter a topic first.")

    api_keys = request.api_keys or ApiKeys()
    require_llm_key(api_keys, "Script generation")
    llm = LLMProcessor(api_key=api_keys.llm_key, api_url=api_keys.llm_url, model=api_keys.llm_model)
    script = llm.generate_full_script(request.topic, vibe=request.vibe, language=request.language)

    if not script:
        raise HTTPException(status_code=500, detail=llm.last_error or "Script generation failed. Check your AI API key.")

    return {"script": script}

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

@app.post("/api/tts/preview")
async def tts_preview(request: VoicePreviewRequest):
    text = (request.text or "").strip()[:180]
    if not text:
        raise HTTPException(status_code=400, detail="Enter preview text.")
    engine = load_video_engine()(output_dir=DOWNLOAD_DIR / "_preview")
    if request.eleven_key:
        engine.set_eleven_key(request.eleven_key)
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
    if src == "pexels": return PexelsScraper(output_dir=output_dir, api_key=keys.pexels_key)
    if src == "pixabay": return PixabayScraper(output_dir=output_dir, api_key=keys.pixabay_key)
    if src == "coverr": return CoverrScraper(output_dir=output_dir, api_key=keys.coverr_key)
    if src == "piapi":
        return PiAPIScraper(
            output_dir=output_dir,
            api_key=keys.piapi_key,
            model=keys.piapi_model or "hailuo-2.3-fast",
        )
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
        raise RuntimeError(f"Invalid media source: {request.source}. Choose pinterest, pexels, pixabay, coverr, piapi, or local.")
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
        resolve_background_music(settings)
        if request.mode == "single" and request.source != "local":
            raise RuntimeError("Single stock search does not assemble a video. Switch to script mode, or turn off auto video.")
        if request.yt_upload and not request.publish_confirmed:
            raise RuntimeError("YouTube publishing requires explicit confirmation.")

def validate_script_keyword_key(request):
    if request.mode != "script" or request.source == "local":
        return
    api_keys = request.api_keys or ApiKeys()
    if not (api_keys.llm_key or "").strip():
        raise RuntimeError("Script mode with Pinterest/Pexels/Pixabay/Coverr/PiAPI requires an AI text API key to split narration into search keywords.")

def validate_request_api_dependencies(request):
    validate_script_keyword_key(request)
    if request.source == "piapi":
        api_keys = request.api_keys or ApiKeys()
        key = PiAPIScraper._clean_key(api_keys.piapi_key)
        if not key:
            raise RuntimeError("PiAPI source requires an API key. Add it in API settings: https://app.piapi.ai/")
        if key.lower().startswith("r8_"):
            raise RuntimeError("PiAPI HTTP 401: this looks like a Replicate token (r8_...). Create a PiAPI key at https://app.piapi.ai/")
        if not request.piapi_confirmed:
            raise RuntimeError("PiAPI generation is paid and requires explicit confirmation for this run.")

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
        if bucket is None:
            bucket = {"sentence": sentence, "keyword": keyword or "scene"}
            spoken = extra
            continue
        if spoken < budget:
            bucket["sentence"] = f"{bucket['sentence']} {sentence}".strip()
            spoken += extra
            continue
        grouped.append(bucket)
        bucket = {"sentence": sentence, "keyword": keyword or "scene"}
        spoken = extra
    if bucket:
        grouped.append(bucket)
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
        scraping_status["is_running"] = status == "running"
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
    path = Path(path)
    digest = hashlib.md5()
    digest.update(str(path.stat().st_size).encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(65536))
    return digest.hexdigest()

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

def require_media_files(files, label):
    valid = existing_media_paths(files)
    if not valid:
        raise RuntimeError(f"No usable media found for: {label}. Try another keyword or source, or check the stock API key/network.")
    return valid

def resolve_background_music(settings):
    music = (settings.music or "none").strip()
    if not music or music.lower() == "none":
        return None
    if Path(music).name != music:
        raise RuntimeError("Background music filename is invalid. Choose a track from the dropdown.")

    music_path = BASE_DIR / "static" / "music" / music
    if not music_path.exists() or music_path.stat().st_size <= 0:
        raise RuntimeError(f"Background music file is missing or empty: static/music/{music}. Choose “No music” or add the file.")
    return str(music_path)

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

async def universal_search(keyword, media_type, count, primary_source, project_path, api_keys=None, vibe="aesthetic", sentence="", llm=None, aspect="9:16", local_files=None, seen_hashes=None):
    if primary_source == "local":
        return [str(path) for path in (local_files or [])]

    if primary_source == "piapi":
        scraper = make_scraper("piapi", project_path, api_keys)
        prompt = (sentence or keyword or "").strip()
        kind = "photo" if media_type == "photo" else "video"
        if kind == "video":
            res = await scraper.search_videos(prompt, num_videos=1, aspect=aspect)
        else:
            res = await scraper.search_images(prompt, num_images=1)
        picked = pick_unique_media(res, seen_hashes, sentence=sentence, keyword=keyword, limit=1)
        if picked:
            print(f"  ✅ [piapi] kept {len(picked)} generated clip(s)")
        return picked

    keywords = stock_keyword_variants(keyword)
    seen_hashes = seen_hashes if seen_hashes is not None else set()

    all_sources = ["pexels", "pixabay", "coverr", "pinterest"]
    if primary_source == "coverr" and media_type != "video":
        primary_source = "pexels"
    ordered = [primary_source] + [s for s in all_sources if s != primary_source]
    if media_type != "video":
        ordered = [s for s in ordered if s != "coverr"]

    collected = []
    for src in ordered:
        scraper = make_scraper(src, project_path, api_keys)
        for k in keywords:
            remaining = max(0, count - len(collected))
            if remaining == 0:
                return collected[:count]
            res = await try_search(scraper, k, media_type, remaining, aspect=aspect)
            picked = pick_unique_media(res, seen_hashes, sentence=sentence, keyword=k, limit=remaining)
            if picked:
                print(f"  ✅ [{src}:{k}] kept {len(picked)} unique clips")
                collected.extend(picked)
                if len(collected) >= count:
                    return collected[:count]

    if llm and sentence and llm.api_key:
        print(f"  🧠 AI Re-Ask for '{keyword}'...")
        try:
            import requests as req
            r = req.post(llm.api_url,
                headers={"Authorization": f"Bearer {llm.api_key}", "Content-Type": "application/json"},
                data=json.dumps({
                    "model": llm.models[0],
                    "messages": [
                        {"role": "system", "content": "Give ONE 2-3 word stock-footage search query that matches the sentence visually. Concrete objects/actions only. Never reply with: exercise, fitness, athlete, people, motivation, success."},
                        {"role": "user", "content": f"Sentence: {sentence}\nFailed: {', '.join(keywords)}\nNew keyword:"}
                    ]
                }), timeout=15)
            if r.status_code == 200:
                new_kw = r.json()["choices"][0]["message"]["content"].strip().strip('"').strip("'").lower()
                print(f"  🆕 AI suggested: '{new_kw}'")
                if new_kw.split()[0] not in BROAD_STOCK_TERMS:
                    for src in ["pexels", "pixabay", "coverr"]:
                        if src == "coverr" and media_type != "video":
                            continue
                        scraper = make_scraper(src, project_path, api_keys)
                        remaining = max(1, count - len(collected))
                        res = await try_search(scraper, new_kw, media_type, remaining, aspect=aspect)
                        picked = pick_unique_media(res, seen_hashes, sentence=sentence, keyword=new_kw, limit=remaining)
                        if picked:
                            return picked
        except Exception as e:
            print(f"  ⚠️ AI Re-Ask failed: {e}")

    return collected

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
        mode=request.mode,
        task_id=task_id,
    )

    try:
        validate_scrape_request_options(request)
        validate_request_api_dependencies(request)
        source, media_type, count = request.source, request.media_type, request.count
        api_keys = request.api_keys or ApiKeys()
        settings = request.video_settings or VideoSettings()
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
                if source == "local":
                    keyword_data = local_script_segments(script)
                    llm = None
                else:
                    llm = LLMProcessor(api_key=api_keys.llm_key, api_url=api_keys.llm_url, model=api_keys.llm_model)
                    keyword_data = llm.extract_keywords(
                        script,
                        vibe=request.vibe,
                        language=settings.language,
                        topic=(request.query or script[:160]).strip(),
                    )

                if not keyword_data:
                    raise RuntimeError((llm.last_error if llm else "") or "No usable scenes were generated. Check that the script is not empty.")

                raw_scenes = len(keyword_data)
                keyword_data = group_scenes_to_clip_budget(
                    keyword_data, count, settings.clip_duration
                )
                print(
                    f"🎞️ Scenes: {raw_scenes} sentences → {len(keyword_data)} "
                    f"({count}×{settings.clip_duration}s per scene)"
                )

                total = len(keyword_data)
                seen_hashes = set()
                for idx, item in enumerate(keyword_data):
                    scraping_status["message"] = f"Searching media {script_idx+1}/{len(scripts)} | {idx+1}/{total}..."
                    try:
                        res_files = await universal_search(
                            keyword=item["keyword"], media_type=media_type, count=count,
                            primary_source=source, project_path=project_path, api_keys=api_keys,
                            vibe=request.vibe, sentence=item["sentence"], llm=llm,
                            aspect=settings.ratio, local_files=local_files, seen_hashes=seen_hashes,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        item["_error"] = describe_scene_media_error(exc)
                        print(f"  ❌ Scene media failed ({item['keyword']}): {item['_error']}")
                        if source == "piapi" or is_fatal_scene_media_error(item["_error"]):
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
                    validate_scene_images(keyword_data, project_path)
                    scraping_status["message"] = f"Generating voiceover {script_idx+1}/{len(scripts)}..."
                    engine = load_video_engine()(output_dir=project_path.parent)
                    if api_keys.eleven_key:
                        engine.set_eleven_key(api_keys.eleven_key)
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

                    settings.vibe = request.vibe
                    bg_music = resolve_background_music(settings)
                    candidate_count = max(1, min(5, int(settings.video_count or 1)))
                    candidates = []
                    for candidate_idx in range(candidate_count):
                        scraping_status["message"] = f"Assembling video {script_idx+1}/{len(scripts)} candidate {candidate_idx+1}/{candidate_count}..."
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

                    if request.yt_upload and request.publish_confirmed and video_file and api_keys.yt_client_id and api_keys.yt_client_secret:
                        scraping_status["message"] = "Uploading to YouTube..."
                        try:
                            uploader = load_youtube_uploader()(api_keys.yt_client_id, api_keys.yt_client_secret)
                            title = project_name.replace("_", " ").title()
                            await asyncio.to_thread(uploader.upload_video, str(video_path), title, "Automated video created with VUZA.", [])
                            scraping_status["message"] += " (uploaded to YouTube)"
                        except Exception as e:
                            scraping_status["message"] += f" (upload failed: {e})"
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
    set_status(task_id=task_id, status="queued", message="Queued", progress=0, error=None, results=[], candidates=[], final_video=None, mode=request.mode)
    background_tasks.add_task(run_scrape, request)
    return {"message": "Started", "task_id": task_id}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
