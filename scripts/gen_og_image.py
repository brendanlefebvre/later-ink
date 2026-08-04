"""Regenerate the social-preview card (src/later_ink/assets/og.png, 1200x630).

Run when the landing copy or palette changes:
    .venv/bin/python scripts/gen_og_image.py

Reuses the site's night-mode palette and the bundled League Spartan variable
font so the card matches the landing page exactly. The arrow glyphs are drawn
with primitives because League Spartan has no U+2192.
"""
import os

from PIL import Image, ImageDraw, ImageFont

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ASSETS = os.path.join(_ROOT, "src", "later_ink", "assets")
FONT = os.path.join(_ASSETS, "fonts", "LeagueSpartan-VF.ttf")
OUT = os.path.join(_ASSETS, "og.png")

W, H = 1200, 630
DESK = (8, 9, 11)        # --desk
BG = (13, 15, 18)        # --bg
LINE = (38, 43, 51)      # --line
TEXT = (244, 245, 247)   # --text
DIM = (198, 204, 212)    # --dim
MUTED = (139, 148, 161)  # --muted
ACCENT = (216, 184, 128) # --accent


def font(size, weight):
    f = ImageFont.truetype(FONT, size)
    f.set_variation_by_axes([weight])
    return f


img = Image.new("RGB", (W, H), DESK)

# Soft warm glow behind the device (cheap radial approximation).
glow = Image.new("L", (W, H), 0)
gd = ImageDraw.Draw(glow)
for i, alpha in enumerate(range(18, 0, -2)):
    pad = 40 + i * 28
    gd.rounded_rectangle([pad, pad, W - pad, H - pad], radius=60, fill=alpha)
img = Image.composite(Image.new("RGB", (W, H), ACCENT), img, glow)
d = ImageDraw.Draw(img)

# The device "screen": rounded panel with the site's border color.
M = 56
d.rounded_rectangle([M, M, W - M, H - M], radius=28, fill=BG, outline=LINE, width=3)

# Status bar, matching the site's device chrome (clock left, battery right).
d.text((M + 44, M + 36), "12:04", font=font(26, 600), fill=MUTED)
d.text((W - M - 44, M + 36), "100%", font=font(26, 600), fill=MUTED, anchor="ra")
d.line([M + 44, M + 92, W - M - 44, M + 92], fill=LINE, width=2)

# Wordmark with accent period.
wm_font = font(120, 700)
wm = "Later.Ink"
wm_w = d.textlength(wm, font=wm_font)
x0 = (W - wm_w) // 2
y0 = 224
d.text((x0, y0), wm, font=wm_font, fill=TEXT)
dot_x = x0 + d.textlength("Later", font=wm_font)
d.text((dot_x, y0), ".", font=wm_font, fill=ACCENT)

# Tagline.
d.text((W // 2, 404), "Your read-later queue, on e-ink.", font=font(44, 450), fill=DIM, anchor="ma")


def draw_arrow(x, cy):
    """Small right-arrow: shaft + head, MUTED, ~30px wide starting at x."""
    d.line([x, cy, x + 18, cy], fill=MUTED, width=3)
    d.polygon([(x + 18, cy - 7), (x + 30, cy), (x + 18, cy + 7)], fill=MUTED)
    return 30


# Pipeline: text segments with drawn arrows between them.
pipe_font = font(30, 450)
segs = ["Readwise Reader / Wallabag", "OPDS", "any e-reader"]
GAP = 22
widths = [d.textlength(s, font=pipe_font) for s in segs]
total = sum(widths) + (len(segs) - 1) * (GAP * 2 + 30)
x = (W - total) // 2
top = 492
cy = top + 15
for i, (s, w) in enumerate(zip(segs, widths)):
    d.text((x, top), s, font=pipe_font, fill=MUTED)
    x += w
    if i < len(segs) - 1:
        x += GAP
        x += draw_arrow(x, cy)
        x += GAP

img.save(OUT, "PNG", optimize=True)
print(OUT)
