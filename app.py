"""FastAPI composition root for the TikTok downloader."""

import asyncio
import os
import re
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import ssstik
from config import QUEUE_CONFIG
from domain import BatchAddRequest, BatchRequest, normalize_url
from queue_store import QueueStore
from scheduler import DownloadQueue
from thumbnails import Thumbnails

# Compatibility exports for existing integrations and tests.
MAX_CONCURRENT = QUEUE_CONFIG.max_concurrent
LAUNCH_INTERVAL = QUEUE_CONFIG.launch_interval
MAX_ATTEMPTS = QUEUE_CONFIG.max_attempts
RETRY_DELAY = QUEUE_CONFIG.retry_delay
RATE_LIMIT_DELAY = QUEUE_CONFIG.rate_limit_delay


def create_app(data_dir: Path | str | None = None, downloader_factory=None, **queue_options):
    base_dir = Path(data_dir) if data_dir is not None else Path(__file__).resolve().parent
    previews = Thumbnails()

    @asynccontextmanager
    async def lifespan(application):
        queue = DownloadQueue(QueueStore(base_dir), downloader_factory, **queue_options)
        application.state.queue = queue
        queue.start()
        try:
            yield
        finally:
            await queue.close()
            await previews.close()

    application = FastAPI(lifespan=lifespan)
    static_dir = Path(__file__).resolve().parent / "static"
    application.mount(
        "/static", StaticFiles(directory=static_dir, check_dir=False), name="static"
    )

    @application.get("/api/thumbnail")
    async def thumbnail(url: str, attempt: int = 0):
        try:
            normalized, _ = normalize_url(url)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return await previews.response(normalized, refresh=attempt > 0)

    @application.get("/")
    async def root():
        return FileResponse(
            static_dir / "index.html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @application.get("/api/batches")
    async def list_batches():
        queue = application.state.queue
        rows = queue.store.db.execute(
            "SELECT id FROM batches ORDER BY updated_at DESC LIMIT 100"
        ).fetchall()
        return {"batches": [queue.store.snapshot(row["id"]) for row in rows]}

    @application.post("/api/open-downloads")
    async def open_downloads_folder():
        folder = application.state.queue.store.downloads_dir.resolve()
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer.exe", str(folder)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as error:
            raise HTTPException(500, f"Could not open {folder}: {error}") from error
        return {"path": str(folder)}

    @application.get("/api/downloads/{filename}")
    async def retrieve_download(filename: str):
        if not re.fullmatch(r"\d+\.mp4", filename):
            raise HTTPException(404, "Download not found")
        folder = application.state.queue.store.downloads_dir.resolve()
        path = folder / filename
        if not ssstik.is_valid_mp4(path):
            raise HTTPException(404, "Downloaded file is missing or invalid")
        return FileResponse(path, media_type="video/mp4", filename=filename)

    @application.get("/api/batch/{batch_id}")
    async def batch_snapshot(batch_id: str):
        return application.state.queue.store.snapshot(batch_id)

    @application.get(
        "/api/batch/{batch_id}/failed.txt", response_class=PlainTextResponse
    )
    async def failed_links(batch_id: str):
        snapshot = application.state.queue.store.snapshot(batch_id)
        urls = [
            result["url"] for result in snapshot["results"]
            if result["status"] == "error"
        ]
        return PlainTextResponse(
            "\n".join(urls) + ("\n" if urls else ""),
            headers={
                "Content-Disposition": f'attachment; filename="failed-{batch_id}.txt"'
            },
        )

    async def add_request(request, batch_id=None):
        queue = application.state.queue
        try:
            result = queue.store.add(
                request.urls, batch_id, request.request_id,
                getattr(request, "start_number", 1),
            )
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

        tasks = [
            asyncio.create_task(send_updates()),
            asyncio.create_task(receive_messages()),
        ]
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
