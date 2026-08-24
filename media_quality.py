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
