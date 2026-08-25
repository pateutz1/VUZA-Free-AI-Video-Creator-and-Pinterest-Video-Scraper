"""MoneyPrinterTurbo-style script and search-term generation, ported to VUZA's LLMProcessor."""

import json
import re

MIN_SCRIPT_PARAGRAPH_NUMBER = 1
MAX_SCRIPT_PARAGRAPH_NUMBER = 10
MAX_SCRIPT_PROMPT_LENGTH = 2000
MAX_SCRIPT_SYSTEM_PROMPT_LENGTH = 8000
_MAX_RETRIES = 3

DEFAULT_SCRIPT_SYSTEM_PROMPT = """
# Role: Video Script Generator

## Goals:
Generate a script for a video, depending on the subject of the video.

## Constrains:
1. the script is to be returned as a string with the specified number of paragraphs.
2. do not under any circumstance reference this prompt in your response.
3. get straight to the point, don't start with unnecessary things like, "welcome to this video".
4. you must not include any type of markdown or formatting in the script, never use a title.
5. only return the raw content of the script.
6. do not include "voiceover", "narrator" or similar indicators of what should be spoken at the beginning of each paragraph or line.
7. you must not mention the prompt, or anything about the script itself. also, never talk about the amount of paragraphs or lines. just write the script.
8. respond in the same language as the video subject.
""".strip()


def _limit_text(text, max_length, field_name):
    value = (text or "").strip()
    if len(value) > max_length:
        print(f"⚠️ {field_name} too long, truncated to {max_length} characters.")
        return value[:max_length]
    return value


def _normalize_paragraph_number(paragraph_number):
    try:
        value = int(paragraph_number or MIN_SCRIPT_PARAGRAPH_NUMBER)
    except (TypeError, ValueError):
        value = MIN_SCRIPT_PARAGRAPH_NUMBER
    return max(MIN_SCRIPT_PARAGRAPH_NUMBER, min(value, MAX_SCRIPT_PARAGRAPH_NUMBER))


def _clean_think_blocks(content):
    content = re.sub(r"<think\b[^>]*>.*?</think>", "", content or "", flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"<think\b[^>]*>.*$", "", content, flags=re.IGNORECASE | re.DOTALL).strip()


def build_script_prompt(
    video_subject,
    language="",
    paragraph_number=1,
    video_script_prompt="",
    custom_system_prompt="",
):
    paragraph_number = _normalize_paragraph_number(paragraph_number)
    video_script_prompt = _limit_text(video_script_prompt, MAX_SCRIPT_PROMPT_LENGTH, "video_script_prompt")
    custom_system_prompt = _limit_text(custom_system_prompt, MAX_SCRIPT_SYSTEM_PROMPT_LENGTH, "custom_system_prompt")

    prompt = custom_system_prompt or DEFAULT_SCRIPT_SYSTEM_PROMPT
    prompt += f"""

# Initialization:
- video subject: {video_subject}
- number of paragraphs: {paragraph_number}""".rstrip()
    if language:
        prompt += f"\n- language: {language}"
    if video_script_prompt:
        prompt += f"""

# Additional User Requirements:
{video_script_prompt}""".rstrip()
    return prompt


def _format_script_response(response):
    response = (response or "").replace("*", "").replace("#", "")
    paragraphs = [part.strip() for part in response.split("\n\n") if part.strip()]
    return "\n\n".join(paragraphs)


def _strip_code_fence(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def generate_script(
    llm,
    video_subject,
    language="",
    paragraph_number=1,
    video_script_prompt="",
    custom_system_prompt="",
):
    """MPT-style script: raw spoken paragraphs from the subject. Returns "" on failure."""
    paragraph_number = _normalize_paragraph_number(paragraph_number)
    prompt = build_script_prompt(
        video_subject=video_subject,
        language=language,
        paragraph_number=paragraph_number,
        video_script_prompt=video_script_prompt,
        custom_system_prompt=custom_system_prompt,
    )
    print(
        f"📝 MPT script: subject={video_subject!r}, paragraphs={paragraph_number}, "
        f"has_prompt={bool(video_script_prompt)}, has_system={bool(custom_system_prompt)}"
    )
    final_script = ""
    for attempt in range(_MAX_RETRIES):
        content = llm._chat(
            llm.models[0] if llm.models else "",
            [{"role": "user", "content": prompt}],
            timeout=120,
            max_tokens=4000 if paragraph_number >= 5 else 1500,
        )
        if content:
            final_script = _format_script_response(_clean_think_blocks(content))
        if final_script:
            break
        print(f"⚠️ script retry {attempt + 1}/{_MAX_RETRIES}: {llm.last_error or 'empty response'}")
    return (final_script or "").strip()


def _terms_prompt(video_subject, video_script, amount, match_script_order):
    if match_script_order:
        goal = (
            f"Generate {amount} chronological stock-video search terms that follow "
            "the order of topics in the video script."
        )
        ordering_rule = (
            "6. keep the terms in the same order as the script narration; "
            "earlier terms must describe earlier visual moments."
        )
        example_terms = [
            "opening visual topic",
            *[f"script visual topic {index}" for index in range(2, max(amount, 1))],
            "final visual topic",
        ]
        output_example = json.dumps(example_terms[:amount], ensure_ascii=False)
    else:
        goal = (
            f"Generate {amount} search terms for stock videos, depending on the "
            "subject of a video."
        )
        ordering_rule = ""
        output_example = (
            '["search term 1", "search term 2", "search term 3",'
            '"search term 4", "search term 5"]'
        )

    return f"""
# Role: Video Search Terms Generator

## Goals:
{goal}

## Constrains:
1. the search terms are to be returned as a json-array of strings.
2. each search term should consist of 1-3 words, always add the main subject of the video.
3. you must only return the json-array of strings. you must not return anything else. you must not return the script.
4. the search terms must be related to the subject of the video.
5. reply with english search terms only.
{ordering_rule}

## Output Example:
{output_example}

## Context:
### Video Subject
{video_subject}

### Video Script
{video_script}

Please note that you must use English for generating video search terms; Chinese is not accepted.
""".strip()


def generate_terms(llm, video_subject, video_script, amount=5, match_script_order=False):
    """MPT-style terms: JSON array of 1-3 word English strings. Returns [] on failure."""
    prompt = _terms_prompt(video_subject, video_script, amount, match_script_order)
    print(f"🔑 MPT terms: subject={video_subject!r}, amount={amount}, match_order={match_script_order}")

    search_terms = []
    response = ""
    for attempt in range(_MAX_RETRIES):
        content = llm._chat(
            llm.models[0] if llm.models else "",
            [{"role": "user", "content": prompt}],
            timeout=60,
            max_tokens=600,
        )
        response = content or ""
        if response.startswith("Error: ") or not response:
            llm.last_error = response or llm.last_error
            print(f"⚠️ terms retry {attempt + 1}/{_MAX_RETRIES}: {llm.last_error or 'empty response'}")
            continue
        try:
            parsed = json.loads(_strip_code_fence(_clean_think_blocks(response)))
            if isinstance(parsed, list) and all(isinstance(term, str) for term in parsed):
                search_terms = parsed
                break
            print("⚠️ terms response was not a list of strings.")
        except Exception as exc:
            match = re.search(r"\[.*\]", response, re.DOTALL)
            if match:
                try:
                    search_terms = json.loads(match.group())
                    break
                except Exception:
                    pass
            print(f"⚠️ terms parse failed: {exc}")
    return [term.strip() for term in search_terms if str(term).strip()]



