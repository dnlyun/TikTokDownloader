import asyncio
import json
import subprocess
from pathlib import Path
from typing import Awaitable, Callable, Optional


class SlideshowCreator:
    DISPLAY_DURATION = 2.5
    TRANSITION_DURATION = 0.5
    TRANSITION_TYPE = "slideleft"
    FPS = 30

    async def create_slideshow(
        self,
        image_paths: list[Path],
        audio_path: Path,
        output_path: Path,
        progress_callback: Optional[Callable[[int, str], Awaitable[None]]] = None,
    ) -> Path:
        if not image_paths:
            raise ValueError("No images provided for slideshow")

        canvas_w, canvas_h = await self._detect_canvas_size(image_paths)
        audio_duration = await self._get_audio_duration(audio_path)

        if progress_callback:
            await progress_callback(72, f"Audio duration: {audio_duration:.1f}s")

        full_sequence = self._build_looped_sequence(image_paths, audio_duration)

        if progress_callback:
            await progress_callback(75, f"Building slideshow with {len(full_sequence)} slides...")

        cmd = self._build_ffmpeg_command(full_sequence, audio_path, output_path, canvas_w, canvas_h)

        if progress_callback:
            await progress_callback(80, "Rendering slideshow video...")

        await self._run_ffmpeg(cmd)

        if progress_callback:
            await progress_callback(95, "Slideshow creation complete!")

        return output_path

    def _build_looped_sequence(self, image_paths: list[Path], audio_duration: float) -> list[Path]:
        effective_display = self.DISPLAY_DURATION - self.TRANSITION_DURATION
        single_cycle = len(image_paths) * effective_display + self.TRANSITION_DURATION
        num_loops = max(1, int(audio_duration / single_cycle) + 1)
        full_sequence = image_paths * num_loops
        min_needed = int(audio_duration / effective_display) + 2
        return full_sequence[:min_needed]

    def _build_ffmpeg_command(
        self,
        image_paths: list[Path],
        audio_path: Path,
        output_path: Path,
        canvas_w: int,
        canvas_h: int,
    ) -> list[str]:
        n = len(image_paths)
        cmd = ["ffmpeg", "-y"]
        for img in image_paths:
            cmd.extend(["-loop", "1", "-t", str(self.DISPLAY_DURATION), "-i", str(img)])
        cmd.extend(["-i", str(audio_path)])

        filter_parts = []
        for i in range(n):
            filter_parts.append(
                f"[{i}]scale={canvas_w}:{canvas_h}:"
                f"force_original_aspect_ratio=decrease,"
                f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:black,"
                f"setsar=1,format=yuv420p,fps={self.FPS}[img{i}]"
            )

        if n == 1:
            final_label = "img0"
        else:
            offset = self.DISPLAY_DURATION - self.TRANSITION_DURATION
            filter_parts.append(
                f"[img0][img1]xfade=transition={self.TRANSITION_TYPE}:"
                f"duration={self.TRANSITION_DURATION}:offset={offset:.4f}[xf0]"
            )
            for i in range(2, n):
                prev = f"xf[i - 2]"
                curr = f"xf[i - 1]"
                off = (i) * (self.DISPLAY_DURATION - self.TRANSITION_DURATION)
                filter_parts.append(
                    f"[{prev}][img{i}]xfade=transition={self.TRANSITION_TYPE}:"
                    f"duration={self.TRANSITION_DURATION}:offset={off:.4f}[{curr}]"
                )
            final_label = f"xf{n - 2}"

        cmd.extend(["-filter_complex", ";\n".join(filter_parts)])
        cmd.extend([
            "-map", f"[{final_label}]",
            "-map", f"{n}:a",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            str(output_path),
        ])
        return cmd

    async def _detect_canvas_size(self, image_paths: list[Path]) -> tuple[int, int]:
        max_w, max_h = 0, 0
        for img in image_paths:
            cmd =[
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", str(img),
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            try:
                data = json.loads(stdout)
                for stream in data.get("streams", []):
                    w = stream.get("width", 0)
                    h = stream.get("height", 0)
                    if w > max_w:
                        max_w = w
                    if h > max_h:
                        max_h = h
            except (json.JSONDecodeError, KeyError):
                continue

        if max_w == 0 or max_h == 0:
            return 1080, 1920
        return max_w + (max_w % 2), max_h + (max_h % 2)

    async def _get_audio_duration(self, audio_path: Path) -> float:
        cmd =[
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", str(audio_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        data = json.loads(stdout)
        return float(data["format"]["duration"])

    async def _run_ffmpeg(self, cmd: list[str]):
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg failed (code {proc.returncode}): {stderr.decode()[-500:]}")