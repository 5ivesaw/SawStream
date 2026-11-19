import csv, json, re, time
from pathlib import Path
import yt_dlp
from sawstream.storage import fmt_dur, sanitize_filename

SCRAPE_DIR = Path.home() / "Downloads" / "Scrapes"
TABS = [("PLAYLISTS", "/playlists"), ("VIDEOS", "/videos"), ("SHORTS", "/shorts"), ("STREAMS", "/streams"), ("RELEASES", "/releases")]

def norm_url(raw: str) -> str:
    s = raw.strip().rstrip("/")
    if s.startswith("@"): return f"https://www.youtube.com/{s}"
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", s): return f"https://www.youtube.com/channel/{s}"
    if not s.startswith(("http://", "https://")): return f"https://www.youtube.com/@{s}"
    return re.sub(r"/(videos|shorts|streams|playlists|releases|featured|about|live)$", "", s, flags=re.I)

def _flat(url: str):
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True, "ignoreerrors": True}) as y:
            return y.extract_info(url, download=False)
    except Exception:
        return None

def _flatten_playlists(entries):
    out = []
    for e in entries or []:
        if not e: continue
        if e.get("entries") and not str(e.get("id", "")).startswith(("PL", "OL", "RD", "UU")):
            out.extend(_flatten_playlists(e["entries"]))
        else:
            out.append(e)
    return out

def scrape_channel_full(raw_input: str, progress_cb=None, cancel_event=None, expand_playlists=True) -> dict:
    base = norm_url(raw_input)
    name = base.rsplit("/", 1)[-1].lstrip("@") or "channel"
    playlists, items, seen = [], [], set()
    for sec, suf in TABS:
        if cancel_event and cancel_event.is_set(): break
        if progress_cb: progress_cb(f"SCANNING {sec}: {base}{suf}")
        info = _flat(base + suf)
        if not info: continue
        name = info.get("channel") or info.get("uploader") or name
        entries = info.get("entries") or []
        if sec in ("PLAYLISTS", "RELEASES"):
            pls = _flatten_playlists(entries)
            for i, pl in enumerate(pls, 1):
                if cancel_event and cancel_event.is_set(): break
                pid, ptitle = pl.get("id") or "", pl.get("title") or f"Playlist {i}"
                purl = pl.get("url") or (f"https://www.youtube.com/playlist?list={pid}" if pid else "")
                if not purl: continue
                pvids = []
                if expand_playlists:
                    if progress_cb: progress_cb(f"PLAYLIST ({i}/{len(pls)}): {ptitle[:40]}")
                    pinfo = _flat(purl) or {}
                    for v in pinfo.get("entries") or []:
                        if not v or not v.get("id"): continue
                        vid = v["id"]
                        rec = {"section": sec, "playlist": ptitle, "id": vid, "title": v.get("title") or "Unknown",
                               "author": v.get("uploader") or v.get("channel") or name, "duration": v.get("duration") or 0,
                               "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "webpage_url": f"https://www.youtube.com/watch?v={vid}"}
                        pvids.append(rec); items.append(rec); seen.add(vid)
                playlists.append({"section": sec, "id": pid, "title": ptitle, "url": purl, "video_count": len(pvids), "videos": pvids})
        else:
            for v in entries:
                if not v or not v.get("id"): continue
                vid = v["id"]
                items.append({"section": sec, "playlist": "", "id": vid, "title": v.get("title") or "Unknown",
                              "author": v.get("uploader") or v.get("channel") or name, "duration": v.get("duration") or 0,
                              "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "webpage_url": f"https://www.youtube.com/watch?v={vid}"})
                seen.add(vid)
    return {"channel_name": name, "channel_url": base, "scraped_at": int(time.time()), "playlist_count": len(playlists),
            "unique_video_count": len(seen), "playlists": playlists, "items": items}

def export_channel_scrape(data: dict) -> Path:
    dest = SCRAPE_DIR / sanitize_filename(data["channel_name"])
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "channel_full.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
    (dest / "playlists.json").write_text(json.dumps(data["playlists"], indent=2, ensure_ascii=False))
    with (dest / "videos.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["section", "playlist", "id", "title", "duration", "url"])
        for it in data["items"]: w.writerow([it["section"], it["playlist"], it["id"], it["title"], fmt_dur(it["duration"]), it["webpage_url"]])
    (dest / "urls.txt").write_text("\n".join(dict.fromkeys(it["webpage_url"] for it in data["items"])) + "\n")
    return dest

def archive_channel_media(data: dict, audio_only=True, progress_cb=None, cancel_event=None):
    dest = export_channel_scrape(data)
    arch, done, failed, seen = str(dest / "archive.txt"), 0, 0, set()
    jobs = [(f"Playlists/{sanitize_filename(p['title'])}/{i:03d} - ", v) for p in data["playlists"] for i, v in enumerate(p["videos"], 1)]
    jobs += [(f"{it['section'].title()}/", it) for it in data["items"] if it["section"] not in ("PLAYLISTS", "RELEASES")]
    uniq = [(p, v) for p, v in jobs if v["id"] not in seen and not seen.add(v["id"])]
    for i, (pref, v) in enumerate(uniq, 1):
        if cancel_event and cancel_event.is_set(): break
        if progress_cb: progress_cb(f"ARCHIVING [{i}/{len(uniq)}]: {v['title'][:42]}")
        opts = {"quiet": True, "no_warnings": True, "ignoreerrors": True, "download_archive": arch, "noplaylist": True,
                "outtmpl": str(dest / f"{pref}%(title).95s [%(id)s].%(ext)s"),
                "format": "bestaudio/best" if audio_only else "bestvideo+bestaudio/best"}
        if audio_only: opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}]
        else: opts["merge_output_format"] = "mkv"
        try:
            with yt_dlp.YoutubeDL(opts) as y: done += (y.download([v["webpage_url"]]) == 0)
        except Exception: failed += 1
    return dest, done, failed

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2: sys.exit("Usage: sawscrape <@handle|url> [--audio|--video]")
    d = scrape_channel_full(sys.argv[1], progress_cb=lambda m: print(f"[SawScrape] {m}"))
    p = export_channel_scrape(d)
    print(f"✔ {d['channel_name']}: {d['playlist_count']} playlists, {d['unique_video_count']} videos -> {p}")
    if "--audio" in sys.argv or "--video" in sys.argv:
        archive_channel_media(d, audio_only="--audio" in sys.argv, progress_cb=lambda m: print(f"  {m}"))
