import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional

from playwright.async_api import BrowserContext, Frame, Page, async_playwright


class RateLimitError(Exception):
    pass


class SsstikDownloader:
    SSSTIK_URL = "https://ssstik.io"
    AD_TIMEOUT = 90

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
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

    async def _do_download(self, page: Page, url: str, number: int, cb) -> str:
        await self._notify(cb, 5, "Navigating to ssstik.io...")
        await page.goto(self.SSSTIK_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        self._check_rate_limit(await page.content())

        await self._notify(cb, 10, "Entering URL...")
        await page.fill("#main_page_text", url)
        await self._notify(cb, 15, "Submitting...")
        await page.press("#main_page_text", "Enter")

        await self._notify(cb, 20, "Waiting for results...")
        await page.wait_for_selector(".download_link", timeout=30000)
        await page.wait_for_timeout(1000)
        self._check_rate_limit(await page.content())

        is_slideshow = await page.query_selector("a#slides_generate")
        await self._notify(cb, 25, f"Detected {'is_slideshow' if is_slideshow else 'video'}")

        if is_slideshow:
            return await self._handle_slideshow(page, number, cb)
        return await self._handle_video_hd(page, number, cb)

    async def _handle_video_hd(self, page: Page, number: int, cb) -> str:
        await self._notify(cb, 30, "Clicking 'HD download'...")
        hd_btn = await page.wait_for_selector("#hd_download", timeout=10000)
        await hd_btn.click()
        await self._notify(cb, 35, "Ad loading...")

        async with page.expect_download(timeout=self.AD_TIMEOUT * 1000) as dl_info:
            await self._wait_for_ad(page, cb)
            await page.wait_for_timeout(5000)

        await self._notify(cb, 90, "Saving file...")
        return await self._save_download(await dl_info.value, number)

    async def _handle_slideshow(self, page: Page, number: int, cb) -> str:
        await self._notify(cb, 30, "Clicking slideshow download...")
        btn = await page.query_selector("a#slides_generate")

        try:
            async with page.expect_download(timeout=30000) as dl_info:
                await btn.click()
            download = await dl_info.value
        except Exception:
            await self._notify(cb, 40, "Ad loading for slideshow...")
            async with page.expect_download(timeout=self.AD_TIMEOUT * 1000) as dl_info:
                await self._wait_for_ad(page, cb)
                await page.wait_for_timeout(5000)
            download = await dl_info.value

        await self._notify(cb, 90, "Saving file...")
        return await self._save_download(download, number)

    async def _wait_for_ad(self, page: Page, cb):
        start = asyncio.get_event_loop().time()
        last_pct = 0

        while asyncio.get_event_loop().time() - start < self.AD_TIMEOUT:
            if await self._find_and_click(page, "#dismiss-button-element"):
                await page.wait_for_timeout(500)
                if await self._find_and_click(page, "#resume-ad-button"):
                    await self._notify(cb, 55, "Resuming ad...")
                    await page.wait_for_timeout(15000)
                    continue
                await self._notify(cb, 70, "Ad closed!")
                return

            elapsed = asyncio.get_event_loop().time() - start
            pct = min(int(elapsed / self.AD_TIMEOUT * 50), 50)
            if pct > last_pct:
                await self._notify(cb, 35 + pct, f"Waiting for ad... ({int(elapsed)}s)")
                last_pct = pct
            await page.wait_for_timeout(1000)

        raise TimeoutError("Ad did not complete within timeout")

    async def _find_and_click(self, page: Page, selector: str) -> bool:
        for frame in await self._collect_frames(page):
            try:
                el = await frame.query_selector(selector)
                if el and await el.is_visible():
                    await el.click()
                    return True
            except Exception:
                continue
        return False

    async def _collect_frames(self, root) -> list[Frame]:
        frames = []

        async def _walk(parent):
            for iframe_el in await parent.query_selector_all("iframes"):
                try:
                    frame = await iframe_el.content_frame()
                    if frame:
                        frames.append(frame)
                        await _walk(frame)
                except Exception:
                    continue

        await _walk(root)
        return frames

    async def _save_download(self, download, number: int) -> str:
        filename = f"{number}.mp4"
        await download.save_as(str(self.output_dir / filename))
        return filename

    @staticmethod
    def _check_rate_limit(html: str):
        lower = html.lower()
        if "too many requests" in lower or "rate limit" in lower:
            raise RateLimitError("ssstik.io: too many requests")

    @staticmethod
    async def _notify(cb, progress: int, message: str):
        if cb:
            await cb(progress, message)