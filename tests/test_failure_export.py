import asyncio
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

from app import create_app
from ssstik import PermanentDownloadError


class FailingDownloader:
    def __init__(self, path): pass
    async def start_browser(self): pass
    async def close_browser(self): pass
    async def download_url(self, url, number, callback):
        raise PermanentDownloadError("unavailable")


class FailureExportTests(unittest.TestCase):
    def test_failed_urls_export_as_plain_text(self):
        url = "https://www.tiktok.com/@example/video/1234567890123456789"
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(directory, FailingDownloader, launch_interval=0)) as client:
                batch_id = client.post("/api/batch", json={"urls": [url], "start_number": 1}).json()["batch_id"]
                for _ in range(100):
                    snapshot = client.get(f"/api/batch/{batch_id}").json()
                    if snapshot["failed"] == 1:
                        break
                    time.sleep(0.01)
                response = client.get(f"/api/batch/{batch_id}/failed.txt")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, url + "\n")
                self.assertIn("attachment", response.headers["content-disposition"])

    def test_open_downloads_uses_platform_folder_opener(self):
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(directory, FailingDownloader)) as client:
                with patch("app.subprocess.Popen") as opener:
                    response = client.post("/api/open-downloads")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["path"], str((Path(directory) / "downloads").resolve()))
                opener.assert_called_once()


if __name__ == "__main__":
    unittest.main()
