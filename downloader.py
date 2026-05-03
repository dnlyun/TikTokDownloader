import asyncio
import re
import shutil
import ssl
from pathlib import Path
from typing import Awaitable, Callable, Optional

import aiohttp
import gallery_dl.extractor
import yt_dlp

from slideshow import SlideshowCreator

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


class TikTokDownloader:
    def __init__(self, temp_dir: Path, output_dir: Path):
        self.temp_dir = temp_dir
        self.output_dir = output_dir
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def download(self, url: str, output_filename: str,
                       progress_callback: Optional[Callable[[int, str], Awaitable[None]]] = None) -> Path:
        ytdlp_url = re.sub(r"/photo/", "/video/", url)

        await self._notify(progress_callback, 5, "Extracting post info...")
        info = await self._extract_info(ytdlp_url)
        if info is None:
            raise ValueError("yt-dlp could not extract info from this URL")

        if self._is_slideshow(info):
            await self._notify(progress_callback, 10, "Detected slideshow post")
            return await self._handle_slideshow(url, ytdlp_url, info, output_filename, progress_callback)

        await self._notify(progress_callback, 10, "Detected video post")
        return await self._handle_video(ytdlp_url, output_filename, progress_callback)

    async def _extract_info(self, url: str) -> dict:
        opts = {"quiet": True, "no_warnings": True, "nocheckcertificate": True}

        def _run():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        return await asyncio.get_event_loop().run_in_executor(None, _run)

    @staticmethod
    def _is_slideshow(info: dict) -> bool:
        formats = info.get("formats", [])
        if not formats:
            return True
        return not any(f.get("vcodec", "none") != "none" for f in formats)

    async def _handle_video(self, url: str, output_filename: str, callback) -> Path:
        await self._notify(callback, 15, "Downloading video...")
        output_path = self.output_dir / f"{output_filename}.mp4"

        def _run():
            opts = {
                "format": "bestvideo+bestaudio/best",
                "merge_output_format": "mp4",
                "outtmpl": str(output_path),
                "quiet": True,
                "nocheckcertificate": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

        await asyncio.get_event_loop().run_in_executor(None, _run)
        await self._notify(callback, 95, "Video downloaded")
        return output_path

    async def _handle_slideshow(self, page_url: str, ytdlp_url: str, info: dict,
                                output_filename: str, callback) -> Path:
        work_dir = self.temp_dir / info.get("id", "slideshow")
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            await self._notify(callback, 15, "Extracting slideshow images...")
            image_urls, audio_url = await self._extract_slideshow_urls(page_url)
            if not image_urls:
                raise ValueError("Could not extract slideshow images from this post")

            await self._notify(callback, 25, "Downloading audio...")
            if audio_url:
                audio_path = await self._download_file(audio_url, work_dir / "audio.mp3")
            else:
                audio_path = await self._download_audio_ytdlp(ytdlp_url, work_dir)

            await self._notify(callback, 35, f"Downloading {len(image_urls)} images...")
            image_paths = await self._download_images(image_urls, work_dir, callback)
            if not image_paths:
                raise ValueError("Failed to download any images")

            await self._notify(callback, 70, "Creating slideshow video...")
            output_path = self.output_dir / f"{output_filename}.mp4"
            await SlideshowCreator().create_slideshow(image_paths, audio_path, output_path, callback)
            return output_path
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _extract_slideshow_urls(self, url: str) -> tuple[list[str], str | None]:
        def _run():
            extr = gallery_dl.extractor.find(url)
            if extr is None:
                return [], None
            extr.initialize()
            images, audio = [], None
            for msg in extr:
                if not isinstance(msg, tuple) or msg[0] != 3:
                    continue
                u = msg[1]
                if "/video/" in u or "audio" in u:
                    audio = u
                else:
                    images.append(u)
            return images, audio
        return await asyncio.get_event_loop().run_in_executor(None, _run)

    async def _download_file(self, url: str, path: Path) -> Path:
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.tiktok.com/"}
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_ssl_ctx)) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    path.write_bytes(await resp.read())
        return path

    async def _download_audio_ytdlp(self, url: str, work_dir: Path) -> Path:
        def _run():
            opts = {
                "format": "bestaudio[ext=m4a]/bestaudio",
                "outtmpl": str(work_dir / "audio.%(ext)s"),
                "quiet": True,
                "nocheckcertificate": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

        await asyncio.get_event_loop().run_in_executor(None, _run)
        audio_files = list(work_dir.glob("audio.*"))
        if not audio_files:
            raise ValueError("Failed to download audio track")
        return audio_files[0]

    async def _download_images(self, image_urls: list[str], work_dir: Path, callback) -> list[Path]:
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.tiktok.com/"}
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_ssl_ctx)) as session:
            tasks = []
            paths = []
            for i, url in enumerate(image_urls):
                path = work_dir / f"slide_{i:03d}.jpeg"
                paths.append(path)
                tasks.append(self._fetch(session, url, path, headers))
            results = await asyncio.gather(*tasks, return_exceptions=True)

        valid = []
        for i, (path, result) in enumerate(zip(paths, results)):
            if isinstance(result, Exception) or not path.exists() or path.stat().st_size == 0:
                continue
            valid.append(path)
            progress = 35 + int((i + 1) / len(image_urls) * 30)
            await self._notify(callback, progress, f"Downloaded image {i + 1}/{len(image_urls)}")
        return valid

    @staticmethod
    async def _fetch(session: aiohttp.ClientSession, url: str, path: Path, headers: dict):
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                path.write_bytes(await resp.read())

    @staticmethod
    async def _notify(callback, progress: int, message: str):
        if callback:
            await callback(progress, message)
