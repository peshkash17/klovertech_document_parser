"""
tools/translator.py

Uses GPT-4o with structured (JSON) output to:
  1. Detect the source language of extracted text.
  2. Translate to English if not already English.

Returns a TranslationResult dataclass.
"""

from dataclasses import dataclass

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()  # ensure OPENAI_API_KEY is available at import time
_client = OpenAI()  # reads OPENAI_API_KEY from env

_SYSTEM_PROMPT = """
You are a language detection and translation assistant.

Given a piece of text, return a JSON object with exactly these fields:
{
  "source_language": "<full language name in English, e.g. French, Arabic, Hindi, English>",
  "is_english": <true | false>,
  "translated_text": "<English translation of the text, or null if already English>"
}

Rules:
- If the text is already in English, set is_english to true and translated_text to null.
- Translate the ENTIRE text faithfully — do not summarise or omit any content.
- Preserve paragraph structure in the translation.
- Respond with valid JSON only — no markdown, no code fences.
""".strip()


@dataclass
class TranslationResult:
    source_language: str
    is_english: bool
    translated_text: str | None  # None when is_english is True


def detect_and_translate(text: str) -> TranslationResult:
    """
    Detect language and translate to English if necessary.
    Uses GPT-4o with JSON mode for reliable structured output.
    """
    # Truncate very long texts for the translation call to save tokens.
    # The full original_text is already stored in the DB.
    truncated = text[:12000] if len(text) > 12000 else text

    response = _client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": truncated},
        ],
        max_tokens=4096,
        temperature=0,
    )

    import json
    raw = response.choices[0].message.content
    data = json.loads(raw)

    return TranslationResult(
        source_language=data.get("source_language", "Unknown"),
        is_english=bool(data.get("is_english", False)),
        translated_text=data.get("translated_text"),
    )
