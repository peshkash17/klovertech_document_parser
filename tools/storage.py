"""
tools/storage.py

All async DB read/write helpers used by the LangGraph agent and FastAPI endpoints.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Document, DocumentLog


# ---------------------------------------------------------------------------
# Write helpers (used inside the agent / background task)
# ---------------------------------------------------------------------------

async def create_document(
    db: AsyncSession,
    *,
    doc_id: uuid.UUID,
    filename: str,
    file_type: str,
) -> Document:
    doc = Document(
        id=doc_id,
        filename=filename,
        file_type=file_type,
        status="processing",
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def update_document(
    db: AsyncSession,
    doc_id: uuid.UUID,
    **kwargs: Any,
) -> None:
    result = await db.get(Document, doc_id)
    if result is None:
        return
    for key, value in kwargs.items():
        setattr(result, key, value)
    await db.commit()


async def append_log(
    db: AsyncSession,
    *,
    document_id: uuid.UUID,
    step: int,
    tool_name: str,
    summary: str,
) -> None:
    log = DocumentLog(
        document_id=document_id,
        step=step,
        tool_name=tool_name,
        summary=summary,
    )
    db.add(log)
    await db.commit()


# ---------------------------------------------------------------------------
# Read helpers (used by FastAPI endpoints)
# ---------------------------------------------------------------------------

async def get_document(db: AsyncSession, doc_id: uuid.UUID) -> Document | None:
    return await db.get(Document, doc_id)


async def list_documents(db: AsyncSession, limit: int = 50) -> list[Document]:
    result = await db.execute(
        select(Document).order_by(Document.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def get_logs(db: AsyncSession, doc_id: uuid.UUID) -> list[DocumentLog]:
    result = await db.execute(
        select(DocumentLog)
        .where(DocumentLog.document_id == doc_id)
        .order_by(DocumentLog.step)
    )
    return list(result.scalars().all())
