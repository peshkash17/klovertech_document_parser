"""
tools/extractor.py

Three extraction strategies:
  1. extract_pdf_text()   — PyMuPDF fast path (no AI cost)
  2. extract_word_text()  — python-docx
  3. extract_image_text() — GPT-4o Vision (images + scanned PDFs via pixmap)

assess_extraction_quality() returns a 0–1 score used by the assess_quality node.
"""

import base64
import io
import json
import re
from dataclasses import dataclass

import pymupdf as fitz  # PyMuPDF (fitz alias kept for compatibility)
from docx import Document as DocxDocument
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image

load_dotenv()  # ensure OPENAI_API_KEY is available at import time
_client = OpenAI()  # reads OPENAI_API_KEY from env


# ---------------------------------------------------------------------------
# Fast-path extractors
# ---------------------------------------------------------------------------

def extract_pdf_text(file_bytes: bytes) -> str:
    """Extract selectable text from a PDF using PyMuPDF."""
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages = [page.get_text("text") for page in doc]
    doc.close()
    return "\n".join(pages).strip()


def extract_word_text(file_bytes: bytes) -> str:
    """Extract text from a .docx file using python-docx."""
    docx = DocxDocument(io.BytesIO(file_bytes))
    paragraphs = [para.text for para in docx.paragraphs if para.text.strip()]
    return "\n".join(paragraphs).strip()


# ---------------------------------------------------------------------------
# Vision fallback — handles images AND scanned PDFs
# ---------------------------------------------------------------------------

def _pdf_to_images(file_bytes: bytes, dpi: int = 150) -> list[bytes]:
    """Convert every PDF page to a PNG image bytes object."""
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    images = []
    for page in doc:
        pixmap = page.get_pixmap(dpi=dpi)
        images.append(pixmap.tobytes("png"))
    doc.close()
    return images


def _resize_image(img_bytes: bytes, max_px: int = 1568) -> bytes:
    """
    Resize image so its longest side is at most max_px.
    GPT-4o Vision charges per tile (512x512); keeping images reasonable
    saves tokens without losing legibility.
    """
    img = Image.open(io.BytesIO(img_bytes))
    w, h = img.size
    if max(w, h) > max_px:
        ratio = max_px / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def extract_image_text(file_bytes: bytes, mime_type: str) -> str:
    """
    Extract text from images or scanned PDFs using GPT-4o Vision.
    For PDFs: converts each page to a pixmap image first.
    """
    if mime_type == "application/pdf":
        raw_images = _pdf_to_images(file_bytes)
    else:
        raw_images = [file_bytes]

    # Build one message with all page images
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "You are a multilingual OCR engine. Extract ALL visible text from the provided image(s) "
                "exactly as written, in the original language and script. "
                "This includes Arabic, Hindi, Chinese, Japanese, Korean, and any other script. "
                "Preserve line breaks and paragraph structure. "
                "Do NOT translate, summarise, or add any commentary. "
                "If the image contains no readable text, respond with an empty string. "
                "If there are multiple pages, separate them with '--- PAGE BREAK ---'."
            ),
        }
    ]

    for img_bytes in raw_images:
        resized = _resize_image(img_bytes)
        encoded = base64.b64encode(resized).decode("utf-8")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{encoded}", "detail": "high"},
            }
        )

    response = _client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": content}],
        max_tokens=4096,
    )
    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# Extraction quality — cheap structural checks, then LLM if uncertain
# ---------------------------------------------------------------------------

QUALITY_RETRY_THRESHOLD = 0.3
_CID_RE = re.compile(r"\(cid:\d+\)", re.IGNORECASE)

_QUALITY_PROMPT = """
You are reviewing text extracted from a PDF, Word file, or image.

Return JSON only:
{
  "score": <float 0.0 to 1.0>,
  "garbled": <true or false>,
  "reason": "<one short sentence>"
}

Score HIGH (0.7–1.0) if the text looks like real document content in ANY language
(including Arabic, Hindi, Chinese, or a short form/receipt).
Score LOW (0.0–0.25) if it is empty, encoding garbage, (cid:N) codes, random
symbols, or clearly not readable prose or data.
Do not judge translation quality or factual accuracy.
""".strip()


@dataclass
class QualityResult:
    score: float
    method: str  # "heuristic" | "llm"
    reason: str


def _heuristic_quality(text: str) -> QualityResult:
    """Catch empty / encoding-broken extracts without an API call."""
    if not text or not text.strip():
        return QualityResult(0.0, "heuristic", "empty extract")

    sample = text.strip()
    n = len(sample)
    control = sum(
        1 for c in sample
        if (ord(c) < 32 and c not in "\n\t\r") or c == "\ufffd"
    )
    control_ratio = control / n
    cid_count = len(_CID_RE.findall(sample))
    letters = sum(1 for c in sample if c.isalpha())
    digits = sum(1 for c in sample if c.isdigit())
    alnum_ratio = (letters + digits) / n

    if cid_count >= 5:
        return QualityResult(
            0.1, "heuristic", f"PDF CID encoding garbage ({cid_count} markers)"
        )
    if control_ratio > 0.15:
        return QualityResult(
            0.1, "heuristic", "high ratio of control or replacement characters"
        )
    if alnum_ratio < 0.08:
        return QualityResult(0.1, "heuristic", "almost no letters or digits")

    # Short but clean text is valid — do not force a Vision retry.
    if control_ratio < 0.02 and cid_count == 0 and alnum_ratio >= 0.25:
        score = 0.85 if n >= 20 else 0.7
        return QualityResult(
            score, "heuristic", "readable letters/digits, no encoding garbage"
        )

    return QualityResult(0.45, "heuristic", "uncertain — needs a language-model review")


def _llm_quality(text: str) -> QualityResult:
    """Self-reflection: does this look like real document text?"""
    response = _client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _QUALITY_PROMPT},
            {"role": "user", "content": text[:2500]},
        ],
        max_tokens=200,
        temperature=0,
    )
    data = json.loads(response.choices[0].message.content)
    score = min(max(float(data.get("score", 0.5)), 0.0), 1.0)
    if data.get("garbled"):
        score = min(score, 0.2)
    return QualityResult(score, "llm", data.get("reason", "LLM quality review"))


def assess_extraction_quality(text: str) -> QualityResult:
    """
    Two-layer quality check used by the assess_quality graph node.

    Clearly empty or broken extracts fail on the heuristic (no API cost).
    Clearly readable extracts pass on the heuristic.
    Borderline extracts get a GPT-4o self-reflection pass.
    Score < 0.3 → agent retries with Vision.
    """
    heuristic = _heuristic_quality(text)
    if heuristic.score <= 0.15 or heuristic.score >= 0.7:
        return heuristic
    try:
        return _llm_quality(text)
    except Exception:
        return heuristic


def quality_score(text: str) -> float:
    """Back-compat wrapper — prefer assess_extraction_quality()."""
    return assess_extraction_quality(text).score
