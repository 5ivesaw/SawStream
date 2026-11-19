import csv
import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

import yt_dlp

from sawstream.storage import extract_vid_id, fmt_dur, normalize_url, sanitize_filename

SCRAPE_DIR = Path.home() / "Downloads" / "Scrapes"


def normalize_channel_target(raw: str) -> tuple[str, str]:
    """
    Returns (kind, url):
      - ("playlist", url) if user pasted a direct playlist URL
      - ("channel", base_url) for @handle, UC... ID, channel URL, or video URL
    """
    s = (raw or "").strip().rstrip("/")
    if not s:
        return "invalid", ""
    if "list=" in s and ("playlist?" in s or "watch?" in s) and "/@" not in s and "/channel/" not in s:
        return "playlist", normalize_url(s)
    if s.startswith("@"):
        return "channel", f"https://www.youtube.com/{s}"
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", s):
        return "channel", f"https://www.youtube.com/channel/{s}"
    if not s.startswith(("http://", "https://", "www.", "youtube.com/", "youtu.be/")):
        return "channel", f"https://www.youtube.com/@{s.lstrip('@')}"

    s = normalize_url(s)
    s = re.sub(r"/(videos|shorts|streams|playlists|releases|featured|about|community|live)$", "", s, flags=re.I)

    # Video URL -> resolve parent channel automatically
    if "watch?v=" in s or "youtu.be/" in s or "/shorts/" in s:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 10}) as ydl:
            info = ydl.extract_info(s, download=False)
        ch_url = (info or {}).get("channel_url") or (info or {}).get("uploader_url")
        if ch_url:
            return "channel", ch_url.rstrip("/")
    return "channel", s


def _extract_flat(url: str, max_items: int = 200):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "ignoreerrors": True,
        "socket_timeout": 10,
        "playlistend": max_items,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _flatten_playlist_stubs(entries) -> list[dict]:
    out = []
    for e in entries or []:
        if not e:
            continue
        if e.get("entries") and not str(e.get("id", "")).startswith(("PL", "OL", "RD", "UU")):
            out.extend(_flatten_playlist_stubs(e["entries"]))
        else:
            out.append(e)
    return out


def scrape_channel_full(
    raw_input: str,
    playlists_only: bool = False,
    progress_cb: Callable[[str], None] | None = None,
    item_cb: Callable[[dict], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict:
    """
    Scrapes public playlists (and optionally videos/shorts/streams/releases) with live streaming callbacks
    so items appear in the table immediately instead of hanging.
    """
    kind, target_url = normalize_channel_target(raw_input)
    if kind == "invalid":
        raise ValueError("Enter a @handle, Channel URL, or Playlist URL.")

    def emit(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    playlists: list[dict] = []
    items: list[dict] = []
    seen_ids: set[str] = set()

    # Direct playlist URL mode
    if kind == "playlist":
        emit(f"LOADING PLAYLIST: {target_url}")
        info = _extract_flat(target_url, max_items=300) or {}
        ptitle = info.get("title") or "Playlist"
        cname = info.get("channel") or info.get("uploader") or ptitle
        pvids = []
        for v in info.get("entries") or []:
            if cancel_event and cancel_event.is_set():
                break
            if not v:
                continue
            vid = v.get("id") or extract_vid_id(v.get("url") or "")
            if not vid:
                continue
            rec = {
                "section": "PLAYLISTS",
                "playlist": ptitle,
                "id": vid,
                "title": v.get("title") or "Unknown",
                "author": v.get("uploader") or v.get("channel") or cname,
                "duration": v.get("duration") or 0,
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "webpage_url": f"https://www.youtube.com/watch?v={vid}",
            }
            pvids.append(rec)
            items.append(rec)
            seen_ids.add(vid)
            if item_cb:
                item_cb(rec)
        playlists.append({"section": "PLAYLISTS", "id": info.get("id") or "", "title": ptitle, "url": target_url, "video_count": len(pvids), "videos": pvids})
        return {
            "channel_name": cname,
            "channel_url": target_url,
            "scraped_at": int(time.time()),
            "playlist_count": len(playlists),
            "unique_video_count": len(seen_ids),
            "playlists": playlists,
            "items": items,
        }

    # Channel scrape mode
    tabs = [("PLAYLISTS", "/playlists"), ("RELEASES", "/releases")]
    if not playlists_only:
        tabs += [("VIDEOS", "/videos"), ("SHORTS", "/shorts"), ("STREAMS", "/streams")]

    channel_name = target_url.rsplit("/", 1)[-1].lstrip("@") or "channel"
    for sec, suf in tabs:
        if cancel_event and cancel_event.is_set():
            break
        tab_url = f"{target_url}{suf}"
        emit(f"SCANNING {sec}: {tab_url}")
        try:
            info = _extract_flat(tab_url, max_items=150)
        except Exception:
            info = None
        if not info:
            continue

        channel_name = info.get("channel") or info.get("uploader") or channel_name
        entries = info.get("entries") or []
        if not entries:
            continue

        if sec in ("PLAYLISTS", "RELEASES"):
            stubs = _flatten_playlist_stubs(entries)
            for idx, pl in enumerate(stubs, 1):
                if cancel_event and cancel_event.is_set():
                    break
                pid = pl.get("id") or ""
                ptitle = pl.get("title") or f"Playlist {idx}"
                purl = pl.get("url") or (f"https://www.youtube.com/playlist?list={pid}" if pid else "")
                if not purl:
                    continue
                if not purl.startswith("http") and pid:
                    purl = f"https://www.youtube.com/playlist?list={pid}"

                emit(f"EXPANDING {sec} ({idx}/{len(stubs)}): {ptitle[:38]}...")
                try:
                    pinfo = _extract_flat(purl, max_items=200) or {}
                except Exception:
                    pinfo = {}
                pvids = []
                for v in pinfo.get("entries") or []:
                    if cancel_event and cancel_event.is_set():
                        break
                    if not v:
                        continue
                    vid = v.get("id") or extract_vid_id(v.get("url") or "")
                    if not vid:
                        continue
                    rec = {
                        "section": sec,
                        "playlist": ptitle,
                        "id": vid,
                        "title": v.get("title") or "Unknown",
                        "author": v.get("uploader") or v.get("channel") or channel_name,
                        "duration": v.get("duration") or 0,
                        "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                        "webpage_url": f"https://www.youtube.com/watch?v={vid}",
                    }
                    pvids.append(rec)
                    items.append(rec)
                    seen_ids.add(vid)
                    if item_cb:
                        item_cb(rec)
                playlists.append({"section": sec, "id": pid, "title": ptitle, "url": purl, "video_count": len(pvids), "videos": pvids})
        else:
            for v in entries:
                if cancel_event and cancel_event.is_set():
                    break
                if not v:
                    continue
                vid = v.get("id") or extract_vid_id(v.get("url") or "")
                if not vid:
                    continue
                rec = {
                    "section": sec,
                    "playlist": "",
                    "id": vid,
                    "title": v.get("title") or "Unknown",
                    "author": v.get("uploader") or v.get("channel") or channel_name,
                    "duration": v.get("duration") or 0,
                    "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                    "webpage_url": f"https://www.youtube.com/watch?v={vid}",
                }
                items.append(rec)
                seen_ids.add(vid)
                if item_cb:
                    item_cb(rec)

    return {
        "channel_name": channel_name,
        "channel_url": target_url,
        "scraped_at": int(time.time()),
        "playlist_count": len(playlists),
        "unique_video_count": len(seen_ids),
        "playlists": playlists,
        "items": items,
    }


def export_channel_scrape(data: dict) -> Path:
    dest = SCRAPE_DIR / sanitize_filename(data.get("channel_name") or "channel")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "channel_full.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    (dest / "playlists.json").write_text(json.dumps(data.get("playlists", []), indent=2, ensure_ascii=False), encoding="utf-8")
    with (dest / "videos.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["section", "playlist", "id", "title", "duration", "url"])
        for it in data.get("items", []):
            w.writerow([it["section"], it["playlist"], it["id"], it["title"], fmt_dur(it["duration"]), it["webpage_url"]])
    (dest / "urls.txt").write_text("\n".join(dict.fromkeys(it["webpage_url"] for it in data.get("items", []))) + "\n", encoding="utf-8")
    return dest


def archive_channel_media(data: dict, audio_only: bool = True, progress_cb=None, cancel_event=None):
    dest = export_channel_scrape(data)
    arch = str(dest / "archive.txt")
    done, failed, seen = 0, 0, set()
    jobs = [(f"Playlists/{sanitize_filename(p['title'])}/{i:03d} - ", v) for p in data.get("playlists", []) for i, v in enumerate(p.get("videos", []), 1)]
    jobs += [(f"{it['section'].title()}/", it) for it in data.get("items", []) if it.get("section") not in ("PLAYLISTS", "RELEASES")]
    uniq = [(p, v) for p, v in jobs if v["id"] not in seen and not seen.add(v["id"])]
    for i, (pref, v) in enumerate(uniq, 1):
        if cancel_event and cancel_event.is_set():
            break
        if progress_cb:
            progress_cb(f"ARCHIVING [{i}/{len(uniq)}]: {v['title'][:42]}")
        opts = {
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "download_archive": arch,
            "noplaylist": True,
            "socket_timeout": 15,
            "outtmpl": str(dest / f"{pref}%(title).95s [%(id)s].%(ext)s"),
            "format": "bestaudio/best" if audio_only else "bestvideo+bestaudio/best",
        }
        if audio_only:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}]
        else:
            opts["merge_output_format"] = "mkv"
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                if y.download([v["webpage_url"]]) == 0:
                    done += 1
                else:
                    failed += 1
        except Exception:
            failed += 1
    return dest, done, failed


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("Usage: sawscrape <@handle|url> [--playlists-only] [--audio|--video]")
    d = scrape_channel_full(
        sys.argv[1],
        playlists_only="--playlists-only" in sys.argv,
        progress_cb=lambda m: print(f"[SawScrape] {m}"),
    )
    p = export_channel_scrape(d)
    print(f"✔ {d['channel_name']}: {d['playlist_count']} playlists, {d['unique_video_count']} videos -> {p}")
    if "--audio" in sys.argv or "--video" in sys.argv:
        archive_channel_media(d, audio_only="--audio" in sys.argv, progress_cb=lambda m: print(f"  {m}"))
