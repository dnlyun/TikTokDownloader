import os
import threading
import time
import webbrowser
from pathlib import Path

os.chdir(Path(__file__).parent)

import uvicorn

from app import app

PORT = 8000


def open_browser():
    time.sleep(1.5)
    webbrowser.open(f"http://localhost:{PORT}")


def main():
    threading.Thread(target=open_browser, daemon=True).start()
    print(f"Starting TikTok Downloader on http://localhost:{PORT}")
    print(f"Downloads will be saved to: {(Path(__file__).resolve().parent / 'downloads')}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
