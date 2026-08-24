"""VUZA CLI — same request models and pipeline as POST /api/scrape.

Exit codes: 0 success, 1 runtime failure, 2 usage / validation error.
Publishing is never automatic. YouTube requires --yt-upload and --publish-confirmed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app import (
    ApiKeys,
    ScrapeRequest,
    VideoSettings,
    run_scrape,
    scraping_status,
    set_status,
    validate_request_api_dependencies,
    validate_scrape_request_options,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="vuza",
        description="Generate a VUZA video from the command line. Does not auto-publish.",
    )
    parser.add_argument("--query", default="", help="Stock search term (single mode)")
    parser.add_argument("--script", default="", help="Narration script (script mode)")
    parser.add_argument("--mode", choices=["single", "script"], default="script")
    parser.add_argument(
        "--source",
        choices=["pinterest", "pexels", "pixabay", "coverr", "mixkit", "local"],
        default="pinterest",
    )
    parser.add_argument("--media-type", choices=["photo", "video"], default="photo")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--vibe", default="suspense_cn")
    parser.add_argument("--language", default="zh-CN")
    parser.add_argument("--voice", default="zh-CN-YunyangNeural")
    parser.add_argument("--ratio", default="9:16")
    parser.add_argument("--music", default="none")
    parser.add_argument("--clip-duration", type=int, default=5)
    parser.add_argument("--video-count", type=int, default=1)
    parser.add_argument("--bgm-volume", type=float, default=0.2)
    parser.add_argument("--local-file", action="append", default=[], dest="local_files")
    parser.add_argument("--no-auto-video", action="store_true")
    parser.add_argument("--yt-upload", action="store_true", help="Opt-in YouTube upload")
    parser.add_argument(
        "--publish-confirmed",
        action="store_true",
        help="Required together with --yt-upload. Never implied.",
    )
    parser.add_argument("--llm-key", default="")
    parser.add_argument("--llm-url", default="https://openrouter.ai/api/v1/chat/completions")
    parser.add_argument("--llm-model", default="")
    parser.add_argument("--pexels-key", default="")
    parser.add_argument("--pixabay-key", default="")
    parser.add_argument("--coverr-key", default="")
    parser.add_argument("--eleven-key", default="")
    parser.add_argument("--yt-client-id", default="")
    parser.add_argument("--yt-client-secret", default="")
    return parser.parse_args(argv)


def build_request(args) -> ScrapeRequest:
    return ScrapeRequest(
        query=args.query or None,
        script=args.script or None,
        source=args.source,
        media_type=args.media_type,
        count=args.count,
        mode=args.mode,
        vibe=args.vibe,
        auto_video=not args.no_auto_video,
        yt_upload=args.yt_upload,
        publish_confirmed=args.publish_confirmed,
        local_files=args.local_files or None,
        video_settings=VideoSettings(
            ratio=args.ratio,
            voice=args.voice,
            language=args.language,
            music=args.music,
            clip_duration=args.clip_duration,
            video_count=args.video_count,
            bgm_volume=args.bgm_volume,
        ),
        api_keys=ApiKeys(
            llm_key=args.llm_key,
            llm_url=args.llm_url,
            llm_model=args.llm_model,
            pexels_key=args.pexels_key,
            pixabay_key=args.pixabay_key,
            coverr_key=args.coverr_key,
            eleven_key=args.eleven_key,
            yt_client_id=args.yt_client_id,
            yt_client_secret=args.yt_client_secret,
        ),
    )


def main(argv=None) -> int:
    args = parse_args(argv)
    request = build_request(args)
    try:
        validate_scrape_request_options(request)
        validate_request_api_dependencies(request)
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    set_status(status="queued", message="CLI queued", progress=0, error=None, results=[], candidates=[], final_video=None)
    try:
        asyncio.run(run_scrape(request))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    payload = {
        "status": scraping_status.get("status"),
        "task_id": scraping_status.get("task_id"),
        "final_video": scraping_status.get("final_video"),
        "candidates": scraping_status.get("candidates") or [],
        "error": scraping_status.get("error"),
        "message": scraping_status.get("message"),
    }
    print(json.dumps(payload, ensure_ascii=False))
    if scraping_status.get("status") == "error" or scraping_status.get("error"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
