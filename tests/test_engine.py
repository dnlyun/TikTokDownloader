import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ssstik import SsstikDownloader

class EngineTests(unittest.IsolatedAsyncioTestCase):
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
            engine._save_download.assert_awaited_once_with(download, 1)
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

if __name__ == '__main__': unittest.main()
