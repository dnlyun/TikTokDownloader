import asyncio
import os
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Awaitable, Callable, Optional

from playwright.async_api import BrowserContext, Page, async_playwright


class RateLimitError(Exception):
    pass


class PermanentDownloadError(Exception):
    pass


class BrowserUnavailableError(Exception):
    pass


class SsstikDownloader:
    SSSTIK_URL = "https://ssstik.io"
    AD_TIMEOUT = 180

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = None
        self._browser = None
        self._context: Optional[BrowserContext] = None

    @staticmethod
    def browser_options():
        headless = os.environ.get("SSSTIK_HEADLESS") == "1"
        channel = os.environ.get("SSSTIK_BROWSER_CHANNEL", "chromium" if headless else "").strip()
        if channel not in ("", "chromium", "chrome", "msedge"):
            raise ValueError("SSSTIK_BROWSER_CHANNEL must be chromium, chrome, or msedge")
        options = {
            "headless": headless,
            "args": ["--disable-blink-features=AutomationControlled", "--mute-audio"],
        }
        if not headless:
            options["args"].append("--start-minimized")
        if channel:
            options["channel"] = channel
        return options

    async def start_browser(self):
        options = self.browser_options()
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(**options)
            self._context = await self._browser.new_context(
                accept_downloads=True,
                viewport={"width": 1280, "height": 800},
            )
        except BaseException:
            await self.close_browser()
            raise

    async def close_browser(self):
        context, browser, playwright = self._context, self._browser, self._playwright
        self._context = self._browser = self._playwright = None
        # A crashed context must not prevent the browser/driver from being closed.
        for resource, method in ((context, "close"), (browser, "close"), (playwright, "stop")):
            if resource:
                with suppress(Exception):
                    await asyncio.wait_for(getattr(resource, method)(), timeout=10)

    async def download_url(
        self,
        url: str,
        number: int,
        progress_callback: Optional[Callable[[int, str], Awaitable[None]]] = None,
    ) -> str:
        if not self._browser or not self._browser.is_connected():
            raise BrowserUnavailableError("Download browser disconnected")
        page = await self._context.new_page()
        try:
            async with asyncio.timeout(360):
                return await self._do_download(page, url, number, progress_callback)
        finally:
            with suppress(Exception):
                await page.close()

    async def _do_download(self, page: Page, url: str, number: int, cb) -> str:
        await self._notify(cb, 5, "Navigating to ssstik.io...")
        await page.goto(self.SSSTIK_URL, wait_until="domcontentloaded")
        await self._notify(cb, 10, "Entering URL...")
        await page.fill("#main_page_text", url)
        await self._notify(cb, 15, "Submitting...")
        await page.press("#main_page_text", "Enter")

        await self._notify(cb, 20, "Waiting for results...")
        deadline = asyncio.get_running_loop().time() + 60
        while asyncio.get_running_loop().time() < deadline:
            await self._check_page_error(page)
            slides = page.locator("#slides_generate_new:visible, #slides_generate:visible").first
            hd = page.locator("#hd_download:visible").first
            if await slides.is_visible():
                return await self._click_and_download(page, slides, number, cb, "Converting slideshow...")
            if await hd.is_visible():
                return await self._click_and_download(page, hd, number, cb, "Downloading HD video...")
            await asyncio.sleep(0.25)
        raise TimeoutError("SSSTik did not offer an HD video or slideshow conversion within 60 seconds")

    async def _check_page_error(self, page):
        # Read visible errors, not scripts/FAQ text containing the words 'rate limit'.
        for selector in ("#errorContainer:visible", "#splash.error #bigmessage:visible"):
            element = page.locator(selector).first
            if await element.is_visible():
                message = (await element.inner_text()).strip()
                if not message:
                    continue
                self._check_rate_limit(message)
                if "invalid link" in message.lower() or "wrong link" in message.lower():
                    raise PermanentDownloadError(message[:250])
                raise RuntimeError("SSSTik: " + message[:250])
        if await page.locator("#splash.hd_error").count():
            raise RuntimeError("SSSTik could not generate HD; no lower-quality fallback was used")

    async def _click_and_download(self, page, button, number, cb, message):
        loop = asyncio.get_running_loop()
        received = loop.create_future()
        def on_download(download):
            if not received.done():
                received.set_result(download)
        page.on("download", on_download)
        try:
            await self._notify(cb, 30, message)
            await button.click(timeout=15000)
            deadline = loop.time() + self.AD_TIMEOUT
            next_close = 0
            last_report = 0
            while not received.done():
                if page.is_closed():
                    raise BrowserUnavailableError("Download tab closed")
                await self._check_page_error(page)
                if loop.time() >= deadline:
                    raise TimeoutError("SSSTik conversion/ad did not produce a download within 180 seconds")
                # Include the main frame and nested frames; tolerate detached ad frames.
                for frame in page.frames:
                    try:
                        resume = frame.locator("#resume-ad-button").first
                        if await resume.is_visible():
                            await resume.click(timeout=750)
                            next_close = loop.time() + 15
                            break
                        if loop.time() >= next_close:
                            close = frame.locator("#dismiss-button-element:visible, #dismiss-button:visible").first
                            if await close.is_visible():
                                await close.click(timeout=750)
                                next_close = loop.time() + 2
                                break
                    except Exception:
                        continue
                if loop.time() - last_report > 5:
                    await self._notify(cb, 50, "Waiting for SSSTik conversion or ad to finish...")
                    last_report = loop.time()
                await asyncio.wait({received}, timeout=0.3)
            await self._notify(cb, 90, "Saving file...")
            return await self._save_download(received.result(), number)
        finally:
            page.remove_listener("download", on_download)
            if not received.done():
                received.cancel()

    async def _save_download(self, download, number: int) -> str:
        filename = f"{number}.mp4"
        target = self.output_dir / filename
        temporary = self.output_dir / f".{number}.{uuid.uuid4().hex}.part"
        try:
            await download.save_as(str(temporary))
            failure = await download.failure()
            if failure:
                raise RuntimeError("Download failed: " + failure)
            with temporary.open("rb") as file:
                header = file.read(32)
            if len(header) < 12 or header[4:8] != b"ftyp":
                raise RuntimeError("SSSTik returned an invalid MP4 file")
            temporary.replace(target)
            return filename
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _check_rate_limit(html: str):
        lower = html.lower()
        if "too many requests" in lower or "rate limit" in lower or "requests too fast" in lower:
            raise RateLimitError("ssstik.io: too many requests")

    @staticmethod
    async def _notify(cb, progress: int, message: str):
        if cb:
            await cb(progress, message)
