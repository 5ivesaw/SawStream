import os
import random
import re
import subprocess
from pathlib import Path

import yt_dlp
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    LoadingIndicator,
    ProgressBar,
    Static,
    TabbedContent,
    TabPane,
)

from sawstream import __version__
from sawstream.artwork import idle_artwork, render_thumbnail
from sawstream.engine import HeadlessMPV
from sawstream.scraper import archive_channel_media, export_channel_scrape, scrape_channel_full
from sawstream.mirrors import (
    extract_media_in_process,
    fetch_anime_feed,
    fetch_movies_feed,
    fetch_yt_feed,
    ping_all_mirrors,
    resolve_audio_stream,
)
from sawstream.storage import (
    ANIME_DIR,
    MOVIE_DIR,
    MUSIC_DIR,
    VIDEO_DIR,
    cleanup_stale_partials,
    ensure_dirs,
    extract_vid_id,
    find_offline_file,
    fmt_dur,
    load_favorites,
    load_settings,
    make_partial_record_path,
    record_history,
    sanitize_filename,
    save_settings,
    scan_offline_library,
    toggle_favorite,
)

BANNER = r"""
 ███████╗ █████╗ ██╗    ██╗███████╗████████╗██████╗ ███████╗ █████╗ ███╗   ███╗
 ██╔════╝██╔══██╗██║    ██║██╔════╝╚══██╔══╝██╔══██╗██╔════╝██╔══██╗████╗ ████║
 ███████╗███████║██║ █╗ ██║███████╗   ██║   ██████╔╝█████╗  ███████║██╔████╔██║
 ╚════██║██╔══██║██║███╗██║╚════██║   ██║   ██╔══██╗██╔══╝  ██╔══██║██║╚██╔╝██║
 ███████║██║  ██║╚███╔███╔╝███████║   ██║   ██║  ██║███████╗██║  ██║██║ ╚═╝ ██║
 ╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝ ╚══════╝   ╚═╝   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝
"""

EQ_BLOCKS = " ▁▂▃▄▅▆▇█"


class SawStreamApp(App):
    CSS = """
    Screen { background: #000000; color: #e6edf3; }
    #banner { color: #ff2e43; text-align: center; height: 7; margin: 0; padding: 0; }
    #status-bar { background: #141418; color: #8b949e; padding: 0 2; height: 1; border-bottom: solid #ff2e43; }
    TabbedContent { height: 1fr; }
    ContentSwitcher { background: #000000; }
    Tab { color: #8b949e; background: #0d0d10; }
    Tab.-active { color: #000000; background: #ff2e43; text-style: bold; }
    Underline > .underline--bar { color: #ff2e43; }

    #np-top { height: 18; margin: 1 1 0 1; }
    #np-thumb { width: 40; height: 18; border: solid #ff2e43; background: #050507; padding: 0 1; }
    #np-meta-box { width: 1fr; height: 18; border: solid #222228; background: #070709; padding: 1 2; margin-left: 1; }
    #np-title { color: #ffffff; text-style: bold; height: 2; }
    #np-author { color: #ff5566; height: 1; }
    #np-tech { color: #7d8590; height: 1; }
    #np-eq { color: #ff2e43; height: 1; margin-top: 1; }
    #np-progress { margin-top: 1; width: 1fr; }
    #np-time { color: #e6edf3; height: 1; }
    #np-controls { height: 3; margin-top: 1; align: left middle; }
    #queue-header { height: 3; margin: 1 1 0 1; align: left middle; }
    #queue-label { color: #ff2e43; text-style: bold; width: 1fr; padding-top: 1; }

    .toolbar { height: 3; margin: 1 1 0 1; align: left middle; }
    .actions { height: 3; margin: 0 1 1 1; align: left middle; }
    Input { width: 1fr; background: #0d0d10; border: tall #30363d; color: #ffffff; }
    Input:focus { border: tall #ff2e43; }
    Button { margin-left: 1; background: #16161a; color: #e6edf3; border: tall #30363d; min-width: 8; }
    Button:hover { background: #2a080d; border: tall #ff2e43; color: #ffffff; }
    Button.primary { background: #4a0e17; border: tall #ff2e43; color: #ffffff; text-style: bold; }
    Button.primary:hover { background: #ff2e43; color: #000000; }
    Button.on { background: #12331b; border: tall #3fb950; color: #56d364; text-style: bold; }
    DataTable { height: 1fr; margin: 0 1; background: #050507; border: solid #222228; }
    DataTable > .datatable--header { background: #16161a; color: #ff2e43; text-style: bold; }
    DataTable > .datatable--cursor { background: #4a0e17; color: #ffffff; text-style: bold; }
    LoadingIndicator { height: 1; color: #ff2e43; display: none; }
    LoadingIndicator.active { display: block; }
    Footer { background: #141418; color: #8b949e; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("space", "toggle_pause", "Play/Pause"),
        ("n", "next_track", "Next"),
        ("p", "prev_track", "Prev"),
        ("s", "toggle_save('auto_save_music')", "Save Music"),
        ("r", "cycle_repeat", "Repeat"),
        ("z", "shuffle_queue", "Shuffle"),
        ("f", "favorite_current", "Favorite"),
        ("m", "toggle_mute", "Mute"),
        ("[", "seek_back", "-10s"),
        ("]", "seek_fwd", "+10s"),
        ("-", "vol_down", "Vol-"),
        ("=", "vol_up", "Vol+"),
        ("1", "switch_tab('tab-np')", "Deck"),
        ("2", "switch_tab('tab-yt')", "YouTube"),
        ("3", "switch_tab('tab-music')", "Music"),
        ("4", "switch_tab('tab-anime')", "Anime"),
        ("5", "switch_tab('tab-movies')", "Movies"),
        ("6", "switch_tab('tab-offline')", "Offline"),
        ("7", "switch_tab('tab-scraper')", "Scraper"),
        ("8", "switch_tab('tab-mirrors')", "Mirrors"),
    ]

    def __init__(self):
        super().__init__()
        ensure_dirs()
        cleanup_stale_partials()
        self.settings = load_settings()
        self.mpv = HeadlessMPV(initial_volume=self.settings.get("volume", 100))
        self.queue: list[dict] = []
        self.queue_idx: int = -1
        self.is_loading_track = False
        self.current_source_tag = "IDLE"
        self.last_progress_ratio = 0.0
        self.yt_rows = []
        self.music_rows = []
        self.anime_rows = []
        self.movie_rows = []
        self.offline_rows = []
        self.scrape_data = None
        self.scrape_rows = []
        self.scrape_filter = "PLAYLISTS"

    def _lbl(self, key: str, prefix: str) -> str:
        return f"{prefix}: ON" if self.settings.get(key) else f"{prefix}: OFF"

    def _rep_lbl(self) -> str:
        return f"REP: {self.settings.get('repeat_mode', 'OFF')}"

    def compose(self) -> ComposeResult:
        yield Static(BANNER, id="banner")
        yield Static(
            f"SAWSTREAM v{__version__} BY 5SAW  |  OFFLINE-FIRST + ATOMIC AUTO-SAVE  |  KEYS 1-7 TABS  |  SPACE PAUSE",
            id="status-bar",
        )
        yield LoadingIndicator(id="loader")

        with TabbedContent(id="tabs"):
            # --- TAB 1: NOW PLAYING DECK ---
            with TabPane("1: DECK", id="tab-np"):
                with Horizontal(id="np-top"):
                    yield Static(idle_artwork(), id="np-thumb")
                    with Vertical(id="np-meta-box"):
                        yield Static("Nothing Playing", id="np-title")
                        yield Static("Author: --", id="np-author")
                        yield Static("IDLE  |  Queue: 0 / 0  |  Vol: 100%", id="np-tech")
                        yield Static("▁" * 44, id="np-eq")
                        yield ProgressBar(total=100, show_eta=False, show_percentage=True, id="np-progress")
                        yield Static("0:00 / 0:00", id="np-time")
                        with Horizontal(id="np-controls"):
                            yield Button("PREV", id="ctl-prev")
                            yield Button("PLAY/PAUSE", id="ctl-pause", classes="primary")
                            yield Button("NEXT", id="ctl-next")
                            yield Button("-10s", id="ctl-back")
                            yield Button("+10s", id="ctl-fwd")
                            yield Button(self._rep_lbl(), id="ctl-repeat")
                            yield Button("FAV", id="ctl-fav")
                            yield Button(
                                self._lbl("auto_save_music", "SAVE"),
                                id="ctl-save-music",
                                classes="on" if self.settings["auto_save_music"] else "",
                            )
                with Horizontal(id="queue-header"):
                    yield Static("UP NEXT QUEUE (ENTER TO JUMP)", id="queue-label")
                    yield Button("SHUFFLE", id="q-shuffle")
                    yield Button("FAVORITES", id="q-favs")
                    yield Button("REMOVE", id="q-del")
                    yield Button("CLEAR", id="q-clear")
                yield DataTable(id="queue-table", cursor_type="row")

            # --- TAB 2: YOUTUBE ---
            with TabPane("2: YOUTUBE", id="tab-yt"):
                with Horizontal(classes="toolbar"):
                    yield Input(placeholder="Search YouTube or paste any YouTube / Playlist / Spotify URL...", id="yt-input")
                    yield Button("GO", id="yt-search", classes="primary")
                    yield Button("HOME", id="yt-home")
                yield DataTable(id="yt-table", cursor_type="row")
                with Horizontal(classes="actions"):
                    yield Button("PLAY AUDIO IN DECK", id="yt-play-audio", classes="primary")
                    yield Button("WATCH VIDEO", id="yt-play-win")
                    yield Button(
                        self._lbl("auto_save_video", "SAVE VID"),
                        id="ctl-save-video",
                        classes="on" if self.settings["auto_save_video"] else "",
                    )
                    yield Button("DOWNLOAD MP4", id="yt-dl")

            # --- TAB 3: MUSIC ---
            with TabPane("3: MUSIC", id="tab-music"):
                with Horizontal(classes="toolbar"):
                    yield Input(placeholder="Search song/artist OR paste YouTube / Spotify track, album, playlist URL...", id="music-input")
                    yield Button("GO", id="music-search", classes="primary")
                    yield Button("TRENDING", id="music-home")
                yield DataTable(id="music-table", cursor_type="row")
                with Horizontal(classes="actions"):
                    yield Button("PLAY IN DECK", id="music-play", classes="primary")
                    yield Button("+ QUEUE", id="music-queue")
                    yield Button("PLAY ALL OFFLINE", id="music-offline")
                    yield Button("DOWNLOAD MP3", id="music-dl")

            # --- TAB 4: ANIME ---
            with TabPane("4: ANIME", id="tab-anime"):
                with Horizontal(classes="toolbar"):
                    yield Input(placeholder="Search Anime (AniList + Jikan/MAL failover)...", id="anime-input")
                    yield Button("SEARCH", id="anime-search", classes="primary")
                    yield Button("TRENDING", id="anime-home")
                yield DataTable(id="anime-table", cursor_type="row")
                with Horizontal(classes="actions"):
                    yield Button("WATCH SUB", id="anime-sub", classes="primary")
                    yield Button("WATCH DUB", id="anime-dub")
                    yield Button(
                        self._lbl("auto_save_anime", "SAVE ANIME"),
                        id="ctl-save-anime",
                        classes="on" if self.settings["auto_save_anime"] else "",
                    )

            # --- TAB 5: MOVIES / TV ---
            with TabPane("5: MOVIES", id="tab-movies"):
                with Horizontal(classes="toolbar"):
                    yield Input(placeholder="Search Movies or TV Series (YTS + TVMaze failover)...", id="movie-input")
                    yield Button("SEARCH", id="movie-search", classes="primary")
                    yield Button("TRENDING", id="movie-home")
                yield DataTable(id="movie-table", cursor_type="row")
                with Horizontal(classes="actions"):
                    yield Button("STREAM MOVIE/TV", id="movie-stream", classes="primary")
                    yield Button(
                        self._lbl("auto_save_movies", "SAVE MOVIE"),
                        id="ctl-save-movies",
                        classes="on" if self.settings["auto_save_movies"] else "",
                    )

            # --- TAB 6: OFFLINE VAULT ---
            with TabPane("6: OFFLINE", id="tab-offline"):
                with Horizontal(classes="toolbar"):
                    yield Input(placeholder="Filter local offline library (Music, Downloads, Anime, Movies)...", id="offline-filter")
                    yield Button("RESCAN", id="offline-rescan", classes="primary")
                    yield Button("PLAY ALL MUSIC", id="offline-play-all")
                yield DataTable(id="offline-table", cursor_type="row")
                with Horizontal(classes="actions"):
                    yield Button("PLAY SELECTED", id="offline-play", classes="primary")
                    yield Button("DELETE FILE", id="offline-delete")

            # --- TAB 7: MIRRORS ---
            with TabPane("7: SCRAPER", id="tab-scraper"):
                with Horizontal(classes="toolbar"):
                    yield Input(placeholder="Paste @handle or Channel URL to scrape all public playlists & videos...", id="scrape-input")
                    yield Button("SCAN", id="scrape-scan", classes="primary")
                    yield Button("PLAYLISTS", id="sf-playlists")
                    yield Button("ALL", id="sf-all")
                yield DataTable(id="scrape-table", cursor_type="row")
                with Horizontal(classes="actions"):
                    yield Button("QUEUE ALL", id="scrape-queue", classes="primary")
                    yield Button("EXPORT JSON/CSV", id="scrape-export")
                    yield Button("ARCHIVE MP3", id="scrape-mp3")
                    yield Button("ARCHIVE MKV", id="scrape-mkv")
            with TabPane("8: MIRRORS", id="tab-mirrors"):
                with Horizontal(classes="toolbar"):
                    yield Button("PING ALL MIRRORS (CONCURRENT)", id="mirror-check", classes="primary")
                yield DataTable(id="mirror-table", cursor_type="row")

        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#queue-table", DataTable).add_columns("#", "TRACK TITLE", "AUTHOR", "DURATION")
        self.query_one("#yt-table", DataTable).add_columns("TITLE", "CHANNEL", "DURATION")
        self.query_one("#music-table", DataTable).add_columns("TRACK", "ARTIST", "DURATION")
        self.query_one("#anime-table", DataTable).add_columns("ANIME TITLE", "EPS", "SCORE", "STATUS")
        self.query_one("#movie-table", DataTable).add_columns("TITLE", "YEAR", "RATING", "GENRE")
        self.query_one("#offline-table", DataTable).add_columns("TYPE", "TITLE", "SIZE")
        self.query_one("#scrape-table", DataTable).add_columns("SECTION", "PLAYLIST", "TITLE", "DURATION")
        self.query_one("#mirror-table", DataTable).add_columns("SERVICE", "MIRROR ENDPOINT", "STATUS", "LATENCY")

        self.set_interval(0.35, self.poll_player)
        self.refresh_offline_table()
        self.load_yt("")
        self.load_music("")
        self.load_anime("")
        self.load_movies("")

    def on_unmount(self) -> None:
        self.mpv.shutdown()

    def set_status(self, text: str, loading: bool | None = None) -> None:
        self.query_one("#status-bar", Static).update(text)
        if loading is not None:
            loader = self.query_one("#loader", LoadingIndicator)
            loader.add_class("active") if loading else loader.remove_class("active")

    def refresh_queue_table(self) -> None:
        t = self.query_one("#queue-table", DataTable)
        t.clear()
        for i, item in enumerate(self.queue):
            t.add_row(
                f"> {i + 1}" if i == self.queue_idx else f"  {i + 1}",
                item["title"][:56],
                item["author"][:24],
                fmt_dur(item.get("duration", 0)),
            )

    def refresh_offline_table(self, needle: str = "") -> None:
        all_items = scan_offline_library()
        q = needle.lower().strip()
        self.offline_rows = [it for it in all_items if not q or q in it["title"].lower() or q in it["kind"].lower()]
        t = self.query_one("#offline-table", DataTable)
        t.clear()
        for it in self.offline_rows:
            t.add_row(it["kind"], it["title"][:64], it["size"])

    # --- LIVE IPC POLLER + EQUALIZER + ATOMIC COMPLETION ---
    def poll_player(self) -> None:
        if self.queue_idx < 0 or self.is_loading_track:
            return
        st = self.mpv.snapshot_state()
        if st.get("idle") is True:
            # Track finished naturally -> commit recording if >= 85% played
            completed = self.last_progress_ratio >= 0.85
            if self.mpv.finalize_recording(completed=completed):
                self.refresh_offline_table()
            self.last_progress_ratio = 0.0

            rep = self.settings.get("repeat_mode", "OFF")
            if rep == "ONE":
                self.play_queue_index(self.queue_idx)
            elif self.queue_idx + 1 < len(self.queue):
                self.play_queue_index(self.queue_idx + 1)
            elif rep == "ALL" and self.queue:
                self.play_queue_index(0)
            else:
                self.query_one("#np-tech", Static).update(f"FINISHED  |  Queue: {self.queue_idx + 1} / {len(self.queue)}")
                self.query_one("#np-eq", Static).update("▁" * 44)
            return

        pos = float(st.get("pos") or 0.0)
        dur = float(st.get("dur") or self.queue[self.queue_idx].get("duration") or 0.0)
        if dur > 0:
            self.last_progress_ratio = max(self.last_progress_ratio, min(1.0, pos / dur))

        paused = st.get("paused", False)
        muted = st.get("muted", False)
        vol = int(st.get("vol")) if st.get("vol") is not None else self.settings.get("volume", 100)
        cache = int(st.get("cache") or 0)
        kbps = int((st.get("bitrate") or 0) / 1000)
        codec = (st.get("codec") or "").upper()

        state_str = "PAUSED" if paused else "PLAYING"
        vol_str = "MUTED" if muted else f"{vol}%"
        tech_parts = [f"{state_str} [{self.current_source_tag}]", f"Queue {self.queue_idx + 1}/{len(self.queue)}", f"Vol {vol_str}"]
        if codec:
            tech_parts.append(f"{codec} {kbps}k" if kbps else codec)
        if cache > 0 and self.current_source_tag != "OFFLINE":
            tech_parts.append(f"Buf {cache}s")
        self.query_one("#np-tech", Static).update("  |  ".join(tech_parts))
        self.query_one("#np-time", Static).update(f"{fmt_dur(pos)} / {fmt_dur(dur)}")

        if dur > 0:
            self.query_one("#np-progress", ProgressBar).update(total=dur, progress=min(pos, dur))

        # Smooth reactive equalizer strip
        if paused or muted:
            self.query_one("#np-eq", Static).update("▁" * 44)
        else:
            bars = "".join(EQ_BLOCKS[random.randint(1, len(EQ_BLOCKS) - 1)] for _ in range(44))
            self.query_one("#np-eq", Static).update(bars)

    # --- IN-PROCESS MEDIA PIPELINE ---
    @work(thread=True)
    def load_and_play_url(self, url: str, append_only: bool = False):
        vid_id = extract_vid_id(url)
        if vid_id and "list=" not in url and "open.spotify.com" not in url:
            local_file = find_offline_file(MUSIC_DIR, vid_id)
            if local_file:
                clean = re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]$", "", Path(local_file).stem)
                track = {"id": vid_id, "title": clean, "author": "Offline Library", "duration": 0, "thumbnail": "", "webpage_url": url, "local_path": local_file}
                if append_only and self.queue:
                    self.queue.append(track)
                    self.call_from_thread(self.refresh_queue_table)
                    self.call_from_thread(self.set_status, f"QUEUED OFFLINE: {clean}", False)
                else:
                    self.queue = [track]
                    self.call_from_thread(self.action_switch_tab, "tab-np")
                    self.call_from_thread(self.play_queue_index, 0)
                return

        label = "SPOTIFY TRACKLIST" if "open.spotify.com" in url else "METADATA VIA YT-DLP API"
        self.call_from_thread(self.set_status, f"EXTRACTING {label}...", True)
        try:
            tracks = extract_media_in_process(url)
        except Exception as e:
            self.call_from_thread(self.set_status, f"EXTRACTION ERROR: {e}", False)
            return

        if not tracks:
            self.call_from_thread(self.set_status, "NO PLAYABLE TRACKS FOUND.", False)
            return

        if append_only and self.queue:
            self.queue.extend(tracks)
            self.call_from_thread(self.refresh_queue_table)
            self.call_from_thread(self.set_status, f"QUEUED {len(tracks)} TRACK(S).", False)
            return

        self.queue = tracks
        self.call_from_thread(self.action_switch_tab, "tab-np")
        self.call_from_thread(self.play_queue_index, 0)

    @work(thread=True)
    def play_queue_index(self, idx: int):
        if idx < 0 or idx >= len(self.queue):
            return
        self.is_loading_track = True
        prev_completed = self.last_progress_ratio >= 0.85
        self.last_progress_ratio = 0.0
        self.queue_idx = idx
        item = self.queue[idx]
        vid_id = item.get("id") or extract_vid_id(item.get("webpage_url", ""))

        self.call_from_thread(self.refresh_queue_table)
        self.call_from_thread(self.query_one("#np-title", Static).update, f"Loading: {item['title'][:65]}...")

        # 1. Offline-First Check
        local_path = item.get("local_path") or find_offline_file(MUSIC_DIR, vid_id, item.get("title", ""))
        if local_path and os.path.exists(local_path):
            self.current_source_tag = "OFFLINE"
            thumb_render = render_thumbnail(item.get("thumbnail", ""), vid_id=vid_id)
            if self.mpv.play_source(local_path, prev_completed=prev_completed):
                self.call_from_thread(self.refresh_offline_table)
            record_history(item)
            self.is_loading_track = False

            def update_offline_ui():
                self.query_one("#np-thumb", Static).update(thumb_render)
                self.query_one("#np-title", Static).update(item["title"][:72])
                self.query_one("#np-author", Static).update(f"Author: {item['author']}  ·  {Path(local_path).name[:30]}")
                self.query_one("#np-progress", ProgressBar).update(total=float(item.get("duration") or 100), progress=0)
                self.refresh_queue_table()
                self.set_status(f"[OFFLINE] {item['title'][:58]}", False)

            self.call_from_thread(update_offline_ui)
            return

        # 2. Online Stream + Atomic Partial Recording
        self.call_from_thread(self.set_status, f"RESOLVING STREAM: {item['title'][:50]}...", True)
        try:
            resolved = resolve_audio_stream(item["webpage_url"])
        except Exception as e:
            self.is_loading_track = False
            self.call_from_thread(self.set_status, f"STREAM ERROR: {e}", False)
            return

        if not resolved or not resolved.get("stream_url"):
            self.is_loading_track = False
            self.call_from_thread(self.set_status, "FAILED TO RESOLVE STREAM.", False)
            return

        self.queue[idx].update(resolved)
        vid_id = resolved.get("id") or vid_id
        partial_path = final_path = ""
        if self.settings.get("auto_save_music"):
            partial_path, final_path = make_partial_record_path(resolved["title"], vid_id)
            self.current_source_tag = "STREAM + SAVING"
        else:
            self.current_source_tag = "STREAM"

        thumb_render = render_thumbnail(resolved["thumbnail"], vid_id=vid_id)
        if self.mpv.play_source(resolved["stream_url"], partial_path=partial_path, final_path=final_path, prev_completed=prev_completed):
            self.call_from_thread(self.refresh_offline_table)
        record_history(resolved)
        self.is_loading_track = False

        def update_ui():
            self.query_one("#np-thumb", Static).update(thumb_render)
            self.query_one("#np-title", Static).update(resolved["title"][:72])
            self.query_one("#np-author", Static).update(f"Author: {resolved['author']}")
            self.query_one("#np-progress", ProgressBar).update(total=float(resolved["duration"] or 100), progress=0)
            self.refresh_queue_table()
            note = " (Auto-saving to ~/Music on completion)" if partial_path else ""
            self.set_status(f"PLAYING: {resolved['title'][:48]}{note}", False)

        self.call_from_thread(update_ui)

    # --- ACTIONS & TOGGLES ---
    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab_id

    def action_toggle_pause(self) -> None:
        self.mpv.toggle_pause()

    def action_toggle_mute(self) -> None:
        self.mpv.toggle_mute()

    def action_next_track(self) -> None:
        if self.queue_idx + 1 < len(self.queue):
            self.play_queue_index(self.queue_idx + 1)

    def action_prev_track(self) -> None:
        if self.queue_idx - 1 >= 0:
            self.play_queue_index(self.queue_idx - 1)

    def action_seek_back(self) -> None:
        self.mpv.seek(-10)

    def action_seek_fwd(self) -> None:
        self.mpv.seek(10)

    def action_vol_down(self) -> None:
        self.settings["volume"] = self.mpv.adjust_volume(-5)
        save_settings(self.settings)

    def action_vol_up(self) -> None:
        self.settings["volume"] = self.mpv.adjust_volume(5)
        save_settings(self.settings)

    def action_cycle_repeat(self) -> None:
        modes = ["OFF", "ALL", "ONE"]
        cur = self.settings.get("repeat_mode", "OFF")
        nxt = modes[(modes.index(cur) + 1) % len(modes)]
        self.settings["repeat_mode"] = nxt
        save_settings(self.settings)
        self.query_one("#ctl-repeat", Button).label = self._rep_lbl()
        self.set_status(f"REPEAT MODE: {nxt}")

    def action_shuffle_queue(self) -> None:
        if len(self.queue) <= 1:
            return
        current = self.queue[self.queue_idx] if 0 <= self.queue_idx < len(self.queue) else None
        rest = [x for i, x in enumerate(self.queue) if i != self.queue_idx]
        random.shuffle(rest)
        self.queue = ([current] + rest) if current else rest
        self.queue_idx = 0 if current else -1
        self.refresh_queue_table()
        self.set_status("QUEUE SHUFFLED.")

    def action_favorite_current(self) -> None:
        if 0 <= self.queue_idx < len(self.queue):
            added = toggle_favorite(self.queue[self.queue_idx])
            self.set_status(f"{'ADDED TO' if added else 'REMOVED FROM'} FAVORITES: {self.queue[self.queue_idx]['title'][:45]}")

    def action_toggle_save(self, key: str) -> None:
        self.settings[key] = not self.settings.get(key, False)
        save_settings(self.settings)
        btn_map = {
            "auto_save_music": ("#ctl-save-music", "SAVE", "~/Music"),
            "auto_save_video": ("#ctl-save-video", "SAVE VID", "~/Downloads"),
            "auto_save_anime": ("#ctl-save-anime", "SAVE ANIME", "~/Videos/Anime"),
            "auto_save_movies": ("#ctl-save-movies", "SAVE MOVIE", "~/Videos/Movies"),
        }
        bid, prefix, folder = btn_map[key]
        btn = self.query_one(bid, Button)
        btn.label = self._lbl(key, prefix)
        btn.add_class("on") if self.settings[key] else btn.remove_class("on")
        self.set_status(f"{prefix}: {'ENABLED -> ' + folder if self.settings[key] else 'DISABLED'}")

    def load_offline_music(self) -> None:
        tracks = [it for it in scan_offline_library() if it["kind"] == "MUSIC"]
        if not tracks:
            self.set_status("NO OFFLINE SONGS IN ~/Music YET.")
            return
        self.queue = tracks
        self.action_switch_tab("tab-np")
        self.play_queue_index(0)

    def spawn_detached_video(self, url: str, title: str = "") -> None:
        vid_id = extract_vid_id(url)
        local_vid = find_offline_file(VIDEO_DIR, vid_id, title)
        if local_vid:
            subprocess.Popen(["mpv", "--no-terminal", "--really-quiet", local_vid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.set_status(f"[OFFLINE VIDEO] {Path(local_vid).name}")
            return
        cmd = ["mpv", "--no-terminal", "--really-quiet"]
        if self.settings.get("auto_save_video"):
            tag = f" [{vid_id}]" if vid_id else ""
            out_path = str(VIDEO_DIR / f"{sanitize_filename(title or vid_id or 'video')}{tag}.mkv")
            cmd.append(f"--stream-record={out_path}")
            self.set_status(f"VIDEO + SAVING -> ~/Downloads/{Path(out_path).name}")
        else:
            self.set_status("LAUNCHED VIDEO WINDOW (SAWSTREAM STILL ACTIVE)")
        cmd.append(url)
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def launch_anime(self, title: str, dub: bool = False) -> None:
        local = find_offline_file(ANIME_DIR, title=title)
        if local:
            subprocess.Popen(["mpv", "--no-terminal", "--really-quiet", local], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.set_status(f"[OFFLINE ANIME] {Path(local).name}")
            return
        cmd = [os.path.expanduser("~/.local/bin/ani-cli")]
        if dub:
            cmd.append("--dub")
        if self.settings.get("auto_save_anime"):
            cmd.append("-d")
        cmd.append(title)
        with self.suspend():
            os.system("clear")
            subprocess.run(cmd, cwd=str(ANIME_DIR))
        self.refresh_offline_table()

    def launch_movie(self, title: str) -> None:
        local = find_offline_file(MOVIE_DIR, title=title)
        if local:
            subprocess.Popen(["mpv", "--no-terminal", "--really-quiet", local], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.set_status(f"[OFFLINE MOVIE] {Path(local).name}")
            return
        cmd = [os.path.expanduser("~/.local/bin/lobster")]
        if self.settings.get("auto_save_movies"):
            cmd.append("-d")
        cmd.append(title)
        with self.suspend():
            os.system("clear")
            subprocess.run(cmd, cwd=str(MOVIE_DIR))
        self.refresh_offline_table()

    # --- BACKGROUND FEED WORKERS ---
    @work(thread=True)
    def load_yt(self, query: str):
        self.call_from_thread(self.set_status, f"FETCHING YOUTUBE: {query or 'TRENDING'}...", True)
        rows, src = fetch_yt_feed(query, music_only=False)
        self.yt_rows = rows
        def update():
            t = self.query_one("#yt-table", DataTable)
            t.clear()
            for r in rows:
                t.add_row(r[0], r[1], r[2])
            self.set_status(f"YOUTUBE READY  |  SOURCE: {src}", False)
        self.call_from_thread(update)

    @work(thread=True)
    def load_music(self, query: str):
        self.call_from_thread(self.set_status, f"FETCHING MUSIC: {query or 'TRENDING'}...", True)
        rows, src = fetch_yt_feed(query, music_only=True)
        self.music_rows = rows
        def update():
            t = self.query_one("#music-table", DataTable)
            t.clear()
            for r in rows:
                t.add_row(r[0], r[1], r[2])
            self.set_status(f"MUSIC READY  |  SOURCE: {src}", False)
        self.call_from_thread(update)

    @work(thread=True)
    def load_anime(self, query: str):
        rows, _ = fetch_anime_feed(query)
        self.anime_rows = rows
        def update():
            t = self.query_one("#anime-table", DataTable)
            t.clear()
            for r in rows:
                t.add_row(r[0], r[1], r[2], r[3])
        self.call_from_thread(update)

    @work(thread=True)
    def load_movies(self, query: str):
        rows, _ = fetch_movies_feed(query)
        self.movie_rows = rows
        def update():
            t = self.query_one("#movie-table", DataTable)
            t.clear()
            for r in rows:
                t.add_row(r[0], r[1], r[2], r[3])
        self.call_from_thread(update)

    @work(thread=True)
    def check_mirrors(self):
        self.call_from_thread(self.set_status, "PINGING ALL MIRRORS CONCURRENTLY...", True)
        results = ping_all_mirrors()
        def update():
            t = self.query_one("#mirror-table", DataTable)
            t.clear()
            for r in results:
                t.add_row(*r)
            online = sum(1 for r in results if r[2] == "ONLINE")
            self.set_status(f"MIRROR CHECK COMPLETE  |  {online}/{len(results)} ONLINE", False)
        self.call_from_thread(update)

    @work(thread=True)
    def download_in_background(self, url: str, audio_only: bool):
        self.call_from_thread(self.set_status, f"DOWNLOADING {'MP3' if audio_only else 'MP4'}...", True)
        dest = MUSIC_DIR if audio_only else VIDEO_DIR
        opts = {"quiet": True, "no_warnings": True, "outtmpl": str(dest / "%(title)s [%(id)s].%(ext)s")}
        if audio_only:
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}]
        else:
            opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            self.call_from_thread(self.refresh_offline_table)
            self.call_from_thread(self.set_status, f"SAVED TO {dest}", False)
        except Exception as e:
            self.call_from_thread(self.set_status, f"DOWNLOAD FAILED: {e}", False)

    def refresh_scrape(self):
        t = self.query_one("#scrape-table", DataTable); t.clear()
        if not self.scrape_data: return
        f = self.scrape_filter
        self.scrape_rows = [x for x in self.scrape_data["items"] if f == "ALL" or x["section"] == f]
        for x in self.scrape_rows: t.add_row(x["section"], (x["playlist"] or "-")[:22], x["title"][:48], fmt_dur(x["duration"]))

    @work(thread=True)
    def run_scrape(self, raw: str):
        self.call_from_thread(self.set_status, f"SCRAPING {raw}...", True)
        try:
            d = scrape_channel_full(raw, progress_cb=lambda m: self.call_from_thread(self.set_status, m, True))
            self.scrape_data = d
            self.scrape_filter = "PLAYLISTS" if d["playlists"] else "ALL"
            self.call_from_thread(self.refresh_scrape)
            self.call_from_thread(self.set_status, f"SCRAPED {d['channel_name']}: {d['playlist_count']} PLAYLISTS, {d['unique_video_count']} VIDEOS", False)
        except Exception as e:
            self.call_from_thread(self.set_status, f"SCRAPE ERROR: {e}", False)

    @work(thread=True)
    def run_archive(self, audio: bool):
        if not self.scrape_data: return
        dest, done, _ = archive_channel_media(self.scrape_data, audio_only=audio, progress_cb=lambda m: self.call_from_thread(self.set_status, m, True))
        self.call_from_thread(self.refresh_offline_table)
        self.call_from_thread(self.set_status, f"ARCHIVED {done} FILES -> {dest}", False)

    # --- EVENT HANDLERS ---
    @on(Input.Submitted, "#yt-input")
    def on_yt_submit(self, event: Input.Submitted):
        val = event.value.strip()
        self.load_and_play_url(val) if val.startswith(("http://", "https://")) else self.load_yt(val)

    @on(Input.Submitted, "#music-input")
    def on_music_submit(self, event: Input.Submitted):
        val = event.value.strip()
        self.load_and_play_url(val) if val.startswith(("http://", "https://")) else self.load_music(val)

    @on(Input.Submitted, "#anime-input")
    def on_anime_submit(self, event: Input.Submitted):
        self.load_anime(event.value.strip())

    @on(Input.Submitted, "#movie-input")
    def on_movie_submit(self, event: Input.Submitted):
        self.load_movies(event.value.strip())

    @on(Input.Submitted, "#scrape-input")
    def on_scrape_sub(self, e: Input.Submitted):
        if e.value.strip(): self.run_scrape(e.value.strip())

    @on(Input.Changed, "#offline-filter")
    def on_offline_filter(self, event: Input.Changed):
        self.refresh_offline_table(event.value)

    @on(Button.Pressed)
    def on_button(self, event: Button.Pressed):
        bid = event.button.id
        if bid == "ctl-pause": self.action_toggle_pause()
        elif bid == "ctl-next": self.action_next_track()
        elif bid == "ctl-prev": self.action_prev_track()
        elif bid == "ctl-back": self.action_seek_back()
        elif bid == "ctl-fwd": self.action_seek_fwd()
        elif bid == "ctl-repeat": self.action_cycle_repeat()
        elif bid == "ctl-fav": self.action_favorite_current()
        elif bid == "ctl-save-music": self.action_toggle_save("auto_save_music")
        elif bid == "ctl-save-video": self.action_toggle_save("auto_save_video")
        elif bid == "ctl-save-anime": self.action_toggle_save("auto_save_anime")
        elif bid == "ctl-save-movies": self.action_toggle_save("auto_save_movies")
        elif bid == "q-shuffle": self.action_shuffle_queue()
        elif bid == "q-favs":
            favs = load_favorites()
            if not favs:
                self.set_status("NO FAVORITES SAVED YET (PRESS 'f' WHILE PLAYING).")
            else:
                self.queue = favs
                self.play_queue_index(0)
        elif bid == "q-del":
            t = self.query_one("#queue-table", DataTable)
            if self.queue and 0 <= t.cursor_row < len(self.queue):
                idx = t.cursor_row
                self.queue.pop(idx)
                if idx < self.queue_idx:
                    self.queue_idx -= 1
                elif idx == self.queue_idx:
                    self.mpv.stop()
                    self.queue_idx = -1
                self.refresh_queue_table()
        elif bid == "q-clear":
            self.mpv.stop()
            self.queue = []
            self.queue_idx = -1
            self.refresh_queue_table()
            self.query_one("#np-thumb", Static).update(idle_artwork())
            self.query_one("#np-title", Static).update("Nothing Playing")
            self.query_one("#np-author", Static).update("Author: --")
            self.query_one("#np-eq", Static).update("▁" * 44)
        elif bid == "yt-search":
            inp = self.query_one("#yt-input", Input)
            self.on_yt_submit(Input.Submitted(inp, inp.value))
        elif bid == "yt-home":
            self.query_one("#yt-input", Input).value = ""
            self.load_yt("")
        elif bid in ("yt-play-audio", "yt-play-win", "yt-dl"):
            t = self.query_one("#yt-table", DataTable)
            if self.yt_rows and t.cursor_row < len(self.yt_rows):
                title, _, _, url = self.yt_rows[t.cursor_row]
                if bid == "yt-play-audio": self.load_and_play_url(url)
                elif bid == "yt-play-win": self.spawn_detached_video(url, title=title)
                elif bid == "yt-dl": self.download_in_background(url, audio_only=False)
        elif bid == "music-search":
            inp = self.query_one("#music-input", Input)
            self.on_music_submit(Input.Submitted(inp, inp.value))
        elif bid == "music-home":
            self.query_one("#music-input", Input).value = ""
            self.load_music("")
        elif bid in ("music-offline", "offline-play-all"):
            self.load_offline_music()
        elif bid in ("music-play", "music-queue", "music-dl"):
            t = self.query_one("#music-table", DataTable)
            if self.music_rows and t.cursor_row < len(self.music_rows):
                url = self.music_rows[t.cursor_row][3]
                if bid == "music-play": self.load_and_play_url(url, append_only=False)
                elif bid == "music-queue": self.load_and_play_url(url, append_only=True)
                elif bid == "music-dl": self.download_in_background(url, audio_only=True)
        elif bid == "anime-search":
            self.load_anime(self.query_one("#anime-input", Input).value.strip())
        elif bid == "anime-home":
            self.query_one("#anime-input", Input).value = ""
            self.load_anime("")
        elif bid in ("anime-sub", "anime-dub"):
            t = self.query_one("#anime-table", DataTable)
            if self.anime_rows and t.cursor_row < len(self.anime_rows):
                self.launch_anime(self.anime_rows[t.cursor_row][4], dub=(bid == "anime-dub"))
        elif bid == "movie-search":
            self.load_movies(self.query_one("#movie-input", Input).value.strip())
        elif bid == "movie-home":
            self.query_one("#movie-input", Input).value = ""
            self.load_movies("")
        elif bid == "movie-stream":
            t = self.query_one("#movie-table", DataTable)
            if self.movie_rows and t.cursor_row < len(self.movie_rows):
                self.launch_movie(self.movie_rows[t.cursor_row][4])
        elif bid == "offline-rescan":
            self.refresh_offline_table(self.query_one("#offline-filter", Input).value)
            self.set_status(f"OFFLINE VAULT SCANNED: {len(self.offline_rows)} FILES")
        elif bid == "offline-play":
            t = self.query_one("#offline-table", DataTable)
            if self.offline_rows and t.cursor_row < len(self.offline_rows):
                self._play_offline_item(self.offline_rows[t.cursor_row])
        elif bid == "offline-delete":
            t = self.query_one("#offline-table", DataTable)
            if self.offline_rows and t.cursor_row < len(self.offline_rows):
                item = self.offline_rows[t.cursor_row]
                try:
                    os.remove(item["local_path"])
                    self.refresh_offline_table(self.query_one("#offline-filter", Input).value)
                    self.set_status(f"DELETED: {item['title'][:50]}")
                except OSError as e:
                    self.set_status(f"DELETE ERROR: {e}")
        elif bid == "scrape-scan":
            v = self.query_one("#scrape-input", Input).value.strip()
            if v: self.run_scrape(v)
        elif bid in ("sf-playlists", "sf-all"):
            self.scrape_filter = "PLAYLISTS" if bid == "sf-playlists" else "ALL"; self.refresh_scrape()
        elif bid == "scrape-queue" and self.scrape_rows:
            self.queue = [dict(x) for x in self.scrape_rows]; self.action_switch_tab("tab-np"); self.play_queue_index(0)
        elif bid == "scrape-export" and self.scrape_data:
            self.set_status(f"EXPORTED -> {export_channel_scrape(self.scrape_data)}")
        elif bid == "scrape-mp3": self.run_archive(True)
        elif bid == "scrape-mkv": self.run_archive(False)
        elif bid == "mirror-check":
            self.check_mirrors()

    def _play_offline_item(self, item: dict) -> None:
        if item["kind"] == "MUSIC":
            self.queue = [item]
            self.action_switch_tab("tab-np")
            self.play_queue_index(0)
        else:
            subprocess.Popen(["mpv", "--no-terminal", "--really-quiet", item["local_path"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.set_status(f"[OFFLINE {item['kind']}] {item['title'][:50]}")

    @on(DataTable.RowSelected)
    def on_row_selected(self, event: DataTable.RowSelected):
        tid, idx = event.data_table.id, event.cursor_row
        if tid == "queue-table" and idx < len(self.queue):
            self.play_queue_index(idx)
        elif tid == "yt-table" and idx < len(self.yt_rows):
            self.load_and_play_url(self.yt_rows[idx][3])
        elif tid == "music-table" and idx < len(self.music_rows):
            self.load_and_play_url(self.music_rows[idx][3])
        elif tid == "anime-table" and idx < len(self.anime_rows):
            self.launch_anime(self.anime_rows[idx][4])
        elif tid == "movie-table" and idx < len(self.movie_rows):
            self.launch_movie(self.movie_rows[idx][4])
        elif tid == "offline-table" and idx < len(self.offline_rows):
            self._play_offline_item(self.offline_rows[idx])
        elif tid == "scrape-table" and idx < len(self.scrape_rows):
            self.queue = [dict(self.scrape_rows[idx])]; self.action_switch_tab("tab-np"); self.play_queue_index(0)


def main() -> None:
    SawStreamApp().run()
