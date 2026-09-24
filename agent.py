"""
agent.py

LangGraph StateGraph — the core agentic pipeline.

Graph nodes (each is a plain async function):
  route          → decide which extractor to call based on MIME type
  extract_pdf    → PyMuPDF fast-path text extraction
  extract_word   → python-docx extraction
  extract_image  → GPT-4o Vision (images + scanned PDFs)
  assess_quality → score extracted text; decide whether to retry with Vision
  translate      → GPT-4o language detection + translation
  save_doc       → write final result to DB

Conditional edges:
  route        → extract_pdf | extract_word | extract_image
  assess_quality → extract_image (retry) | translate (proceed)

Every node calls append_log() so the UI can show a live step trace.
"""

import asyncio
import uuid
from typing import TypedDict

from langgraph.graph import END, StateGraph

from database import AsyncSessionLocal
from tools.extractor import (
    assess_extraction_quality,
    extract_image_text,
    extract_pdf_text,
    extract_word_text,
)
from tools.storage import append_log, update_document
from tools.translator import detect_and_translate


# ---------------------------------------------------------------------------
# Shared graph state
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    doc_id: uuid.UUID
    filename: str
    mime_type: str
    file_bytes: bytes

    # filled in as the graph progresses
    extracted_text: str
    quality: float
    source_language: str
    translated_text: str | None
    is_english: bool
    vision_attempted: bool   # guards against infinite retry loop
    step: int                # auto-incremented log counter
    _next: str               # internal routing key set by the route node


# ---------------------------------------------------------------------------
# Helper: write a log row and bump the step counter
# ---------------------------------------------------------------------------

async def _log(state: AgentState, tool_name: str, summary: str) -> int:
    step = state["step"] + 1
    async with AsyncSessionLocal() as db:
        await append_log(
            db,
            document_id=state["doc_id"],
            step=step,
            tool_name=tool_name,
            summary=summary,
        )
    return step


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def route(state: AgentState) -> AgentState:
    mime = state["mime_type"]
    if "pdf" in mime:
        label = "PDF detected — will try PyMuPDF text extraction first"
        next_extractor = "pdf"
    elif "word" in mime or "docx" in mime or "officedocument" in mime:
        label = "Word document detected — using python-docx"
        next_extractor = "word"
    else:
        label = f"Image detected ({mime}) — using GPT-4o Vision"
        next_extractor = "image"

    step = await _log(state, "route", label)
    return {**state, "step": step, "_next": next_extractor}


async def extract_pdf(state: AgentState) -> AgentState:
    text = await asyncio.to_thread(extract_pdf_text, state["file_bytes"])
    step = await _log(
        state,
        "extract_pdf",
        f"Extracted {len(text):,} characters from PDF",
    )
    return {**state, "extracted_text": text, "step": step}


async def extract_word(state: AgentState) -> AgentState:
    text = await asyncio.to_thread(extract_word_text, state["file_bytes"])
    step = await _log(
        state,
        "extract_word",
        f"Extracted {len(text):,} characters from Word document",
    )
    return {**state, "extracted_text": text, "step": step}


async def extract_image(state: AgentState) -> AgentState:
    text = await asyncio.to_thread(
        extract_image_text, state["file_bytes"], state["mime_type"]
    )
    step = await _log(
        state,
        "extract_image",
        f"GPT-4o Vision extracted {len(text):,} characters",
    )
    return {**state, "extracted_text": text, "vision_attempted": True, "step": step}


async def assess_quality(state: AgentState) -> AgentState:
    result = await asyncio.to_thread(
        assess_extraction_quality, state.get("extracted_text", "")
    )
    score = result.score
    if score < 0.3 and not state.get("vision_attempted", False):
        summary = (
            f"Quality {score:.2f} ({result.method}) — {result.reason} "
            "Retrying with GPT-4o Vision."
        )
    elif score < 0.3:
        summary = (
            f"Quality {score:.2f} ({result.method}) — {result.reason} "
            "Vision already attempted. Proceeding with best available text."
        )
    else:
        summary = (
            f"Quality {score:.2f} ({result.method}) — {result.reason} "
            "Proceeding."
        )

    step = await _log(state, "assess_quality", summary)
    return {**state, "quality": score, "step": step}


async def translate(state: AgentState) -> AgentState:
    result = await asyncio.to_thread(detect_and_translate, state["extracted_text"])

    if result.is_english:
        summary = f"Language: {result.source_language} — already English, no translation needed."
    else:
        summary = (
            f"Language: {result.source_language} — translated to English "
            f"({len(result.translated_text or ''):,} chars)."
        )

    step = await _log(state, "translate", summary)
    return {
        **state,
        "source_language": result.source_language,
        "translated_text": result.translated_text,
        "is_english": result.is_english,
        "step": step,
    }


async def save_doc(state: AgentState) -> AgentState:
    async with AsyncSessionLocal() as db:
        await update_document(
            db,
            state["doc_id"],
            original_text=state.get("extracted_text"),
            translated_text=state.get("translated_text"),
            source_language=state.get("source_language", "Unknown"),
            quality_score=state.get("quality"),
            status="done",
        )

    step = await _log(state, "save_doc", "Document saved to database successfully.")
    return {**state, "step": step}


# ---------------------------------------------------------------------------
# Conditional edge functions
# ---------------------------------------------------------------------------

def route_to_extractor(state: AgentState) -> str:
    return state.get("_next", "image")


def quality_check(state: AgentState) -> str:
    score = state.get("quality", 0.0)
    vision_tried = state.get("vision_attempted", False)
    if score < 0.3 and not vision_tried:
        return "retry_vision"
    return "translate"


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------

def build_graph() -> StateGraph:
    g = StateGraph(AgentState)

    # Register nodes
    g.add_node("route", route)
    g.add_node("extract_pdf", extract_pdf)
    g.add_node("extract_word", extract_word)
    g.add_node("extract_image", extract_image)
    g.add_node("assess_quality", assess_quality)
    g.add_node("translate", translate)
    g.add_node("save_doc", save_doc)

    # Entry point
    g.set_entry_point("route")

    # route → appropriate extractor
    g.add_conditional_edges(
        "route",
        route_to_extractor,
        {
            "pdf": "extract_pdf",
            "word": "extract_word",
            "image": "extract_image",
        },
    )

    # All extractors → assess_quality
    g.add_edge("extract_pdf", "assess_quality")
    g.add_edge("extract_word", "assess_quality")
    g.add_edge("extract_image", "assess_quality")

    # assess_quality → translate OR retry with Vision
    g.add_conditional_edges(
        "assess_quality",
        quality_check,
        {
            "retry_vision": "extract_image",
            "translate": "translate",
        },
    )

    # translate → save → END
    g.add_edge("translate", "save_doc")
    g.add_edge("save_doc", END)

    return g.compile()


_graph = build_graph()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_graph(
    doc_id: uuid.UUID,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
) -> None:
    """
    Execute the LangGraph pipeline for one uploaded document.
    Called as a FastAPI BackgroundTask — errors are caught and written to DB.
    """
    initial_state: AgentState = {
        "doc_id": doc_id,
        "filename": filename,
        "mime_type": mime_type,
        "file_bytes": file_bytes,
        "extracted_text": "",
        "quality": 0.0,
        "source_language": "",
        "translated_text": None,
        "is_english": False,
        "vision_attempted": False,
        "step": 0,
    }

    try:
        await _graph.ainvoke(initial_state)
    except Exception as exc:
        # Mark document as failed so the UI shows an error state
        async with AsyncSessionLocal() as db:
            await update_document(
                db,
                doc_id,
                status="failed",
                error=str(exc),
            )
        raise
