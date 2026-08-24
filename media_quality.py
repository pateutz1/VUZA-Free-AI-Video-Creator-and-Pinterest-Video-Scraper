"""Technical download validation, fingerprints, and secret redaction."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

MIN_VIDEO_BYTES = 40000
MIN_IMAGE_BYTES = 8000

_URL_USERINFO_RE = re.compile(
    r"((?:https?|wss?)://)([^/\s?#@]*:[^/\s?#@]*@)",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_RE = re.compile(
    r"([?&](?:api[_-]?key|access[_-]?token|token|key|secret|password|signature|jwt)=)([^&#\s]+)",
    re.IGNORECASE,
)
_SIGNED_QUERY_RE = re.compile(
    r"(?:jwt|signature|token|expires|key|api[_-]?key)=",
    re.IGNORECASE,
)


def redact_secret(message, *secrets):
    text = str(message or "")
    text = _URL_USERINFO_RE.sub(r"\1***:***@", text)
    text = _SENSITIVE_QUERY_RE.sub(r"\1***", text)
    for secret in secrets:
        value = str(secret or "").strip()
        if len(value) >= 8:
            text = text.replace(value, "***")
    return text


def is_signed_url(url):
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False
    if parsed.username or parsed.password:
        return True
    return bool(parsed.query and _SIGNED_QUERY_RE.search(parsed.query))


def public_url(url):
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def canonical_url(url):
    public = public_url(url)
    if public:
        return public
    if isinstance(url, str) and url.strip():
        return url.strip().split("?", 1)[0]
    return ""


def url_safe_to_cache(url):
    if not isinstance(url, str) or not url.strip():
        return None
    if is_signed_url(url):
        return None
    return url.strip()


def content_fingerprint(path):
    path = Path(path)
    digest = hashlib.md5()
    digest.update(str(path.stat().st_size).encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(65536))
    return digest.hexdigest()


def download_http(url, path, timeout=60, min_bytes=MIN_VIDEO_BYTES, verify=True):
    """Download url to path. Returns True on non-empty valid HTTP body."""
    path = Path(path)
    try:
        response = requests.get(url, timeout=timeout, verify=verify)
        if response.status_code != 200 or not response.content:
            return False
        if len(response.content) < min_bytes:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
        return path.exists() and path.stat().st_size >= min_bytes
    except Exception:
        return False


def probe_video(path):
    from moviepy import VideoFileClip

    clip = VideoFileClip(str(path))
    try:
        width, height = clip.size if clip.size else (0, 0)
        return {
            "duration": float(clip.duration or 0),
            "fps": float(clip.fps or 0),
            "width": int(width or 0),
            "height": int(height or 0),
        }
    finally:
        clip.close()


def overlay_text_score(gray):
    import numpy as np

    gray = np.asarray(gray, dtype="float32")
    if gray.ndim != 2 or gray.size == 0:
        return 0.0
    height, width = gray.shape
    if height < 16 or width < 16:
        return 0.0
    dx = np.abs(np.diff(gray, axis=1))
    dy = np.abs(np.diff(gray, axis=0))
    height_e = min(dx.shape[0], dy.shape[0])
    width_e = min(dx.shape[1], dy.shape[1])
    edge = (dx[:height_e, :width_e] > 28) & (dy[:height_e, :width_e] > 18)

    def _parts(region):
        if region.size == 0:
            return []
        width_r = region.shape[1]
        return [
            region,
            region[:, : max(1, width_r // 3)],
            region[:, width_r // 3 : 2 * width_r // 3],
            region[:, 2 * width_r // 3 :],
        ]

    def band_score(y0, y1):
        scores = []
        edge_band = edge[y0:min(y1, edge.shape[0])]
        for part in _parts(edge_band):
            if part.size == 0:
                continue
            scores.append(float((part.mean(axis=1) > 0.06).mean()))
        gray_band = gray[y0:min(y1, height), :width]
        for part in _parts(gray_band):
            if part.size == 0:
                continue
            bright = float((part > 200).mean())
            if 0.08 <= bright <= 0.55:
                scores.append(min(bright * 1.4, 1.0))
        return max(scores) if scores else 0.0

    top = band_score(0, max(1, int(height * 0.30)))
    mid = band_score(int(height * 0.28), int(height * 0.72))
    bottom = band_score(int(height * 0.68), height)
    return max(top, mid, bottom)


def video_has_overlay_text(path):
    try:
        from moviepy import VideoFileClip
        import numpy as np
    except Exception:
        return False
    clip = None
    try:
        clip = VideoFileClip(str(path))
        duration = float(clip.duration or 0)
        if duration <= 0:
            return False
        stamps = [
            min(max(duration * 0.05, 0), max(duration - 0.05, 0)),
            min(max(duration * 0.35, 0), max(duration - 0.05, 0)),
        ]
        if duration > 2:
            stamps.append(min(max(duration * 0.7, 0), max(duration - 0.05, 0)))
        for stamp in stamps:
            frame = clip.get_frame(stamp)
            if frame is None:
                continue
            gray = np.mean(frame, axis=2)
            if overlay_text_score(gray) >= 0.28:
                return True
        return False
    except Exception:
        return False
    finally:
        if clip is not None:
            try:
                clip.close()
            except Exception:
                pass


def validate_downloaded_video(path, probe=None):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_VIDEO_BYTES:
        return False, "empty or too small"
    try:
        info = (probe or probe_video)(path)
    except Exception as exc:
        return False, f"undecodable: {type(exc).__name__}"
    duration = float(info.get("duration") or 0)
    fps = float(info.get("fps") or 0)
    if duration <= 0 or fps <= 0:
        return False, "non-positive duration or fps"
    if probe is None and video_has_overlay_text(path):
        return False, "text overlay"
    return True, info


def delete_rejected_file(path, newly_downloaded=True):
    if not newly_downloaded:
        return False
    path = Path(path)
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False
