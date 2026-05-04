import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# from downloader import TikTokDownloader
from ssstik import RateLimitError, SsstikDownloader

app = FastAPI()

BASE_DIR = Path("")
DOWNLOADS_DIR = BASE_DIR / "downloads"
COUNTER_FILE = Path("counter.txt")

_counter_lock = Lock()


def _load_counter() -> int:
    try:
        return int(COUNTER_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _next_number() -> int:
    with _counter_lock:
        n = _load_counter() + 1
        COUNTER_FILE.write_text(str(n))
        return n


def _reserve_numbers(count: int) -> list[int]:
    with _counter_lock:
        start = _load_counter() + 1
        COUNTER_FILE.write_text(str(start + count - 1))
        return list(range(start, start + count))


@dataclass
class BatchState:
    urls: list[str]
    numbers: list[int]
    results: dict[int, dict] = field(default_factory=dict)
    next_launch: int = 0
    status: str = "running"

    @property
    def completed_count(self) -> int:
        return sum(1 for r in self.results.values() if r["status"] in ("completed", "error"))

    @property
    def successful_count(self) -> int:
        return sum(1 for r in self.results.values() if r["status"] == "completed")

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results.values() if r["status"] == "error")

    @property
    def active_count(self) -> int:
        return sum(1 for r in self.results.values() if r["status"] == "processing")


batches: dict[str, BatchState] = {}
ws_connections: dict[str, WebSocket] = {}

STAGGER_INTERVAL = 12
MAX_RETRIES = 1


class BatchRequest(BaseModel):
    urls: list[str]


class BatchAddRequest(BaseModel):
    batch_id: str
    urls: list[str]


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
async def startup():
    DOWNLOADS_DIR.mkdir(exist_ok=True)


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.post("/api/batch")
async def start_batch(request: BatchRequest):
    batch_id = str(uuid.uuid4())
    numbers = _reserve_numbers(len(request.urls))
    batch = BatchState(urls=list(request.urls), numbers=numbers)
    batches[batch_id] = batch
    asyncio.create_task(_run_batch(batch_id))
    return JSONResponse({
        "batch_id": batch_id,
        "numbers": numbers,
    })


@app.post("/api/batch/add")
async def add_to_batch(request: BatchAddRequest):
    batch = batches.get(request.batch_id)
    if not batch:
        return JSONResponse({"error": "Batch not found"}, status_code=404)
    new_numbers = _reserve_numbers(len(request.urls))
    batch.urls.extend(request.urls)
    batch.numbers.extend(new_numbers)
    return JSONResponse({"numbers": new_numbers, "total": len(batch.urls)})


@app.websocket("/ws/{batch_id}")
async def websocket_progress(websocket: WebSocket, batch_id: str):
    await websocket.accept()
    ws_connections[batch_id] = websocket
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_connections.pop(batch_id, None)


async def _send_ws(batch_id: str, data: dict):
    ws = ws_connections.get(batch_id)
    if ws:
        try:
            await ws.send_json(data)
        except Exception:
            ws_connections.pop(batch_id, None)


async def _run_batch(batch_id: str):
    batch = batches[batch_id]
    downloader = SsstikDownloader(DOWNLOADS_DIR, COUNTER_FILE)

    try:
        await downloader.start_browser()
    except Exception as e:
        batch.status = "error"
        await _send_ws(batch_id, {
            "type": "batch_error",
            "message": f"Failed to start browser:" {e},
        })
        return

    in_flight: list[asyncio.Task] = []

    try:
        while batch.next_launch < len(batch.urls) or in_flight:
            if batch.next_launch < len(batch.urls):
                idx = batch.next_launch
                url = batch.urls[idx]
                number = batch.numbers[idx]
                batch.results[idx] = {"status": "processing", "url": url, "number": number}

                await _send_ws(batch_id, {
                    "type": "progress",
                    "url_index": idx,
                    "total": len(batch.urls),
                    "url": url,
                    "number": number,
                    "status": "starting",
                    "progress": 0,
                    "message": "Starting...",
                    "completed_count": batch.completed_count,
                    "active_count": batch.active_count,
                })

                task = asyncio.create_task(
                    _download_one(batch_id, batch, downloader, idx, url, number)
                )
                in_flight.append(task)
                batch.next_launch += 1

                if batch.next_launch < len(batch.urls):
                    await asyncio.sleep(STAGGER_INTERVAL)
            else:
                if in_flight:
                    done, pending = await asyncio.wait(
                        in_flight, return_when=asyncio.FIRST_COMPLETED
                    )
                    in_flight = list(pending)

            in_flight = [t for t in in_flight if not t.done()]

        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)

    finally:
        await downloader.close_browser()

    batch.status = "completed"
    await _send_ws(batch_id, {
        "type": "batch_complete",
        "successful": batch.successful_count,
        "failed": batch.failed_count,
        "total": len(batch.urls),
    })


async def _download_one(
    batch_id: str, batch: BatchState, downloader: SsstikDownloader,
    idx: int, url: str, number: int
):
    retries = 0

    while retries <= MAX_RETRIES:
        try:
            async def progress_cb(progress: int, message: str):
                await _send_ws(batch_id, {
                    "type": "progress",
                    "url_index": idx,
                    "total": len(batch.urls),
                    "url": url,
                    "number": number,
                    "status": "processing",
                    "progress": progress,
                    "message": message,
                    "completed_count": batch.completed_count,
                    "active_count": batch.active_count,
                })

            filename = await downloader.download_url(url, number, progress_cb)

            batch.results[idx] = {
                "status": "completed",
                "url": url,
                "number": number,
                "filename": filename,
            }
            await _send_ws(batch_id, {
                "type": "progress",
                "url_index": idx,
                "total": len(batch.urls),
                "url": url,
                "number": number,
                "status": "completed",
                "progress": 100,
                "message": f"Saved as {filename}",
                "filename": filename,
                "completed_count": batch.completed_count,
                "active_count": batch.active_count,
            })
            return

        except RateLimitError:
            retries += 1
            if retries > MAX_RETRIES:
                break
            await _send_ws(batch_id, {
                "type": "progress",
                "url_index": idx,
                "total": len(batch.urls),
                "url": url,
                "number": number,
                "status": "rate_limited",
                "progress": 0,
                "message": f"Rate limited, retrying in {STAGGER_INTERVAL}s ({retries}/{MAX_RETRIES})",
                "completed_count": batch.completed_count,
                "active_count": batch.active_count,
            })
            await asyncio.sleep(STAGGER_INTERVAL)

        except Exception as e:
            batch.results[idx] = {
                "status": "error",
                "url": url,
                "number": number,
                "error": str(e),
            }
            await _send_ws(batch_id, {
                "type": "progress",
                "url_index": idx,
                "total": len(batch.urls),
                "url": url,
                "number": number,
                "status": "error",
                "progress": 0,
                "message": str(e),
                "completed_count": batch.completed_count,
                "active_count": batch.active_count,
            })
            return

    batch.results[idx] = {
        "status": "error",
        "url": url,
        "number": number,
        "error": "Rate limited after max retries",
    }
    await _send_ws(batch_id, {
        "type": "progress",
        "url_index": idx,
        "total": len(batch.urls),
        "url": url,
        "number": number,
        "status": "error",
        "progress": 0,
        "message": "Rate limited after max retries",
        "completed_count": batch.completed_count,
        "active_count": batch.active_count,
    })
