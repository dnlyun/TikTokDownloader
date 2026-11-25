import asyncio
import json
import os
import platform
import re
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    async_playwright,
)


class RateLimitError(Exception):
    pass


class PermanentDownloadError(Exception):
    pass


class BrowserUnavailableError(Exception):
    pass


class DownloadStage(str, Enum):
    STARTING = "starting"
    NAVIGATING = "navigating"
    SUBMITTING = "submitting"
    WAITING_FOR_RESULT = "waiting_for_result"
    STARTING_DOWNLOAD = "starting_download"
    WAITING_FOR_DOWNLOAD = "waiting_for_ad_or_download"
    SAVING = "saving"
    VALIDATING = "validating"


@dataclass
class RunState:
    stage: DownloadStage = DownloadStage.STARTING


def is_valid_mp4(path: Path, *, minimum_bytes: int = 1024) -> bool:
    """Perform a cheap, dependency-free validation suitable for recovery paths."""
    try:
        if not path.is_file() or path.stat().st_size < minimum_bytes:
            return False
        with path.open("rb") as file:
            header = file.read(32)
        return len(header) >= 12 and header[4:8] == b"ftyp"
    except OSError:
        return False


class SsstikDownloader:
    SSSTIK_URL = "https://ssstik.io"
    DEFAULT_NAVIGATION_TIMEOUT = 45
    DEFAULT_RESULT_TIMEOUT = 90
    DEFAULT_FLOW_TIMEOUT = 600
    DEFAULT_SAVE_TIMEOUT = 180

    # SSSTik currently serves several ad providers.  Prefer semantic selectors so
    # a provider changing an element id does not wedge the queue.
    AD_CONTROL_SELECTORS = (
        ".videoAdUiSkipButton:visible",
        ".ytp-ad-skip-button:visible",
        "button[aria-label*='skip' i]:visible",
        "button[aria-label*='close ad' i]:visible",
        "button[title*='skip' i]:visible",
        "button[title*='close ad' i]:visible",
    )
    AD_CONTROL_NAME = re.compile(
        r"^(skip( ad| ads| video)?|close ad|dismiss ad)$",
        re.IGNORECASE,
    )

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = None
        self._browser = None
        self._context: Optional[BrowserContext] = None
        self.flow_timeout = self._positive_int_env(
            "SSSTIK_FLOW_TIMEOUT", self.DEFAULT_FLOW_TIMEOUT
        )
        self.navigation_timeout = self._positive_int_env(
            "SSSTIK_NAVIGATION_TIMEOUT", self.DEFAULT_NAVIGATION_TIMEOUT
        )
        self.result_timeout = self._positive_int_env(
            "SSSTIK_RESULT_TIMEOUT", self.DEFAULT_RESULT_TIMEOUT
        )
        self.save_timeout = self._positive_int_env(
            "SSSTIK_SAVE_TIMEOUT", self.DEFAULT_SAVE_TIMEOUT
        )

    @staticmethod
    def _positive_int_env(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be a positive integer") from exc
        if value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def browser_options():
        headless = os.environ.get("SSSTIK_HEADLESS") == "1"
        default_channel = (
            "chrome"
            if platform.system() == "Windows"
            else ("chromium" if headless else "")
        )
        channel = os.environ.get("SSSTIK_BROWSER_CHANNEL", default_channel).strip()
        if channel not in ("", "chromium", "chrome", "msedge"):
            raise ValueError("SSSTIK_BROWSER_CHANNEL must be chromium, chrome, or msedge")
        options = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--mute-audio",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
        }
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
        try:
            page = await self._context.new_page()
        except PlaywrightError as error:
            raise BrowserUnavailableError("Could not open a new Chrome tab") from error
        state = RunState()
        try:
            async with asyncio.timeout(
                self.navigation_timeout + self.result_timeout + self.flow_timeout + self.save_timeout
            ):
                return await self._do_download(page, url, number, progress_callback, state)
        except asyncio.CancelledError:
            raise
        except PlaywrightError as error:
            await self._write_diagnostics(page, number, url, state.stage, error)
            if self._browser_is_unavailable(error):
                raise BrowserUnavailableError(
                    f"Chrome disconnected during {state.stage.value}"
                ) from error
            raise
        except Exception as error:
            await self._write_diagnostics(page, number, url, state.stage, error)
            raise
        finally:
            with suppress(Exception):
                await page.close()

    async def _do_download(
        self, page: Page, url: str, number: int, cb, state: Optional[RunState] = None
    ) -> str:
        state = state or RunState()
        response_statuses = []

        def on_response(response):
            try:
                host = (urlsplit(response.url).hostname or "").lower()
                resource_type = response.request.resource_type
                if ((host == "ssstik.io" or host.endswith(".ssstik.io")) and
                        resource_type in {"document", "xhr", "fetch"}):
                    if response.status in (403, 429) or response.status >= 500:
                        response_statuses.append((response.status, response.url))
            except Exception:
                pass

        page.on("response", on_response)
        state.stage = DownloadStage.NAVIGATING
        await self._notify(cb, 5, "Navigating to ssstik.io...")
        try:
            async with asyncio.timeout(self.navigation_timeout):
                response = await page.goto(self.SSSTIK_URL, wait_until="domcontentloaded")
                if response and response.status == 429:
                    raise RateLimitError("ssstik.io returned HTTP 429")
                if response and response.status >= 500:
                    raise RuntimeError(f"SSSTik returned HTTP {response.status}")
                await page.locator("#main_page_text").wait_for(state="visible")
            state.stage = DownloadStage.SUBMITTING
            await self._notify(cb, 10, "Entering URL...")
            await page.fill("#main_page_text", url)
            await self._notify(cb, 15, "Submitting...")
            submit = page.locator("#submit:visible").first
            if await submit.is_visible():
                await submit.click(timeout=15000)
            else:
                await page.press("#main_page_text", "Enter")

            state.stage = DownloadStage.WAITING_FOR_RESULT
            await self._notify(cb, 20, "Waiting for results...")
            deadline = asyncio.get_running_loop().time() + self.result_timeout
            while asyncio.get_running_loop().time() < deadline:
                self._check_response_errors(response_statuses)
                await self._check_page_error(page)
                slides = page.locator(
                    "#slides_generate_new:visible, #slides_generate:visible"
                ).first
                hd = page.locator("#hd_download:visible").first
                if await slides.is_visible():
                    return await self._click_and_download(
                        page, slides, number, cb, "Converting slideshow...", state,
                        response_statuses,
                    )
                if await hd.is_visible():
                    return await self._click_and_download(
                        page, hd, number, cb, "Downloading HD video...", state,
                        response_statuses,
                    )
                await asyncio.sleep(0.25)
            raise TimeoutError(
                f"SSSTik did not offer an HD video or slideshow conversion within "
                f"{self.result_timeout} seconds"
            )
        finally:
            with suppress(Exception):
                page.remove_listener("response", on_response)

    async def _check_page_error(self, page):
        # Read visible errors, not scripts/FAQ text containing the words 'rate limit'.
        for selector in ("#errorContainer:visible", "#splash.error:visible"):
            element = page.locator(selector).first
            if await element.is_visible():
                message = (await element.inner_text()).strip()
                if not message:
                    continue
                self._check_rate_limit(message)
                lower = message.lower()
                if any(
                    phrase in lower
                    for phrase in (
                        "invalid link",
                        "wrong link",
                        "currently unavailable",
                        "private or not reachable",
                        "removed by user",
                    )
                ):
                    raise PermanentDownloadError(message[:250])
                raise RuntimeError("SSSTik: " + message[:250])
        if await page.locator("#splash.hd_error:visible").count():
            raise RuntimeError("SSSTik could not generate HD; no lower-quality fallback was used")
        challenge = page.locator("#challenge-running:visible, .cf-challenge:visible").first
        if await challenge.is_visible():
            raise RuntimeError("SSSTik presented an anti-bot challenge")

    async def _click_and_download(
        self, page, button, number, cb, message, state=None, response_statuses=None
    ):
        state = state or RunState()
        response_statuses = response_statuses if response_statuses is not None else []
        loop = asyncio.get_running_loop()
        received = loop.create_future()
        watched_pages = set()

        def on_download(download):
            if not received.done():
                received.set_result(download)

        def watch_page(candidate):
            if candidate not in watched_pages:
                watched_pages.add(candidate)
                candidate.on("download", on_download)

        def on_popup(popup):
            watch_page(popup)

        watch_page(page)
        page.on("popup", on_popup)
        try:
            state.stage = DownloadStage.STARTING_DOWNLOAD
            await self._notify(cb, 30, message)
            await button.click(timeout=15000)
            state.stage = DownloadStage.WAITING_FOR_DOWNLOAD
            deadline = loop.time() + self.flow_timeout
            last_report = 0
            while not received.done():
                if page.is_closed():
                    raise BrowserUnavailableError("Download tab closed")
                self._check_response_errors(response_statuses)
                await self._check_page_error(page)
                if loop.time() >= deadline:
                    raise TimeoutError(
                        f"SSSTik conversion/ad did not produce a download within "
                        f"{self.flow_timeout} seconds"
                    )
                clicked = await self._advance_ad_flow(watched_pages)
                if loop.time() - last_report > 5:
                    status = (
                        "Ad control handled; waiting for download..."
                        if clicked
                        else "Waiting for SSSTik conversion or ad to finish..."
                    )
                    await self._notify(cb, 50, status)
                    last_report = loop.time()
                await asyncio.wait({received}, timeout=0.3)
            state.stage = DownloadStage.SAVING
            await self._notify(cb, 90, "Saving file...")
            async with asyncio.timeout(self.save_timeout):
                filename = await self._save_download(received.result(), number, state)
            return filename
        finally:
            with suppress(Exception):
                page.remove_listener("popup", on_popup)
            for watched in watched_pages:
                with suppress(Exception):
                    watched.remove_listener("download", on_download)
                if watched is not page:
                    with suppress(Exception):
                        await watched.close()
            if not received.done():
                received.cancel()

    async def _advance_ad_flow(self, pages) -> bool:
        """Click an actionable ad control, if one is currently available.

        Controls are polled instead of scheduled: a 5-second ad, a 90-second ad,
        and a no-ad download all follow the same path. Detached cross-origin ad
        frames and popups are expected and intentionally ignored.
        """
        for candidate in tuple(pages):
            try:
                if candidate.is_closed():
                    continue
                frames = candidate.frames
            except Exception:
                continue
            for frame in frames:
                if await self._resume_close_confirmation(frame):
                    return True
                countdown_active = await self._reward_countdown_active(frame)
                # Rewarded Google ads expose this control as soon as the timer
                # reaches its eligible state.  Handle it before walking the
                # provider-neutral selectors across every frame; otherwise a
                # collection of slow/detached ad frames can delay the click by
                # several seconds.
                if not countdown_active and await self._click_reward_dismiss(frame):
                    return True
                for selector in self.AD_CONTROL_SELECTORS:
                    if countdown_active and any(
                        word in selector.lower() for word in ("dismiss", "close")
                    ):
                        continue
                    try:
                        for control in await frame.locator(selector).all():
                            if await control.is_visible():
                                await control.click(timeout=1000)
                                return True
                    except Exception:
                        continue
                try:
                    for semantic in await frame.get_by_role(
                        "button", name=self.AD_CONTROL_NAME
                    ).all():
                        if not countdown_active and await semantic.is_visible():
                            await semantic.click(timeout=1000)
                            return True
                except Exception:
                    continue
        return False

    @staticmethod
    async def _click_reward_dismiss(frame) -> bool:
        """Immediately close a completed rewarded ad, never an active timer."""
        # Do not click the #dismiss-button wrapper: it can remain visible while
        # the timer is running and is what triggers the forfeit confirmation.
        for selector in ("#dismiss-button-element",):
            try:
                control = frame.locator(selector).first
                if not await control.is_visible():
                    continue
                # Some providers leave the wrapper visible while disabling the
                # actual button.  Respect that state when Playwright exposes it.
                if not await control.is_enabled():
                    continue
                await control.click(timeout=500)
                return True
            except Exception:
                continue
        return False

    @staticmethod
    async def _reward_countdown_active(frame) -> bool:
        """Return true while a rewarded ad says closing would forfeit the reward."""
        try:
            locator = frame.locator("#dismiss-button")
            # Most frames are not the rewarded-ad frame.  Locator.inner_text()
            # auto-waits for a missing element by default, which multiplied the
            # close delay across every unrelated frame.
            if await locator.count() == 0:
                return False
            text = (await locator.first.inner_text(timeout=250)).strip()
            match = re.search(r"(?:reward|close|skip)\s+in\s+(\d+)", text, re.I)
            # Google rewarded ads commonly leave the label at "1 seconds" even
            # after the eligible Close control is enabled in a background tab.
            return bool(match and int(match.group(1)) > 1)
        except Exception:
            return False

    @staticmethod
    async def _resume_close_confirmation(frame) -> bool:
        """Recover from an early-close confirmation without forfeiting the file."""
        try:
            dialogs = frame.locator("#close-confirmation-dialog")
            if await dialogs.count() == 0:
                return False
            dialog = dialogs.first
            active = await dialog.evaluate("""element => {
                const style = getComputedStyle(element);
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                    Number(style.opacity || 1) > 0.1 && style.pointerEvents !== 'none';
            }""", timeout=250)
            if not active:
                return False
            resumes = frame.locator("#resume-ad-button")
            if await resumes.count() == 0:
                return False
            resume = resumes.first
            if await resume.is_visible():
                await resume.click(timeout=1000)
                return True
        except Exception:
            pass
        return False

    async def _save_download(self, download, number: int, state=None) -> str:
        filename = f"{number}.mp4"
        target = self.output_dir / filename
        temporary = self.output_dir / f".{number}.{uuid.uuid4().hex}.part"
        try:
            await download.save_as(str(temporary))
            failure = await download.failure()
            if failure:
                raise RuntimeError("Download failed: " + failure)
            if state:
                state.stage = DownloadStage.VALIDATING
            if not is_valid_mp4(temporary):
                raise RuntimeError("SSSTik returned an invalid MP4 file")
            temporary.replace(target)
            return filename
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _check_response_errors(response_statuses):
        if not response_statuses:
            return
        status, _ = response_statuses.pop(0)
        if status == 429:
            raise RateLimitError("ssstik.io returned HTTP 429")
        if status == 403:
            raise RuntimeError("SSSTik blocked the request with HTTP 403")
        if status >= 500:
            raise RuntimeError(f"SSSTik returned HTTP {status}")

    def _browser_is_unavailable(self, error) -> bool:
        if not self._browser or not self._browser.is_connected():
            return True
        message = str(error).lower()
        return any(
            phrase in message
            for phrase in (
                "target page, context or browser has been closed",
                "browser has been closed",
                "context has been closed",
                "connection closed",
            )
        )

    async def _write_diagnostics(self, page, number, source_url, stage, error):
        diagnostics = self.output_dir / "diagnostics"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        stem = f"{number}-{stamp}-{uuid.uuid4().hex[:8]}"
        try:
            diagnostics.mkdir(parents=True, exist_ok=True)
            frame_hosts = []
            controls = []
            current_url = ""
            if page and not page.is_closed():
                current_url = page.url
                for frame in page.frames:
                    host = (urlsplit(frame.url).hostname or "").lower()
                    if host and host not in frame_hosts:
                        frame_hosts.append(host)
                    with suppress(Exception):
                        for selector in self.AD_CONTROL_SELECTORS:
                            for control in await frame.locator(selector).all():
                                if await control.is_visible():
                                    controls.append(selector)
                with suppress(Exception):
                    await page.screenshot(
                        path=str(diagnostics / f"{stem}.png"), timeout=5000
                    )
            report = {
                "source_url": source_url,
                "page_url": current_url,
                "stage": stage.value if isinstance(stage, DownloadStage) else str(stage),
                "error_type": error.__class__.__name__,
                "error": str(error).splitlines()[0][:500],
                "browser_connected": bool(
                    self._browser and self._browser.is_connected()
                ),
                "frame_hosts": frame_hosts,
                "visible_ad_controls": sorted(set(controls)),
            }
            (diagnostics / f"{stem}.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    @staticmethod
    def _check_rate_limit(html: str):
        lower = html.lower()
        if "too many requests" in lower or "rate limit" in lower or "requests too fast" in lower:
            raise RateLimitError("ssstik.io: too many requests")

    @staticmethod
    async def _notify(cb, progress: int, message: str):
        if cb:
            await cb(progress, message)
