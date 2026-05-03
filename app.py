import asyncio
import sys
import uuid
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from downloader import TikTokDownloader

app = FastAPI()

if getattr(sys, "frozen", False):
    _BUNDLE_DIR = Path(sys._MEIPASS)
    BASE_DIR = Path(sys.executable).parent
else:
    _BUNDLE_DIR = Path(__file__).parent
    BASE_DIR = _BUNDLE_DIR

DOWNLOADS_DIR = BASE_DIR / "downloads"
TEMP_DIR = BASE_DIR / "temp"

tasks: dict[str, str] = {}

connections: dict[str, WebSocket] = {}

_counter = 0
_counter_lock = Lock()

def _next_number() -> int:
    global _counter
    with _counter_lock:
        _counter += 1
        return _counter

class DownloadRequest(BaseModel):
    url: str

app.mount("/static", StaticFiles(directory=str(_BUNDLE_DIR / "static")), name="static")

@app.on_event("startup")
async def startup():
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    TEMP_DIR.mkdir(exist_ok=True)

@app.get("/")
async def root():
    return FileResponse(str(_BUNDLE_DIR / "static" / "index.html"))

@app.post("/api/download")
async def start_download(request: DownloadRequest):
    task_id = str(uuid.uuid4())
    number = _next_number()
    tasks[task_id] = {
        "status": "pending",
        "progress": 0,
        "message": "Queued",
        "file_path": None,
        "error": None,
        "number": number,
    }
    asyncio.create_task(_run_download(task_id, request.url, number))
    return JSONResponse({"task_id": task_id, "number": number})

@app.websocket("/ws/{task_id}")
async def websocket_progress(websocket: WebSocket, task_id: str):
    await websocket.accept()
    connections[task_id] = websocket
    if task_id in tasks:
        t = tasks[task_id]
        try:
            await websocket.send_json({"progress": t["progress"], "message": t["message"], "status": t["status"]})
        except Exception:
            pass
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connections.pop(task_id, None)

@app.get("/api/status/{task_id}")
async def get_status(task_id: str):
    if task_id not in tasks:
        return JSONResponse({"error": "Task not found"}, status_code=404)
    return JSONResponse(tasks[task_id])

@app.get("/api/download/{task_id}")
async def download_file(task_id: str):
    task = tasks.get(task_id)
    if not task or not task["file_path"]:
        return JSONResponse({"error": "File not ready"}, status_code=404)
    return FileResponse(
        task["file_path"],
        filename=Path(task["file_path"]).name,
        media_type="video/mp4",
    )

async def _send_progress(task_id: str, progress: int, message: str, status: str = "processing"):
    tasks[task_id].update({"progress": progress, "message": message, "status": status})
    ws = connections.get(task_id)
    if ws:
        try:
            await ws.send_json({"progress": progress, "message": message, "status": status})
        except Exception:
            connections.pop(task_id, None)

async def _run_download(task_id: str, url: str, number: int):
    try:
        downloader = TikTokDownloader(TEMP_DIR, DOWNLOADS_DIR)
        result_path = await downloader.download(
            url,
            output_filename=str(number),
            progress_callback=lambda p, m: _send_progress(task_id, p, m),
        )
        tasks[task_id].update({
            "status": "completed",
            "progress": 100,
            "message": "Done!",
            "file_path": str(result_path),
        })
        await _send_progress(task_id, 100, "Done!", "completed")
    except Exception as e:
        tasks[task_id].update({"status": "error", "error": str(e), "message": str(e)})
        await _send_progress(task_id, 0, str(e), "error")