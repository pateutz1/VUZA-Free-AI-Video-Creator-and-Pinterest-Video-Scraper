---
name: vuza-video
description: Generate a local VUZA video from a topic or script using the VUZA CLI. Ask before running. Never auto-publish.
---

# VUZA local video skill

Use this only after the user confirms they want VUZA to generate a video. Do not auto-install packages without asking. Do not skip confirmation. Do not upload to YouTube, TikTok, or any publisher unless the user explicitly confirms in this chat AND passes `--publish-confirmed` (YouTube only). Default is local file output.

## Working directory

`VUZA-Free-AI-Video-Creator-and-Pinterest-Video-Scraper`

## Confirm first

Ask:

1. Topic or narration script
2. Source: `pinterest` (default), `pexels`, `pixabay`, `coverr`, or `local`
3. Language / voice (zh-CN Yunyang is the default content voice)
4. Whether to assemble a video (`--no-auto-video` for assets only)
5. Publish? Default **no**

Stop if they decline.

## CLI

Exit codes: `0` success, `1` runtime failure, `2` usage/validation error.

```powershell
python cli.py --mode script --source pinterest --script "Your narration." --llm-key "<key>"
```

Topic search (no auto video):

```powershell
python cli.py --mode single --source pexels --query "rain alley" --no-auto-video --pexels-key "<key>"
```

YouTube is opt-in and requires both flags:

```powershell
python cli.py --mode script --script "..." --yt-upload --publish-confirmed --yt-client-id "<id>" --yt-client-secret "<secret>"
```

Do not add `--yt-upload` unless the user confirmed publishing in this session.

PiAPI is paid. Use `--source piapi` only after the user explicitly confirms the paid generation, and pass both `--piapi-key` and `--piapi-confirmed`. Never infer that confirmation from a previous run.

## After success

Read the JSON stdout. Deliver `final_video` / `candidates` paths. Do not print API keys.
