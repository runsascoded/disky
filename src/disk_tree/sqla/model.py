from datetime import datetime
from typing import Optional

from sqlalchemy import Integer, String, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Scan(Base):
    """One imported scan: `disk-tree import` writes a row per bucket beside its blob."""
    __tablename__ = "scan"
    __table_args__ = (
        Index('ix_scan_path_time', 'path', 'time'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, init=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    blob: Mapped[str] = mapped_column(String, nullable=False)
    error_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    error_paths: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)  # JSON array
    # Denormalized root stats (to avoid parquet reads on scan list)
    size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    n_children: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    n_desc: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    mtime: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
