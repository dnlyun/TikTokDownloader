import sys
import threading
import time
import webbrowser

import uvicorn

from app import app

PORT = 8000


def open_browser():
    time.sleep(1.5)
    webbrowser.open(f"http://localhost:{PORT}")


def main():
    threading.Thread(target=open_browser, daemon=True).start()
    print(f"Starting TikTok Downloader on http://localhost:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()