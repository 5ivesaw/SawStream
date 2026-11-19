import io
import urllib.request
from PIL import Image, ImageEnhance
from rich.style import Style
from rich.text import Text

from sawstream.storage import THUMB_CACHE, ensure_dirs

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) SawStream/2.0"}


def idle_artwork() -> Text:
    art = (
        "\n\n"
        "     ╔════════════════════════╗\n"
        "     ║                        ║\n"
        "     ║     S A W S T R E A M  ║\n"
        "     ║     ─────────────────  ║\n"
        "     ║      IDLE  ·  READY    ║\n"
        "     ║                        ║\n"
        "     ║   Select a track or    ║\n"
        "     ║   paste any URL/link   ║\n"
        "     ║                        ║\n"
        "     ╚════════════════════════╝\n"
    )
    return Text(art, style="bold #ff2e43")


def render_thumbnail(url: str, vid_id: str = "", cols: int = 36, rows: int = 16) -> Text:
    """Loads cached or remote thumbnail in memory and renders crisp 24-bit Truecolor half-blocks (▀)."""
    ensure_dirs()
    cache_path = THUMB_CACHE / f"{vid_id}.jpg" if vid_id else None
    raw = None

    if cache_path and cache_path.exists():
        try:
            raw = cache_path.read_bytes()
        except OSError:
            raw = None

    if raw is None and url:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=6) as resp:
                raw = resp.read()
            if cache_path and raw:
                try:
                    cache_path.write_bytes(raw)
                except OSError:
                    pass
        except Exception:
            raw = None

    if not raw:
        return idle_artwork()

    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        # Crop black letterbox bars common on 4:3 hqdefault YouTube thumbnails
        w, h = img.size
        if w == 480 and h == 360:
            img = img.crop((0, 45, 480, 315))
        # Subtle punch so artwork pops against pitch-black #000000
        img = ImageEnhance.Contrast(img).enhance(1.12)
        img = ImageEnhance.Color(img).enhance(1.15)
        img = ImageEnhance.Sharpness(img).enhance(1.25)

        img = img.resize((cols, rows * 2), Image.Resampling.LANCZOS)
        pixels = img.load()
        out = Text()
        for y in range(0, rows * 2, 2):
            for x in range(cols):
                r1, g1, b1 = pixels[x, y]
                r2, g2, b2 = pixels[x, y + 1]
                out.append(
                    "▀",
                    style=Style(
                        color=f"rgb({r1},{g1},{b1})",
                        bgcolor=f"rgb({r2},{g2},{b2})",
                    ),
                )
            if y < rows * 2 - 2:
                out.append("\n")
        return out
    except Exception:
        return Text("\n" * 6 + "     [ THUMBNAIL UNAVAILABLE ]", style="bold #ff2e43")
