import asyncio
import json
import logging
import random
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import ssstik
from thumbnails import Thumbnails

LOG = logging.getLogger(__name__)
MAX_CONCURRENT = 3
LAUNCH_INTERVAL = 12.0
MAX_ATTEMPTS = 4
RETRY_DELAY = 5.0
RATE_LIMIT_DELAY = 60.0


def normalize_url(value: str) -> tuple[str, str]:
    """Accept individual TikTok posts and TikTok-owned short links only."""
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower()
        if host == "m.tiktok.com":
            host = "www.tiktok.com"
        if (parsed.scheme.lower() not in ("http", "https") or
                host not in {"tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"} or
                parsed.username or parsed.password or parsed.port not in (None, 80, 443)):
            raise ValueError
        path = parsed.path.rstrip("/")
        match = re.fullmatch(r"/(?:@[^/\s]+/)?(video|photo)/(\d+)", path)
        if host in {"tiktok.com", "www.tiktok.com"} and match:
            return f"https://www.tiktok.com{path}", f"post:{match.group(2)}"
        if ((host in {"vm.tiktok.com", "vt.tiktok.com"} and re.fullmatch(r"/[A-Za-z0-9_-]+", path)) or
                (host in {"tiktok.com", "www.tiktok.com"} and re.fullmatch(r"/t/[A-Za-z0-9_-]+", path))):
            return f"https://{host}{path}/", f"short:{host}{path}"
    except (ValueError, TypeError):
        pass
    raise ValueError(f"Not a TikTok post or share link: {value[:160]}")


class BatchRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10000)
    request_id: str | None = Field(default=None, max_length=128)


class BatchAddRequest(BatchRequest):
    batch_id: str


class QueueStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.downloads_dir = self.data_dir / "downloads"
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.counter_file = self.data_dir / "counter.txt"
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
        # The downloader installs the final filename only after a validated, atomic save.
        with self.db:
            for job in self.db.execute("SELECT * FROM jobs WHERE status='processing'").fetchall():
                path = self.downloads_dir / f"{job['number']}.mp4"
                if path.is_file() and path.stat().st_size > 0:
                    self.update(job["id"], status="completed", progress=100,
                                filename=path.name, message="Recovered completed download")
                else:
                    self.update(job["id"], status="queued", progress=0,
                                attempts=max(0, job["attempts"] - 1), next_attempt_at=0,
                                message="Resumed after interruption")

    def close(self):
        self.db.close()

    def update(self, job_id: int, **values):
        values["updated_at"] = time.time()
        assignments = ", ".join(f"{key}=?" for key in values)
        self.db.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*values.values(), job_id))
        self.db.execute("UPDATE batches SET updated_at=? WHERE id=(SELECT batch_id FROM jobs WHERE id=?)",
                        (values["updated_at"], job_id))

    def get_setting(self, key: str) -> float:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return float(row[0]) if row else 0.0

    def set_setting(self, key: str, value: float):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key, value))

    def _next_number(self) -> int:
        try:
            counter = int(self.counter_file.read_text().strip())
        except (OSError, ValueError):
            counter = 0
        stored = self.db.execute("SELECT COALESCE(MAX(number), 0) FROM jobs").fetchone()[0]
        existing = max((int(p.stem) for p in self.downloads_dir.glob("*.mp4") if p.stem.isdigit()), default=0)
        return max(counter, stored, existing) + 1

    def _sync_counter(self):
        # SQLite is authoritative; the legacy counter remains useful for older versions.
        try:
            number = self._next_number() - 1
            temp = self.counter_file.with_suffix(".tmp")
            temp.write_text(str(number))
            temp.replace(self.counter_file)
        except OSError:
            LOG.warning("Could not update legacy counter; queue.sqlite3 retains reserved numbers")

    def archived(self, identity: str, exclude: int = 0):
        rows = self.db.execute("SELECT * FROM jobs WHERE identity=? AND status='completed' AND id != ? ORDER BY id DESC",
                               (identity, exclude)).fetchall()
        for row in rows:
            path = self.downloads_dir / (row["filename"] or "")
            if path.is_file() and path.stat().st_size > 0:
                return row
        return None

    def add(self, urls: list[str], batch_id: str | None = None, request_id: str | None = None):
        normalized = [normalize_url(url) for url in urls]
        request_urls = json.dumps(normalized)
        if request_id:
            if batch_id:
                previous = self.db.execute("SELECT * FROM additions WHERE request_id=?", (request_id,)).fetchone()
                if previous:
                    if previous["batch_id"] != batch_id or previous["request_urls"] != request_urls:
                        raise HTTPException(409, "This request ID was already used for different links")
                    response = json.loads(previous["response"])
                    response["snapshot"] = self.snapshot(batch_id)
                    return response
            else:
                previous = self.db.execute("SELECT * FROM batches WHERE request_id=?", (request_id,)).fetchone()
                if previous:
                    if previous["request_urls"] != request_urls:
                        raise HTTPException(409, "This request ID was already used for different links")
                    snapshot = self.snapshot(previous["id"])
                    return {"batch_id": previous["id"], "urls": snapshot["urls"], "numbers": snapshot["numbers"],
                            "duplicates": max(0, len(urls) - len(snapshot["urls"])),
                            "total": snapshot["total"], "snapshot": snapshot}
        now = time.time()
        is_addition = batch_id is not None
        with self.db:
            if batch_id is None:
                batch_id = str(uuid.uuid4())
                self.db.execute("INSERT INTO batches VALUES (?,?,?,?,?)", (batch_id, request_id, request_urls, now, now))
            elif not self.db.execute("SELECT id FROM batches WHERE id=?", (batch_id,)).fetchone():
                raise HTTPException(404, "Batch not found")
            next_number = self._next_number()
            accepted_urls, numbers = [], []
            for url, identity in normalized:
                if self.db.execute("SELECT id FROM jobs WHERE batch_id=? AND identity=?", (batch_id, identity)).fetchone():
                    continue
                archive = self.archived(identity)
                number = archive["number"] if archive else next_number
                if not archive:
                    next_number += 1
                self.db.execute("""INSERT INTO jobs
                    (batch_id,url,identity,number,status,progress,message,filename,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""", (batch_id, url, identity, number,
                    "completed" if archive else "queued", 100 if archive else 0,
                    "Already downloaded" if archive else "Waiting...", archive["filename"] if archive else None, now))
                accepted_urls.append(url)
                numbers.append(number)
            self.db.execute("UPDATE batches SET updated_at=? WHERE id=?", (now, batch_id))
            snapshot = self.snapshot(batch_id)
            response = {"batch_id": batch_id, "urls": accepted_urls, "numbers": numbers,
                        "duplicates": len(urls) - len(accepted_urls), "total": snapshot["total"], "snapshot": snapshot}
            if request_id and is_addition:
                self.db.execute("INSERT INTO additions VALUES (?,?,?,?)",
                                (request_id, batch_id, request_urls, json.dumps(response)))
        self._sync_counter()
        return response

    def snapshot(self, batch_id: str):
        batch = self.db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise HTTPException(404, "Batch not found")
        rows = self.db.execute("SELECT * FROM jobs WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        results = [{"url_index": i, **{key: row[key] for key in (
            "url", "number", "status", "progress", "message", "filename", "attempts", "next_attempt_at"
        )}} for i, row in enumerate(rows)]
        successful = sum(row["status"] == "completed" for row in rows)
        failed = sum(row["status"] == "error" for row in rows)
        active = sum(row["status"] == "processing" for row in rows)
        finished = successful + failed == len(rows)
        return {"batch_id": batch_id, "request_id": batch["request_id"],
                "status": "completed" if finished else "running", "total": len(rows),
                "urls": [row["url"] for row in rows], "numbers": [row["number"] for row in rows],
                "results": results, "successful": successful, "failed": failed,
                "completed_count": successful + failed, "active_count": active,
                "created_at": batch["created_at"], "updated_at": batch["updated_at"],
                "cooldown_until": self.get_setting("cooldown_until")}

    def retry(self, batch_id: str):
        self.snapshot(batch_id)
        with self.db:
            self.db.execute("""UPDATE jobs SET status='queued', progress=0, attempts=0,
                next_attempt_at=0, message='Queued for retry', updated_at=? WHERE batch_id=? AND status='error'""",
                (time.time(), batch_id))
            self.db.execute("UPDATE batches SET updated_at=? WHERE id=?", (time.time(), batch_id))
        return self.snapshot(batch_id)


class DownloadQueue:
    def __init__(self, store: QueueStore, downloader_factory=None, *, max_concurrent=MAX_CONCURRENT,
                 launch_interval=LAUNCH_INTERVAL, retry_delay=RETRY_DELAY, rate_limit_delay=RATE_LIMIT_DELAY):
        self.store = store
        self.downloader_factory = downloader_factory or ssstik.SsstikDownloader
        self.max_concurrent = max(1, max_concurrent)
        self.launch_interval = max(0, launch_interval)
        self.retry_delay = retry_delay
        self.rate_limit_delay = rate_limit_delay
        self.downloader = None
        self.browser_broken = False
        self.active: dict[int, asyncio.Task] = {}
        self.observers: dict[str, dict[WebSocket, asyncio.Queue]] = {}
        self.wake = asyncio.Event()
        self.runner = None
        self.stopping = False
        self.next_launch_at = 0.0

    def start(self):
        self.runner = asyncio.create_task(self.run())

    def publish(self, batch_id: str):
        snapshot = {"type": "snapshot", **self.store.snapshot(batch_id)}
        for queue in list(self.observers.get(batch_id, {}).values()):
            # Slow observers never stall downloads or grow memory without a bound.
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(snapshot)
        self.wake.set()

    async def close(self):
        self.stopping = True
        self.wake.set()
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        for task in self.active.values():
            task.cancel()
        if self.active:
            await asyncio.gather(*self.active.values(), return_exceptions=True)
        if self.downloader:
            try:
                await self.downloader.close_browser()
            except Exception:
                LOG.exception("Failed to close downloader browser")
        with self.store.db:
            self.store.db.execute("""UPDATE jobs SET status='queued', progress=0,
                attempts=MAX(0,attempts-1), message='Resumed after interruption'
                WHERE status='processing'""")
        self.store.close()

    def ready(self):
        return self.store.db.execute("""SELECT * FROM jobs j WHERE status IN ('queued','retrying')
            AND next_attempt_at <= ? AND NOT EXISTS
            (SELECT 1 FROM jobs other WHERE other.identity=j.identity AND other.status='processing')
            ORDER BY next_attempt_at, id LIMIT 1""", (time.time(),)).fetchone()

    async def run(self):
        try:
            while not self.stopping:
                self.wake.clear()
                self.active = {key: task for key, task in self.active.items() if not task.done()}
                if self.browser_broken and not self.active:
                    try:
                        if self.downloader:
                            await self.downloader.close_browser()
                    except Exception:
                        LOG.exception("Could not close failed browser")
                    self.downloader = None
                    self.browser_broken = False
                now = time.time()
                can_launch = (not self.browser_broken and len(self.active) < self.max_concurrent and
                              now >= max(self.next_launch_at, self.store.get_setting("cooldown_until")))
                job = self.ready() if can_launch else None
                if job:
                    archived = self.store.archived(job["identity"], job["id"])
                    if archived:
                        with self.store.db:
                            self.store.update(job["id"], status="completed", progress=100, number=archived["number"],
                                              filename=archived["filename"], message="Already downloaded")
                        self.publish(job["batch_id"])
                        continue
                    with self.store.db:
                        self.store.update(job["id"], status="processing", progress=0,
                                          attempts=job["attempts"] + 1, message="Starting...")
                    self.publish(job["batch_id"])
                    if self.downloader is None:
                        candidate = self.downloader_factory(self.store.downloads_dir)
                        try:
                            await candidate.start_browser()
                            self.downloader = candidate
                        except asyncio.CancelledError:
                            await candidate.close_browser()
                            raise
                        except Exception as error:
                            try:
                                await candidate.close_browser()
                            except Exception:
                                pass
                            self.fail(job, error, browser_error=True)
                            continue
                    task = asyncio.create_task(self.download(job))
                    task.add_done_callback(lambda _: self.wake.set())
                    self.active[job["id"]] = task
                    self.next_launch_at = time.time() + self.launch_interval
                    continue
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Download scheduler failed")
            with self.store.db:
                for job in self.store.db.execute("SELECT * FROM jobs WHERE status IN ('queued','retrying')").fetchall():
                    self.store.update(job["id"], status="error", message="Queue error; restart the app to recover")
                    self.publish(job["batch_id"])

    def fail(self, job, error: Exception, *, browser_error=False):
        attempts = job["attempts"] + 1
        permanent_type = getattr(ssstik, "PermanentDownloadError", ())
        browser_type = getattr(ssstik, "BrowserUnavailableError", ())
        is_permanent = isinstance(error, permanent_type)
        is_rate_limit = isinstance(error, ssstik.RateLimitError)
        if browser_error or isinstance(error, browser_type):
            self.browser_broken = True
        message = (str(error) or error.__class__.__name__).splitlines()[0][:300]
        delay = self.retry_delay * (2 ** (attempts - 1))
        if is_rate_limit:
            delay = self.rate_limit_delay * (2 ** (attempts - 1))
            self.store.set_setting("cooldown_until", max(self.store.get_setting("cooldown_until"), time.time() + delay))
        retry = not is_permanent and attempts < MAX_ATTEMPTS
        retry_at = time.time() + delay + random.uniform(0, max(0, delay * 0.15))
        with self.store.db:
            self.store.update(job["id"], status="retrying" if retry else "error", progress=0,
                              next_attempt_at=retry_at if retry else 0,
                              message=(f"{message}; retry {attempts + 1}/{MAX_ATTEMPTS} scheduled" if retry else message))
        self.publish(job["batch_id"])
        if is_rate_limit:
            for batch_id in list(self.observers):
                if batch_id != job["batch_id"]:
                    self.publish(batch_id)

    async def download(self, job):
        async def progress_callback(progress: int, message: str):
            with self.store.db:
                self.store.update(job["id"], progress=max(0, min(int(progress), 99)), message=message[:300])
            self.publish(job["batch_id"])
        try:
            filename = await self.downloader.download_url(job["url"], job["number"], progress_callback)
            with self.store.db:
                self.store.update(job["id"], status="completed", progress=100, filename=filename,
                                  message=f"Saved as {filename}", next_attempt_at=0)
            self.publish(job["batch_id"])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fail(job, error)


def create_app(data_dir: Path | str | None = None, downloader_factory=None, **queue_options):
    base_dir = Path(data_dir) if data_dir is not None else Path(__file__).resolve().parent

    @asynccontextmanager
    async def lifespan(application):
        queue = DownloadQueue(QueueStore(base_dir), downloader_factory, **queue_options)
        application.state.queue = queue
        queue.start()
        try:
            yield
        finally:
            await queue.close()

    application = FastAPI(lifespan=lifespan)
    static_dir = Path(__file__).resolve().parent / "static"
    application.mount("/static", StaticFiles(directory=static_dir, check_dir=False), name="static")

    previews = Thumbnails()

    @application.get("/api/thumbnail")
    async def thumbnail(url: str):
        try:
            normalized, _ = normalize_url(url)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return await previews.response(normalized)

    @application.get("/")
    async def root():
        return FileResponse(static_dir / "index.html")

    @application.get("/api/batches")
    async def list_batches():
        queue = application.state.queue
        rows = queue.store.db.execute("SELECT id FROM batches ORDER BY updated_at DESC LIMIT 100").fetchall()
        return {"batches": [queue.store.snapshot(row["id"]) for row in rows]}

    @application.get("/api/batch/{batch_id}")
    async def batch_snapshot(batch_id: str):
        return application.state.queue.store.snapshot(batch_id)

    async def add_request(request, batch_id=None):
        queue = application.state.queue
        try:
            result = queue.store.add(request.urls, batch_id, request.request_id)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        queue.publish(result["batch_id"])
        return result

    @application.post("/api/batch")
    async def start_batch(request: BatchRequest):
        return await add_request(request)

    @application.post("/api/batch/add")
    async def add_to_batch(request: BatchAddRequest):
        return await add_request(request, request.batch_id)

    @application.post("/api/batch/{batch_id}/retry")
    async def retry_failed(batch_id: str):
        queue = application.state.queue
        snapshot = queue.store.retry(batch_id)
        queue.publish(batch_id)
        return snapshot

    @application.websocket("/ws/{batch_id}")
    async def websocket_progress(websocket: WebSocket, batch_id: str):
        queue = application.state.queue
        try:
            snapshot = queue.store.snapshot(batch_id)
        except HTTPException:
            await websocket.close(code=1008, reason="Batch not found")
            return
        await websocket.accept()
        updates = asyncio.Queue(maxsize=1)
        queue.observers.setdefault(batch_id, {})[websocket] = updates
        updates.put_nowait({"type": "snapshot", **snapshot})

        async def send_updates():
            while True:
                await websocket.send_json(await updates.get())

        async def receive_messages():
            while True:
                await websocket.receive_text()

        tasks = [asyncio.create_task(send_updates()), asyncio.create_task(receive_messages())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    task.result()
                except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
                    pass
        except asyncio.CancelledError:
            pass
        finally:
            for task in tasks:
                task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                pass
            observers = queue.observers.get(batch_id, {})
            observers.pop(websocket, None)
            if not observers:
                queue.observers.pop(batch_id, None)

    return application


app = create_app()
