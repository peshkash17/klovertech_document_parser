import uuid
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------

class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)  # pdf / docx / image
    source_language: Mapped[str | None] = mapped_column(String(100), nullable=True)
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    translated_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="processing")  # processing / done / failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    logs: Mapped[list["DocumentLog"]] = relationship(
        "DocumentLog", back_populates="document", order_by="DocumentLog.step"
    )


class DocumentLog(Base):
    __tablename__ = "document_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    document: Mapped["Document"] = relationship("Document", back_populates="logs")


# ---------------------------------------------------------------------------
# Pydantic Response Schemas
# ---------------------------------------------------------------------------

class DocumentLogSchema(BaseModel):
    id: uuid.UUID
    step: int
    tool_name: str
    summary: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentSummary(BaseModel):
    id: uuid.UUID
    filename: str
    file_type: str
    source_language: str | None
    quality_score: float | None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentDetail(DocumentSummary):
    original_text: str | None
    translated_text: str | None
    error: str | None
    logs: list[DocumentLogSchema] = []
