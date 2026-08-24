import asyncio
import os
import re
import sys
from pathlib import Path
from edge_tts import Communicate
from moviepy import VideoFileClip, ImageClip, AudioFileClip, TextClip, CompositeVideoClip, concatenate_videoclips
from moviepy.video import fx as vfx

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

# ═══════════════════════════════════════════════════════════════
# ANTIGRAVITY VIDEO ENGINE (MOVIEPY + EDGE-TTS)
# ═══════════════════════════════════════════════════════════════

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
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

def resolve_font_path():
    for font_path in FONT_CANDIDATES:
        if os.path.exists(font_path):
            return font_path
    return None

def contains_cjk(text):
    return bool(re.search(r'[\u3400-\u9fff]', text or ""))

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

def crop_center(clip, w, h):
    cw, ch = clip.size
    if cw / ch > w / h:
        new_w = int(ch * w / h)
        clip = clip.cropped(x1=int((cw - new_w) / 2), width=new_w)
    else:
        new_h = int(cw * h / w)
        clip = clip.cropped(y1=int((ch - new_h) / 2), height=new_h)
    return clip.resized((w, h))

def frame_is_unusable(frame):
    import numpy as np
    arr = np.asarray(frame)
    if arr.size == 0:
        return True
    return float(arr.mean()) < 16 or float(arr.std()) < 7

def clip_is_unusable(clip):
    if clip is None or getattr(clip, "duration", 0) < 1.15:
        return True
    try:
        import numpy as np
        times = [clip.duration * t for t in (0.25, 0.5, 0.75)]
        frames = [clip.get_frame(min(t, max(clip.duration - 0.02, 0))) for t in times]
        if any(frame_is_unusable(f) for f in frames):
            return True
        diffs = [
            float(np.mean(np.abs(frames[i].astype("float32") - frames[0].astype("float32"))))
            for i in range(1, len(frames))
        ]
        return bool(diffs) and max(diffs) < 2.5
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

    def set_eleven_key(self, key):
        self.eleven_key = key

    async def generate_voiceover(self, text, idx, voice="en-US-ChristopherNeural", language="en-US", voice_rate=1.0, voice_volume=1.0, timeout_seconds=60):
        """Generates TTS audio for a single sentence with retries. Supports Edge-TTS and ElevenLabs."""
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

        rate_pct = int(round((float(voice_rate or 1.0) - 1.0) * 100))
        rate = f"{rate_pct:+d}%"
        max_retries = 3
        for attempt in range(max_retries):
            try:
                communicate = Communicate(text, voice, rate=rate)
                path = self.temp_dir / f"speech_{idx}.mp3"
                await asyncio.wait_for(communicate.save(str(path)), timeout=timeout_seconds)
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

        for i, item in enumerate(script_data):
            sentence = item["sentence"]
            keyword = item["keyword"]
            audio_path = str(self.temp_dir / f"speech_{i}.mp3")

            if not os.path.exists(audio_path) or os.path.getsize(audio_path) <= 0:
                print(f"❌ Missing TTS for scene {i + 1}: {audio_path}")
                return None

            # Create Audio Clip
            audio_clip = AudioFileClip(audio_path)
            duration = max(float(audio_clip.duration), 0.8)

            explicit_files = [
                str(Path(f)) for f in item.get("_files", [])
                if Path(f).exists() and Path(f).stat().st_size > 40000
            ]

            media_folder = None
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
                print(f"❌ Empty media folder for scene {i + 1}: {keyword}")
                return None

            image_exts = {'.jpg', '.jpeg', '.png', '.webp'}
            video_exts = {'.mp4', '.mov', '.m4v', '.webm'}
            video_files = [f for f in files if Path(f).suffix.lower() in video_exts]
            image_files = [f for f in files if Path(f).suffix.lower() in image_exts]
            preferred_files = video_files if media_type == "video" and video_files else (image_files or files)
            segment_seconds = getattr(settings, "clip_duration", None) or (4 if preferred_files == video_files else 3)
            segment_seconds = max(2, min(12, float(segment_seconds)))

            ratio = settings.ratio if settings else "9:16"
            w, h = 1080, 1920
            if ratio == "16:9":
                w, h = 1920, 1080
            elif ratio == "1:1":
                w, h = 1080, 1080

            def build_visual_clip(file_path, clip_duration):
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
                        elif clip.duration >= 1.5:
                            clip = clip.with_effects([vfx.Loop(duration=clip_duration)])
                        else:
                            clip.close()
                            return None
                        return crop_center(clip.resized(height=h), w, h)

                    clip = ImageClip(file_path).with_duration(clip_duration).resized(height=h)
                    if frame_is_unusable(clip.get_frame(0)):
                        clip.close()
                        return None
                    return crop_center(apply_ken_burns(clip, clip_duration), w, h)
                except Exception as exc:
                    print(f"⚠️ Skip clip {file_path}: {exc}")
                    return None

            num_segments = 1
            if duration > 5 and len(preferred_files) > 1:
                num_segments = max(1, int(duration / segment_seconds))
                if duration / num_segments < 1.6:
                    num_segments = max(1, int(duration / 1.6))
            segment_duration = duration / num_segments
            parts = []
            used = []
            for file_path in preferred_files:
                needed = duration if num_segments == 1 else segment_duration
                part = build_visual_clip(file_path, needed)
                if part is None:
                    continue
                parts.append(part)
                used.append(file_path)
                if len(parts) >= num_segments:
                    break
            if not parts:
                print(f"❌ No usable (non-black) clip for scene {i + 1}: {keyword}")
                return None
            if len(parts) == 1 and num_segments > 1:
                rebuilt = build_visual_clip(used[0], duration)
                visual_clip = rebuilt or parts[0]
            elif len(parts) == 1:
                visual_clip = parts[0]
            else:
                visual_clip = concatenate_videoclips(parts, method="chain")

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

                    # Check Duration
                    if duration < 0.5: duration = 2 # fallback

                    style = settings.subtitle_style if settings else "default"

                    def make_text_image(txt, w, h, current_style="default"):
                        img = Image.new('RGBA', (w, h), (0,0,0,0))
                        draw = ImageDraw.Draw(img)

                        requested_size = int(getattr(settings, "font_size", 0) or 0)
                        if requested_size <= 60:
                            font_size = 96 if current_style == "high_retention" else 80
                        else:
                            font_size = requested_size
                        try:
                            font_path = resolve_font_path()
                            font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
                        except:
                            font = ImageFont.load_default()

                        # Manual multiline
                        lines = wrap_text_lines(txt, draw, font, w * 0.8)

                        full_text = "\n".join(lines)
                        left, top, right, bottom = draw.textbbox((0, 0), full_text, font=font)
                        tw, th = right - left, bottom - top

                        x = (w - tw) / 2
                        position = (getattr(settings, "subtitle_position", "bottom") or "bottom").strip().lower()
                        if position == "top":
                            y = 80
                        elif position == "center":
                            y = (h - th) / 2
                        else:
                            y = h - th - 200
                        fill = getattr(settings, "text_fore_color", None) or "white"
                        stroke_fill = getattr(settings, "stroke_color", None) or "black"
                        stroke_w = max(int(getattr(settings, "stroke_width", 0) or 0), 6)
                        bg_mode = getattr(settings, "subtitle_background", "none")

                        if current_style == "high_retention" or current_style == "bold_outline" or current_style == "default":
                            draw.text((x, y), full_text, font=font, fill="white", stroke_width=stroke_w, stroke_fill="black", align="center")
                        elif bg_mode == "box" or current_style == "yellow_box":
                            padding = 20
                            box_color = "yellow" if current_style == "yellow_box" else (0, 0, 0, 160)
                            draw.rectangle([x - padding, y - padding, x + tw + padding, y + th + padding], fill=box_color)
                            draw.text((x, y), full_text, font=font, fill="black" if current_style == "yellow_box" else fill, align="center")
                        elif current_style == "minimal":
                            draw.text((x, y), full_text, font=font, fill=fill, align="center")
                        else:
                            draw.text((x, y), full_text, font=font, fill=fill, stroke_width=stroke_w, stroke_fill=stroke_fill, align="center")

                        return np.array(img)

                    if style == "high_retention":
                        # Break sentence into 3-word chunks for punchy effect
                        display_text = sentence
                        if getattr(settings, 'emoji_subtitles', False):
                            display_text = SubtitleHelper.insert_emojis(sentence)

                        chunks = chunk_subtitle_text(display_text)
                        chunk_duration = duration / len(chunks)

                        subs_clips = []
                        for idx, chunk in enumerate(chunks):
                            t_img = make_text_image(chunk, visual_clip.w, visual_clip.h, style)
                            t_clip = ImageClip(t_img).with_duration(chunk_duration).with_start(idx * chunk_duration)
                            t_clip = t_clip.with_opacity(0.98)
                            subs_clips.append(t_clip)

                        visual_clip = CompositeVideoClip([visual_clip] + subs_clips)
                    else:
                        display_text = sentence
                        if getattr(settings, 'emoji_subtitles', False):
                            display_text = SubtitleHelper.insert_emojis(sentence)

                        txt_img = make_text_image(display_text, visual_clip.w, visual_clip.h, style)
                        txt_clip = ImageClip(txt_img).with_duration(duration)
                        visual_clip = CompositeVideoClip([visual_clip, txt_clip])

                except Exception as e:
                    print(f"⚠️ Subtitle Error (PIL): {e}")

            final_clips.append(visual_clip)

        if len(final_clips) != len(script_data):
            print(f"❌ Scene assembly incomplete: expected {len(script_data)}, got {len(final_clips)}")
            return None
        if not final_clips: return None

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
            print(f"❌ Video export failed or empty: {output_filename}")
            return None
        print(f"✅ Video Saved: {output_filename}")
        return str(output_filename)
