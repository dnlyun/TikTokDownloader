import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ssstik import (
    RateLimitError,
    SsstikDownloader,
    is_valid_mp4,
)

class EngineTests(unittest.IsolatedAsyncioTestCase):
    def test_windows_defaults_to_installed_chrome(self):
        with patch("ssstik.platform.system", return_value="Windows"), patch.dict(
            "os.environ", {}, clear=True
        ):
            self.assertEqual(SsstikDownloader.browser_options()["channel"], "chrome")

    def test_flow_timeout_is_configurable(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"SSSTIK_FLOW_TIMEOUT": "900"}, clear=True
        ):
            self.assertEqual(SsstikDownloader(Path(directory)).flow_timeout, 900)

    async def test_fast_download_is_not_missed(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = SsstikDownloader(Path(directory))
            download = object()
            listeners = {}
            class Page:
                def on(self, event, callback): listeners[event] = callback
                def remove_listener(self, event, callback): listeners.pop(event)
            class Button:
                async def click(self, **kwargs): listeners['download'](download)
            engine._save_download = AsyncMock(return_value='1.mp4')
            result = await engine._click_and_download(Page(), Button(), 1, None, 'HD')
            self.assertEqual(result, '1.mp4')
            engine._save_download.assert_awaited_once()
            self.assertEqual(engine._save_download.await_args.args[:2], (download, 1))
            self.assertFalse(listeners)

    async def test_cleanup_survives_crashed_context(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = SsstikDownloader(Path(directory))
            context = AsyncMock(); context.close.side_effect = RuntimeError('crashed')
            browser = AsyncMock(); driver = AsyncMock()
            engine._context, engine._browser, engine._playwright = context, browser, driver
            await engine.close_browser()
            browser.close.assert_awaited_once()
            driver.stop.assert_awaited_once()
            self.assertIsNone(engine._browser)
            await engine.close_browser()
            browser.close.assert_awaited_once()

    async def test_popup_download_is_captured_and_popup_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = SsstikDownloader(Path(directory))
            listeners = {}
            popup_listeners = {}
            download = object()

            class Popup:
                def on(self, event, callback): popup_listeners[event] = callback
                def remove_listener(self, event, callback): popup_listeners.pop(event)
                async def close(self): self.closed = True
                closed = False

            popup = Popup()
            class Page:
                def on(self, event, callback): listeners[event] = callback
                def remove_listener(self, event, callback): listeners.pop(event)
            page = Page()
            class Button:
                async def click(self, **kwargs):
                    listeners["popup"](popup)
                    popup_listeners["download"](download)

            engine._save_download = AsyncMock(return_value="2.mp4")
            result = await engine._click_and_download(page, Button(), 2, None, "HD")
            self.assertEqual(result, "2.mp4")
            self.assertTrue(popup.closed)
            self.assertFalse(listeners)
            self.assertFalse(popup_listeners)

    async def test_ad_control_is_clicked_when_it_becomes_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = SsstikDownloader(Path(directory))
            visible = AsyncMock()
            visible.is_visible.return_value = True

            class HiddenLocator:
                async def all(self): return []

            class Locator:
                async def all(self): return [self]
                async def is_visible(self): return await visible.is_visible()
                async def click(self, **kwargs): return await visible.click(**kwargs)

            class Frame:
                def locator(self, selector): return HiddenLocator()
                def get_by_role(self, role, name): return Locator()

            class Page:
                frames = [Frame()]
                def is_closed(self): return False

            self.assertTrue(await engine._advance_ad_flow({Page()}))
            visible.click.assert_awaited_once_with(timeout=1000)

    async def test_no_ad_control_is_a_normal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = SsstikDownloader(Path(directory))
            hidden = AsyncMock()
            hidden.first = hidden
            hidden.is_visible.return_value = False
            hidden.all.return_value = []
            dialog = AsyncMock(); dialog.first = dialog; dialog.evaluate.return_value = False
            countdown = AsyncMock(); countdown.first = countdown; countdown.inner_text.return_value = ""

            class Frame:
                def locator(self, selector):
                    if selector == "#close-confirmation-dialog": return dialog
                    if selector == "#dismiss-button": return countdown
                    return hidden
                def get_by_role(self, role, name): return hidden

            class Page:
                frames = [Frame()]
                def is_closed(self): return False

            self.assertFalse(await engine._advance_ad_flow({Page()}))

    async def test_later_visible_ad_control_is_not_hidden_by_first_match(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = SsstikDownloader(Path(directory))
            first = AsyncMock(); first.is_visible.return_value = False
            second = AsyncMock(); second.is_visible.return_value = True
            locator = AsyncMock(); locator.all.return_value = [first, second]
            semantic = AsyncMock(); semantic.all.return_value = []
            absent = AsyncMock(); absent.first = absent
            absent.is_visible.return_value = False
            dialog = AsyncMock(); dialog.first = dialog; dialog.evaluate.return_value = False
            countdown = AsyncMock(); countdown.first = countdown; countdown.inner_text.return_value = ""

            class Frame:
                def locator(self, selector):
                    if selector == "#close-confirmation-dialog": return dialog
                    if selector == "#dismiss-button": return countdown
                    if selector == "#dismiss-button-element": return absent
                    return locator
                def get_by_role(self, role, name): return semantic
            class Page:
                frames = [Frame()]
                def is_closed(self): return False

            self.assertTrue(await engine._advance_ad_flow({Page()}))
            second.click.assert_awaited_once_with(timeout=1000)

    async def test_rewarded_ad_is_not_closed_while_countdown_is_active(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = SsstikDownloader(Path(directory))
            dismiss = AsyncMock(); dismiss.is_visible.return_value = True
            countdown = AsyncMock(); countdown.first = countdown
            countdown.inner_text.return_value = "Reward in 5 seconds Close"
            dialog = AsyncMock(); dialog.first = dialog
            dialog.evaluate.return_value = False
            empty = AsyncMock(); empty.all.return_value = []

            class Frame:
                def locator(self, selector):
                    if selector == "#close-confirmation-dialog": return dialog
                    if selector == "#dismiss-button": return countdown
                    if selector == "#dismiss-button-element:visible":
                        found = AsyncMock(); found.all.return_value = [dismiss]; return found
                    return empty
                def get_by_role(self, role, name): return empty
            class Page:
                frames = [Frame()]
                def is_closed(self): return False

            self.assertFalse(await engine._advance_ad_flow({Page()}))
            dismiss.click.assert_not_awaited()

    async def test_visible_close_confirmation_is_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = SsstikDownloader(Path(directory))
            dialog = AsyncMock(); dialog.first = dialog
            dialog.evaluate.return_value = True
            resume = AsyncMock(); resume.first = resume
            resume.is_visible.return_value = True

            class Frame:
                def locator(self, selector):
                    if selector == "#close-confirmation-dialog": return dialog
                    if selector == "#resume-ad-button": return resume
                    return AsyncMock()
            class Page:
                frames = [Frame()]
                def is_closed(self): return False

            self.assertTrue(await engine._advance_ad_flow({Page()}))
            resume.click.assert_awaited_once_with(timeout=1000)

    async def test_reward_countdown_at_one_is_eligible(self):
        countdown = AsyncMock(); countdown.first = countdown
        countdown.count.return_value = 1
        countdown.inner_text.return_value = "Reward in 1 seconds Close"
        class Frame:
            def locator(self, selector): return countdown
        self.assertFalse(await SsstikDownloader._reward_countdown_active(Frame()))

    async def test_missing_ad_elements_do_not_wait_for_playwright_timeout(self):
        missing = AsyncMock(); missing.count.return_value = 0
        class Frame:
            def locator(self, selector): return missing

        self.assertFalse(await SsstikDownloader._reward_countdown_active(Frame()))
        self.assertFalse(await SsstikDownloader._resume_close_confirmation(Frame()))
        missing.inner_text.assert_not_awaited()
        missing.evaluate.assert_not_awaited()

    async def test_completed_reward_ad_uses_dismiss_fast_path(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = SsstikDownloader(Path(directory))
            dismiss = AsyncMock(); dismiss.first = dismiss
            dismiss.is_visible.return_value = True
            dismiss.is_enabled.return_value = True
            countdown = AsyncMock(); countdown.first = countdown
            countdown.inner_text.return_value = "Reward in 1 seconds Close"
            dialog = AsyncMock(); dialog.first = dialog
            dialog.evaluate.return_value = False
            unused = AsyncMock(); unused.first = unused
            unused.is_visible.return_value = False

            class Frame:
                def locator(self, selector):
                    if selector == "#close-confirmation-dialog": return dialog
                    if selector == "#dismiss-button": return countdown
                    if selector == "#dismiss-button-element": return dismiss
                    return unused
                def get_by_role(self, role, name): return unused
            class Page:
                frames = [Frame()]
                def is_closed(self): return False

            self.assertTrue(await engine._advance_ad_flow({Page()}))
            dismiss.click.assert_awaited_once_with(timeout=500)

    def test_mp4_validation_rejects_empty_and_html(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.mp4"
            path.write_bytes(b"")
            self.assertFalse(is_valid_mp4(path))
            path.write_bytes(b"<html>not a video</html>" * 100)
            self.assertFalse(is_valid_mp4(path))

    def test_mp4_validation_accepts_ftyp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.mp4"
            path.write_bytes(b"\0\0\0\x18ftypisom" + bytes(2048))
            self.assertTrue(is_valid_mp4(path))

    def test_http_429_becomes_rate_limit(self):
        with self.assertRaises(RateLimitError):
            SsstikDownloader._check_response_errors([(429, "https://ssstik.io/api")])

    def test_browser_closed_error_is_recognized(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = SsstikDownloader(Path(directory))
            browser = Mock()
            browser.is_connected.return_value = True
            engine._browser = browser
            self.assertTrue(engine._browser_is_unavailable(
                RuntimeError("Target page, context or browser has been closed")
            ))

    async def test_new_page_failure_is_browser_unavailable(self):
        from playwright.async_api import Error as PlaywrightError
        from ssstik import BrowserUnavailableError

        with tempfile.TemporaryDirectory() as directory:
            engine = SsstikDownloader(Path(directory))
            browser = Mock(); browser.is_connected.return_value = True
            context = AsyncMock(); context.new_page.side_effect = PlaywrightError("closed")
            engine._browser, engine._context = browser, context
            with self.assertRaises(BrowserUnavailableError):
                await engine.download_url("https://www.tiktok.com/@x/video/1", 1)

    async def test_diagnostics_are_written_without_frame_query_strings(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = SsstikDownloader(Path(directory))
            class Page:
                def is_closed(self): return True
            await engine._write_diagnostics(
                Page(), 7, "https://www.tiktok.com/@x/video/1",
                "waiting", RuntimeError("test failure"),
            )
            reports = list((Path(directory) / "diagnostics").glob("*.json"))
            self.assertEqual(len(reports), 1)
            contents = reports[0].read_text()
            self.assertIn('"stage": "waiting"', contents)
            self.assertNotIn("googleads.g.doubleclick.net/pagead", contents)


if __name__ == '__main__': unittest.main()
