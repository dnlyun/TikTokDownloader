"""Download scheduling, global pacing, retries, and progress publication."""

import asyncio
import logging
import random
import time

from fastapi import WebSocket

import ssstik
from config import QUEUE_CONFIG

LOG = logging.getLogger(__name__)


class DownloadQueue:
    def __init__(
        self, store, downloader_factory=None, *,
        max_concurrent=QUEUE_CONFIG.max_concurrent,
        launch_interval=QUEUE_CONFIG.launch_interval,
        retry_delay=QUEUE_CONFIG.retry_delay,
        rate_limit_delay=QUEUE_CONFIG.rate_limit_delay,
        max_attempts=QUEUE_CONFIG.max_attempts,
    ):
        self.store = store
        self.downloader_factory = downloader_factory or ssstik.SsstikDownloader
        self.max_concurrent = max(1, max_concurrent)
        self.launch_interval = max(0, launch_interval)
        self.retry_delay = retry_delay
        self.rate_limit_delay = rate_limit_delay
        self.max_attempts = max_attempts
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
            self.store.db.execute("""UPDATE jobs SET status='error', progress=0,
                next_attempt_at=0, message='Paused when the app closed; click Retry failed to resume'
                WHERE status IN ('processing','queued','retrying')""")
        self.store.close()

    def ready(self):
        return self.store.db.execute("""SELECT * FROM jobs j
            WHERE status IN ('queued','retrying') AND next_attempt_at <= ?
            AND NOT EXISTS (SELECT 1 FROM jobs other
                WHERE other.identity=j.identity AND other.status='processing')
            ORDER BY next_attempt_at, id LIMIT 1""", (time.time(),)).fetchone()

    async def run(self):
        try:
            while not self.stopping:
                self.wake.clear()
                self.active = {
                    key: task for key, task in self.active.items() if not task.done()
                }
                await self._recover_browser_if_needed()
                now = time.time()
                can_launch = (
                    not self.browser_broken
                    and len(self.active) < self.max_concurrent
                    and now >= max(
                        self.next_launch_at,
                        self.store.get_setting("cooldown_until"),
                    )
                )
                job = self.ready() if can_launch else None
                if job:
                    await self._launch(job)
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
                jobs = self.store.db.execute(
                    "SELECT * FROM jobs WHERE status IN ('queued','retrying')"
                ).fetchall()
                for job in jobs:
                    self.store.update(
                        job["id"], status="error",
                        message="Queue error; restart the app to recover",
                    )
                    self.publish(job["batch_id"])

    async def _recover_browser_if_needed(self):
        if not self.browser_broken or self.active:
            return
        try:
            if self.downloader:
                await self.downloader.close_browser()
        except Exception:
            LOG.exception("Could not close failed browser")
        self.downloader = None
        self.browser_broken = False

    async def _launch(self, job):
        archived = self.store.archived(job["identity"], job["id"])
        if archived:
            with self.store.db:
                self.store.update(
                    job["id"], status="completed", progress=100,
                    number=archived["number"], filename=archived["filename"],
                    message="Already downloaded",
                )
            self.publish(job["batch_id"])
            return
        with self.store.db:
            self.store.update(
                job["id"], status="processing", progress=0,
                attempts=job["attempts"] + 1, message="Starting...",
            )
        self.publish(job["batch_id"])
        if self.downloader is None and not await self._start_downloader(job):
            return
        task = asyncio.create_task(self.download(job))
        task.add_done_callback(lambda _: self.wake.set())
        self.active[job["id"]] = task
        self.next_launch_at = time.time() + self.launch_interval

    async def _start_downloader(self, job) -> bool:
        candidate = self.downloader_factory(self.store.downloads_dir)
        try:
            await candidate.start_browser()
            self.downloader = candidate
            return True
        except asyncio.CancelledError:
            await candidate.close_browser()
            raise
        except Exception as error:
            try:
                await candidate.close_browser()
            except Exception:
                pass
            self.fail(job, error, browser_error=True)
            return False

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
            self.store.set_setting(
                "cooldown_until",
                max(self.store.get_setting("cooldown_until"), time.time() + delay),
            )
        retry = not is_permanent and attempts < self.max_attempts
        retry_at = time.time() + delay + random.uniform(0, max(0, delay * 0.15))
        with self.store.db:
            self.store.update(
                job["id"], status="retrying" if retry else "error", progress=0,
                next_attempt_at=retry_at if retry else 0,
                message=(
                    f"{message}; retry {attempts + 1}/{self.max_attempts} scheduled"
                    if retry else message
                ),
            )
        self.publish(job["batch_id"])
        if is_rate_limit:
            for batch_id in list(self.observers):
                if batch_id != job["batch_id"]:
                    self.publish(batch_id)

    async def download(self, job):
        async def progress_callback(progress: int, message: str):
            with self.store.db:
                self.store.update(
                    job["id"], progress=max(0, min(int(progress), 99)),
                    message=message[:300],
                )
            self.publish(job["batch_id"])

        try:
            filename = await self.downloader.download_url(
                job["url"], job["number"], progress_callback
            )
            saved_path = self.store.downloads_dir / filename
            if not ssstik.is_valid_mp4(saved_path):
                raise RuntimeError(
                    f"Download finished but no valid file was saved at {saved_path.resolve()}"
                )
            with self.store.db:
                self.store.update(
                    job["id"], status="completed", progress=100, filename=filename,
                    message=f"Saved to {saved_path.resolve()}", next_attempt_at=0,
                )
            LOG.warning("Saved TikTok download to %s", saved_path.resolve())
            self.publish(job["batch_id"])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fail(job, error)

