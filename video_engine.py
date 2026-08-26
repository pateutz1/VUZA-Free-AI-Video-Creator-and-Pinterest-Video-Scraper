import asyncio
import html
import json
import os
import re
import sys
from pathlib import Path
from edge_tts import Communicate
from moviepy import VideoFileClip, ImageClip, AudioFileClip, TextClip, CompositeVideoClip, concatenate_videoclips
from moviepy.video import fx as vfx
from media_quality import frame_is_unusable, frames_are_frozen

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

# ═══════════════════════════════════════════════════════════════
# ANTIGRAVITY VIDEO ENGINE (MOVIEPY + EDGE-TTS)
# ═══════════════════════════════════════════════════════════════

ENGINE_DIR = Path(__file__).resolve().parent
FONT_DIR = ENGINE_DIR / "static" / "fonts"
LATIN_CAPTION_FONTS = {
    "BeVietnamPro-Bold.ttf",
    "BeVietnamPro-Medium.ttf",
    "Charm-Bold.ttf",
    "Charm-Regular.ttf",
    "UTM Kabel KT.ttf",
}
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "static/fonts/NotoSansSC-Bold.ttf",
    "static/fonts/NotoSansSC-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "arialbd.ttf",
    "arial.ttf",
]


def _font_in_dirs(name):
    name = Path(name or "").name
    if not name:
        return None
    path = FONT_DIR / name
    if path.is_file():
        return str(path)
    return None


def resolve_font_path(font_name=""):
    chosen = _font_in_dirs(font_name)
    if chosen:
        return chosen
    for preferred in ("BeVietnamPro-Bold.ttf", "BeVietnamPro-Medium.ttf", "MicrosoftYaHeiBold.ttc"):
        found = _font_in_dirs(preferred)
        if found:
            return found
    for font_path in FONT_CANDIDATES:
        if os.path.exists(font_path):
            return font_path
    return None


def subtitle_top(frame_h, text_h, position="bottom", custom_pct=70.0):
    pos = (position or "bottom").strip().lower()
    if pos == "top":
        pct = 12.0
    elif pos == "center":
        pct = 50.0
    elif pos == "custom":
        try:
            pct = float(custom_pct)
        except (TypeError, ValueError):
            pct = 70.0
    else:
        pct = 80.0
    pct = max(0.0, min(100.0, pct))
    y = frame_h * (pct / 100.0) - text_h / 2.0
    margin = 24
    return max(margin, min(max(margin, frame_h - text_h - margin), y))


def parse_azure_voice_name(name):
    return (name or "").replace("-Female", "").replace("-Male", "").strip()


def azure_v2_synthesis_name(name):
    parsed = parse_azure_voice_name(name)
    if parsed.endswith("-V2"):
        return parsed[:-3]
    return ""


def contains_cjk(text):
    return bool(re.search(r'[\u3400-\u9fff]', text or ""))


def scene_visual_plan(speech_seconds, clip_count, clip_duration):
    """Show exactly assets-per-scene clips. Visual length = assets × clip_duration."""
    n = max(1, int(clip_count or 1))
    slot = max(2.0, min(12.0, float(clip_duration or 5)))
    visual = n * slot
    return n, slot, visual


def trim_audio_to(audio_clip, target_seconds):
    current = float(audio_clip.duration or 0)
    target = max(0.8, float(target_seconds or 0))
    if target >= current - 0.05:
        return audio_clip
    try:
        return audio_clip.subclipped(0, target)
    except Exception:
        try:
            return audio_clip.with_duration(target)
        except Exception:
            return audio_clip


def pad_audio_to(audio_clip, target_seconds):
    current = float(audio_clip.duration or 0)
    target = float(target_seconds or 0)
    if target <= current + 0.05:
        return audio_clip
    try:
        import numpy as np
        from moviepy import AudioArrayClip, concatenate_audioclips
        fps = int(getattr(audio_clip, "fps", None) or 44100)
        extra = target - current
        n_samples = max(1, int(extra * fps))
        nch = max(1, int(getattr(audio_clip, "nchannels", 2) or 2))
        silence = AudioArrayClip(np.zeros((n_samples, nch), dtype="float32"), fps=fps)
        return concatenate_audioclips([audio_clip, silence])
    except Exception:
        try:
            return audio_clip.with_duration(target)
        except Exception:
            return audio_clip

def wrap_text_lines(text, draw, font, max_width):
    if not text:
        return [""]

    if contains_cjk(text) and " " not in text:
        lines, current = [], ""
        for char in text:
            candidate = current + char
            if current and draw.textlength(candidate, font=font) > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines

    lines, curr_line = [], []
    for word in text.split():
        candidate = " ".join(curr_line + [word])
        if curr_line and draw.textlength(candidate, font=font) > max_width:
            lines.append(" ".join(curr_line))
            curr_line = [word]
        elif draw.textlength(candidate, font=font) > max_width:
            chunk = ""
            for char in word:
                next_chunk = chunk + char
                if chunk and draw.textlength(next_chunk, font=font) > max_width:
                    lines.append(chunk)
                    chunk = char
                else:
                    chunk = next_chunk
            curr_line = [chunk] if chunk else []
        else:
            curr_line.append(word)
    if curr_line:
        lines.append(" ".join(curr_line))
    return lines or [text]

def chunk_subtitle_text(text, words_per_chunk=3, cjk_chars_per_chunk=8):
    if contains_cjk(text) and " " not in text:
        return [text[i:i + cjk_chars_per_chunk] for i in range(0, len(text), cjk_chars_per_chunk)] or [text]

    words = text.split()
    return [" ".join(words[i:i + words_per_chunk]) for i in range(0, len(words), words_per_chunk)] or [text]


EDGE_TTS_TICKS = 10_000_000.0


def _normalize_word_timing(item):
    word = (item.get("word") or item.get("text") or "").strip()
    if not word or item.get("type") in ("SentenceBoundary", "audio"):
        return None
    offset = float(item.get("offset") or 0)
    duration = float(item.get("duration") or 0)
    if offset > 1000 or duration > 1000:
        offset /= EDGE_TTS_TICKS
        duration /= EDGE_TTS_TICKS
    return {"word": word, "offset": offset, "duration": duration}


def parse_word_timings(path):
    path = Path(path)
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    items = []
    if raw.startswith("["):
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return []
        items = loaded if isinstance(loaded, list) else []
    else:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") and item.get("type") != "WordBoundary":
            continue
        row = _normalize_word_timing(item)
        if row:
            out.append(row)
    return out


def caption_chunk_windows(chunks, word_timings, speech_duration):
    """Hold each caption until the next spoken chunk starts."""
    total = max(0.5, float(speech_duration or 0.5))
    if not chunks:
        return []
    if not word_timings:
        even = total / len(chunks)
        return [(chunk, i * even, even) for i, chunk in enumerate(chunks)]

    starts = []
    word_idx = 0
    for chunk in chunks:
        n_words = max(1, len(re.findall(r"[A-Za-z0-9']+", chunk)))
        if word_idx < len(word_timings):
            starts.append(float(word_timings[word_idx]["offset"]))
        else:
            starts.append(None)
        word_idx += n_words

    windows = []
    for i, chunk in enumerate(chunks):
        start = starts[i]
        if start is None:
            prev_end = (windows[-1][1] + windows[-1][2]) if windows else 0.0
            leftover = len(chunks) - i
            dur = max(0.45, (total - prev_end) / leftover)
            windows.append((chunk, prev_end, dur))
            continue
        if i + 1 < len(starts) and starts[i + 1] is not None:
            end = starts[i + 1]
        else:
            end = total
        dur = max(0.45, end - start)
        windows.append((chunk, max(0.0, start), dur))
    return windows

def apply_ken_burns(clip, duration):
    """Applies a slow zoom-in effect (Ken Burns)."""
    return clip

def apply_zoom_in(clip, duration):
    """Dramatic zoom in."""
    return clip.resized(lambda t: 1 + 0.3 * t / duration)

def apply_zoom_out(clip, duration):
    """Dramatic zoom out."""
    return clip.resized(lambda t: 1.3 - 0.3 * t / duration)

def apply_slide_left(clip, duration):
    """Slides the clip from right to left."""
    w, h = clip.size
    return clip.with_position(lambda t: (max(0, w * (1 - 5*t/duration)), "center"))

def apply_glitch(clip, duration):
    """Simulates a glitch effect by random shifting."""
    return clip

def crop_window_score(frames, x1, y1, x2, y2):
    import numpy as np
    total = 0.0
    prev = None
    for frame in frames or []:
        if frame is None:
            continue
        region = frame[y1:y2, x1:x2]
        if region.size == 0:
            continue
        small = region[::4, ::4]
        gray = small.mean(axis=2) if small.ndim == 3 else small.astype("float32")
        gx = float(np.abs(np.diff(gray, axis=1)).mean()) if gray.shape[1] > 1 else 0.0
        gy = float(np.abs(np.diff(gray, axis=0)).mean()) if gray.shape[0] > 1 else 0.0
        score = float(gray.std()) + gx * 2 + gy
        if prev is not None and prev.shape == small.shape:
            score += float(np.abs(small.astype("int16") - prev.astype("int16")).mean()) * 3
        prev = small
        total += score
    return total


def best_crop_box(frames, src_w, src_h, target_w, target_h, steps=7):
    src_w, src_h = int(src_w), int(src_h)
    target_w, target_h = max(1, int(target_w)), max(1, int(target_h))
    if src_w < 2 or src_h < 2:
        return 0, 0, max(1, src_w), max(1, src_h)
    src_aspect = src_w / src_h
    target_aspect = target_w / target_h
    if abs(src_aspect - target_aspect) < 0.03:
        return 0, 0, src_w, src_h
    usable = [frame for frame in (frames or []) if frame is not None]
    if src_aspect > target_aspect:
        win_w = max(1, min(src_w, int(round(src_h * target_aspect))))
        max_x = max(0, src_w - win_w)
        if not usable:
            return max_x // 2, 0, win_w, src_h
        best_x, best_s = max_x // 2, -1.0
        for i in range(max(2, steps)):
            x = 0 if max_x == 0 else int(round(max_x * i / (steps - 1)))
            score = crop_window_score(usable, x, 0, x + win_w, src_h)
            if score > best_s:
                best_s, best_x = score, x
        return best_x, 0, win_w, src_h
    win_h = max(1, min(src_h, int(round(src_w / target_aspect))))
    max_y = max(0, src_h - win_h)
    if not usable:
        return 0, max_y // 2, src_w, win_h
    best_y, best_s = max_y // 2, -1.0
    for i in range(max(2, steps)):
        y = 0 if max_y == 0 else int(round(max_y * i / (steps - 1)))
        score = crop_window_score(usable, 0, y, src_w, y + win_h)
        if score > best_s:
            best_s, best_y = score, y
    return 0, best_y, src_w, win_h


def crop_to_frame(clip, w, h):
    cw, ch = clip.size
    if abs((cw / max(ch, 1)) - (w / max(h, 1))) < 0.03:
        return clip.resized((w, h))
    frames = []
    duration = float(getattr(clip, "duration", 0) or 0)
    times = [0.0] if duration <= 0.05 else [duration * t for t in (0.2, 0.5, 0.8)]
    for t in times:
        try:
            frames.append(clip.get_frame(min(max(t, 0.0), max(duration - 0.02, 0.0))))
        except Exception:
            continue
    x, y, nw, nh = best_crop_box(frames, cw, ch, w, h)
    return clip.cropped(x1=x, y1=y, width=nw, height=nh).resized((w, h))


def crop_center(clip, w, h):
    return crop_to_frame(clip, w, h)

def clip_is_unusable(clip):
    if clip is None or getattr(clip, "duration", 0) < 1.15:
        return True
    try:
        times = [clip.duration * t for t in (0.25, 0.5, 0.75)]
        frames = [clip.get_frame(min(t, max(clip.duration - 0.02, 0))) for t in times]
        if any(frame_is_unusable(f) for f in frames):
            return True
        return frames_are_frozen(frames)
    except Exception:
        return False

class SubtitleHelper:
    @staticmethod
    def insert_emojis(text):
        """Inserts emojis based on common keywords for higher retention."""
        emoji_map = {
            "money": "💰", "cash": "💸", "rich": "🤑", "success": "🏆", "win": "🥇",
            "happy": "😊", "love": "❤️", "sad": "😢", "angry": "😡", "fear": "😨",
            "space": "🚀", "stars": "✨", "future": "🤖", "tech": "💻", "ai": "🧠",
            "ocean": "🌊", "beach": "🏖️", "sun": "☀️", "night": "🌙", "fire": "🔥",
            "water": "💧", "earth": "🌍", "nature": "🌿", "forest": "🌲", "mountain": "🏔️",
            "food": "🍕", "health": "🥗", "fitness": "💪", "gym": "🏋️", "sport": "⚽",
            "book": "📚", "idea": "💡", "learn": "🧠", "school": "🏫", "work": "💼",
            "travel": "✈️", "plane": "🛫", "car": "🚗", "city": "🏙️", "home": "🏠",
            "time": "⏰", "fast": "⚡", "slow": "🐌", "stop": "🛑", "go": "🚦",
            "strong": "💪", "stronger": "💪", "strength": "💪", "excuses": "🚫",
            "unlock": "🔓", "ready": "🔥", "push": "🏋️", "motivation": "🔥",
            "workout": "🏋️", "lift": "🏋️", "warrior": "⚔️", "power": "⚡",
            "never": "🚫", "now": "🔥", "yes": "✅",
        }
        words = text.split()
        inserted = False
        for i, word in enumerate(words):
            clean_word = re.sub(r'[^\w]', '', word.lower())
            if clean_word in emoji_map:
                words[i] = f"{word} {emoji_map[clean_word]}"
                inserted = True
        joined = " ".join(words)
        if not inserted and joined.strip():
            joined = f"{joined} 🔥"
        return joined

class VideoEngine:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = self.output_dir / "temp"
        self.temp_dir.mkdir(exist_ok=True)
        self.eleven_key = None
        self.azure_speech_key = None
        self.azure_speech_region = None
        self.tts_server = "azure-tts-v1"

    def set_eleven_key(self, key):
        self.eleven_key = key

    def set_azure_speech(self, key, region):
        self.azure_speech_key = (key or "").strip()
        self.azure_speech_region = (region or "").strip()

    async def generate_voiceover(self, text, idx, voice="en-US-ChristopherNeural", language="en-US", voice_rate=1.0, voice_volume=1.0, timeout_seconds=60):
        """Generates TTS audio for a single sentence with retries. Supports Edge-TTS, Azure V2, and ElevenLabs."""
        if not text or not text.strip():
            return None

        if not voice or voice == "default":
            voice_defaults = {
                "en-US": "en-US-ChristopherNeural",
                "en-GB": "en-GB-RyanNeural",
                "es-ES": "es-ES-AlvaroNeural",
                "fr-FR": "fr-FR-HenriNeural",
                "de-DE": "de-DE-ConradNeural",
                "it-IT": "it-IT-DiegoNeural",
                "hi-IN": "hi-IN-MadhurNeural",
                "ur-PK": "ur-PK-AsadNeural",
                "zh-CN": "zh-CN-YunyangNeural",
                "ja-JP": "ja-JP-KeitaNeural"
            }
            voice = voice_defaults.get(language, "en-US-ChristopherNeural")

        if voice.startswith("eleven_"):
            return await self._generate_elevenlabs(text, idx, voice.replace("eleven_", ""))

        azure_name = parse_azure_voice_name(voice)
        v2_name = azure_v2_synthesis_name(voice)
        use_v2 = self.tts_server == "azure-tts-v2" or bool(v2_name)
        if use_v2:
            return await self._generate_azure_v2(text, idx, v2_name or azure_name, voice_rate, voice_volume, timeout_seconds)

        rate_pct = int(round((float(voice_rate or 1.0) - 1.0) * 100))
        rate = f"{rate_pct:+d}%"
        max_retries = 3
        for attempt in range(max_retries):
            try:
                communicate = Communicate(text, azure_name, rate=rate, boundary="WordBoundary")
                path = self.temp_dir / f"speech_{idx}.mp3"
                timing_path = self.temp_dir / f"speech_{idx}_timings.json"
                await asyncio.wait_for(
                    communicate.save(str(path), metadata_fname=str(timing_path)),
                    timeout=timeout_seconds,
                )
                
                if float(voice_volume or 1.0) != 1.0 and path.exists():
                    from moviepy import AudioFileClip
                    clip = AudioFileClip(str(path)).with_volume_scaled(float(voice_volume))
                    clip.write_audiofile(str(path), logger=None)
                    clip.close()
                return str(path)
            except Exception as e:
                print(f"⚠️ TTS Attempt {attempt+1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    print(f"❌ TTS Error after {max_retries} attempts: {e}")
                    return None

    async def _generate_azure_v2(self, text, idx, voice_name, voice_rate, voice_volume, timeout_seconds):
        if not self.azure_speech_key or not self.azure_speech_region:
            print("⚠️ Azure Speech key or region missing")
            return None
        path = self.temp_dir / f"speech_{idx}.mp3"
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._azure_v2_rest, text, voice_name, voice_rate, path),
                timeout=timeout_seconds,
            )
        except Exception as e:
            print(f"❌ Azure TTS V2 error: {e}")
            return None
        if not path.exists():
            return None
        if float(voice_volume or 1.0) != 1.0:
            from moviepy import AudioFileClip
            clip = AudioFileClip(str(path)).with_volume_scaled(float(voice_volume))
            clip.write_audiofile(str(path), logger=None)
            clip.close()
        return str(path)

    def _azure_v2_rest(self, text, voice_name, voice_rate, path):
        import requests
        rate = max(0.25, min(4.0, float(voice_rate or 1.0)))
        parts = voice_name.split("-", 2)
        locale = "-".join(parts[:2]) if len(parts) >= 2 else "en-US"
        ssml = (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xml:lang="{html.escape(locale)}">'
            f'<voice name="{html.escape(voice_name, quote=True)}">'
            f'<prosody rate="{rate:g}">{html.escape(text)}</prosody>'
            "</voice></speak>"
        )
        url = f"https://{self.azure_speech_region}.tts.speech.microsoft.com/cognitiveservices/v1"
        response = requests.post(
            url,
            headers={
                "Ocp-Apim-Subscription-Key": self.azure_speech_key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
                "User-Agent": "VUZA",
            },
            data=ssml.encode("utf-8"),
            timeout=45,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Azure TTS HTTP {response.status_code}: {response.text[:240]}")
        path.write_bytes(response.content)

    async def _generate_elevenlabs(self, text, idx, voice_id):
        """Generates TTS using ElevenLabs API."""
        if not self.eleven_key:
            print("⚠️ ElevenLabs API key missing!")
            return None

        import requests
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        headers = {
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": self.eleven_key
        }
        data = {
            "text": text,
            "model_id": "eleven_monolingual_v1",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.5}
        }

        try:
            path = self.temp_dir / f"speech_{idx}.mp3"
            response = await asyncio.to_thread(requests.post, url, json=data, headers=headers)
            if response.status_code == 200:
                with open(path, "wb") as f:
                    f.write(response.content)
                return str(path)
            else:
                print(f"❌ ElevenLabs Error: {response.text}")
                return None
        except Exception as e:
            print(f"⚠️ ElevenLabs Exception: {e}")
            return None

    def generate_thumbnail(self, video_path, title):
        """Generates a viral thumbnail from the video."""
        print(f"🖼️ Generating Thumbnail for: {title}")
        try:
            clip = VideoFileClip(video_path)
            # Take a frame at 1/3 of the video
            frame_t = clip.duration / 3
            frame_path = self.output_dir / "thumbnail_raw.jpg"
            clip.save_frame(str(frame_path), t=frame_t)

            # Use PIL to add text
            from PIL import Image, ImageDraw, ImageFont
            img = Image.open(frame_path)
            draw = ImageDraw.Draw(img)
            w, h = img.size

            # Title text
            try:
                font_path = resolve_font_path()
                font = ImageFont.truetype(font_path, 120) if font_path else ImageFont.load_default()
            except:
                font = ImageFont.load_default()

            # Draw shadow/outline
            tw = draw.textlength(title[:30], font=font)
            tx, ty = (w - tw) / 2, h / 2
            for off in range(-5, 6):
                draw.text((tx+off, ty+off), title[:30], font=font, fill="black")
            draw.text((tx, ty), title[:30], font=font, fill="yellow")

            thumb_path = self.output_dir / "thumbnail.jpg"
            img.save(thumb_path)
            clip.close()
            return str(thumb_path)
        except Exception as e:
            print(f"⚠️ Thumbnail generation failed: {e}")
            return None

    def create_video(self, script_data, project_path, media_type="video", bg_music=None, settings=None, output_name="final_aesthetic_video.mp4"):
        """Assembles the final video from script chunks and downloaded media."""
        print("🎬 Starting Video Assembly...")

        final_clips = []
        watermark_clip = None

        # Load Watermark if available
        if settings and getattr(settings, 'watermark', False):
            logo_path = getattr(settings, 'logo_path', "static/logo.png") # default
            if os.path.exists(logo_path):
                watermark_clip = ImageClip(logo_path).with_duration(10).resized(width=150).with_opacity(0.5)
        bg_audio = None

        # Load BG Music if needed (Loop it later)
        if bg_music and os.path.exists(bg_music):
            pass # We will add it at the end

        used_source_keys = []
        used_source_set = set()

        for i, item in enumerate(script_data):
            sentence = item["sentence"]
            keyword = item["keyword"]
            audio_path = str(self.temp_dir / f"speech_{i}.mp3")

            if not os.path.exists(audio_path) or os.path.getsize(audio_path) <= 0:
                raise RuntimeError(f"Missing TTS for scene {i + 1}: {audio_path}")

            # Create Audio Clip
            audio_clip = AudioFileClip(audio_path)
            speech_duration = max(float(audio_clip.duration), 0.8)
            duration = speech_duration

            explicit_files = [
                str(Path(f)) for f in item.get("_files", [])
                if Path(f).exists() and Path(f).stat().st_size > 40000
            ]

            media_folder = None
            if media_type != "video":
                if (project_path / keyword).exists():
                    media_folder = project_path / keyword
                else:
                    safe_keyword = re.sub(r'[^\w\-]', '_', keyword)[:40]
                    if safe_keyword and (project_path / safe_keyword).exists():
                        media_folder = project_path / safe_keyword

            folder_files = []
            if media_folder:
                folder_files = sorted([
                    str(f) for f in media_folder.glob("*")
                    if f.suffix.lower() in ['.mp4', '.jpg', '.jpeg', '.png', '.webp'] and f.stat().st_size > 40000
                ])
            files = explicit_files or folder_files
            if not files:
                raise RuntimeError(f"Empty media folder for scene {i + 1}: {keyword}")

            image_exts = {'.jpg', '.jpeg', '.png', '.webp'}
            video_exts = {'.mp4', '.mov', '.m4v', '.webm'}
            video_files = [f for f in files if Path(f).suffix.lower() in video_exts]
            image_files = [f for f in files if Path(f).suffix.lower() in image_exts]
            preferred_files = video_files if media_type == "video" and video_files else (image_files or files)
            use_video_slots = bool(media_type == "video" and video_files)
            raw_cap = getattr(settings, "assets_per_scene", None) if settings else None
            try:
                assets_cap = int(raw_cap) if raw_cap else 0
            except (TypeError, ValueError):
                assets_cap = 0
            if assets_cap > 0:
                preferred_files = preferred_files[:max(1, assets_cap)]
            segment_seconds = getattr(settings, "clip_duration", None) or (4 if use_video_slots else 3)
            segment_seconds = max(2, min(12, float(segment_seconds)))

            ratio = settings.ratio if settings else "9:16"
            w, h = 1080, 1920
            if ratio == "16:9":
                w, h = 1920, 1080
            elif ratio == "1:1":
                w, h = 1080, 1080

            def source_key(file_path):
                try:
                    return str(Path(file_path).resolve())
                except OSError:
                    return str(file_path)

            def build_visual_clip(file_path, clip_duration, allow_loop=False):
                suffix = Path(file_path).suffix.lower()
                try:
                    if suffix in video_exts:
                        clip = VideoFileClip(file_path)
                        if clip_is_unusable(clip):
                            print(f"  🚫 Rejected dark/frozen clip: {Path(file_path).name}")
                            clip.close()
                            return None
                        if clip.duration >= clip_duration:
                            clip = clip.subclipped(0, clip_duration)
                        elif allow_loop and clip.duration >= 1.5:
                            clip = clip.with_effects([vfx.Loop(duration=clip_duration)])
                        elif clip.duration >= 1.5:
                            # Stock video: use the unique source once, never stretch it by looping.
                            pass
                        else:
                            clip.close()
                            return None
                        return crop_to_frame(clip, w, h)

                    clip = ImageClip(file_path).with_duration(clip_duration)
                    if frame_is_unusable(clip.get_frame(0)):
                        clip.close()
                        return None
                    return crop_to_frame(apply_ken_burns(clip, clip_duration), w, h)
                except Exception as exc:
                    print(f"⚠️ Skip clip {file_path}: {exc}")
                    return None

            video_mode = media_type == "video" and bool(video_files)
            if video_mode:
                unique_first = []
                seen_local = set()
                for file_path in preferred_files:
                    key = source_key(file_path)
                    if key in seen_local:
                        continue
                    seen_local.add(key)
                    unique_first.append(file_path)
                preferred_pool = unique_first
                if not preferred_pool:
                    raise RuntimeError(f"No unique unused clip for scene {i + 1}: {keyword}")
            else:
                preferred_pool = preferred_files

            num_segments = 1
            if video_mode:
                num_segments, segment_duration, visual_target = scene_visual_plan(
                    speech_duration, len(preferred_pool), segment_seconds
                )
            elif duration > 5 and len(preferred_pool) > 1:
                num_segments = max(1, int(duration / segment_seconds))
                if duration / num_segments < 1.6:
                    num_segments = max(1, int(duration / 1.6))
                segment_duration = duration / num_segments
                visual_target = duration
            else:
                segment_duration = duration
                visual_target = duration
            if video_mode and len(preferred_pool) < num_segments:
                raise RuntimeError(
                    f"Scene {i + 1} needs {num_segments} unique clips for {visual_target:.2f}s "
                    f"(clip_duration={segment_seconds}s) but only {len(preferred_pool)} unique sources remain"
                )
            parts = []
            used = []
            for file_path in preferred_pool:
                needed = duration if (not video_mode and num_segments == 1) else segment_duration
                part = build_visual_clip(file_path, needed, allow_loop=not video_mode)
                if part is None:
                    continue
                parts.append(part)
                used.append(file_path)
                if len(parts) >= num_segments:
                    break
            if not parts:
                raise RuntimeError(f"No usable (non-black) clip for scene {i + 1}: {keyword}")
            if video_mode:
                covered = sum(float(part.duration or 0) for part in parts)
                if covered + 0.05 < visual_target and used:
                    print(
                        f"  ⚠️ Scene {i + 1} unique footage {covered:.2f}s < assets budget {visual_target:.2f}s"
                    )
                if speech_duration + 0.05 > covered:
                    print(
                        f"  ✂️ Scene {i + 1} narration {speech_duration:.2f}s > visuals {covered:.2f}s; "
                        f"trim speech to assets × clip duration"
                    )
            if (not video_mode) and len(parts) == 1 and num_segments > 1:
                rebuilt = build_visual_clip(used[0], duration, allow_loop=True)
                visual_clip = rebuilt or parts[0]
            elif len(parts) == 1:
                visual_clip = parts[0]
            else:
                visual_clip = concatenate_videoclips(parts, method="chain")
            scene_duration = float(getattr(visual_clip, "duration", 0) or duration)
            if video_mode:
                for file_path in used:
                    key = source_key(file_path)
                    used_source_keys.append(key)
                    used_source_set.add(key)
                if speech_duration > scene_duration + 0.05:
                    audio_clip = trim_audio_to(audio_clip, scene_duration)
                    speech_duration = scene_duration
                else:
                    audio_clip = pad_audio_to(audio_clip, scene_duration)
                duration = scene_duration

            try:
                visual_clip = crop_center(visual_clip, w, h)
            except Exception as e:
                print(f"Resize Error: {e}")

            # Apply Vibe-based filters (from VideoSettings or vibe field)
            vibe = getattr(settings, 'vibe', 'general')
            if vibe == "futuristic":
                visual_clip = visual_clip.image_transform(lambda im: (im * [0.7, 1.2, 1.4]).clip(0, 255).astype('uint8')) # Cyan/Blue tint
            elif vibe == "black_and_white":
                visual_clip = visual_clip.with_effects([vfx.BlackAndWhite()])

            # Apply Filter
            if settings and settings.filter != "none":
                if settings.filter == "grayscale":
                    visual_clip = visual_clip.with_effects([vfx.BlackAndWhite()])
                elif settings.filter == "sepia":
                    visual_clip = visual_clip.image_transform(lambda im: (im @ [
                        [0.393, 0.769, 0.189],
                        [0.349, 0.686, 0.168],
                        [0.272, 0.534, 0.131]
                    ]).clip(0, 255).astype('uint8'))
                elif settings.filter == "invert":
                    visual_clip = visual_clip.with_effects([vfx.InvertColors()])

            trans_type = getattr(settings, "transition", None) or "fade"
            if trans_type == "zoom_in":
                visual_clip = apply_zoom_in(visual_clip, duration)
            elif trans_type == "zoom_out":
                visual_clip = apply_zoom_out(visual_clip, duration)
            elif trans_type == "slide":
                visual_clip = apply_slide_left(visual_clip, duration)
            elif trans_type == "fade" and duration >= 1.2:
                fade = min(0.12, duration / 8)
                visual_clip = visual_clip.with_effects([vfx.FadeIn(fade), vfx.FadeOut(fade)])

            try:
                visual_clip = crop_center(visual_clip, w, h)
            except Exception as e:
                print(f"Post-transition resize error: {e}")

            visual_clip = visual_clip.with_audio(audio_clip)

            # Add Subtitles
            if settings and settings.subtitles:
                try:
                    from PIL import Image, ImageDraw, ImageFont
                    import numpy as np

                    # Subtitles follow spoken audio, not padded silence after the last word.
                    subtitle_duration = max(0.5, speech_duration)
                    timing_path = Path(audio_path).parent / f"{Path(audio_path).stem}_timings.json"
                    word_timings = parse_word_timings(timing_path)

                    style = settings.subtitle_style if settings else "default"

                    def make_text_image(txt, w, h, current_style="default"):
                        img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
                        draw = ImageDraw.Draw(img)

                        requested_size = int(getattr(settings, "font_size", 0) or 52)
                        font_size = max(24, min(160, requested_size))
                        font_name = getattr(settings, "font_name", "") or ""
                        if contains_cjk(txt) and Path(font_name).name in LATIN_CAPTION_FONTS:
                            font_name = "MicrosoftYaHeiBold.ttc"
                        try:
                            font_path = resolve_font_path(font_name)
                            font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
                        except Exception:
                            font = ImageFont.load_default()

                        lines = wrap_text_lines(txt, draw, font, w * 0.82)
                        fill = getattr(settings, "text_fore_color", None) or "#FFFFFF"
                        stroke_fill = getattr(settings, "stroke_color", None) or "#000000"
                        stroke_w = int(round(float(getattr(settings, "stroke_width", 3) or 0)))
                        stroke_w = max(0, min(12, stroke_w))
                        if current_style == "minimal":
                            stroke_w = 0
                        elif current_style == "bold_outline":
                            stroke_w = max(stroke_w, max(3, font_size // 16))
                        bg_mode = getattr(settings, "subtitle_background", "none")

                        line_sizes = []
                        for line in lines:
                            try:
                                box = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_w)
                            except TypeError:
                                box = draw.textbbox((0, 0), line, font=font)
                            line_sizes.append((box[2] - box[0], box[3] - box[1], -box[1]))
                        line_gap = max(8, font_size // 6)
                        th = sum(size[1] for size in line_sizes) + line_gap * (len(lines) - 1)
                        tw = max((size[0] for size in line_sizes), default=0)
                        y = subtitle_top(
                            h, th,
                            getattr(settings, "subtitle_position", "bottom"),
                            getattr(settings, "subtitle_custom_position", 70.0),
                        )

                        if bg_mode == "box" or current_style == "yellow_box":
                            padding = max(16, font_size // 4)
                            x0 = (w - tw) / 2 - padding
                            box_color = (255, 214, 0, 230) if current_style == "yellow_box" else (0, 0, 0, 160)
                            draw.rectangle(
                                [x0, y - padding, x0 + tw + padding * 2, y + th + padding],
                                fill=box_color,
                            )
                            fill = "#111111" if current_style == "yellow_box" else fill
                            stroke_w = 0

                        cursor_y = y
                        for line, (lw, lh, baseline) in zip(lines, line_sizes):
                            lx = (w - lw) / 2
                            ly = cursor_y - baseline
                            if current_style != "minimal" and current_style != "yellow_box" and bg_mode != "box":
                                shadow = max(2, font_size // 24)
                                draw.text((lx + shadow, ly + shadow), line, font=font, fill=(0, 0, 0, 110), align="left")
                            draw.text(
                                (lx, ly),
                                line,
                                font=font,
                                fill=fill,
                                stroke_width=stroke_w,
                                stroke_fill=stroke_fill,
                                align="left",
                            )
                            cursor_y += lh + line_gap

                        return np.array(img)

                    if style == "high_retention":
                        display_text = sentence
                        if getattr(settings, 'emoji_subtitles', False):
                            display_text = SubtitleHelper.insert_emojis(sentence)

                        chunks = chunk_subtitle_text(display_text)
                        subs_clips = []
                        for chunk, chunk_start, chunk_duration in caption_chunk_windows(
                            chunks, word_timings, subtitle_duration
                        ):
                            t_img = make_text_image(chunk, visual_clip.w, visual_clip.h, style)
                            t_clip = ImageClip(t_img).with_duration(chunk_duration).with_start(chunk_start)
                            t_clip = t_clip.with_opacity(0.98)
                            subs_clips.append(t_clip)

                        visual_clip = CompositeVideoClip([visual_clip] + subs_clips)
                    else:
                        display_text = sentence
                        if getattr(settings, 'emoji_subtitles', False):
                            display_text = SubtitleHelper.insert_emojis(sentence)

                        txt_img = make_text_image(display_text, visual_clip.w, visual_clip.h, style)
                        txt_clip = ImageClip(txt_img).with_duration(subtitle_duration)
                        visual_clip = CompositeVideoClip([visual_clip, txt_clip])

                except Exception as e:
                    print(f"⚠️ Subtitle Error (PIL): {e}")

            final_clips.append(visual_clip)

        if len(final_clips) != len(script_data):
            raise RuntimeError(
                f"Scene assembly incomplete: expected {len(script_data)}, got {len(final_clips)}"
            )
        if not final_clips:
            raise RuntimeError("Scene assembly incomplete: no clips produced")

        # Concatenate
        final_video = concatenate_videoclips(final_clips, method="chain")

        # Overlay Watermark
        if watermark_clip:
            watermark_clip = watermark_clip.with_duration(final_video.duration)
            # Position at top right
            watermark_clip = watermark_clip.with_position(("right", "top"))
            final_video = CompositeVideoClip([final_video, watermark_clip])

        # Add BG Music
        if bg_music and os.path.exists(bg_music):
            from moviepy import CompositeAudioClip
            bg = AudioFileClip(bg_music).with_volume_scaled(float(getattr(settings, "bgm_volume", 0.2) or 0.2))
            if bg.duration < final_video.duration:
                from moviepy.audio import fx as afx
                bg = bg.with_effects([afx.AudioLoop(duration=final_video.duration)])
            else:
                bg = bg.subclipped(0, final_video.duration)

            final_audio = CompositeAudioClip([final_video.audio, bg])
            final_video = final_video.with_audio(final_audio)

        # Export
        output_filename = self.output_dir / (output_name or "final_aesthetic_video.mp4")
        final_video.write_videofile(str(output_filename), fps=24, codec='libx264', audio_codec='aac', threads=4)
        if not output_filename.exists() or output_filename.stat().st_size <= 0:
            raise RuntimeError(f"Video export failed or empty: {output_filename}")
        print(f"✅ Video Saved: {output_filename}")
        return str(output_filename)
