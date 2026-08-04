"""Generate an EPUB cover image from an article's hero image + title/author.

A top-weighted white fade keeps the title legible while the image shows through
below; when there's no usable image it degrades to a clean typographic cover.
"""
import io
import logging
import os

from PIL import Image, ImageDraw, ImageFont, ImageOps

logger = logging.getLogger(__name__)

W, H = 1200, 1800
MARGIN = 110
TOP_ALPHA, BOTTOM_ALPHA = 210, 70
BLANK_BG = (245, 244, 242)
TITLE_WEIGHT, AUTHOR_WEIGHT = 700, 450

# League Spartan (OFL), bundled as a variable font so covers render identically
# everywhere with no system-font dependency. Its default instance is Thin, so
# the weight axis must be set explicitly.
_FONT_PATH = os.path.join(os.path.dirname(__file__), "assets", "fonts", "LeagueSpartan-VF.ttf")
_FALLBACKS = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]

_font_cache: dict[tuple[int, int], ImageFont.FreeTypeFont] = {}


def _font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    key = (size, weight)
    if key in _font_cache:
        return _font_cache[key]
    try:
        font = ImageFont.truetype(_FONT_PATH, size)
        font.set_variation_by_axes([weight])
        _font_cache[key] = font
        return font
    except Exception:
        logger.debug("bundled font unavailable; trying system sans")
    for path in _FALLBACKS:
        try:
            font = ImageFont.truetype(path, size)
            _font_cache[key] = font
            return font
        except OSError:
            continue
    font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _faded_hero(image_bytes: bytes) -> Image.Image:
    hero = ImageOps.fit(
        Image.open(io.BytesIO(image_bytes)).convert("RGB"), (W, H), method=Image.LANCZOS
    )
    col = Image.new("L", (1, H))
    for y in range(H):
        col.putpixel((0, y), int(TOP_ALPHA + (BOTTOM_ALPHA - TOP_ALPHA) * (y / H)))
    mask = col.resize((W, H))
    return Image.composite(Image.new("RGB", (W, H), "white"), hero, mask)


def make_cover(image_bytes: bytes | None, title: str, author: str | None) -> bytes:
    """Return JPEG bytes for a generated cover."""
    canvas = Image.new("RGB", (W, H), BLANK_BG)
    if image_bytes:
        try:
            canvas = _faded_hero(image_bytes)
        except Exception:
            logger.debug("cover hero render failed; using blank background")

    draw = ImageDraw.Draw(canvas)
    max_w = W - 2 * MARGIN

    size = 88
    tfont = _font(size, TITLE_WEIGHT)
    tlines = _wrap(draw, title or "Untitled", tfont, max_w)
    while len(tlines) > 5 and size > 54:
        size -= 8
        tfont = _font(size, TITLE_WEIGHT)
        tlines = _wrap(draw, title or "Untitled", tfont, max_w)

    y = int(H * 0.13)
    for ln in tlines:
        w = draw.textlength(ln, font=tfont)
        draw.text(((W - w) / 2, y), ln, fill=(18, 18, 18), font=tfont)
        y += tfont.size + 20

    if author:
        afont = _font(46, AUTHOR_WEIGHT)
        y += 34
        for ln in _wrap(draw, author, afont, max_w):
            w = draw.textlength(ln, font=afont)
            draw.text(((W - w) / 2, y), ln, fill=(55, 55, 55), font=afont)
            y += afont.size + 12

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=88)
    return buf.getvalue()
