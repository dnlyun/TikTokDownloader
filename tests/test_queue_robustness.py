import asyncio
import tempfile
import unittest
from pathlib import Path

from app import DownloadQueue, QueueStore
from ssstik import BrowserUnavailableError


VIDEO = "https://www.tiktok.com/@example/video/1234567890123456789"


class QueueRecoveryTests(unittest.TestCase):
    def test_user_start_number_does_not_scan_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            store = QueueStore(Path(directory))
            try:
                (store.downloads_dir / "999.mp4").write_bytes(b"unrelated")
                response = store.add([VIDEO], start_number=42)
                self.assertEqual(response["numbers"], [42])
                self.assertFalse((Path(directory) / "counter.txt").exists())
            finally:
                store.close()

    def _processing_store(self, directory):
        store = QueueStore(Path(directory))
        response = store.add([VIDEO])
        row = store.db.execute("SELECT * FROM jobs").fetchone()
        with store.db:
            store.update(row["id"], status="processing", attempts=1)
        return store, response["batch_id"], row

    def test_invalid_file_is_not_recovered_as_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            store, batch_id, row = self._processing_store(directory)
            (store.downloads_dir / f"{row['number']}.mp4").write_bytes(b"broken")
            store.close()

            reopened = QueueStore(Path(directory))
            try:
                self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
                self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 0)
                self.assertFalse((reopened.downloads_dir / f"{row['number']}.mp4").exists())
            finally:
                reopened.close()

    def test_queued_work_does_not_start_automatically_after_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            store = QueueStore(Path(directory))
            batch_id = store.add([VIDEO])["batch_id"]
            store.close()

            reopened = QueueStore(Path(directory))
            try:
                self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
                self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 0)
                new_number = reopened.add([VIDEO], start_number=17)["numbers"][0]
                self.assertEqual(new_number, 17)
            finally:
                reopened.close()

    def test_valid_file_is_recovered_as_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            store, batch_id, row = self._processing_store(directory)
            target = store.downloads_dir / f"{row['number']}.mp4"
            target.write_bytes(b"\0\0\0\x18ftypisom" + bytes(2048))
            store.close()

            reopened = QueueStore(Path(directory))
            try:
                result = reopened.snapshot(batch_id)["results"][0]
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["filename"], target.name)
            finally:
                reopened.close()


class QueueConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_many_jobs_respect_configured_concurrency(self):
        with tempfile.TemporaryDirectory() as directory:
            store = QueueStore(Path(directory))
            urls = [f"https://www.tiktok.com/@example/video/{10**18 + i}" for i in range(60)]
            batch_id = store.add(urls)["batch_id"]

            class FakeDownloader:
                active = 0
                peak = 0
                def __init__(self, output): self.output = output
                async def start_browser(self): pass
                async def close_browser(self): pass
                async def download_url(self, url, number, callback):
                    type(self).active += 1
                    type(self).peak = max(type(self).peak, type(self).active)
                    try:
                        await asyncio.sleep(0.005)
                        (self.output / f"{number}.mp4").write_bytes(
                            b"\0\0\0\x18ftypisom" + bytes(2048)
                        )
                        return f"{number}.mp4"
                    finally:
                        type(self).active -= 1

            queue = DownloadQueue(
                store, FakeDownloader, max_concurrent=6, launch_interval=0
            )
            queue.start()
            try:
                for _ in range(400):
                    if store.snapshot(batch_id)["successful"] == len(urls):
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(store.snapshot(batch_id)["successful"], len(urls))
                self.assertGreater(FakeDownloader.peak, 1)
                self.assertLessEqual(FakeDownloader.peak, 6)
            finally:
                await queue.close()

    async def test_browser_disconnect_restarts_downloader_before_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = QueueStore(Path(directory))
            batch_id = store.add([VIDEO])["batch_id"]

            class Factory:
                created = 0
                closed = 0
                def __init__(self, output):
                    self.output = output
                    type(self).created += 1
                    self.generation = type(self).created
                async def start_browser(self): pass
                async def close_browser(self): type(self).closed += 1
                async def download_url(self, url, number, callback):
                    if self.generation == 1:
                        raise BrowserUnavailableError("simulated crash")
                    (self.output / f"{number}.mp4").write_bytes(
                        b"\0\0\0\x18ftypisom" + bytes(2048)
                    )
                    return f"{number}.mp4"

            queue = DownloadQueue(
                store, Factory, max_concurrent=1, launch_interval=0,
                retry_delay=0.001,
            )
            queue.start()
            try:
                for _ in range(300):
                    if store.snapshot(batch_id)["successful"] == 1:
                        break
                    await asyncio.sleep(0.01)
                result = store.snapshot(batch_id)["results"][0]
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["attempts"], 2)
                self.assertEqual(Factory.created, 2)
                self.assertGreaterEqual(Factory.closed, 1)
            finally:
                await queue.close()

if __name__ == "__main__":
    unittest.main()
