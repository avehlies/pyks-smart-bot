from __future__ import annotations

import io

from PIL import Image

from yks.bot.welcome_image import fit_font_size, render_welcome_image, welcome_text
from yks.config import DEFAULT_ASSETS_DIR

FONT = DEFAULT_ASSETS_DIR / "fonts" / "DejaVuSans.ttf"
BACKGROUND = DEFAULT_ASSETS_DIR / "jf-blessing.png"
GREEN = (0x83, 0xC1, 0x33)


def _avatar(color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (128, 128), color).save(out, "PNG")
    return out.getvalue()


def test_font_shrinks_for_long_names() -> None:
    assert fit_font_size(welcome_text("Andrew"), FONT) == 60
    assert fit_font_size(welcome_text("Sir Reginald Fluffington-Smythe III"), FONT) < 60
    assert fit_font_size(welcome_text("x" * 500), FONT) == 10  # floor, then squeezed


def test_render_layout() -> None:
    png = render_welcome_image(
        "Andrew", _avatar((10, 200, 10)), background_path=BACKGROUND, font_path=FONT
    )
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    assert image.size == (1000, 1000)
    assert image.getpixel((550, 120)) == (10, 200, 10, 255)  # avatar
    assert image.getpixel((990, 990))[3] == 0  # untouched canvas stays transparent
    assert image.getpixel((200, 500))[3] == 255  # background column is opaque
    # Some text pixels are the TS bot's green, in the text area right of the background.
    region = image.crop((450, 240, 1000, 700))
    colors = region.getcolors(region.width * region.height) or []
    assert any(color == (*GREEN, 255) for _, color in colors)


def test_long_text_is_squeezed_into_max_width() -> None:
    png = render_welcome_image(
        "W" * 200, _avatar((0, 0, 0)), background_path=BACKGROUND, font_path=FONT
    )
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    rows = range(250, 300)  # the first (very long) line
    # Squeezed to exactly 550 px starting at x = 450: it reaches the right edge...
    assert any(image.getpixel((995, y))[3] for y in rows)
    # ...and nothing spills left into the gap between background and text.
    assert not any(image.getpixel((440, y))[3] for y in rows)
