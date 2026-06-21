from pathlib import Path

from PIL import Image

from xangi_stackchan.app import SpriteAnimationLoop, _send_sprite_frame
from xangi_stackchan.app_types import BridgeConfig
from xangi_stackchan.sprite_face import CELL_H, CELL_W, SpriteFaceRenderer
from xangi_stackchan.stackchan import StackchanConfig


class RectBackend:
    def __init__(self):
        self.images = []
        self.rects = []
        self.cached = []
        self.shown = []
        self.animations = []
        self.stopped = 0
        self.raw_failures = 0
        self.is_connected = True

    def send_image(self, image_jpeg: bytes, chunk_size: int = 1024, chunk_delay: float = 0.005):
        self.images.append((image_jpeg, chunk_size, chunk_delay))
        return {"status": "ok", "image": len(image_jpeg)}

    def send_rect(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        rgb565: bytes,
        chunk_size: int = 4096,
        chunk_delay: float = 0.0,
    ):
        self.rects.append((x, y, width, height, rgb565, chunk_size, chunk_delay))
        return {"status": "ok", "rect": [x, y, width, height], "bytes": len(rgb565)}

    def cache_image_frame(
        self,
        slot: int,
        image_jpeg: bytes,
        chunk_size: int = 1024,
        chunk_delay: float = 0.005,
    ):
        self.cached.append((slot, image_jpeg, chunk_size, chunk_delay))
        return {"status": "ok", "sprite_image": slot, "size": len(image_jpeg)}

    def show_cached_image(self, slot: int):
        self.shown.append(slot)
        if self.raw_failures > 0:
            self.raw_failures -= 1
            return {"raw": ""}
        return {"status": "ok", "sprite_frame": slot}

    def start_cached_sprite_animation(self, slots: list[int], interval_ms: int):
        self.animations.append((list(slots), interval_ms))
        return {"status": "ok", "sprite_anim": True, "frames": len(slots), "interval_ms": interval_ms}

    def stop_cached_sprite_animation(self):
        self.stopped += 1
        return {"status": "ok", "sprite_anim": False}


def _config(sheet: Path) -> BridgeConfig:
    return BridgeConfig(
        xangi_url="http://127.0.0.1:18888",
        thread_id=None,
        stackchan=StackchanConfig(wifi=False, host="", port="/dev/null", baud=921600),
        volume=128,
        tts="piper",
        piper_bin="",
        piper_model="",
        piper_speaker=0,
        voicevox_url="",
        voicevox_speaker=0,
        serial_chunk=512,
        serial_delay=0.015,
        stackchan_retry_seconds=3.0,
        face_idle="neutral",
        face_thinking="doubt",
        face_talking="happy",
        face_error="sad",
        face_mode="sprite",
        sprite_sheet=str(sheet),
        sprite_jpeg_quality=85,
        stream_timeout=65,
        retry_seconds=1.0,
        max_retry_seconds=30.0,
    )


def test_sprite_face_renderer_extracts_expression_cell(tmp_path: Path):
    sheet = Image.new("RGBA", (CELL_W * 8, CELL_H * 9), (0, 0, 0, 0))
    # thinking/doubt maps to row 8. Use a distinct fill so the JPEG is not blank.
    row = 8
    col = 0
    patch = Image.new("RGBA", (CELL_W, CELL_H), (20, 200, 80, 255))
    sheet.paste(patch, (col * CELL_W, row * CELL_H))
    path = tmp_path / "spritesheet.webp"
    sheet.save(path)

    data = SpriteFaceRenderer(path).render_expression("doubt")

    assert data.startswith(b"\xff\xd8")
    assert len(data) > 500


def test_sprite_face_renderer_outputs_different_expression_images(tmp_path: Path):
    sheet = Image.new("RGBA", (CELL_W * 8, CELL_H * 9), (0, 0, 0, 0))
    sheet.paste(Image.new("RGBA", (CELL_W, CELL_H), (40, 80, 220, 255)), (0, 0))
    sheet.paste(Image.new("RGBA", (CELL_W, CELL_H), (220, 80, 40, 255)), (3 * CELL_W, 8 * CELL_H))
    path = tmp_path / "spritesheet.webp"
    sheet.save(path)

    renderer = SpriteFaceRenderer(path)

    assert renderer.render_expression("neutral") != renderer.render_expression("thinking")


def test_sprite_face_renderer_cycles_filled_frames(tmp_path: Path):
    sheet = Image.new("RGBA", (CELL_W * 8, CELL_H * 9), (0, 0, 0, 0))
    # idle row has frame 0 and a blink frame at 2, with empty cells between.
    sheet.paste(Image.new("RGBA", (CELL_W, CELL_H), (40, 80, 220, 255)), (0, 0))
    sheet.paste(Image.new("RGBA", (CELL_W, CELL_H), (220, 80, 40, 255)), (2 * CELL_W, 0))
    path = tmp_path / "spritesheet.webp"
    sheet.save(path)

    renderer = SpriteFaceRenderer(path)

    assert renderer.frame_columns_for_expression("neutral") == [0, 2]
    assert renderer.render_expression_frame("neutral", 0) != renderer.render_expression_frame("neutral", 1)
    assert renderer.render_expression_frame("neutral", 0) == renderer.render_expression_frame("neutral", 2)


def test_send_sprite_frame_uses_firmware_cache(tmp_path: Path):
    sheet = Image.new("RGBA", (CELL_W * 8, CELL_H * 9), (0, 0, 0, 0))
    sheet.paste(Image.new("RGBA", (CELL_W, CELL_H), (40, 80, 220, 255)), (0, 0))
    sheet.paste(Image.new("RGBA", (CELL_W, CELL_H), (40, 80, 220, 255)), (1 * CELL_W, 0))
    sheet.paste(Image.new("RGBA", (CELL_W, CELL_H), (220, 80, 40, 255)), (2 * CELL_W, 0))
    path = tmp_path / "spritesheet.webp"
    sheet.save(path)

    backend = RectBackend()
    current_face: list[str | None] = [None]
    renderer: list[SpriteFaceRenderer | None] = [None]
    config = _config(path)

    assert _send_sprite_frame(backend, config, "neutral", 0, current_face, renderer)
    assert not backend.images
    assert not backend.rects
    assert len(backend.cached) == 1
    assert backend.shown == [0]

    assert _send_sprite_frame(backend, config, "neutral", 0, current_face, renderer)
    assert len(backend.cached) == 1
    assert backend.shown == [0]

    assert _send_sprite_frame(backend, config, "neutral", 1, current_face, renderer)
    assert len(backend.cached) == 2
    assert backend.shown == [0, 1]

    assert _send_sprite_frame(backend, config, "neutral", 3, current_face, renderer)
    assert len(backend.cached) == 2
    assert backend.shown == [0, 1, 0]

    backend.raw_failures = 1
    assert _send_sprite_frame(backend, config, "neutral", 4, current_face, renderer)
    assert len(backend.cached) == 2
    assert backend.shown[-2:] == [1, 1]


def test_sprite_animation_loop_prefers_firmware_local_animation(tmp_path: Path):
    sheet = Image.new("RGBA", (CELL_W * 8, CELL_H * 9), (0, 0, 0, 0))
    sheet.paste(Image.new("RGBA", (CELL_W, CELL_H), (40, 80, 220, 255)), (0, 0))
    sheet.paste(Image.new("RGBA", (CELL_W, CELL_H), (220, 80, 40, 255)), (2 * CELL_W, 0))
    path = tmp_path / "spritesheet.webp"
    sheet.save(path)

    backend = RectBackend()
    current_face: list[str | None] = [None]
    renderer: list[SpriteFaceRenderer | None] = [None]

    animator = SpriteAnimationLoop(backend, _config(path), current_face, renderer)
    animator.set_expression("neutral")
    animator.pause()
    animator.resume()

    assert len(backend.cached) == 2
    assert backend.shown == []
    assert backend.animations[0][0] == [0, 1]
    assert backend.animations[0][1] == 1000
    assert backend.stopped == 0
    assert animator.keeps_running_during_wav() is True
