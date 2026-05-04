import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional

from playwright.async_api import BrowserContext, Page, async_playwright


class RateLimitError(Exception):
    pass


class SsstikDownloader:
    SSSTIK_URL = "https://ssstik.io"
    AD_TIMEOUT = 90
    RATE_LIMIT_WAIT = 12

    def __init__(self, output_dir: Path, counter_file: Path):
        self.output_dir = output_dir
        self.counter_file = counter_file
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = None
        self._browser = None
        self._context: Optional[BrowserContext] = None

    async def start_browser(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )


    async def close_browser(self):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def download_url(
        self,
        url: str,
        number: int,
        progress_callback: Optional[Callable[[int, str], Awaitable[None]]] = None,
    ) -> str:
        page = await self._context.new_page()
        try:
            return await self._do_download(page, url, number, progress_callback)
        finally:
            await page.close()

    async def _do_download(
        self, page: Page, url: str, number: int, cb
    ) -> str:
        await self._notify(cb, 5, "Navigating to ssstik.io...")
        await page.goto(self.SSSTIK_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        self._check_rate_limit_text(await page.content())

        await self._notify(cb, 10, "Entering URL...")
        await page.fill("#main_page_text", url)

        await self._notify(cb, 15, "Submitting...")
        await page.press("#main_page_text", "Enter")

        await self._notify(cb, 20, "Waiting for results...")
        await page.wait_for_selector(".download_link", timeout=30000)
        await page.wait_for_timeout(1000)

        self._check_rate_limit_text(await page.content())

        post_type = await self._detect_post_type(page)
        await self._notify(cb, 25, f"Detected {post_type}")

        if post_type == "slideshow":
            return await self._handle_slideshow(page, number, cb)
        return await self._handle_video_hd(page, number, cb)

    async def _detect_post_type(self, page: Page) -> str:
        slideshow_btn = await page.query_selector("a:has-text('slideshow')")
        if slideshow_btn and await slideshow_btn.is_visible():
            return "slideshow"
        return "video"

    async def _handle_video_hd(self, page: Page, number: int, cb) -> str:
        await self._notify(cb, 30, "Clicking 'Without watermark HD'...")
        hd_btn = await page.wait_for_selector("#hd_download", timeout=10000)
        if not hd_btn:
            raise ValueError("HD download button not found")

        page.on("popup", lambda popup: asyncio.create_task(popup.close()))

        await hd_btn.click()
        await self._notify(cb, 35, "Ad loading...")

        try:
            async with page.expect_download(timeout=self.AD_TIMEOUT * 1000) as dl_info:
                await self._wait_for_ad(page, cb)
                await page.wait_for_timeout(5000)
            download = await dl_info.value
        except Exception:
            await self._notify(cb, 70, "Looking for download link...")
            download = await self._try_fallback_download(page)

        await self._notify(cb, 90, "Saving file...")
        return await self._save_download(download, number)

    async def _try_fallback_download(self, page: Page):
        for selector in [
            "a.without_watermark_hd[href*='tikcdn']",
            "a.without_watermark[href*='tikcdn']",
            "a[href*='tikcdn']",
        ]:
            link = await page.query_selector(selector)
            if link:
                href = await link.get_attribute("href")
                if href and "tikcdn" in href:
                    async with page.expect_download(timeout=30000) as dl_info:
                        await link.click()
                    return await dl_info.value
        raise ValueError("No download link found after ad")

    async def _handle_slideshow(self, page: Page, number: int, cb) -> str:
        await self._notify(cb, 30, "Clicking slideshow download...")
        slideshow_btn = await page.query_selector("a:has-text('slideshow')")
        if not slideshow_btn:
            raise ValueError("Slideshow download button not found")

        try:
            async with page.expect_download(timeout=15000) as dl_info:
                await slideshow_btn.click()
            download = await dl_info.value
        except Exception:
            await self._notify(cb, 40, "Ad loading for slideshow...")
            try:
                async with page.expect_download(timeout=self.AD_TIMEOUT * 1000) as dl_info:
                    await self._wait_for_ad(page, cb)
                download = await dl_info.value
            except Exception:
                raise ValueError("Slideshow download failed")

        await self._notify(cb, 90, "Saving file...")
        return await self._save_download(download, number)

    async def _wait_for_ad(self, page: Page, cb):
        start = asyncio.get_event_loop().time()
        last_report = 0

        while asyncio.get_event_loop().time() - start < self.AD_TIMEOUT:
            for frame in page.frames:
                try:
                    close_btn = await frame.query_selector(
                        "button:has-text('Close'), [aria-label='Close'], "
                        "button:has-text('close'), .close-button, "
                        "#close-button, [class*='close']"
                    )
                    if close_btn:
                        is_visible = await close_btn.is_visible()
                        is_enabled = await close_btn.is_enabled()
                        if is_visible and is_enabled:
                            await page.wait_for_timeout(1500)
                            await close_btn.click()
                            await page.wait_for_timeout(500)
                            return
                except Exception:
                    continue

            elapsed = asyncio.get_event_loop().time() - start
            pct = min(int(elapsed / self.AD_TIMEOUT * 50), 50)
            if pct > last_report:
                await self._notify(cb, 35 + pct, f"Waiting for ad... ({int(elapsed)}s)")
                last_report = pct

            await page.wait_for_timeout(500)

    async def _save_download(self, download, number: int) -> str:
        suggested = download.suggested_filename
        ext = Path(suggested).suffix if suggested else ".mp4"
        if not ext:
            ext = ".mp4"

        filename = f"{number}{ext}"
        output_path = self.output_dir / filename

        await download.save_as(str(output_path))
        return filename

    @staticmethod
    def _check_rate_limit_text(html: str):
        lower = html.lower()
        if "too many requests" in lower or "rate limit" in lower:
            raise RateLimitError("ssstik.io: too many requests")

    @staticmethod
    async def _notify(cb, progress: int, message: str):
        if cb:
            await cb(progress, message)