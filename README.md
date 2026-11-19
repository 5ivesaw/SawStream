<div align="center">

```text
 ███████╗ █████╗ ██╗    ██╗███████╗████████╗██████╗ ███████╗ █████╗ ███╗   ███╗
 ██╔════╝██╔══██╗██║    ██║██╔════╝╚══██╔══╝██╔══██╗██╔════╝██╔══██╗████╗ ████║
 ███████╗███████║██║ █╗ ██║███████╗   ██║   ██████╔╝█████╗  ███████║██╔████╔██║
 ╚════██║██╔══██║██║███╗██║╚════██║   ██║   ██╔══██╗██╔══╝  ██╔══██║██║╚██╔╝██║
 ███████║██║  ██║╚███╔███╔╝███████║   ██║   ██║  ██║███████╗██║  ██║██║ ╚═╝ ██║
 ╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝ ╚══════╝   ╚═╝   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝
```

**An offline-first, multi-mirror terminal media hub for YouTube, Music, Spotify links, Anime, and Movies/TV.**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-ff2e43?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Textual TUI](https://img.shields.io/badge/UI-Textual_TUI-16161a?style=flat-square)](https://textual.textualize.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-3fb950?style=flat-square)](LICENSE)

</div>

---

## Why SawStream?

Most CLI media wrappers hijack your terminal, dump raw `mpv` / PipeWire log spam, break whenever one scraper dies, and re-download the same song every time you replay it.

**SawStream** runs everything inside one crimson-and-obsidian Textual TUI:

- **Silent Headless `mpv` IPC Engine** — Controls playback, volume, mute, seek, codec/bitrate telemetry, and live cache stats over a Unix JSON socket with zero terminal stdout pollution.
- **Offline-First + Atomic Auto-Save** — Streams directly via `mpv` `stream-record` into a staging cache and atomically moves finished tracks into `~/Music` (so skipping a song early never leaves corrupt half-files behind). Replaying any saved track skips the network completely.
- **24-Bit Truecolor Cover Art** — Renders cropped, sharpened Unicode half-block (`▀`) thumbnails in memory and caches them locally so even offline tracks show artwork.
- **No-Login Spotify & Playlist Pipeline** — Paste any YouTube video, full playlist, or Spotify track/album/playlist URL straight into the bar to extract and queue every track in-process.
- **Multi-Mirror Auto-Failover** — Cycles across **Piped**, **Invidious**, and in-process **`yt-dlp`** for YouTube/Music, **AniList GraphQL** + **Jikan (MAL)** for Anime, and **YTS** + **TVMaze** for Movies/TV, with a concurrent Mirror Health Checker.
- **Offline Vault, Favorites & Queue Deck** — Manage local files across `~/Music`, `~/Downloads`, `~/Videos/Anime`, and `~/Videos/Movies`, toggle Repeat (`OFF / ALL / ONE`), Shuffle, and Favorites.

---

## Architecture

```text
SawStream/
├── sawstream/
│   ├── app.py        # Textual TUI, live equalizer, keybindings & deck controls
│   ├── engine.py     # Silent headless mpv Unix JSON IPC controller + atomic stream-record
│   ├── mirrors.py    # Multi-mirror failover (Piped, Invidious, Spotify, AniList, Jikan, YTS, TVMaze)
│   ├── artwork.py    # In-memory Pillow 24-bit Truecolor half-block (▀) renderer + disk cache
│   └── storage.py    # Offline-first file lookup, atomic partial commits, settings & favorites
├── install.sh
├── pyproject.toml
└── requirements.txt
```

---

## Quick Install

Requires `python3`, `mpv`, and `ffmpeg` (optional: `ani-cli` and `lobster` for Anime & Movies tabs).

```bash
git clone https://github.com/5ivesaw/SawStream.git
cd SawStream
./install.sh
sawstream
```

---

## Keybindings

| Key | Action |
| :--- | :--- |
| `1` – `7` | Switch Tabs (Deck, YouTube, Music, Anime, Movies, Offline Vault, Mirrors) |
| `Space` | Play / Pause |
| `n` / `p` | Next / Previous track in queue |
| `[` / `]` | Seek -10s / +10s |
| `-` / `=` | Volume Down / Up |
| `m` | Mute / Unmute |
| `r` | Cycle Repeat (`OFF` → `ALL` → `ONE`) |
| `z` | Shuffle queue |
| `f` | Toggle Favorite on current track |
| `s` | Toggle Auto-Save Music (`~/Music`) |
| `q` | Quit SawStream cleanly |

---

## License

MIT © [5saw (5ivesaw)](https://github.com/5ivesaw)
