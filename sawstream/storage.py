import glob
import json
import os
import re
import shutil
import time
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "sawstream"
CACHE_DIR = Path.home() / ".cache" / "sawstream"
THUMB_CACHE = CACHE_DIR / "thumbs"
PARTIAL_DIR = CACHE_DIR / "partial"

MUSIC_DIR = Path.home() / "Music"
VIDEO_DIR = Path.home() / "Downloads"
ANIME_DIR = Path.home() / "Videos" / "Anime"
MOVIE_DIR = Path.home() / "Videos" / "Movies"

SETTINGS_FILE = CONFIG_DIR / "settings.json"
FAVORITES_FILE = CONFIG_DIR / "favorites.json"
HISTORY_FILE = CONFIG_DIR / "history.json"

AUDIO_EXTS = (".mka", ".mp3", ".m4a", ".opus", ".webm", ".ogg", ".flac", ".wav")
VIDEO_EXTS = (".mkv", ".mp4", ".webm", ".mov", ".avi")
ALL_MEDIA_EXTS = AUDIO_EXTS + VIDEO_EXTS

DEFAULT_SETTINGS = {
    "auto_save_music": True,
    "auto_save_video": False,
    "auto_save_anime": False,
    "auto_save_movies": False,
    "volume": 100,
    "repeat_mode": "OFF",
}


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, CACHE_DIR, THUMB_CACHE, PARTIAL_DIR, MUSIC_DIR, VIDEO_DIR, ANIME_DIR, MOVIE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default.copy() if isinstance(default, (dict, list)) else default


def save_json(path: Path, data) -> None:
    ensure_dirs()
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def load_settings() -> dict:
    ensure_dirs()
    cfg = DEFAULT_SETTINGS.copy()
    cfg.update(load_json(SETTINGS_FILE, {}))
    return cfg


def save_settings(cfg: dict) -> None:
    save_json(SETTINGS_FILE, cfg)


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|\r\n\t]+', " ", name or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:92] or "media"


def extract_vid_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else ""


def is_url(text: str) -> bool:
    s = (text or "").strip().lower()
    return s.startswith(("http://", "https://", "www.", "youtube.com/", "youtu.be/", "music.youtube.com/", "open.spotify.com/"))


def normalize_url(text: str) -> str:
    s = (text or "").strip()
    if s and not s.startswith(("http://", "https://")):
        return "https://" + s
    return s


def fmt_dur(sec) -> str:
    try:
        s = int(float(sec))
        if s <= 0:
            return "0:00"
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except Exception:
        return "0:00"


def fmt_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes} B"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


def find_offline_file(directory: Path, vid_id: str = "", exact_title: str = "") -> str | None:
    """
    Strictly matches offline files so random local files are NEVER accidentally played:
    - For YouTube/Music: matches ONLY exact `[11-char-ID]` tag in filename.
    - For Anime/Movies (no vid_id): matches ONLY exact sanitized filename stem.
    """
    if not directory.exists():
        return None
    if vid_id:
        needle = f"[{vid_id}]"
        for p in directory.iterdir():
            if p.is_file() and p.suffix.lower() in ALL_MEDIA_EXTS and needle in p.name:
                if p.stat().st_size > 40_000:
                    return str(p)
        return None

    if exact_title:
        target = sanitize_filename(exact_title).lower()
        if len(target) >= 4:
            for p in directory.iterdir():
                if p.is_file() and p.suffix.lower() in ALL_MEDIA_EXTS:
                    stem = re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]$", "", p.stem).strip().lower()
                    if stem == target and p.stat().st_size > 40_000:
                        return str(p)
    return None


def make_partial_record_path(title: str, vid_id: str = "") -> tuple[str, str]:
    ensure_dirs()
    safe = sanitize_filename(title)
    tag = f" [{vid_id}]" if vid_id else ""
    filename = f"{safe}{tag}.mka"
    return str(PARTIAL_DIR / f".rec_{os.getpid()}_{filename}"), str(MUSIC_DIR / filename)


def commit_partial_recording(partial_path: str, final_path: str, completed: bool) -> bool:
    if not partial_path or not os.path.exists(partial_path):
        return False
    try:
        size = os.path.getsize(partial_path)
        if completed and size > 40_000:
            shutil.move(partial_path, final_path)
            return True
        os.remove(partial_path)
    except OSError:
        pass
    return False


def cleanup_stale_partials() -> None:
    if not PARTIAL_DIR.exists():
        return
    for p in PARTIAL_DIR.iterdir():
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass


def scan_offline_library() -> list[dict]:
    ensure_dirs()
    sources = [
        ("MUSIC", MUSIC_DIR, AUDIO_EXTS),
        ("VIDEO", VIDEO_DIR, VIDEO_EXTS),
        ("ANIME", ANIME_DIR, VIDEO_EXTS),
        ("MOVIE", MOVIE_DIR, VIDEO_EXTS),
    ]
    items = []
    for kind, folder, exts in sources:
        if not folder.exists():
            continue
        for p in sorted(folder.iterdir(), key=lambda x: x.stat().st_mtime if x.is_file() else 0, reverse=True):
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            size = p.stat().st_size
            if size < 40_000:
                continue
            m = re.search(r"\[([A-Za-z0-9_-]{11})\]", p.name)
            vid = m.group(1) if m else ""
            clean = re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]$", "", p.stem)
            items.append({
                "kind": kind,
                "id": vid,
                "title": clean,
                "author": f"Offline ({kind.title()})",
                "duration": 0,
                "size": fmt_size(size),
                "thumbnail": "",
                "webpage_url": "",
                "local_path": str(p),
            })
    return items


def load_favorites() -> list[dict]:
    return load_json(FAVORITES_FILE, [])


def toggle_favorite(track: dict) -> bool:
    favs = load_favorites()
    key = track.get("id") or track.get("webpage_url") or track.get("local_path") or track.get("title")
    for i, f in enumerate(favs):
        fkey = f.get("id") or f.get("webpage_url") or f.get("local_path") or f.get("title")
        if fkey == key:
            favs.pop(i)
            save_json(FAVORITES_FILE, favs)
            return False
    favs.insert(0, {
        "id": track.get("id", ""),
        "title": track.get("title", "Unknown"),
        "author": track.get("author", "Unknown"),
        "duration": track.get("duration", 0),
        "thumbnail": track.get("thumbnail", ""),
        "webpage_url": track.get("webpage_url", ""),
        "local_path": track.get("local_path", ""),
    })
    save_json(FAVORITES_FILE, favs[:250])
    return True


def record_history(track: dict) -> None:
    hist = load_json(HISTORY_FILE, [])
    entry = {
        "id": track.get("id", ""),
        "title": track.get("title", "Unknown"),
        "author": track.get("author", "Unknown"),
        "duration": track.get("duration", 0),
        "thumbnail": track.get("thumbnail", ""),
        "webpage_url": track.get("webpage_url", ""),
        "local_path": track.get("local_path", ""),
        "played_at": int(time.time()),
    }
    key = entry["id"] or entry["webpage_url"] or entry["local_path"] or entry["title"]
    hist = [h for h in hist if (h.get("id") or h.get("webpage_url") or h.get("local_path") or h.get("title")) != key]
    hist.insert(0, entry)
    save_json(HISTORY_FILE, hist[:100])
