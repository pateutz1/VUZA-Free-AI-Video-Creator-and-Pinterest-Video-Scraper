"""Chronological stock-query parsing, candidates, cache, selection, coverage."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from media_quality import canonical_url, public_url, redact_secret, url_safe_to_cache

CACHE_TTL_SECONDS = 24 * 60 * 60
CACHE_FORMAT_VERSION = 1
MAX_ALT_QUERIES_PER_SCENE = 1
VIBE_SUFFIXES = (" aesthetic", " lofi art", " futuristic", " black and white")
CAMERA_PREFIXES = (
    "shot of ",
    "close up of ",
    "close-up of ",
    "cinematic ",
    "slow motion ",
    "timelapse of ",
    "time lapse of ",
)

_LOCKS = tuple(threading.Lock() for _ in range(64))


@dataclass
class MediaCandidate:
    provider: str = ""
    asset_id: str = ""
    url: str = ""
    source_page: str = ""
    creator: str = ""
    query: str = ""
    scene_index: int = 0
    duration: float = 0.0
    width: int = 0
    height: int = 0
    local_path: str = ""
    relevance: float = 0.0
    quality: str = ""
    fingerprint: str = ""
    rendition: dict = field(default_factory=dict)
    provider_order: int = 0

    def identity_keys(self):
        keys = []
        if self.provider and self.asset_id:
            keys.append(f"{self.provider}:{self.asset_id}")
        canon = canonical_url(self.url) or canonical_url(self.source_page)
        if canon:
            keys.append(canon)
        if self.fingerprint:
            keys.append(f"fp:{self.fingerprint}")
        return tuple(keys)


class CoverageError(RuntimeError):
    pass


def normalize_stock_query(keyword):
    query = (keyword or "").strip().strip('"').strip("'")
    query = re.sub(r"[#@]", "", query)
    query = re.sub(r"\s+", " ", query).strip()
    lowered = query.lower()
    for suffix in VIBE_SUFFIXES:
        if lowered.endswith(suffix):
            query = query[: len(query) - len(suffix)].strip()
            lowered = query.lower()
    for prefix in CAMERA_PREFIXES:
        if lowered.startswith(prefix):
            query = query[len(prefix):].strip()
            lowered = query.lower()
            break
    words = [word for word in query.split() if word]
    if len(words) > 6:
        query = " ".join(words[:6])
    return query


def is_concrete_query(keyword):
    query = normalize_stock_query(keyword)
    words = [word for word in query.split() if word]
    if not words or len(words) > 6:
        return False
    if query.startswith("#"):
        return False
    return True


def parse_sentence_queries(text):
    """Parse `sentence → query` lines into ordered mappings."""
    rows = []
    for line in (text or "").split("\n"):
        if "→" not in line and "->" not in line:
            continue
        arrow = "→" if "→" in line else "->"
        left, right = line.split(arrow, 1)
        sentence = re.sub(r"^\s*[\-\*\d\.\)\uff08\uff09、]+\s*", "", left).strip()
        keyword = normalize_stock_query(right)
        if sentence and keyword and is_concrete_query(keyword):
            rows.append({"sentence": sentence, "keyword": keyword})
    return rows


def query_relevance(candidate, sentence="", keyword=""):
    stop = {"the", "and", "for", "you", "your", "are", "this", "that", "with", "from", "have", "will", "just"}
    words = {
        word.lower()
        for word in re.findall(r"[a-zA-Z]{3,}", f"{sentence} {keyword} {candidate.query}")
        if word.lower() not in stop
    }
    haystack = " ".join(
        [
            str(candidate.asset_id),
            candidate.query,
            candidate.source_page,
            candidate.creator,
            str(candidate.rendition.get("id") or ""),
        ]
    ).lower().replace("_", " ")
    overlap = sum(1 for word in words if word in haystack)
    score = overlap * 3.0
    duration = float(candidate.duration or 0)
    if duration > 0:
        score += min(duration / 8.0, 2.0)
    if candidate.width and candidate.height:
        score += min((candidate.width * candidate.height) / 2_000_000, 2.0)
    return score


def matches_orientation(width, height, aspect="9:16"):
    try:
        width = int(float(width or 0))
        height = int(float(height or 0))
    except (TypeError, ValueError):
        return False
    if width <= 0 or height <= 0:
        return False
    if aspect == "16:9":
        return width > height
    if aspect == "1:1":
        return True
    return height > width


def usable_duration(source_duration, clip_duration):
    try:
        source = float(source_duration or 0)
        clip = float(clip_duration or 0)
    except (TypeError, ValueError):
        return 0.0
    if source <= 0 or clip <= 0:
        return 0.0
    return min(clip, source)


def rank_scene_candidates(candidates, sentence="", keyword=""):
    ranked = []
    for index, candidate in enumerate(candidates or []):
        candidate.provider_order = index
        candidate.relevance = query_relevance(candidate, sentence=sentence, keyword=keyword)
        ranked.append(candidate)
    ranked.sort(key=lambda item: (-item.relevance, item.provider_order))
    return ranked


def dedupe_candidates(candidates, seen=None):
    seen = seen if seen is not None else set()
    unique = []
    for candidate in candidates or []:
        keys = candidate.identity_keys()
        if not keys:
            continue
        if any(key in seen for key in keys):
            continue
        for key in keys:
            seen.add(key)
        unique.append(candidate)
    return unique


def round_robin_order(groups):
    """Yield (scene_index, candidate) first-of-each-scene before any second."""
    groups = list(groups or [])
    index = 0
    while True:
        yielded = False
        for scene_index, group in enumerate(groups):
            if index < len(group):
                yield scene_index, group[index]
                yielded = True
        if not yielded:
            return
        index += 1


def unique_usable_duration(selected_by_scene, clip_duration):
    seen = set()
    total = 0.0
    for group in selected_by_scene or []:
        for candidate in group:
            identity = candidate.identity_keys() or (candidate.local_path,)
            if any(key in seen for key in identity):
                continue
            for key in identity:
                seen.add(key)
            total += usable_duration(candidate.duration, clip_duration)
    return total


def adjacent_reuse(selected_by_scene):
    previous = set()
    for group in selected_by_scene or []:
        current = set()
        for candidate in group:
            current.update(candidate.identity_keys() or {candidate.local_path})
        if previous and current and previous.intersection(current):
            return True
        previous = current
    return False


def coverage_failures(selected_by_scene, scenes, clip_duration, narration_duration, provider):
    failures = []
    unique_total = unique_usable_duration(selected_by_scene, clip_duration)
    required_total = float(narration_duration or 0)
    for index, scene in enumerate(scenes or []):
        chosen = (selected_by_scene or [None])[index] if index < len(selected_by_scene or []) else []
        available = sum(usable_duration(item.duration, clip_duration) for item in (chosen or []))
        required = float(scene.get("required_duration") or clip_duration or 0)
        query = scene.get("keyword") or scene.get("query") or ""
        if available + 1e-6 < required:
            failures.append(
                {
                    "scene": index + 1,
                    "query": query,
                    "provider": provider,
                    "required_duration": required,
                    "available_duration": available,
                }
            )
    if required_total and unique_total + 1e-6 < required_total:
        first = failures[0] if failures else {
            "scene": 1,
            "query": (scenes[0].get("keyword") if scenes else ""),
            "provider": provider,
            "required_duration": required_total,
            "available_duration": unique_total,
        }
        first = dict(first)
        first["unique_usable_duration"] = unique_total
        first["narration_duration"] = required_total
        if not failures:
            failures.append(first)
        else:
            failures[0]["unique_usable_duration"] = unique_total
            failures[0]["narration_duration"] = required_total
    if adjacent_reuse(selected_by_scene):
        failures.append({"reason": "adjacent source reuse"})
    return failures, unique_total


def format_coverage_error(failures, provider, clip_duration, narration_duration, unique_total):
    first = next((item for item in failures if "scene" in item), failures[0] if failures else {})
    scene = first.get("scene", "?")
    query = first.get("query", "")
    required = first.get("required_duration", clip_duration)
    available = first.get("available_duration", unique_total)
    return (
        f"Scene coverage incomplete: scene {scene}, query={query!r}, provider={provider}, "
        f"required_duration={required}s, available_duration={available}s "
        f"(unique usable {unique_total}s, narration {narration_duration}s, clip_duration={clip_duration}s). "
        "Refusing to loop a single source across the video."
    )


def raise_if_incomplete(selected_by_scene, scenes, clip_duration, narration_duration, provider):
    failures, unique_total = coverage_failures(
        selected_by_scene, scenes, clip_duration, narration_duration, provider
    )
    if failures:
        raise CoverageError(
            format_coverage_error(failures, provider, clip_duration, narration_duration, unique_total)
        )
    return unique_total


def candidate_to_cache_item(candidate: MediaCandidate):
    item = {
        "provider": candidate.provider,
        "asset_id": candidate.asset_id,
        "source_page": public_url(candidate.source_page) or "",
        "creator": candidate.creator,
        "duration": candidate.duration,
        "width": candidate.width,
        "height": candidate.height,
        "rendition": {
            key: value
            for key, value in (candidate.rendition or {}).items()
            if key in {"id", "width", "height"}
        },
    }
    safe_url = url_safe_to_cache(candidate.url)
    if safe_url:
        item["url"] = safe_url
    return item


def candidate_from_cache_item(item, query="", scene_index=0):
    return MediaCandidate(
        provider=str(item.get("provider") or ""),
        asset_id=str(item.get("asset_id") or ""),
        url=str(item.get("url") or ""),
        source_page=str(item.get("source_page") or ""),
        creator=str(item.get("creator") or ""),
        query=query,
        scene_index=scene_index,
        duration=float(item.get("duration") or 0),
        width=int(item.get("width") or 0),
        height=int(item.get("height") or 0),
        rendition=dict(item.get("rendition") or {}),
    )


def cache_key(provider, query, min_duration, aspect):
    payload = json.dumps(
        {
            "provider": str(provider or "").strip().lower(),
            "query": normalize_stock_query(query).lower(),
            "min_duration": int(float(min_duration or 0)),
            "aspect": str(aspect or "9:16"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _lock_for(digest):
    return _LOCKS[int(digest[:8], 16) % len(_LOCKS)]


class SearchCache:
    def __init__(self, directory=None, ttl=CACHE_TTL_SECONDS, now=None):
        self.directory = Path(directory) if directory else Path(__file__).resolve().parent / "storage" / "cache_material_search"
        self.ttl = ttl
        self.now = now or time.time

    def path_for(self, provider, query, min_duration, aspect):
        digest = cache_key(provider, query, min_duration, aspect)
        return self.directory / f"{digest}.json", digest

    def load(self, provider, query, min_duration, aspect):
        path, _digest = self.path_for(provider, query, min_duration, aspect)
        try:
            if not path.is_file():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if int(data.get("version") or 0) != CACHE_FORMAT_VERSION:
                return None
            saved_at = float(data.get("saved_at") or 0)
            if self.now() - saved_at > self.ttl:
                return None
            items = data.get("items") or []
            if not items:
                return None
            return [candidate_from_cache_item(item, query=query) for item in items]
        except Exception:
            return None

    def save(self, provider, query, min_duration, aspect, candidates):
        items = [candidate_to_cache_item(item) for item in (candidates or [])]
        if not items:
            return False
        path, _digest = self.path_for(provider, query, min_duration, aspect)
        payload = {
            "version": CACHE_FORMAT_VERSION,
            "saved_at": self.now(),
            "provider": provider,
            "items": items,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            return True
        except Exception:
            return False


def search_with_cache(cache, provider, query, min_duration, aspect, search_fn):
    """Thread-safe 24h cache. Empty results and failures are not stored."""
    _path, digest = cache.path_for(provider, query, min_duration, aspect)

    def load_valid():
        try:
            cached = cache.load(provider, query, min_duration, aspect)
        except Exception:
            return None
        if not cached:
            return None
        filtered = [
            item
            for item in cached
            if not item.width or not item.height or matches_orientation(item.width, item.height, aspect)
        ]
        if len(filtered) != len(cached):
            return None
        if not any(item.url for item in filtered):
            return None
        return filtered

    try:
        hit = load_valid()
        if hit is not None:
            return hit, "hit"
    except Exception:
        pass

    lock = _lock_for(digest)
    with lock:
        try:
            hit = load_valid()
            if hit is not None:
                return hit, "hit"
        except Exception:
            pass
        try:
            items = list(search_fn() or [])
        except Exception:
            return [], "error"
        if items:
            try:
                cache.save(provider, query, min_duration, aspect, items)
            except Exception:
                pass
        return items, "live"


def redact_manifest_value(value):
    if isinstance(value, str):
        return redact_secret(public_url(value) or value)
    if isinstance(value, dict):
        return {key: redact_manifest_value(item) for key, item in value.items() if key not in {"url", "download_url"}}
    if isinstance(value, list):
        return [redact_manifest_value(item) for item in value]
    return value


def write_source_manifest(project_path, payload):
    path = Path(project_path) / "source_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = redact_manifest_value(payload)
    path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def selected_record(candidate):
    if not candidate:
        return None
    return {
        "provider": candidate.provider,
        "asset_id": candidate.asset_id,
        "source_page": public_url(candidate.source_page) or "",
        "rendition": candidate.rendition,
        "duration": candidate.duration,
        "width": candidate.width,
        "height": candidate.height,
        "local_filename": Path(candidate.local_path).name if candidate.local_path else "",
        "relevance": candidate.relevance,
        "reused": False,
    }
