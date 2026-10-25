"""The "JF has blessed your timeline" welcome image (port of the node-canvas version).

Layout on a transparent 1000x1000 canvas:
* `jf-blessing.png` stretched into the left 423 px.
* The member's avatar in a 200 px circle centred at (550, 120).
* A green multi-line message whose first baseline is at (450, 300). The font starts at 60 px and
  shrinks in 10 px steps until the widest line fits in 600 px; anything still wider than 550 px
  is squeezed horizontally, like canvas `fillText`'s `maxWidth`.
"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CANVAS_SIZE = (1000, 1000)
BACKGROUND_BOX = (0, 0, 423, 1000)
AVATAR_BOX = (450, 20, 650, 220)  # 200x200, so the circle is centred at (550, 120)
TEXT_ORIGIN = (450, 300)  # left edge, first-line baseline
TEXT_FIT_WIDTH = 600  # canvas width minus "approximate avatar size" (400) in the TS code
TEXT_MAX_WIDTH = 550  # fillText maxWidth
TEXT_COLOR = (0x83, 0xC1, 0x33, 255)
START_FONT_SIZE = 60
MIN_FONT_SIZE = 10
LINE_SPACING = 1.17  # line height as a multiple of font size (Pango's default for DejaVu Sans)


def welcome_text(display_name: str) -> str:
    return (
        f'{display_name},\nJF has blessed\nyour timeline.\nsay "thank you\nmr. jf" for\n'
        "good fortune\nin the new year"
    )


@lru_cache(maxsize=16)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _widest_line(font: ImageFont.FreeTypeFont, text: str) -> float:
    return max(font.getlength(line) for line in text.split("\n"))


def fit_font_size(text: str, font_path: Path) -> int:
    """Largest size (60, 50, ... 10) at which the widest line fits in TEXT_FIT_WIDTH."""
    size = START_FONT_SIZE
    while size > MIN_FONT_SIZE and _widest_line(_font(str(font_path), size), text) > TEXT_FIT_WIDTH:
        size -= 10
    return size


def _render_text(text: str, font: ImageFont.FreeTypeFont, size: int) -> tuple[Image.Image, int]:
    """Draw the text on its own transparent layer. Returns (layer, ascent)."""
    ascent, descent = font.getmetrics()
    line_height = round(size * LINE_SPACING)
    lines = text.split("\n")
    width = max(1, round(_widest_line(font, text)))
    height = ascent + descent + line_height * (len(lines) - 1)
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for index, line in enumerate(lines):
        draw.text((0, ascent + index * line_height), line, font=font, fill=TEXT_COLOR, anchor="ls")
    return layer, ascent


def _circle_mask(size: tuple[int, int]) -> Image.Image:
    # Draw at 4x and downsample for a smooth (anti-aliased) edge.
    scale = 4
    big = Image.new("L", (size[0] * scale, size[1] * scale), 0)
    ImageDraw.Draw(big).ellipse((0, 0, big.width - 1, big.height - 1), fill=255)
    return big.resize(size, Image.LANCZOS)


def render_welcome_image(
    display_name: str, avatar_bytes: bytes, *, background_path: Path, font_path: Path
) -> bytes:
    """Build the welcome PNG and return its bytes."""
    canvas = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))

    left, top, right, bottom = BACKGROUND_BOX
    with Image.open(background_path) as background:
        canvas.alpha_composite(
            background.convert("RGBA").resize((right - left, bottom - top), Image.LANCZOS),
            (left, top),
        )

    text = welcome_text(display_name)
    size = fit_font_size(text, font_path)
    layer, ascent = _render_text(text, _font(str(font_path), size), size)
    if layer.width > TEXT_MAX_WIDTH:
        layer = layer.resize((TEXT_MAX_WIDTH, layer.height), Image.LANCZOS)
    x, baseline = TEXT_ORIGIN
    canvas.alpha_composite(layer, (x, baseline - ascent))

    left, top, right, bottom = AVATAR_BOX
    avatar_size = (right - left, bottom - top)
    with Image.open(io.BytesIO(avatar_bytes)) as raw_avatar:
        avatar = raw_avatar.convert("RGBA").resize(avatar_size, Image.LANCZOS)
    mask = _circle_mask(avatar_size)
    avatar.putalpha(Image.composite(avatar.getchannel("A"), Image.new("L", avatar_size), mask))
    canvas.alpha_composite(avatar, (left, top))

    out = io.BytesIO()
    canvas.save(out, "PNG", optimize=True)
    return out.getvalue()
