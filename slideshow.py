import asyncio
import json
from pathlib import Path
from typing import Awaitable, Callable, Optional


class SlideshowCreator:
    DISPLAY_DURATION = 2.5
    TRANSITION_DURATION = 0.5
    TRANSITION_TYPE = "slideleft"
    FPS = 30

    async def create_slideshow(self, image_paths: list[Path], audio_path: Path,
                               output_path: Path,
                               progress_callback: Optional[Callable[[int, str], Awaitable[None]]] = None) -> Path:
        if not image_paths:
            raise ValueError("No images provided")

        canvas_w, canvas_h = await self._detect_canvas_size(image_paths)
        audio_duration = await self._get_audio_duration(audio_path)

        if progress_callback:
            await progress_callback(72, f"Audio duration: {audio_duration:.1f}s")

        sequence = self._build_looped_sequence(image_paths, audio_duration)

        if progress_callback:
            await progress_callback(75, f"Building slideshow with {len(sequence)} slides...")

        cmd = self._build_ffmpeg_command(sequence, audio_path, output_path, canvas_w, canvas_h)

        if progress_callback:
            await progress_callback(80, "Rendering slideshow video...")

        await self._run_ffmpeg(cmd)

        if progress_callback:
            await progress_callback(95, "Slideshow creation complete!")
        return output_path

    def _build_looped_sequence(self, image_paths: list[Path], audio_duration: float) -> list[Path]:
        effective = self.DISPLAY_DURATION - self.TRANSITION_DURATION
        cycle = len(image_paths) * effective + self.TRANSITION_DURATION
        loops = max(1, int(audio_duration / cycle) + 1)
        sequence = image_paths * loops
        return sequence[:int(audio_duration / effective) + 2]

    def _build_ffmpeg_command(self, image_paths: list[Path], audio_path: Path,
                              output_path: Path, w: int, h: int) -> list[str]:
        n = len(image_paths)
        cmd = ["ffmpeg", "-y"]
        for img in image_paths:
            cmd.extend(["-loop", "1", "-t", str(self.DISPLAY_DURATION), "-i", str(img)])
        cmd.extend(["-i", str(audio_path)])

        filters = []
        for i in range(n):
            filters.append(
                f"[{i}]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,"
                f"setsar=1,format=yuv420p,fps={self.FPS}[img{i}]"
            )

        if n == 1:
            final = "img0"
        else:
            eff = self.DISPLAY_DURATION - self.TRANSITION_DURATION
            filters.append(
                f"[img0][img1]xfade=transition={self.TRANSITION_TYPE}:"
                f"duration={self.TRANSITION_DURATION}:offset={eff:.4f}[xf0]"
            )
            for i in range(2, n):
                filters.append(
                    f"[xf{i - 2}][img{i}]xfade=transition={self.TRANSITION_TYPE}:"
                    f"duration={self.TRANSITION_DURATION}:offset={eff:.4f}[xf{i - 1}]"
                )
            final = f"xf{n - 2}"

        cmd.extend(["-filter_complex", ";\n".join(filters)])
        cmd.extend([
            "-map", f"[{final}]", "-map", f"{n}:a",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart",
            str(output_path),
        ])
        return cmd

    async def _detect_canvas_size(self, image_paths: list[Path]) -> tuple[int, int]:
        max_w, max_h = 0, 0
        for img in image_paths:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(img),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            try:
                for stream in json.loads(stdout).get("streams", []):
                    max_w = max(max_w, stream.get("width", 0))
                    max_h = max(max_h, stream.get("height", 0))
            except (json.JSONDecodeError, KeyError):
                continue
        if max_w == 0 or max_h == 0:
            return 1080, 1920
        return max_w + (max_w % 2), max_h + (max_h % 2)

    async def _get_audio_duration(self, audio_path: Path) -> float:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(audio_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return float(json.loads(stdout)["format"]["duration"])

    async def _run_ffmpeg(self, cmd: list[str]):
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg failed ({proc.returncode}): {stderr.decode()[-500:]}")
