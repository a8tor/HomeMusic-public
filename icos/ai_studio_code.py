from __future__ import annotations

import io
from pathlib import Path

import cairosvg
from PIL import Image


BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR.parent / "web" / "public"
ICONS_DIR = PUBLIC_DIR / "icons"


def svg_to_pil(svg_path: Path, size: int) -> Image.Image:
    png_bytes = cairosvg.svg2png(
        url=str(svg_path), output_width=size, output_height=size
    )
    with Image.open(io.BytesIO(png_bytes)) as image:
        return image.convert("RGBA")


def save_png(image: Image.Image, filename: str) -> None:
    image.save(ICONS_DIR / filename, format="PNG")


ICONS_DIR.mkdir(parents=True, exist_ok=True)

# Keep all standard PWA icons on the black-backed, no-white favicon artwork.
favicon = BASE_DIR / "favicon.svg"
for size in (192, 512):
    save_png(svg_to_pil(favicon, size), f"icon-{size}.png")

# ICO contains the three requested embedded resolutions.
ico = svg_to_pil(favicon, 48)
ico.save(PUBLIC_DIR / "favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])

# The source includes a full-bleed black rectangle; preserve it at both sizes.
maskable = BASE_DIR / "maskable.svg"
for size in (192, 512):
    save_png(svg_to_pil(maskable, size), f"maskable-{size}.png")

# Flatten transparent SVG edges onto black and emit an RGB-only Apple icon.
apple_rgba = svg_to_pil(BASE_DIR / "apple-touch.svg", 180)
alpha = apple_rgba.getchannel("A")
apple_rgb = Image.new("RGB", apple_rgba.size, "black")
apple_rgb.paste(apple_rgba, mask=alpha)
save_png(apple_rgb, "apple-touch-icon.png")

# Badge remains a white silhouette with transparent surroundings.
save_png(svg_to_pil(BASE_DIR / "badge-mono.svg", 96), "badge-96.png")

# Report near-white share over visible pixels, excluding transparent padding.
for path in sorted(ICONS_DIR.glob("*.png")):
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        pixels = list(rgba.getdata())
        visible_pixels = [pixel for pixel in pixels if pixel[3] > 0]
        white_pixels = sum(
            1
            for red, green, blue, _alpha in visible_pixels
            if red >= 245 and green >= 245 and blue >= 245
        )
        white_fraction = white_pixels / max(1, len(visible_pixels))
        print(
            f"{path.name}: size={image.size}, mode={image.mode}, "
            f"near_white_visible={white_fraction:.4%}"
        )

with Image.open(PUBLIC_DIR / "favicon.ico") as image:
    print(f"favicon.ico: sizes={image.info.get('sizes')}")