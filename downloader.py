import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Awaitable, Callable, Optional

import ssl

import aiohttp
import yt_dlp

from slideshow import SlideshowCreator

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


class TikTokDownloader:
    def __init__(self, temp_dir: Path, output_dir: Path):
        self.temp_dir = temp_dir
        self.output_dir = output_dir

    async def download(
        self,
        url: str,
        output_filename: str,
        progress_callback: Optional[Callable[[int, str], Awaitable[None]]] = None,
    ) -> Path:
        await self._notify(progress_callback, 5, "Extracting post info...")
        info = await self._extract_info(url)

        if info is None:
            raise ValueError("yt-dlp could not extract info from this URL")

        if self._is_slideshow(info):
            await self._notify(progress_callback, 10, "Detected slideshow post")
            return await self._handle_slideshow(url, info, output_filename, progress_callback)
        else:
            await self._notify(progress_callback, 10, "Detected video post")
            return await self._handle_video(url, info, output_filename, progress_callback)

    async def _extract_info(self, url: str) -> dict:
        loop = asyncio.get_event_loop()
        ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": False, "nocheckcertificate": True}

        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        return await loop.run_in_executor(None, _extract)

    def _is_slideshow(self, info: dict) -> bool:
        formats = info.get("formats", [])
        if not formats:
            return True
        return not any(f.get("vcodec", "none") != "none" for f in formats)

    async def _handle_video(self, url: str, info: dict, output_filename: str, callback) -> Path:
        await self._notify(callback, 15, "Downloading video in highest quality...")
        output_path = self.output_dir / f"{output_filename}.mp4"
        loop = asyncio.get_event_loop()

        def _download():
            ydl_opts = {
                "format": "bestvideo+bestaudio/best",
                "merge_output_format": "mp4",
                "outtmpl": str(output_path),
                "quiet": True,
                "nocheckcertificate": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

        await loop.run_in_executor(None, _download)
        await self._notify(callback, 95, "Video downloaded")
        return output_path

    async def _handle_slideshow(self, url: str, info: dict, output_filename: str, callback) -> Path:
        video_id = info.get("id", "slideshow")
        work_dir = self.temp_dir / video_id
        work_dir.mkdir(exist_ok=True)

        try:
            await self._notify(callback, 15, "Fetching slideshow images...")
            image_urls = await self._extract_slideshow_images(url, info)
            if not image_urls:
                raise ValueError("Could not extract slideshow images from this post")

            await self._notify(callback, 25, "Downloading audio track...")
            audio_path = await self._download_audio(url, work_dir)

            await self._notify(callback, 35, f"Downloading {len(image_urls)} images...")
            image_paths = await self._download_images(image_urls, work_dir, callback)
            if not image_paths:
                raise ValueError("Failed to download any images")

            await self._notify(callback, 70, "Creating slideshow video...")
            creator = SlideshowCreator()
            output_path = self.output_dir / f"{output_filename}.mp4"
            await creator.create_slideshow(image_paths, audio_path, output_path, callback)
            return output_path
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _extract_slideshow_images(self, url: str, info: dict) -> list[str]:
        image_urls = []

        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_ssl_ctx)) as session:
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml",
                }
                async with session.get(url, headers=headers, allow_redirects=True) as resp:
                    html = await resp.text()

            image_urls = self._parse_images_from_html(html)
        except Exception:
            pass

        if not image_urls:
            image_urls = self._parse_images_from_info(info)

        return image_urls

    def _parse_images_from_html(self, html: str) -> list[str]:
        urls = []
        pattern = r'<script\s+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>'
        match = re.search(pattern, html, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                detail = data.get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {})
                item = detail.get("itemInfo", {}).get("itemStruct", {})
                images = item.get("imagePost", {}).get("images", [])
                for img in images:
                    url_list = img.get("imageURL", {}).get("urlList", [])
                    if url_list:
                        urls.append(url_list[0])
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        if not urls:
            pattern2 = r'<script\s+id="SIGI_STATE"[^>]*>(.*?)</script>'
            match2 = re.search(pattern2, html, re.DOTALL)
            if match2:
                try:
                    data = json.loads(match2.group(1))
                    for _item_id, item in data.get("ItemModule", {}).items():
                        images = item.get("imagePost", {}).get("images", [])
                        for img in images:
                            url_list = img.get("imageURL", {}).get("urlList", [])
                            if url_list:
                                urls.append(url_list[0])
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

        return urls

    def _parse_images_from_info(self, info: dict) -> list[str]:
        urls = []
        skip_ids = {"cover", "origin_cover", "dynamic_cover", "ai_dynamic_cover", "animated_cover"}
        for t in info.get("thumbnails", []):
            if t.get("id") not in skip_ids and t.get("url"):
                urls.append(t["url"])
        return urls

    async def _download_audio(self, url: str, work_dir: Path) -> Path:
        loop = asyncio.get_event_loop()

        def _download():
            ydl_opts = {
                "format": "bestaudio[ext=m4a]/bestaudio",
                "outtmpl": str(work_dir / "audio.%(ext)s"),
                "quiet": True,
                "nocheckcertificate": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

        await loop.run_in_executor(None, _download)
        audio_files = list(work_dir.glob("audio.*"))
        if not audio_files:
            raise ValueError("Failed to download audio track")
        return audio_files[0]

    async def _download_images(
        self, image_urls: list[str], work_dir: Path, callback
    ) -> list[Path]:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_ssl_ctx)) as session:
            tasks = []
            paths = []
            for i, img_url in enumerate(image_urls):
                ext = "jpeg"
                if ".webp" in img_url:
                    ext = "webp"
                elif ".png" in img_url:
                    ext = "png"
                path = work_dir / f"slide_{i:03d}.{ext}"
                paths.append(path)
                tasks.append(self._download_single_image(session, img_url, path))

            results = await asyncio.gather(*tasks, return_exceptions=True)

        valid = []
        for i, (path, result) in enumerate(zip(paths, results)):
            if isinstance(result, Exception):
                continue
            if path.exists() and path.stat().st_size > 0:
                valid.append(path)
                progress = 35 + int((i + 1) / len(image_urls) * 30)
                await self._notify(callback, progress, f"Downloaded image {i + 1}/{len(image_urls)}")

        return valid

    async def _download_single_image(self, session: aiohttp.ClientSession, url: str, path: Path):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.tiktok.com/",
        }
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                path.write_bytes(await resp.read())

    @staticmethod
    async def _notify(callback, progress: int, message: str):
        if callback:
            await callback(progress, message)