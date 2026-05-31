from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw


CELL_W = 192
CELL_H = 208
COLS = 8
ROWS = 9
# M5Stack LCD redraws each JPEG frame over USB serial. Animate slowly enough
# for the serial bandwidth while the firmware double-buffers the LCD update with
# M5Canvas to avoid visible full-screen clearing.
SPRITE_FPS = 1.0

EXPRESSION_TO_ROW = {
    "neutral": 0,
    "idle": 0,
    "happy": 3,
    "talking": 3,
    "waving": 3,
    "sad": 5,
    "error": 5,
    "failed": 5,
    "angry": 5,
    "doubt": 8,
    "thinking": 8,
    "review": 8,
    "listening": 8,
    "sleepy": 6,
    "waiting": 6,
}


class SpriteFaceRenderer:
    """Convert sprite sheet cells to LCD-ready JPEG bytes."""

    def __init__(self, sheet_path: str | Path, quality: int = 85):
        self.sheet_path = Path(sheet_path).expanduser()
        self.quality = max(1, min(95, int(quality)))
        self._sheet_mtime: float | None = None
        self._sheet: Image.Image | None = None
        self._filled_frames: list[list[int]] | None = None
        self._cache: dict[str, bytes] = {}
        self._last_frame: Image.Image | None = None
        self._pending_frame: Image.Image | None = None

    def render_expression(self, expression: str) -> bytes:
        return self.render_expression_frame(expression, 0)

    def render_expression_frame(self, expression: str, step: int) -> bytes:
        expr = (expression or "").strip().lower()
        row = EXPRESSION_TO_ROW.get(expr, EXPRESSION_TO_ROW["neutral"])
        cols = self.frame_columns_for_expression(expression)
        col = cols[step % len(cols)] if cols else 0
        label = "LISTENING" if expr == "listening" else None
        return self.render_cell(row=row, col=col, label=label)

    def render_expression_frame_rect(self, expression: str, step: int) -> tuple[int, int, int, int, bytes] | None:
        """Return the changed RGB565 rect against the last committed frame.

        The first frame is full-screen. Later frames are cropped to the dirty
        bbox so the device can update only changed pixels with pushImage().
        """
        row = EXPRESSION_TO_ROW.get((expression or "").strip().lower(), EXPRESSION_TO_ROW["neutral"])
        cols = self.frame_columns_for_expression(expression)
        col = cols[step % len(cols)] if cols else 0
        frame = self.render_cell_image(row=row, col=col)

        bbox = (0, 0, frame.width, frame.height)
        if self._last_frame is not None and self._last_frame.size == frame.size:
            bbox = ImageChops.difference(self._last_frame, frame).getbbox()
            if bbox is None:
                self._pending_frame = None
                return None

        crop = frame.crop(bbox)
        self._pending_frame = frame
        x, y, right, bottom = bbox
        return x, y, right - x, bottom - y, self._to_rgb565_le(crop)

    def commit_pending_frame(self) -> None:
        if self._pending_frame is not None:
            self._last_frame = self._pending_frame
            self._pending_frame = None

    def discard_pending_frame(self) -> None:
        self._pending_frame = None

    def frame_columns_for_expression(self, expression: str) -> list[int]:
        row = EXPRESSION_TO_ROW.get((expression or "").strip().lower(), EXPRESSION_TO_ROW["neutral"])
        return self._detect_filled_frames()[row]

    def render_cell(
        self,
        row: int,
        col: int = 0,
        *,
        crop_head: bool = False,
        label: str | None = None,
    ) -> bytes:
        canvas = self.render_cell_image(row=row, col=col, crop_head=crop_head, label=label)
        key = f"{self.sheet_path}:{self._sheet_mtime}:{row}:{col}:{crop_head}:{label}:{self.quality}:jpeg"
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        out = io.BytesIO()
        canvas.save(out, format="JPEG", quality=self.quality, optimize=True)
        data = out.getvalue()
        self._cache[key] = data
        return data

    def render_cell_image(
        self,
        row: int,
        col: int = 0,
        *,
        crop_head: bool = False,
        label: str | None = None,
    ) -> Image.Image:
        sheet = self._load_sheet()
        row = max(0, min(row, (sheet.height // CELL_H) - 1))
        col = max(0, min(col, COLS - 1))

        cell = sheet.crop((col * CELL_W, row * CELL_H, (col + 1) * CELL_W, (row + 1) * CELL_H))
        if cell.mode != "RGBA":
            cell = cell.convert("RGBA")
        if crop_head:
            cell = self._crop_head(cell)
        # CoreS3 LCD is 320x240. Sprite mode is a face display, so zoom the
        # head instead of shrinking the whole body where expression changes
        # become invisible.
        cell.thumbnail((300, 220), Image.Resampling.NEAREST)
        canvas = Image.new("RGB", (320, 240), (0, 0, 0))
        x = (320 - cell.width) // 2
        y = (240 - cell.height) // 2
        canvas.paste(cell, (x, y), cell)
        if label:
            draw = ImageDraw.Draw(canvas)
            draw.rounded_rectangle((88, 206, 232, 232), radius=6, fill=(0, 0, 0), outline=(255, 255, 255), width=2)
            draw.text((111, 214), label, fill=(255, 255, 255))
        return canvas

    @staticmethod
    def _to_rgb565_le(image: Image.Image) -> bytes:
        rgb = image.convert("RGB").tobytes()
        out = bytearray((len(rgb) // 3) * 2)
        j = 0
        for i in range(0, len(rgb), 3):
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            out[j] = value & 0xFF
            out[j + 1] = value >> 8
            j += 2
        return bytes(out)

    @staticmethod
    def _crop_head(cell: Image.Image) -> Image.Image:
        alpha = cell.getchannel("A")
        bbox = alpha.getbbox()
        if bbox is None:
            return cell
        left, top, right, bottom = bbox
        width = right - left
        height = bottom - top
        head_bottom = top + int(height * 0.58)
        margin_x = max(8, int(width * 0.08))
        margin_y = max(6, int(height * 0.04))
        crop = (
            max(0, left - margin_x),
            max(0, top - margin_y),
            min(cell.width, right + margin_x),
            min(cell.height, head_bottom + margin_y),
        )
        return cell.crop(crop)

    def _detect_filled_frames(self) -> list[list[int]]:
        sheet = self._load_sheet()
        if self._filled_frames is not None:
            return self._filled_frames

        frames: list[list[int]] = []
        for row in range(min(ROWS, sheet.height // CELL_H)):
            filled: list[int] = []
            for col in range(min(COLS, sheet.width // CELL_W)):
                cell = sheet.crop((col * CELL_W, row * CELL_H, (col + 1) * CELL_W, (row + 1) * CELL_H))
                alpha = cell.getchannel("A") if cell.mode == "RGBA" else cell.convert("RGBA").getchannel("A")
                if alpha.getbbox() is not None:
                    filled.append(col)
            frames.append(filled or [0])
        while len(frames) < ROWS:
            frames.append([0])
        self._filled_frames = frames
        return frames

    def _load_sheet(self) -> Image.Image:
        stat = self.sheet_path.stat()
        if self._sheet is None or self._sheet_mtime != stat.st_mtime:
            self._sheet = Image.open(self.sheet_path).convert("RGBA")
            self._sheet_mtime = stat.st_mtime
            self._filled_frames = None
            self._cache.clear()
            self._last_frame = None
            self._pending_frame = None
        return self._sheet
