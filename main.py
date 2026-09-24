"""
main.py — FastAPI application

Endpoints:
  GET  /                      → serves index.html (UI)
  POST /upload                → accept file, kick off BackgroundTask
  GET  /documents             → list all documents
  GET  /documents/{id}        → document detail
  GET  /documents/{id}/logs   → agent step trace (polled by UI)
"""

import uuid
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from agent import run_graph
from database import get_db, lifespan
from models import DocumentDetail, DocumentLogSchema, DocumentSummary
from tools.storage import (
    create_document,
    get_document,
    get_logs,
    list_documents,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="KloverTech IDP Agent",
    description="Intelligent Document Processing — extract, detect language, translate.",
    version="1.0.0",
    lifespan=lifespan,
)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/tiff",
    "image/bmp",
}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/upload")
async def upload(
    file: UploadFile,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    # Validate content type
    content_type = file.content_type or ""
    # Normalise docx mime (browsers sometimes send octet-stream)
    filename_lower = (file.filename or "").lower()
    if filename_lower.endswith(".docx"):
        content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif filename_lower.endswith(".doc"):
        content_type = "application/msword"
    elif filename_lower.endswith(".pdf"):
        content_type = "application/pdf"

    if content_type not in SUPPORTED_MIME_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {content_type}. "
                   "Accepted: PDF, DOCX, JPEG, PNG, WEBP, TIFF, BMP.",
        )

    # Read into memory
    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(file_bytes) / 1024 / 1024:.1f} MB). Maximum is 20 MB.",
        )

    # Determine simplified file_type for storage
    if "pdf" in content_type:
        file_type = "pdf"
    elif "word" in content_type or "msword" in content_type or "officedocument" in content_type:
        file_type = "docx"
    else:
        file_type = "image"

    # Create DB row immediately → frontend can poll right away
    doc_id = uuid.uuid4()
    await create_document(db, doc_id=doc_id, filename=file.filename or "unknown", file_type=file_type)

    # Fire the LangGraph agent in the background
    background_tasks.add_task(run_graph, doc_id, file_bytes, file.filename or "unknown", content_type)

    return JSONResponse({"id": str(doc_id), "status": "processing"})


@app.get("/documents", response_model=list[DocumentSummary])
async def list_docs(db: AsyncSession = Depends(get_db)):
    docs = await list_documents(db)
    return docs


@app.get("/documents/{doc_id}", response_model=DocumentDetail)
async def get_doc(doc_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    doc = await get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    logs = await get_logs(db, doc_id)
    # Build the response explicitly — avoids triggering SQLAlchemy lazy-load
    # on the `logs` relationship (not allowed in async sessions).
    return DocumentDetail(
        id=doc.id,
        filename=doc.filename,
        file_type=doc.file_type,
        source_language=doc.source_language,
        quality_score=doc.quality_score,
        status=doc.status,
        created_at=doc.created_at,
        original_text=doc.original_text,
        translated_text=doc.translated_text,
        error=doc.error,
        logs=[DocumentLogSchema.model_validate(l) for l in logs],
    )


@app.get("/documents/{doc_id}/logs", response_model=list[DocumentLogSchema])
async def get_doc_logs(doc_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    doc = await get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return await get_logs(db, doc_id)
