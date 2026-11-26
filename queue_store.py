"""SQLite persistence for batches, jobs, recovery, and idempotency."""

import json
import sqlite3
import time
import uuid
from pathlib import Path

from fastapi import HTTPException

import ssstik
from domain import normalize_url


class QueueStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.downloads_dir = self.data_dir / "downloads"
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.data_dir / "queue.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE, request_urls TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL REFERENCES batches(id),
                url TEXT NOT NULL, identity TEXT NOT NULL, number INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', progress INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT 'Waiting...', filename TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                UNIQUE(batch_id, identity)
            );
            CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, next_attempt_at, id);
            CREATE INDEX IF NOT EXISTS jobs_identity ON jobs(identity, status);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS additions (
                request_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
                request_urls TEXT NOT NULL, response TEXT NOT NULL
            );
        """)
        self.db.commit()
        self._recover_and_clear_unfinished()

    def _recover_and_clear_unfinished(self):
        """Recover valid final files and discard stale work from the last run."""
        with self.db:
            jobs = self.db.execute(
                "SELECT * FROM jobs WHERE status IN ('processing','queued','retrying')"
            ).fetchall()
            for job in jobs:
                path = self.downloads_dir / f"{job['number']}.mp4"
                if job["status"] == "processing" and ssstik.is_valid_mp4(path):
                    self.update(
                        job["id"], status="completed", progress=100,
                        filename=path.name, message="Recovered completed download",
                    )
                else:
                    path.unlink(missing_ok=True)
                    self.update(
                        job["id"], status="error", progress=0, next_attempt_at=0,
                        message="Paused after app restart; click Retry failed to resume",
                    )
            completed = self.db.execute(
                "SELECT * FROM jobs WHERE status='completed'"
            ).fetchall()
            for job in completed:
                path = self.downloads_dir / (job["filename"] or "")
                if not ssstik.is_valid_mp4(path):
                    path.unlink(missing_ok=True)
                    self.update(
                        job["id"], status="error", progress=0, filename=None,
                        next_attempt_at=0,
                        message="Downloaded file is missing; click Retry failed",
                    )
            self.db.execute("DELETE FROM jobs WHERE status='error'")
            self.db.execute("DELETE FROM additions")
            self.db.execute(
                "DELETE FROM batches WHERE NOT EXISTS "
                "(SELECT 1 FROM jobs WHERE jobs.batch_id=batches.id)"
            )

    def close(self):
        self.db.close()

    def update(self, job_id: int, **values):
        values["updated_at"] = time.time()
        assignments = ", ".join(f"{key}=?" for key in values)
        self.db.execute(
            f"UPDATE jobs SET {assignments} WHERE id=?",
            (*values.values(), job_id),
        )
        self.db.execute(
            "UPDATE batches SET updated_at=? WHERE id=(SELECT batch_id FROM jobs WHERE id=?)",
            (values["updated_at"], job_id),
        )

    def get_setting(self, key: str) -> float:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return float(row[0]) if row else 0.0

    def set_setting(self, key: str, value: float):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                (key, value),
            )

    def archived(self, identity: str, exclude: int = 0):
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE identity=? AND status='completed' "
            "AND id != ? ORDER BY id DESC",
            (identity, exclude),
        ).fetchall()
        for row in rows:
            path = self.downloads_dir / (row["filename"] or "")
            if ssstik.is_valid_mp4(path):
                return row
        return None

    def add(
        self, urls: list[str], batch_id: str | None = None,
        request_id: str | None = None, start_number: int = 1,
    ):
        normalized = [normalize_url(url) for url in urls]
        request_urls = json.dumps(
            normalized if batch_id else {"urls": normalized, "start_number": start_number}
        )
        previous = self._idempotent_response(
            urls, request_urls, batch_id, request_id
        )
        if previous is not None:
            return previous
        now = time.time()
        is_addition = batch_id is not None
        with self.db:
            if batch_id is None:
                batch_id = str(uuid.uuid4())
                self.db.execute(
                    "INSERT INTO batches VALUES (?,?,?,?,?)",
                    (batch_id, request_id, request_urls, now, now),
                )
            elif not self.db.execute(
                "SELECT id FROM batches WHERE id=?", (batch_id,)
            ).fetchone():
                raise HTTPException(404, "Batch not found")
            next_number = self._next_number(batch_id, start_number, is_addition)
            accepted_urls, numbers = [], []
            for url, identity in normalized:
                exists = self.db.execute(
                    "SELECT id FROM jobs WHERE batch_id=? AND identity=?",
                    (batch_id, identity),
                ).fetchone()
                if exists:
                    continue
                archive = self.archived(identity)
                number = archive["number"] if archive else next_number
                if not archive:
                    target = self.downloads_dir / f"{number}.mp4"
                    if target.exists():
                        raise HTTPException(
                            409,
                            f"{target.name} already exists. Choose a different start number.",
                        )
                    next_number += 1
                self.db.execute(
                    """INSERT INTO jobs
                    (batch_id,url,identity,number,status,progress,message,filename,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        batch_id, url, identity, number,
                        "completed" if archive else "queued",
                        100 if archive else 0,
                        "Already downloaded" if archive else "Waiting...",
                        archive["filename"] if archive else None,
                        now,
                    ),
                )
                accepted_urls.append(url)
                numbers.append(number)
            self.db.execute(
                "UPDATE batches SET updated_at=? WHERE id=?", (now, batch_id)
            )
            snapshot = self.snapshot(batch_id)
            response = {
                "batch_id": batch_id, "urls": accepted_urls, "numbers": numbers,
                "duplicates": len(urls) - len(accepted_urls),
                "total": snapshot["total"], "snapshot": snapshot,
            }
            if request_id and is_addition:
                self.db.execute(
                    "INSERT INTO additions VALUES (?,?,?,?)",
                    (request_id, batch_id, request_urls, json.dumps(response)),
                )
        return response

    def _idempotent_response(self, urls, request_urls, batch_id, request_id):
        if not request_id:
            return None
        if batch_id:
            previous = self.db.execute(
                "SELECT * FROM additions WHERE request_id=?", (request_id,)
            ).fetchone()
            if not previous:
                return None
            if previous["batch_id"] != batch_id or previous["request_urls"] != request_urls:
                raise HTTPException(409, "This request ID was already used for different links")
            response = json.loads(previous["response"])
            response["snapshot"] = self.snapshot(batch_id)
            return response
        previous = self.db.execute(
            "SELECT * FROM batches WHERE request_id=?", (request_id,)
        ).fetchone()
        if not previous:
            return None
        if previous["request_urls"] != request_urls:
            raise HTTPException(409, "This request ID was already used for different links")
        snapshot = self.snapshot(previous["id"])
        return {
            "batch_id": previous["id"], "urls": snapshot["urls"],
            "numbers": snapshot["numbers"],
            "duplicates": max(0, len(urls) - len(snapshot["urls"])),
            "total": snapshot["total"], "snapshot": snapshot,
        }

    def _next_number(self, batch_id: str, start_number: int, is_addition: bool):
        if not is_addition:
            return start_number
        return self.db.execute(
            "SELECT COALESCE(MAX(number), 0) + 1 FROM jobs WHERE batch_id=?",
            (batch_id,),
        ).fetchone()[0]

    def snapshot(self, batch_id: str):
        batch = self.db.execute(
            "SELECT * FROM batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, "Batch not found")
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall()
        fields = (
            "url", "number", "status", "progress", "message", "filename",
            "attempts", "next_attempt_at",
        )
        results = [
            {"url_index": index, **{key: row[key] for key in fields}}
            for index, row in enumerate(rows)
        ]
        successful = sum(row["status"] == "completed" for row in rows)
        failed = sum(row["status"] == "error" for row in rows)
        active = sum(row["status"] == "processing" for row in rows)
        finished = successful + failed == len(rows)
        return {
            "batch_id": batch_id, "request_id": batch["request_id"],
            "status": "completed" if finished else "running", "total": len(rows),
            "urls": [row["url"] for row in rows],
            "numbers": [row["number"] for row in rows], "results": results,
            "successful": successful, "failed": failed,
            "completed_count": successful + failed, "active_count": active,
            "created_at": batch["created_at"], "updated_at": batch["updated_at"],
            "download_directory": str(self.downloads_dir.resolve()),
            "cooldown_until": self.get_setting("cooldown_until"),
        }

    def retry(self, batch_id: str):
        self.snapshot(batch_id)
        with self.db:
            now = time.time()
            self.db.execute(
                """UPDATE jobs SET status='queued', progress=0, attempts=0,
                next_attempt_at=0, message='Queued for retry', updated_at=?
                WHERE batch_id=? AND status='error'""",
                (now, batch_id),
            )
            self.db.execute(
                "UPDATE batches SET updated_at=? WHERE id=?", (now, batch_id)
            )
        return self.snapshot(batch_id)

