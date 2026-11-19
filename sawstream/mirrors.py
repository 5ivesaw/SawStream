import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import yt_dlp

from sawstream.storage import extract_vid_id, fmt_dur

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) SawStream/2.0"}

PIPED_MIRRORS = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://api.piped.private.coffee",
    "https://pipedapi.leptons.xyz",
    "https://pipedapi.darkness.services",
]

INVIDIOUS_MIRRORS = [
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
    "https://yewtu.be",
    "https://invidious.privacyredirect.com",
]

YTS_MIRRORS = [
    "https://yts.mx/api/v2",
    "https://yts.lt/api/v2",
    "https://yts.am/api/v2",
    "https://yts.ag/api/v2",
]


def http_json(url: str, data: bytes | None = None, timeout: int = 5):
    req = urllib.request.Request(url, data=data, headers=HEADERS)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def parse_spotify_url(url: str) -> list[dict]:
    """Extracts tracks from any Spotify track/album/playlist URL with ZERO sign-in or API keys."""
    clean_url = url.split("?")[0]
    embed_url = re.sub(
        r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/",
        r"open.spotify.com/embed/\1/",
        clean_url,
    )
    tracks = []
    try:
        req = urllib.request.Request(embed_url, headers=HEADERS)
        html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if m:
            data = json.loads(m.group(1))
            entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
            cover = ""
            imgs = (entity.get("coverArt") or {}).get("sources") or (entity.get("visualIdentity") or {}).get("image") or []
            if imgs:
                cover = imgs[0].get("url", "")
            if "trackList" in entity:
                for t in entity["trackList"]:
                    title = t.get("title", "Unknown")
                    artist = t.get("subtitle", "Spotify")
                    dur = int((t.get("duration") or 0) / 1000)
                    tracks.append({
                        "id": "",
                        "title": title,
                        "author": artist,
                        "duration": dur,
                        "thumbnail": cover,
                        "webpage_url": f"ytsearch1:{title} {artist} audio",
                    })
            elif entity.get("name") or entity.get("title"):
                title = entity.get("name") or entity.get("title")
                artists = ", ".join(a.get("name", "") for a in entity.get("artists", [])) or entity.get("subtitle", "Spotify")
                dur = int((entity.get("duration") or 0) / 1000)
                tracks.append({
                    "id": "",
                    "title": title,
                    "author": artists,
                    "duration": dur,
                    "thumbnail": cover,
                    "webpage_url": f"ytsearch1:{title} {artists} audio",
                })
    except Exception:
        pass

    if not tracks:
        try:
            oembed = http_json(f"https://open.spotify.com/oembed?url={urllib.parse.quote(clean_url)}", timeout=6)
            title = oembed.get("title", "")
            if title:
                tracks.append({
                    "id": "",
                    "title": title,
                    "author": "Spotify",
                    "duration": 0,
                    "thumbnail": oembed.get("thumbnail_url", ""),
                    "webpage_url": f"ytsearch1:{title} audio",
                })
        except Exception:
            pass
    return tracks


def extract_media_in_process(url: str) -> list[dict]:
    """Extracts a single URL, full YouTube playlist, or Spotify link completely in-process."""
    if "open.spotify.com" in url:
        return parse_spotify_url(url)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return []

    entries = info.get("entries")
    if entries:
        out = []
        for e in entries:
            if not e:
                continue
            vid = e.get("id") or extract_vid_id(e.get("url") or "")
            web_url = e.get("webpage_url") or e.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
            if not web_url:
                continue
            out.append({
                "id": vid,
                "title": e.get("title") or "Unknown Track",
                "author": e.get("uploader") or e.get("channel") or "YouTube",
                "duration": e.get("duration") or 0,
                "thumbnail": e.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ""),
                "webpage_url": web_url,
            })
        return out

    vid = info.get("id") or extract_vid_id(url)
    return [{
        "id": vid,
        "title": info.get("title") or "Unknown Track",
        "author": info.get("uploader") or info.get("channel") or "YouTube",
        "duration": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ""),
        "webpage_url": info.get("webpage_url") or url,
    }]


def resolve_audio_stream(webpage_url: str) -> dict | None:
    """Resolves a direct bestaudio stream URL + full metadata via yt_dlp Python API."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "noplaylist": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(webpage_url, download=False)
    if not info:
        return None
    if "entries" in info and info["entries"]:
        info = info["entries"][0]
    if not info:
        return None
    vid = info.get("id") or extract_vid_id(webpage_url)
    return {
        "id": vid,
        "stream_url": info.get("url"),
        "title": info.get("title") or "Unknown Track",
        "author": info.get("uploader") or info.get("channel") or "Unknown",
        "duration": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ""),
        "webpage_url": info.get("webpage_url") or webpage_url,
    }


def fetch_yt_feed(query: str = "", music_only: bool = False):
    for base in PIPED_MIRRORS:
        try:
            if query:
                filt = "music_songs" if music_only else "videos"
                items = http_json(f"{base}/search?q={urllib.parse.quote(query)}&filter={filt}").get("items", [])
            else:
                items = http_json(f"{base}/trending?region=US")
            out = []
            for it in items[:25]:
                vid = it.get("url", "").split("v=")[-1].split("/")[-1]
                if not vid or len(vid) < 5:
                    continue
                out.append((
                    it.get("title", "Unknown")[:60],
                    it.get("uploaderName", "YouTube")[:24],
                    fmt_dur(it.get("duration", 0)),
                    f"https://www.youtube.com/watch?v={vid}",
                ))
            if out:
                return out, f"PIPED ({base.split('//')[1]})"
        except Exception:
            continue

    for base in INVIDIOUS_MIRRORS:
        try:
            if query:
                url = f"{base}/api/v1/search?q={urllib.parse.quote(query)}&type=video"
            else:
                url = f"{base}/api/v1/trending?region=US{'&type=Music' if music_only else ''}"
            items = http_json(url)
            out = []
            for it in items[:25]:
                vid = it.get("videoId")
                if not vid:
                    continue
                out.append((
                    it.get("title", "Unknown")[:60],
                    it.get("author", "YouTube")[:24],
                    fmt_dur(it.get("lengthSeconds", 0)),
                    f"https://www.youtube.com/watch?v={vid}",
                ))
            if out:
                return out, f"INVIDIOUS ({base.split('//')[1]})"
        except Exception:
            continue

    q = query if query else ("top music hits 2026" if music_only else "trending videos today")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}) as ydl:
            res = ydl.extract_info(f"ytsearch18:{q}", download=False)
        out = []
        for d in (res.get("entries") or []):
            vid = d.get("id")
            if vid:
                out.append((
                    (d.get("title") or "Unknown")[:60],
                    (d.get("channel") or d.get("uploader") or "YouTube")[:24],
                    fmt_dur(d.get("duration", 0)),
                    f"https://www.youtube.com/watch?v={vid}",
                ))
        if out:
            return out, "YT-DLP IN-PROCESS"
    except Exception:
        pass
    return [], "ALL MIRRORS FAILED"


def fetch_anime_feed(query: str = ""):
    try:
        if query:
            gql = """query ($s: String) { Page(perPage: 25) { media(search: $s, type: ANIME, sort: POPULARITY_DESC) { title { romaji english } episodes averageScore status } } }"""
            payload = json.dumps({"query": gql, "variables": {"s": query}}).encode()
        else:
            gql = """query { Page(perPage: 25) { media(type: ANIME, sort: TRENDING_DESC) { title { romaji english } episodes averageScore status } } }"""
            payload = json.dumps({"query": gql}).encode()
        raw = http_json("https://graphql.anilist.co", data=payload)
        out = []
        for it in raw["data"]["Page"]["media"]:
            t = it["title"].get("english") or it["title"].get("romaji") or "Unknown"
            out.append((
                t[:56],
                str(it.get("episodes") or "?"),
                f"{it.get('averageScore')}%" if it.get("averageScore") else "--",
                it.get("status") or "UNKNOWN",
                t,
            ))
        if out:
            return out, "ANILIST GRAPHQL"
    except Exception:
        pass

    try:
        url = f"https://api.jikan.moe/v4/anime?q={urllib.parse.quote(query)}&limit=25" if query else "https://api.jikan.moe/v4/top/anime?filter=airing&limit=25"
        out = []
        for it in http_json(url).get("data", []):
            t = it.get("title_english") or it.get("title") or "Unknown"
            out.append((
                t[:56],
                str(it.get("episodes") or "?"),
                str(it.get("score") or "--"),
                (it.get("status") or "AIRING")[:16],
                t,
            ))
        if out:
            return out, "JIKAN / MAL API"
    except Exception:
        pass
    return [], "ALL MIRRORS FAILED"


def fetch_movies_feed(query: str = ""):
    for base in YTS_MIRRORS:
        try:
            url = f"{base}/list_movies.json?query_term={urllib.parse.quote(query)}&limit=25" if query else f"{base}/list_movies.json?sort_by=download_count&limit=25"
            movies = http_json(url).get("data", {}).get("movies") or []
            out = [
                (
                    m.get("title", "Unknown")[:52],
                    str(m.get("year", "")),
                    f"{m.get('rating', '--')}/10",
                    ", ".join((m.get("genres") or [])[:2])[:22],
                    m.get("title", ""),
                )
                for m in movies
            ]
            if out:
                return out, f"YTS ({base.split('//')[1].split('/')[0]})"
        except Exception:
            continue

    try:
        url = f"https://api.tvmaze.com/search/shows?q={urllib.parse.quote(query)}" if query else "https://api.tvmaze.com/shows?page=0"
        out = []
        for item in http_json(url)[:25]:
            s = item.get("show", item)
            out.append((
                s.get("name", "Unknown")[:52],
                (s.get("premiered") or "----")[:4],
                f"{(s.get('rating') or {}).get('average') or '--'}/10",
                ", ".join((s.get("genres") or [])[:2])[:22],
                s.get("name", ""),
            ))
        if out:
            return out, "TVMAZE API"
    except Exception:
        pass
    return [], "ALL MIRRORS FAILED"


def ping_all_mirrors() -> list[tuple[str, str, str, str]]:
    """Concurrently pings every mirror endpoint in ~2 seconds."""
    endpoints = (
        [("YOUTUBE (PIPED)", u) for u in PIPED_MIRRORS]
        + [("YOUTUBE (INVIDIOUS)", u) for u in INVIDIOUS_MIRRORS]
        + [("ANIME (ANILIST)", "https://graphql.anilist.co")]
        + [("ANIME (JIKAN/MAL)", "https://api.jikan.moe/v4/top/anime?limit=1")]
        + [("MOVIES (YTS)", u + "/list_movies.json?limit=1") for u in YTS_MIRRORS]
        + [("TV (TVMAZE)", "https://api.tvmaze.com/shows/1")]
    )

    def _probe(item):
        svc, url = item
        host = url.split("//")[1][:38]
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers=HEADERS, method="HEAD")
            with urllib.request.urlopen(req, timeout=3.5):
                pass
            return (svc, host, "ONLINE", f"{int((time.time() - t0) * 1000)} ms")
        except Exception:
            return (svc, host, "OFFLINE", "---")

    with ThreadPoolExecutor(max_workers=12) as pool:
        return list(pool.map(_probe, endpoints))
