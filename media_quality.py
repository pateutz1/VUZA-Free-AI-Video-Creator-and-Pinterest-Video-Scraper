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
_DOWNLOAD_FAIL_LOG_MAX = 5
_download_fail_logs = 0
_last_download_error = ""
DEFAULT_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

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


def reset_download_fail_logs():
    global _download_fail_logs, _last_download_error
    _download_fail_logs = 0
    _last_download_error = ""


def last_download_error():
    return _last_download_error


def set_download_error(reason):
    global _last_download_error
    _last_download_error = str(reason or "")


def download_headers_for(url):
    headers = dict(DEFAULT_DOWNLOAD_HEADERS)
    lowered = str(url or "").lower()
    if "pinimg.com" in lowered or "pinterest." in lowered:
        headers["Referer"] = "https://www.pinterest.com/"
        headers["Origin"] = "https://www.pinterest.com"
    return headers


def _log_download_fail(url, reason):
    global _download_fail_logs
    set_download_error(reason)
    if _download_fail_logs >= _DOWNLOAD_FAIL_LOG_MAX:
        return
    _download_fail_logs += 1
    msg = f"  download fail {_download_fail_logs}/{_DOWNLOAD_FAIL_LOG_MAX}: {reason} | {redact_secret(url)}"
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


def download_http(url, path, timeout=60, min_bytes=MIN_VIDEO_BYTES, verify=True):
    """Download url to path. Returns True on non-empty valid HTTP body."""
    path = Path(path)
    try:
        response = requests.get(
            url,
            timeout=timeout,
            verify=verify,
            headers=download_headers_for(url),
            allow_redirects=True,
        )
        if response.status_code != 200 or not response.content:
            size = len(response.content or b"")
            _log_download_fail(url, f"HTTP {response.status_code} bytes={size}")
            return False
        if len(response.content) < min_bytes:
            _log_download_fail(url, f"too small {len(response.content)}B (need {min_bytes})")
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
        return path.exists() and path.stat().st_size >= min_bytes
    except Exception as exc:
        _log_download_fail(url, f"{type(exc).__name__}: {redact_secret(exc)}")
        return False


def probe_video(path):
    from moviepy import VideoFileClip

    clip = VideoFileClip(str(path), audio=False)
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


def caption_line_score(gray):
    """Fraction of frame height occupied by the thickest locally-bright letter row-run."""
    import numpy as np

    gray = np.asarray(gray, dtype="float32")
    if gray.ndim != 2 or gray.size == 0:
        return 0.0
    height, width = gray.shape
    if height < 16 or width < 16:
        return 0.0
    step = max(1, height // 240)
    small = gray[::step, ::step]
    rows, cols = small.shape
    hits = np.zeros(rows, dtype=np.uint8)
    max_letter = max(3, int(cols * 0.38))
    for y in range(rows):
        row = small[y]
        median = float(np.median(row))
        hot = row > max(median + 45, 155)
        padded = np.concatenate([[0], hot.astype(np.int8), [0]])
        delta = np.diff(padded)
        starts = np.where(delta == 1)[0]
        ends = np.where(delta == -1)[0]
        widths = ends - starts
        letters = widths[(widths >= 2) & (widths <= max_letter)]
        cover = float(letters.sum()) / max(cols, 1)
        if 2 <= len(letters) <= 20 and 0.07 <= cover <= 0.65:
            hits[y] = 1
    best = run = 0
    for value in hits:
        run = run + 1 if value else 0
        best = max(best, run)
    return best / max(rows, 1)


def red_banner_score(frame):
    import numpy as np

    frame = np.asarray(frame)
    if frame.ndim != 3 or frame.shape[2] < 3:
        return 0.0
    red = frame[:, :, 0].astype("float32")
    green = frame[:, :, 1].astype("float32")
    blue = frame[:, :, 2].astype("float32")
    mask = (red > 155) & (green < 90) & (blue < 90)
    height, width = mask.shape
    top = mask[: max(1, int(height * 0.28))]
    scores = [float(top.mean())]
    third = max(1, width // 3)
    scores.append(float(top[:, :third].mean()))
    scores.append(float(top[:, third: 2 * third].mean()))
    scores.append(float(top[:, 2 * third:].mean()))
    return max(scores)


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


def compact_caption_score(gray):
    """Short centered white caption line (e.g. 'Let down?'), not gym lights or clothing."""
    import numpy as np

    gray = np.asarray(gray, dtype="float32")
    if gray.ndim != 2 or gray.size == 0:
        return 0.0
    height, width = gray.shape
    if height < 32 or width < 32:
        return 0.0
    bright = gray > 200
    counts = bright.sum(axis=1)
    min_px = max(8, width // 90)
    max_px = max(min_px + 1, int(width * 0.40))
    hits = (counts >= min_px) & (counts <= max_px)
    best = run = 0
    best_s = best_e = cur_s = 0
    for index, hit in enumerate(hits):
        if hit:
            if run == 0:
                cur_s = index
            run += 1
            if run > best:
                best = run
                best_s, best_e = cur_s, index
        else:
            run = 0
    min_h = max(5, height // 180)
    max_h = max(28, height // 22)
    if best < min_h or best > max_h:
        return 0.0
    band = bright[best_s:best_e + 1]
    xs = np.where(band.any(axis=0))[0]
    if len(xs) < 2:
        return 0.0
    span = int(xs[-1] - xs[0] + 1)
    if span < max(24, int(width * 0.06)) or span > int(width * 0.72):
        return 0.0
    fill = float(band.mean())
    if fill < 0.012 or fill > 0.22:
        return 0.0
    mid_y = (best_s + best_e) / 2 / max(height, 1)
    if mid_y < 0.12 or mid_y > 0.88:
        return 0.0
    dark = gray < 90
    ring = (
        np.roll(dark, 1, 0)
        | np.roll(dark, -1, 0)
        | np.roll(dark, 1, 1)
        | np.roll(dark, -1, 1)
    )
    glyph_fill = float((bright & ring)[best_s:best_e + 1].mean())
    if glyph_fill < 0.006:
        return 0.0
    cols = band.any(axis=0)
    blobs = 0
    run_w = 0
    max_letter = max(4, int(band.shape[1] * 0.18))
    for lit in cols:
        if lit:
            run_w += 1
            continue
        if 2 <= run_w <= max_letter:
            blobs += 1
        run_w = 0
    if 2 <= run_w <= max_letter:
        blobs += 1
    if blobs < 3:
        return 0.0
    return best / max(height, 1)


def frame_is_unusable(frame):
    import numpy as np

    arr = np.asarray(frame)
    if arr.size == 0:
        return True
    return float(arr.mean()) < 16 or float(arr.std()) < 7


def frames_are_frozen(frames):
    import numpy as np

    if len(frames) < 2:
        return False
    first = frames[0].astype("float32")
    diffs = [
        float(np.mean(np.abs(frame.astype("float32") - first)))
        for frame in frames[1:]
    ]
    return bool(diffs) and max(diffs) < 2.5


def overlay_reason(frame):
    """Which overlay heuristic fired, or None. Gym lights/bars often trip a
    single mid-frame caption-line hit around 0.05 of frame height; require a
    thicker run (0.10) so those don't count as burned-in text."""
    import numpy as np

    if frame is None:
        return None
    gray = np.mean(frame, axis=2)
    if overlay_text_score(gray) >= 0.28:
        return "edges"
    if caption_line_score(gray) >= 0.10:
        return "caption-line"
    if compact_caption_score(gray) >= 0.004:
        return "compact-caption"
    if red_banner_score(frame) >= 0.04:
        return "red-banner"
    return None


def frame_has_overlay(frame):
    return overlay_reason(frame) is not None


def video_has_overlay_text(path):
    """Disabled: Mixkit/Pexels/Pixabay/Coverr stock is clean; Pinterest is off the UI."""
    return False


def probe_and_scan_overlay(path):
    from moviepy import VideoFileClip

    clip = VideoFileClip(str(path), audio=False)
    try:
        width, height = clip.size if clip.size else (0, 0)
        info = {
            "duration": float(clip.duration or 0),
            "fps": float(clip.fps or 0),
            "width": int(width or 0),
            "height": int(height or 0),
        }
        duration = info["duration"]
        if duration <= 0 or info["fps"] <= 0:
            return info, "non-positive duration or fps"
        quality_frames = []
        for frac in (0.25, 0.5, 0.75):
            stamp = min(max(duration * frac, 0), max(duration - 0.05, 0))
            frame = clip.get_frame(stamp)
            if frame_is_unusable(frame):
                return info, "dark or frozen"
            quality_frames.append(frame)
        if frames_are_frozen(quality_frames):
            return info, "dark or frozen"
        return info, None
    finally:
        clip.close()


def validate_downloaded_video(path, probe=None):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_VIDEO_BYTES:
        return False, "empty or too small"
    try:
        if probe is None:
            info, reason = probe_and_scan_overlay(path)
            if reason:
                return False, reason
        else:
            info = probe(path)
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
