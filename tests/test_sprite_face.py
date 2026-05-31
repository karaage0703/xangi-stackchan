from pathlib import Path

from PIL import Image

from xangi_stackchan.sprite_face import CELL_H, CELL_W, SpriteFaceRenderer


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
