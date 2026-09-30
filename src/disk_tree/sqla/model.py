import json
import os
from datetime import datetime
from os import makedirs, remove
from os.path import join, exists
from typing import Optional

import pandas as pd
from sqlalchemy import Integer, String, DateTime, Float, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from utz import err, iec

from disk_tree import find
from disk_tree.storage import get_backend
from .base import Base


class ScanProgress(Base):
    """Track progress of active scans. One row per active scan, deleted when complete."""
    __tablename__ = "scan_progress"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, init=False)
    path: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    started: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    items_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_per_sec: Mapped[Optional[float]] = mapped_column(Integer, nullable=True, default=None)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False, default='running')  # running, completed, failed

    @classmethod
    def start(cls, path: str) -> 'ScanProgress':
        """Start tracking a new scan."""
        from .db import db
        import os

        from disk_tree.backends import canonical
        path = canonical(path)
        # Remove any existing progress for this path
        db.session.query(cls).filter_by(path=path).delete()
        progress = cls(
            path=path,
            pid=os.getpid(),
            started=datetime.now().astimezone(),
            items_found=0,
            error_count=0,
            status='running',
        )
        db.session.add(progress)
        db.session.commit()
        return progress

    @classmethod
    def update(cls, path: str, items_found: int, items_per_sec: float | None = None, error_count: int = 0):
        """Update progress for a scan."""
        from .db import db

        progress = db.session.query(cls).filter_by(path=path).first()
        if progress:
            progress.items_found = items_found
            if items_per_sec is not None:
                progress.items_per_sec = items_per_sec
            progress.error_count = error_count
            db.session.commit()

    @classmethod
    def finish(cls, path: str, status: str = 'completed'):
        """Mark a scan as complete and remove progress tracking."""
        from .db import db

        db.session.query(cls).filter_by(path=path).delete()
        db.session.commit()

    @classmethod
    def get_all(cls) -> list['ScanProgress']:
        """Get all active scans."""
        from .db import db
        return db.session.query(cls).all()


class Diff(Base):
    """A persisted diff index for a scan pair (`diff_index.py`)."""
    __tablename__ = "diff"
    __table_args__ = (UniqueConstraint('scan_a', 'scan_b'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, init=False)
    scan_a: Mapped[int] = mapped_column(Integer, nullable=False)
    scan_b: Mapped[int] = mapped_column(Integer, nullable=False)
    time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    blob: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    status: Mapped[str] = mapped_column(String, nullable=False, default='building')  # building, done, failed
    n_rows: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    n_added: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    n_removed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    n_changed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    n_touched: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)


class Scan(Base):
    __tablename__ = "scan"
    __table_args__ = (
        # Index for finding fresher child scans: path LIKE 'prefix%' AND time > parent_time
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

    @classmethod
    def create(
        cls,
        path: str,
        gc: bool = False,
        sudo: bool = False,
        mean_mtime: bool = False,
        track_progress: bool = True,
        progress: bool = True,
    ) -> tuple['Scan', pd.DataFrame]:
        from .db import db

        from disk_tree.backends import canonical
        path = canonical(path)
        now = datetime.now().astimezone()

        # Set up progress tracking
        progress_callback = None
        if track_progress:
            ScanProgress.start(path)

            def progress_callback(items_found: int, items_per_sec: float | None, error_count: int):
                ScanProgress.update(path, items_found, items_per_sec, error_count)

        try:
            result = find.index(path, sudo=sudo, mean_mtime=mean_mtime, progress_callback=progress_callback, progress=progress)
        except Exception as e:
            if track_progress:
                ScanProgress.finish(path, status='failed')
            raise
        finally:
            if track_progress:
                ScanProgress.finish(path, status='completed')

        df = result.df

        # Save via storage backend
        backend = get_backend()
        blob_ref = backend.save(df, path)

        # Extract root stats for denormalization
        root_rows = df[df['parent'] == '']
        root_size = None
        root_n_children = None
        root_n_desc = None
        root_mtime = None
        if not root_rows.empty:
            root = root_rows.iloc[0]
            root_size = int(root['size']) if pd.notna(root['size']) else None
            root_n_children = int(root['n_children']) if pd.notna(root.get('n_children')) else None
            root_n_desc = int(root['n_desc']) if pd.notna(root.get('n_desc')) else None
            root_mtime = int(root['mtime']) if pd.notna(root.get('mtime')) else None

        # Save "scan" record with error info and denormalized stats
        error_paths_json = json.dumps(result.error_paths) if result.error_paths else None
        scan = Scan(
            path=path,
            time=now,
            blob=blob_ref,
            error_count=result.error_count if result.error_count > 0 else None,
            error_paths=error_paths_json,
            size=root_size,
            n_children=root_n_children,
            n_desc=root_n_desc,
            mtime=root_mtime,
        )
        db.session.add(scan)
        db.session.commit()
        err(f"{path}: saved {len(df)} rows to {blob_ref} ({backend.name})")
        if result.error_count > 0:
            err(f"{path}: {result.error_count} permission errors")
        if gc:
            cls.gc(path, now)

        return scan, df

    @classmethod
    def gc(
        cls,
        path: str,
        cutoff: datetime,
    ):
        from .db import db

        backend = get_backend()
        scans = db.session.query(Scan).filter_by(path=path).filter(Scan.time < cutoff).all()
        err(f"{path}: deleting {len(scans)} old scans:")
        for scan in scans:
            blob = scan.blob
            err(f"\t{scan.time}: {blob}")
            db.session.delete(scan)
            db.session.commit()
            backend.delete(blob)
        if scans:
            # Diff indexes referencing a deleted scan can't be served.
            from disk_tree.cli.diff_index import gc_indexes
            gc_indexes()

    @classmethod
    def load(
        cls,
        path: str,
    ) -> 'Scan | None':
        from .db import db

        abspath = os.path.abspath(path).rstrip('/')
        return db.session.query(cls).filter_by(path=abspath).order_by(cls.time.desc()).first()

    def df(self) -> pd.DataFrame:
        backend = get_backend()
        df = backend.load(self.blob)
        err(f"{self.path}: loaded {len(df)} rows from {self.blob} ({backend.name})")
        return df

    @classmethod
    def load_reachable(cls, path: str) -> 'Scan | None':
        """The freshest scan of `path` whose blob is reachable now — skips scans
        whose parquet is on an unmounted volume (spec `r2-scan-target.md`)."""
        from disk_tree.config import blob_reachable
        scans = db.session.query(cls).filter_by(path=os.path.abspath(path).rstrip('/')).order_by(cls.time.desc()).all()
        return next((s for s in scans if blob_reachable(s.blob)), None)

    @classmethod
    def load_or_create(
        cls,
        path: str,
        gc: bool = False,
        sudo: bool = False,
        mean_mtime: bool = False,
        track_progress: bool = True,
        progress: bool = True,
    ) -> tuple['Scan', pd.DataFrame]:
        from disk_tree.config import blob_reachable
        scan = cls.load(path)
        if scan and not blob_reachable(scan.blob):
            # Freshest cached scan's blob is unreachable (e.g. on an unmounted
            # volume). Prefer an older reachable scan; if none, scan fresh rather
            # than crashing on the missing blob (spec `r2-scan-target.md`).
            older = cls.load_reachable(path)
            if older:
                err(f"{path}: freshest scan's blob {scan.blob} is unreachable, using scan {older.id} instead")
                scan = older
            else:
                err(f"{path}: cached scan's blob {scan.blob} is unreachable (unmounted volume?), rescanning")
                scan = None
        if not scan:
            return cls.create(path, gc=gc, sudo=sudo, mean_mtime=mean_mtime, track_progress=track_progress, progress=progress)
        df = scan.df()
        if mean_mtime and 'mtime_mean' not in df.columns:
            # Cached scan predates the flag — rescan rather than silently
            # serving a frame without the requested column.
            err(f"{path}: cached scan lacks `mtime_mean`, rescanning")
            return cls.create(path, gc=gc, sudo=sudo, mean_mtime=True, track_progress=track_progress, progress=progress)
        cls.gc(path=path, cutoff=scan.time)
        return scan, df
