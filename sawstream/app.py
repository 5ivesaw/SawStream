import argparse
import os
import random
import re
import subprocess
import sys
import threading
from pathlib import Path

import yt_dlp
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
from sawstream.artwork import (
    idle_artwork,
    mini_idle_artwork,
    render_mini_thumbnail,
    render_thumbnail,
)
from sawstream.engine import HeadlessMPV
from sawstream.mirrors import (
    extract_media_in_process,
    fetch_anime_feed,
    fetch_movies_feed,
    fetch_yt_feed,
    ping_all_mirrors,
    resolve_audio_stream,
)
from sawstream.scraper import (
    archive_channel_media,
    export_channel_scrape,
    scrape_channel_full,
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
    is_url,
    load_favorites,
    load_settings,
    make_partial_record_path,
    normalize_url,
    record_history,
    sanitize_filename,
    save_settings,
    scan_offline_library,
    toggle_favorite,
)
from sawstream.visualizer import STYLES as VIS_STYLES, AudioVisualizerEngine, render_visualizer

BANNER = r"""
 ███████╗ █████╗ ██╗    ██╗███████╗████████╗██████╗ ███████╗ █████╗ ███╗   ███╗
 ██╔════╝██╔══██╗██║    ██║██╔════╝╚══██╔══╝██╔══██╗██╔════╝██╔══██╗████╗ ████║
 ███████╗███████║██║ █╗ ██║███████╗   ██║   ██████╔╝█████╗  ███████║██╔████╔██║
 ╚════██║██╔══██║██║███╗██║╚════██║   ██║   ██╔══██╗██╔══╝  ██╔══██║██║╚██╔╝██║
 ███████║██║  ██║╚███╔███╔╝███████║   ██║   ██║  ██║███████╗██║  ██║██║ ╚═╝ ██║
 ╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝ ╚══════╝   ╚═╝   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝
"""

EQ_BLOCKS = " ▁▂▃▄▅▆▇█"


def venv_ytdlp_path() -> str:
    cand = Path(sys.executable).parent / "yt-dlp"
    return str(cand) if cand.exists() else "yt-dlp"


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

    /* Full Mode Deck */
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

    /* Minimal Spotify + Visualizer Mode */
    #minimal-view { display: none; height: 1fr; background: #000000; }
    #minimal-view.active { display: block; }
    .hidden-in-minimal { display: none; }

    #min-vis {
        height: 1fr;
        width: 1fr;
        background: #000000;
        padding: 0 1;
    }
    #min-quick {
        display: none;
        height: 3;
        margin: 0 2;
        background: #0d0d10;
        border: tall #ff2e43;
        color: #ffffff;
    }
    #min-quick.open { display: block; }
    #min-bar {
        height: 9;
        background: #09090c;
        border-top: solid #26262e;
        padding: 1 2;
        align: left middle;
    }
    #min-thumb {
        width: 16;
        height: 7;
        background: #050507;
        padding: 0 1;
    }
    #min-meta {
        width: 1fr;
        height: 7;
        margin-left: 2;
    }
    #min-title {
        color: #ffffff;
        text-style: bold;
        height: 1;
    }
    #min-author {
        color: #ff5566;
        height: 1;
    }
    #min-progress {
        width: 1fr;
        margin-top: 1;
    }
    #min-info {
        color: #a1a8b3;
        height: 1;
    }
    #min-hints {
        color: #5a606c;
        height: 1;
        margin-top: 1;
    }

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
        ("v", "toggle_minimal", "Minimal Mode"),
        ("b", "cycle_vis_style", "Vis Style"),
        ("/", "open_quick_play", "Quick Play"),
        ("escape", "close_quick_play", "Close Input"),
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

    def __init__(self, start_minimal: bool = False, initial_target: str = ""):
        super().__init__()
        ensure_dirs()
        cleanup_stale_partials()
        self.settings = load_settings()
        self.mpv = HeadlessMPV(initial_volume=self.settings.get("volume", 100))
        self.vis = AudioVisualizerEngine(num_bands=48)
        self.minimal_mode = start_minimal
        self.initial_target = initial_target
        self.vis_style = self.settings.get("vis_style", "SPEAK")
        if self.vis_style not in VIS_STYLES:
            self.vis_style = "SPEAK"

        self.queue: list[dict] = []
        self.queue_idx: int = -1
        self.is_loading_track = False
        self.is_playing_audio = False
        self._vis_idle_drawn = False
        self.current_source_tag = "IDLE"
        self.last_progress_ratio = 0.0
        self._busy_count = 0
        self._loaded_tabs: set[str] = set()

        self.yt_rows = []
        self.music_rows = []
        self.anime_rows = []
        self.movie_rows = []
        self.offline_rows = []

        self.scrape_data: dict | None = None
        self.scrape_rows: list[dict] = []
        self.scrape_filter = "ALL"
        self.scrape_cancel = threading.Event()

    def _lbl(self, key: str, prefix: str) -> str:
        return f"{prefix}: ON" if self.settings.get(key) else f"{prefix}: OFF"

    def _rep_lbl(self) -> str:
        return f"REP: {self.settings.get('repeat_mode', 'OFF')}"

    def _begin_work(self, msg: str) -> None:
        self._busy_count += 1
        self.query_one("#status-bar", Static).update(msg)
        if not self.minimal_mode:
            self.query_one("#loader", LoadingIndicator).add_class("active")
        else:
            self.query_one("#min-info", Static).update(msg[:90])

    def _end_work(self, msg: str = "") -> None:
        self._busy_count = max(0, self._busy_count - 1)
        if msg:
            self.query_one("#status-bar", Static).update(msg)
            if self.minimal_mode and self.queue_idx < 0:
                self.query_one("#min-info", Static).update(msg[:90])
        if self._busy_count == 0:
            self.query_one("#loader", LoadingIndicator).remove_class("active")

    def set_status(self, text: str) -> None:
        self.query_one("#status-bar", Static).update(text)
        if self.minimal_mode:
            self.query_one("#min-info", Static).update(text[:90])

    def compose(self) -> ComposeResult:
        # --- FULL MODE CHROME ---
        yield Static(BANNER, id="banner", markup=False)
        yield Static(f"SAWSTREAM v{__version__} BY 5SAW  |  KEYS 1-8 TABS  |  [v] MINIMAL VISUALIZER  |  [Space] PAUSE", id="status-bar", markup=False)
        yield LoadingIndicator(id="loader")

        with TabbedContent(id="tabs"):
            with TabPane("1: DECK", id="tab-np"):
                with Horizontal(id="np-top"):
                    yield Static(idle_artwork(), id="np-thumb", markup=False)
                    with Vertical(id="np-meta-box"):
                        yield Static("Nothing Playing", id="np-title", markup=False)
                        yield Static("Author: --", id="np-author", markup=False)
                        yield Static("IDLE  |  Queue: 0 / 0  |  Vol: 100%", id="np-tech", markup=False)
                        yield Static("▁" * 44, id="np-eq", markup=False)
                        yield ProgressBar(total=100, show_eta=False, show_percentage=True, id="np-progress")
                        yield Static("0:00 / 0:00", id="np-time", markup=False)
                        with Horizontal(id="np-controls"):
                            yield Button("PREV", id="ctl-prev")
                            yield Button("PLAY/PAUSE", id="ctl-pause", classes="primary")
                            yield Button("NEXT", id="ctl-next")
                            yield Button("-10s", id="ctl-back")
                            yield Button("+10s", id="ctl-fwd")
                            yield Button(self._rep_lbl(), id="ctl-repeat")
                            yield Button("MINIMAL", id="ctl-minimal", classes="primary")
                            yield Button(self._lbl("auto_save_music", "SAVE"), id="ctl-save-music", classes="on" if self.settings["auto_save_music"] else "")
                with Horizontal(id="queue-header"):
                    yield Static("UP NEXT QUEUE (ENTER TO JUMP)", id="queue-label", markup=False)
                    yield Button("SHUFFLE", id="q-shuffle")
                    yield Button("FAVORITES", id="q-favs")
                    yield Button("FAV", id="ctl-fav")
                    yield Button("REMOVE", id="q-del")
                    yield Button("CLEAR", id="q-clear")
                yield DataTable(id="queue-table", cursor_type="row")

            with TabPane("2: YOUTUBE", id="tab-yt"):
                with Horizontal(classes="toolbar"):
                    yield Input(placeholder="Search YouTube OR paste any Video / Playlist URL...", id="yt-input")
                    yield Button("SEARCH / LOAD", id="yt-search", classes="primary")
                    yield Button("HOME", id="yt-home")
                yield DataTable(id="yt-table", cursor_type="row")
                with Horizontal(classes="actions"):
                    yield Button("WATCH VIDEO", id="yt-play-win", classes="primary")
                    yield Button("PLAY AUDIO IN DECK", id="yt-play-audio")
                    yield Button(self._lbl("auto_save_video", "SAVE VID"), id="ctl-save-video", classes="on" if self.settings["auto_save_video"] else "")
                    yield Button("DOWNLOAD MP4", id="yt-dl")

            with TabPane("3: MUSIC", id="tab-music"):
                with Horizontal(classes="toolbar"):
                    yield Input(placeholder="Search song/artist OR paste YouTube / Spotify URL...", id="music-input")
                    yield Button("SEARCH / PLAY", id="music-search", classes="primary")
                    yield Button("TRENDING", id="music-home")
                yield DataTable(id="music-table", cursor_type="row")
                with Horizontal(classes="actions"):
                    yield Button("PLAY IN DECK", id="music-play", classes="primary")
                    yield Button("+ QUEUE", id="music-queue")
                    yield Button("PLAY ALL OFFLINE", id="music-offline")
                    yield Button("DOWNLOAD MP3", id="music-dl")

            with TabPane("4: ANIME", id="tab-anime"):
                with Horizontal(classes="toolbar"):
                    yield Input(placeholder="Search Anime (AniList + Jikan/MAL)...", id="anime-input")
                    yield Button("SEARCH", id="anime-search", classes="primary")
                    yield Button("TRENDING", id="anime-home")
                yield DataTable(id="anime-table", cursor_type="row")
                with Horizontal(classes="actions"):
                    yield Button("WATCH SUB", id="anime-sub", classes="primary")
                    yield Button("WATCH DUB", id="anime-dub")
                    yield Button(self._lbl("auto_save_anime", "SAVE ANIME"), id="ctl-save-anime", classes="on" if self.settings["auto_save_anime"] else "")

            with TabPane("5: MOVIES", id="tab-movies"):
                with Horizontal(classes="toolbar"):
                    yield Input(placeholder="Search Movies or TV Series (YTS + TVMaze)...", id="movie-input")
                    yield Button("SEARCH", id="movie-search", classes="primary")
                    yield Button("TRENDING", id="movie-home")
                yield DataTable(id="movie-table", cursor_type="row")
                with Horizontal(classes="actions"):
                    yield Button("STREAM MOVIE/TV", id="movie-stream", classes="primary")
                    yield Button(self._lbl("auto_save_movies", "SAVE MOVIE"), id="ctl-save-movies", classes="on" if self.settings["auto_save_movies"] else "")

            with TabPane("6: OFFLINE", id="tab-offline"):
                with Horizontal(classes="toolbar"):
                    yield Input(placeholder="Filter local offline library...", id="offline-filter")
                    yield Button("RESCAN", id="offline-rescan", classes="primary")
                    yield Button("PLAY ALL MUSIC", id="offline-play-all")
                yield DataTable(id="offline-table", cursor_type="row")
                with Horizontal(classes="actions"):
                    yield Button("PLAY SELECTED", id="offline-play", classes="primary")
                    yield Button("DELETE FILE", id="offline-delete")

            with TabPane("7: SCRAPER", id="tab-scraper"):
                with Horizontal(classes="toolbar"):
                    yield Input(placeholder="Paste @handle, Channel URL, or Playlist URL...", id="scrape-input")
                    yield Button("PUBLIC PLAYLISTS", id="scrape-pl", classes="primary")
                    yield Button("FULL CHANNEL", id="scrape-full")
                    yield Button("STOP", id="scrape-stop")
                with Horizontal(classes="actions"):
                    yield Button("ALL", id="sf-all")
                    yield Button("PLAYLISTS", id="sf-playlists")
                    yield Button("VIDEOS", id="sf-videos")
                    yield Button("SHORTS", id="sf-shorts")
                    yield Button("QUEUE ALL", id="scrape-queue", classes="primary")
                    yield Button("WATCH", id="scrape-watch")
                    yield Button("EXPORT", id="scrape-export")
                    yield Button("MP3", id="scrape-mp3")
                    yield Button("MKV", id="scrape-mkv")
                yield DataTable(id="scrape-table", cursor_type="row")

            with TabPane("8: MIRRORS", id="tab-mirrors"):
                with Horizontal(classes="toolbar"):
                    yield Button("PING ALL MIRRORS", id="mirror-check", classes="primary")
                yield DataTable(id="mirror-table", cursor_type="row")

        yield Footer()

        # --- MINIMAL VISUALIZER + SPOTIFY-LIKE BAR ---
        with Vertical(id="minimal-view"):
            yield Static("", id="min-vis", markup=False)
            yield Input(
                placeholder="Quick Play: type a song name OR paste YouTube / Spotify / Playlist URL (Esc to close)...",
                id="min-quick",
            )
            with Horizontal(id="min-bar"):
                yield Static(mini_idle_artwork(), id="min-thumb", markup=False)
                with Vertical(id="min-meta"):
                    yield Static("Nothing Playing", id="min-title", markup=False)
                    yield Static("Press [/] to play a song, or [v] to open Full Mode", id="min-author", markup=False)
                    yield ProgressBar(total=100, show_eta=False, show_percentage=False, id="min-progress")
                    yield Static("0:00 / 0:00  ·  IDLE", id="min-info", markup=False)
                    yield Static("[v] Full  [b] Style  [/] Play  [Space] Pause  [n/p] Skip  [s] Save  [f] Fav  [-/=] Vol  [q] Quit", id="min-hints", markup=False)

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
        self.set_interval(0.04, self.tick_visualizer)
        self.refresh_offline_table()
        if self.minimal_mode:
            self._apply_minimal_mode(True)
        if self.initial_target:
            self.load_and_play_url(self.initial_target)

    def on_unmount(self) -> None:
        self.scrape_cancel.set()
        self.vis.stop()
        self.mpv.shutdown()

    # --- MINIMAL MODE & REAL-TIME VISUALIZER ---
    def _apply_minimal_mode(self, enabled: bool) -> None:
        self.minimal_mode = enabled
        chrome_ids = ("#banner", "#status-bar", "#loader", "#tabs")
        for cid in chrome_ids:
            w = self.query_one(cid)
            w.add_class("hidden-in-minimal") if enabled else w.remove_class("hidden-in-minimal")
        footer = self.query_one(Footer)
        footer.add_class("hidden-in-minimal") if enabled else footer.remove_class("hidden-in-minimal")

        mv = self.query_one("#minimal-view")
        if enabled:
            self._vis_idle_drawn = False
            mv.add_class("active")
            self.vis.start()
            self.set_focus(None)
        else:
            mv.remove_class("active")
            self.query_one("#min-quick", Input).remove_class("open")
            self.vis.stop()

    def action_toggle_minimal(self) -> None:
        if isinstance(self.focused, Input):
            return
        self._apply_minimal_mode(not self.minimal_mode)

    def action_cycle_vis_style(self) -> None:
        if isinstance(self.focused, Input):
            return
        idx = (VIS_STYLES.index(self.vis_style) + 1) % len(VIS_STYLES)
        self.vis_style = VIS_STYLES[idx]
        self.settings["vis_style"] = self.vis_style
        self._vis_idle_drawn = False
        save_settings(self.settings)
        if not self.minimal_mode:
            self.set_status(f"VISUALIZER STYLE: {self.vis_style} (Press 'v' for Minimal Mode)")

    def action_open_quick_play(self) -> None:
        if isinstance(self.focused, Input):
            return
        if self.minimal_mode:
            inp = self.query_one("#min-quick", Input)
            inp.add_class("open")
            inp.focus()
        else:
            self.action_switch_tab("tab-music")
            self.query_one("#music-input", Input).focus()

    def action_close_quick_play(self) -> None:
        inp = self.query_one("#min-quick", Input)
        if inp.has_class("open"):
            inp.remove_class("open")
            inp.value = ""
        self.set_focus(None)

    def tick_visualizer(self) -> None:
        if not self.minimal_mode:
            return
        frame = self.vis.step(self.is_playing_audio)
        if frame["quiet"]:
            if self._vis_idle_drawn:
                return
            self._vis_idle_drawn = True
        else:
            self._vis_idle_drawn = False
        vis_widget = self.query_one("#min-vis", Static)
        w = max(24, vis_widget.size.width - 2)
        h = max(6, vis_widget.size.height)
        src = "LIVE PCM" if frame["live"] else ("REACTIVE" if self.is_playing_audio else "IDLE")
        tag = f"SAWSTREAM · {self.vis_style} · {src}"
        vis_widget.update(render_visualizer(frame, width=w, height=h, style_name=self.vis_style, header_tag=tag))

    @on(TabbedContent.TabActivated)
    def on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        tid = event.pane.id
        if tid in self._loaded_tabs:
            return
        if tid == "tab-yt" and not self.yt_rows:
            self._loaded_tabs.add(tid)
            self.load_yt("")
        elif tid == "tab-music" and not self.music_rows:
            self._loaded_tabs.add(tid)
            self.load_music("")
        elif tid == "tab-anime" and not self.anime_rows:
            self._loaded_tabs.add(tid)
            self.load_anime("")
        elif tid == "tab-movies" and not self.movie_rows:
            self._loaded_tabs.add(tid)
            self.load_movies("")

    def refresh_queue_table(self) -> None:
        t = self.query_one("#queue-table", DataTable)
        t.clear()
        for i, item in enumerate(self.queue):
            t.add_row(f"> {i + 1}" if i == self.queue_idx else f"  {i + 1}", item["title"][:56], item["author"][:24], fmt_dur(item.get("duration", 0)))

    def refresh_offline_table(self, needle: str = "") -> None:
        all_items = scan_offline_library()
        q = needle.lower().strip()
        self.offline_rows = [it for it in all_items if not q or q in it["title"].lower() or q in it["kind"].lower()]
        t = self.query_one("#offline-table", DataTable)
        t.clear()
        for it in self.offline_rows:
            t.add_row(it["kind"], it["title"][:64], it["size"])

    def refresh_scrape_table(self) -> None:
        t = self.query_one("#scrape-table", DataTable)
        t.clear()
        if not self.scrape_data:
            self.scrape_rows = []
            return
        f = self.scrape_filter
        self.scrape_rows = [x for x in self.scrape_data.get("items", []) if f == "ALL" or x.get("section") == f]
        for x in self.scrape_rows:
            t.add_row(x["section"], (x["playlist"] or "-")[:22], x["title"][:48], fmt_dur(x["duration"]))

    def poll_player(self) -> None:
        if self.queue_idx < 0 or self.is_loading_track:
            self.is_playing_audio = False
            return
        st = self.mpv.snapshot_state()
        if st.get("idle") is True:
            self.is_playing_audio = False
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
                self.query_one("#min-info", Static).update(f"FINISHED  ·  Queue {self.queue_idx + 1}/{len(self.queue)}")
            return

        pos = float(st.get("pos") or 0.0)
        dur = float(st.get("dur") or self.queue[self.queue_idx].get("duration") or 0.0)
        if dur > 0:
            self.last_progress_ratio = max(self.last_progress_ratio, min(1.0, pos / dur))

        paused = st.get("paused", False)
        muted = st.get("muted", False)
        self.is_playing_audio = not (paused or muted)

        vol = int(st.get("vol")) if st.get("vol") is not None else self.settings.get("volume", 100)
        cache = int(st.get("cache") or 0)
        kbps = int((st.get("bitrate") or 0) / 1000)
        codec = (st.get("codec") or "").upper()

        state_str = "PAUSED" if paused else "PLAYING"
        vol_str = "MUTED" if muted else f"{vol}%"
        time_str = f"{fmt_dur(pos)} / {fmt_dur(dur)}"

        # Update Full Deck widgets
        tech_parts = [f"{state_str} [{self.current_source_tag}]", f"Queue {self.queue_idx + 1}/{len(self.queue)}", f"Vol {vol_str}"]
        if codec:
            tech_parts.append(f"{codec} {kbps}k" if kbps else codec)
        if cache > 0 and self.current_source_tag != "OFFLINE":
            tech_parts.append(f"Buf {cache}s")
        self.query_one("#np-tech", Static).update("  |  ".join(tech_parts))
        self.query_one("#np-time", Static).update(time_str)
        if dur > 0:
            self.query_one("#np-progress", ProgressBar).update(total=dur, progress=min(pos, dur))
        if not self.minimal_mode:
            self.query_one("#np-eq", Static).update("▁" * 44 if (paused or muted) else "".join(EQ_BLOCKS[random.randint(1, len(EQ_BLOCKS) - 1)] for _ in range(44)))

        # Update Minimal Spotify-like Bar
        save_tag = "AUTO-SAVE" if self.settings.get("auto_save_music") else "STREAM ONLY"
        self.query_one("#min-info", Static).update(
            f"{time_str}   ·   {state_str} [{self.current_source_tag}]   ·   {self.queue_idx + 1}/{len(self.queue)}   ·   Vol {vol_str}   ·   {save_tag}"
        )
        if dur > 0:
            self.query_one("#min-progress", ProgressBar).update(total=dur, progress=min(pos, dur))

    # --- WORKERS ---
    @work(thread=True, group="yt", exclusive=True)
    def load_yt(self, query: str):
        self.call_from_thread(self._begin_work, f"LOADING YOUTUBE: {query or 'HOME'}...")
        msg = "YOUTUBE READY"
        try:
            if is_url(query):
                tracks = extract_media_in_process(query)
                rows = [(tr["title"][:60], tr["author"][:24], fmt_dur(tr["duration"]), tr["webpage_url"]) for tr in tracks]
                src = "DIRECT URL"
            else:
                rows, src = fetch_yt_feed(query, music_only=False)
            self.yt_rows = rows
            def update():
                t = self.query_one("#yt-table", DataTable)
                t.clear()
                for r in rows:
                    t.add_row(r[0], r[1], r[2])
            self.call_from_thread(update)
            msg = f"YOUTUBE: {len(rows)} RESULTS ({src})" if rows else f"YOUTUBE: NO RESULTS ({src})"
        except Exception as e:
            msg = f"YOUTUBE ERROR: {e}"
        finally:
            self.call_from_thread(self._end_work, msg)

    @work(thread=True, group="music", exclusive=True)
    def load_music(self, query: str):
        self.call_from_thread(self._begin_work, f"SEARCHING MUSIC: {query or 'TRENDING'}...")
        msg = "MUSIC READY"
        try:
            rows, src = fetch_yt_feed(query, music_only=True)
            self.music_rows = rows
            def update():
                t = self.query_one("#music-table", DataTable)
                t.clear()
                for r in rows:
                    t.add_row(r[0], r[1], r[2])
            self.call_from_thread(update)
            msg = f"MUSIC: {len(rows)} TRACKS ({src})" if rows else f"MUSIC: NO RESULTS ({src})"
        except Exception as e:
            msg = f"MUSIC ERROR: {e}"
        finally:
            self.call_from_thread(self._end_work, msg)

    @work(thread=True, group="anime", exclusive=True)
    def load_anime(self, query: str):
        self.call_from_thread(self._begin_work, f"FETCHING ANIME: {query or 'TRENDING'}...")
        msg = "ANIME READY"
        try:
            rows, src = fetch_anime_feed(query)
            self.anime_rows = rows
            def update():
                t = self.query_one("#anime-table", DataTable)
                t.clear()
                for r in rows:
                    t.add_row(r[0], r[1], r[2], r[3])
            self.call_from_thread(update)
            msg = f"ANIME: {len(rows)} TITLES ({src})"
        except Exception as e:
            msg = f"ANIME ERROR: {e}"
        finally:
            self.call_from_thread(self._end_work, msg)

    @work(thread=True, group="movies", exclusive=True)
    def load_movies(self, query: str):
        self.call_from_thread(self._begin_work, f"FETCHING MOVIES: {query or 'TRENDING'}...")
        msg = "MOVIES READY"
        try:
            rows, src = fetch_movies_feed(query)
            self.movie_rows = rows
            def update():
                t = self.query_one("#movie-table", DataTable)
                t.clear()
                for r in rows:
                    t.add_row(r[0], r[1], r[2], r[3])
            self.call_from_thread(update)
            msg = f"MOVIES: {len(rows)} TITLES ({src})"
        except Exception as e:
            msg = f"MOVIES ERROR: {e}"
        finally:
            self.call_from_thread(self._end_work, msg)

    @work(thread=True, group="mirrors", exclusive=True)
    def check_mirrors(self):
        self.call_from_thread(self._begin_work, "PINGING ALL MIRRORS...")
        msg = "MIRROR CHECK DONE"
        try:
            results = ping_all_mirrors()
            def update():
                t = self.query_one("#mirror-table", DataTable)
                t.clear()
                for r in results:
                    t.add_row(*r)
            self.call_from_thread(update)
            online = sum(1 for r in results if r[2] == "ONLINE")
            msg = f"MIRRORS: {online}/{len(results)} ONLINE"
        except Exception as e:
            msg = f"MIRROR CHECK ERROR: {e}"
        finally:
            self.call_from_thread(self._end_work, msg)

    @work(thread=True, group="load_url", exclusive=True)
    def load_and_play_url(self, raw_input: str, append_only: bool = False):
        raw_input = raw_input.strip()
        if not raw_input:
            return
        # Support direct song-name search from Quick Play bar in Minimal Mode!
        if not is_url(raw_input):
            self.call_from_thread(self._begin_work, f"QUICK SEARCH: {raw_input}...")
            msg = ""
            try:
                rows, _ = fetch_yt_feed(raw_input, music_only=True)
                if not rows:
                    msg = f"NO SONG FOUND FOR '{raw_input}'."
                    return
                tracks = [
                    {
                        "id": extract_vid_id(r[3]),
                        "title": r[0],
                        "author": r[1],
                        "duration": 0,
                        "thumbnail": f"https://i.ytimg.com/vi/{extract_vid_id(r[3])}/hqdefault.jpg",
                        "webpage_url": r[3],
                    }
                    for r in rows[:15]
                ]
                self.queue = tracks
                if not self.minimal_mode:
                    self.call_from_thread(self.action_switch_tab, "tab-np")
                self.call_from_thread(self.play_queue_index, 0)
                msg = f"QUEUED {len(tracks)} TRACKS FOR '{raw_input}'"
            except Exception as e:
                msg = f"QUICK SEARCH ERROR: {e}"
            finally:
                self.call_from_thread(self._end_work, msg)
            return

        url = normalize_url(raw_input)
        vid_id = extract_vid_id(url)
        if vid_id and "list=" not in url and "open.spotify.com" not in url:
            local_file = find_offline_file(MUSIC_DIR, vid_id=vid_id)
            if local_file:
                clean = re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]$", "", Path(local_file).stem)
                track = {"id": vid_id, "title": clean, "author": "Offline Library", "duration": 0, "thumbnail": "", "webpage_url": url, "local_path": local_file}
                if append_only and self.queue:
                    self.queue.append(track)
                    self.call_from_thread(self.refresh_queue_table)
                    self.call_from_thread(self.set_status, f"QUEUED OFFLINE: {clean}")
                else:
                    self.queue = [track]
                    if not self.minimal_mode:
                        self.call_from_thread(self.action_switch_tab, "tab-np")
                    self.call_from_thread(self.play_queue_index, 0)
                return

        self.call_from_thread(self._begin_work, "EXTRACTING TRACKS...")
        msg = ""
        try:
            tracks = extract_media_in_process(url)
            if not tracks:
                msg = "NO PLAYABLE TRACKS FOUND AT THAT LINK."
                return
            if append_only and self.queue:
                self.queue.extend(tracks)
                self.call_from_thread(self.refresh_queue_table)
                msg = f"QUEUED {len(tracks)} TRACK(S)."
                return
            self.queue = tracks
            if not self.minimal_mode:
                self.call_from_thread(self.action_switch_tab, "tab-np")
            self.call_from_thread(self.play_queue_index, 0)
            msg = f"LOADED {len(tracks)} TRACK(S)."
        except Exception as e:
            msg = f"LINK ERROR: {e}"
        finally:
            self.call_from_thread(self._end_work, msg)

    @work(thread=True, group="play_track", exclusive=True)
    def play_queue_index(self, idx: int):
        if idx < 0 or idx >= len(self.queue):
            return
        self.is_loading_track = True
        self.call_from_thread(self._begin_work, f"LOADING: {self.queue[idx]['title'][:48]}...")
        msg = ""
        try:
            prev_completed = self.last_progress_ratio >= 0.85
            self.last_progress_ratio = 0.0
            self.queue_idx = idx
            item = self.queue[idx]
            vid_id = item.get("id") or extract_vid_id(item.get("webpage_url", ""))

            self.call_from_thread(self.refresh_queue_table)
            self.call_from_thread(self.query_one("#np-title", Static).update, f"Loading: {item['title'][:65]}...")
            self.call_from_thread(self.query_one("#min-title", Static).update, f"Loading: {item['title'][:65]}...")

            local_path = item.get("local_path") or (find_offline_file(MUSIC_DIR, vid_id=vid_id) if vid_id else None)
            if local_path and os.path.exists(local_path):
                self.current_source_tag = "OFFLINE"
                thumb_full = render_thumbnail(item.get("thumbnail", ""), vid_id=vid_id)
                thumb_mini = render_mini_thumbnail(item.get("thumbnail", ""), vid_id=vid_id)
                if self.mpv.play_source(local_path, prev_completed=prev_completed):
                    self.call_from_thread(self.refresh_offline_table)
                record_history(item)
                self.is_playing_audio = True

                def update_offline_ui():
                    self.query_one("#np-thumb", Static).update(thumb_full)
                    self.query_one("#np-title", Static).update(item["title"][:72])
                    self.query_one("#np-author", Static).update(f"Author: {item['author']}  ·  {Path(local_path).name[:30]}")
                    self.query_one("#np-progress", ProgressBar).update(total=float(item.get("duration") or 100), progress=0)
                    self.query_one("#min-thumb", Static).update(thumb_mini)
                    self.query_one("#min-title", Static).update(item["title"][:78])
                    self.query_one("#min-author", Static).update(item["author"][:60])
                    self.query_one("#min-progress", ProgressBar).update(total=float(item.get("duration") or 100), progress=0)
                    self.refresh_queue_table()

                self.call_from_thread(update_offline_ui)
                msg = f"[OFFLINE] {item['title'][:58]}"
                return

            resolved = resolve_audio_stream(item["webpage_url"])
            if not resolved or not resolved.get("stream_url"):
                msg = "FAILED TO RESOLVE AUDIO STREAM."
                return

            self.queue[idx].update(resolved)
            vid_id = resolved.get("id") or vid_id
            partial_path = final_path = ""
            if self.settings.get("auto_save_music"):
                partial_path, final_path = make_partial_record_path(resolved["title"], vid_id)
                self.current_source_tag = "STREAM + SAVING"
            else:
                self.current_source_tag = "STREAM"

            thumb_full = render_thumbnail(resolved["thumbnail"], vid_id=vid_id)
            thumb_mini = render_mini_thumbnail(resolved["thumbnail"], vid_id=vid_id)
            if self.mpv.play_source(resolved["stream_url"], partial_path=partial_path, final_path=final_path, prev_completed=prev_completed):
                self.call_from_thread(self.refresh_offline_table)
            record_history(resolved)
            self.is_playing_audio = True

            def update_ui():
                self.query_one("#np-thumb", Static).update(thumb_full)
                self.query_one("#np-title", Static).update(resolved["title"][:72])
                self.query_one("#np-author", Static).update(f"Author: {resolved['author']}")
                self.query_one("#np-progress", ProgressBar).update(total=float(resolved["duration"] or 100), progress=0)
                self.query_one("#min-thumb", Static).update(thumb_mini)
                self.query_one("#min-title", Static).update(resolved["title"][:78])
                self.query_one("#min-author", Static).update(resolved["author"][:60])
                self.query_one("#min-progress", ProgressBar).update(total=float(resolved["duration"] or 100), progress=0)
                self.refresh_queue_table()

            self.call_from_thread(update_ui)
            msg = f"PLAYING: {resolved['title'][:50]}{' (Auto-saving)' if partial_path else ''}"
        except Exception as e:
            msg = f"PLAYBACK ERROR: {e}"
        finally:
            self.is_loading_track = False
            self.call_from_thread(self._end_work, msg)

    @work(thread=True, group="scraper", exclusive=True)
    def run_scrape(self, raw: str, playlists_only: bool = True):
        self.scrape_cancel.clear()
        self.scrape_data = None
        self.scrape_rows = []
        self.scrape_filter = "ALL"

        def reset_tbl():
            self.query_one("#scrape-table", DataTable).clear()
            self._begin_work(f"SCRAPING {'PLAYLISTS' if playlists_only else 'FULL CHANNEL'}: {raw}...")

        self.call_from_thread(reset_tbl)
        msg = "SCRAPE FINISHED"
        try:
            def on_item(rec: dict):
                self.scrape_rows.append(rec)
                def add_row():
                    self.query_one("#scrape-table", DataTable).add_row(
                        rec["section"],
                        (rec["playlist"] or "-")[:22],
                        rec["title"][:48],
                        fmt_dur(rec["duration"]),
                    )
                self.call_from_thread(add_row)

            d = scrape_channel_full(
                raw,
                playlists_only=playlists_only,
                progress_cb=lambda m: self.call_from_thread(self.set_status, m),
                item_cb=on_item,
                cancel_event=self.scrape_cancel,
            )
            self.scrape_data = d
            msg = f"SCRAPED {d['channel_name']}: {d['playlist_count']} PLAYLISTS, {d['unique_video_count']} UNIQUE VIDEOS"
            if d["unique_video_count"] == 0:
                msg = f"NO PUBLIC ITEMS FOUND FOR '{raw}' (CHECK HANDLE / PLAYLIST PRIVACY)."
        except Exception as e:
            msg = f"SCRAPE ERROR: {e}"
        finally:
            self.call_from_thread(self._end_work, msg)

    @work(thread=True, group="archive", exclusive=True)
    def run_archive(self, audio: bool):
        if not self.scrape_data:
            self.call_from_thread(self.set_status, "SCAN A CHANNEL FIRST.")
            return
        self.scrape_cancel.clear()
        self.call_from_thread(self._begin_work, f"ARCHIVING {'MP3' if audio else 'MKV'}...")
        msg = "ARCHIVE DONE"
        try:
            dest, done, failed = archive_channel_media(
                self.scrape_data,
                audio_only=audio,
                progress_cb=lambda m: self.call_from_thread(self.set_status, m),
                cancel_event=self.scrape_cancel,
            )
            self.call_from_thread(self.refresh_offline_table)
            msg = f"ARCHIVED {done} FILES ({failed} ERRORS) -> {dest}"
        except Exception as e:
            msg = f"ARCHIVE ERROR: {e}"
        finally:
            self.call_from_thread(self._end_work, msg)

    @work(thread=True)
    def download_in_background(self, url: str, audio_only: bool):
        url = normalize_url(url)
        self.call_from_thread(self._begin_work, f"DOWNLOADING {'MP3' if audio_only else 'MP4'}...")
        dest = MUSIC_DIR if audio_only else VIDEO_DIR
        opts = {"quiet": True, "no_warnings": True, "socket_timeout": 15, "outtmpl": str(dest / "%(title)s [%(id)s].%(ext)s")}
        if audio_only:
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}]
        else:
            opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best"
        msg = f"SAVED TO {dest}"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            self.call_from_thread(self.refresh_offline_table)
        except Exception as e:
            msg = f"DOWNLOAD FAILED: {e}"
        finally:
            self.call_from_thread(self._end_work, msg)

    def spawn_detached_video(self, url: str, title: str = "") -> None:
        url = normalize_url(url)
        vid_id = extract_vid_id(url)
        local_vid = find_offline_file(VIDEO_DIR, vid_id=vid_id) if vid_id else None
        if local_vid:
            subprocess.Popen(["mpv", "--no-terminal", "--really-quiet", local_vid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.set_status(f"[OFFLINE VIDEO] {Path(local_vid).name}")
            return

        cmd = ["mpv", "--no-terminal", "--really-quiet", f"--script-opts=ytdl_hook-ytdl_path={venv_ytdlp_path()}"]
        if self.settings.get("auto_save_video") and vid_id:
            out_path = str(VIDEO_DIR / f"{sanitize_filename(title or vid_id)} [{vid_id}].mkv")
            cmd.append(f"--stream-record={out_path}")
            self.set_status(f"WATCHING + SAVING -> ~/Downloads/{Path(out_path).name}")
        else:
            self.set_status(f"OPENING VIDEO WINDOW: {(title or url)[:48]}")
        cmd.append(url)
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def launch_anime(self, title: str, dub: bool = False) -> None:
        local = find_offline_file(ANIME_DIR, exact_title=title)
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
        local = find_offline_file(MOVIE_DIR, exact_title=title)
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

    # --- ACTIONS ---
    def action_switch_tab(self, tab_id: str) -> None:
        if isinstance(self.focused, Input):
            return
        if self.minimal_mode:
            self._apply_minimal_mode(False)
        self.query_one("#tabs", TabbedContent).active = tab_id

    def action_toggle_pause(self) -> None:
        if not isinstance(self.focused, Input):
            self.mpv.toggle_pause()

    def action_toggle_mute(self) -> None:
        if not isinstance(self.focused, Input):
            self.mpv.toggle_mute()

    def action_next_track(self) -> None:
        if not isinstance(self.focused, Input) and self.queue_idx + 1 < len(self.queue):
            self.play_queue_index(self.queue_idx + 1)

    def action_prev_track(self) -> None:
        if not isinstance(self.focused, Input) and self.queue_idx - 1 >= 0:
            self.play_queue_index(self.queue_idx - 1)

    def action_seek_back(self) -> None:
        if not isinstance(self.focused, Input):
            self.mpv.seek(-10)

    def action_seek_fwd(self) -> None:
        if not isinstance(self.focused, Input):
            self.mpv.seek(10)

    def action_vol_down(self) -> None:
        if not isinstance(self.focused, Input):
            self.settings["volume"] = self.mpv.adjust_volume(-5)
            save_settings(self.settings)

    def action_vol_up(self) -> None:
        if not isinstance(self.focused, Input):
            self.settings["volume"] = self.mpv.adjust_volume(5)
            save_settings(self.settings)

    def action_cycle_repeat(self) -> None:
        if isinstance(self.focused, Input):
            return
        modes = ["OFF", "ALL", "ONE"]
        nxt = modes[(modes.index(self.settings.get("repeat_mode", "OFF")) + 1) % len(modes)]
        self.settings["repeat_mode"] = nxt
        save_settings(self.settings)
        self.query_one("#ctl-repeat", Button).label = self._rep_lbl()
        self.set_status(f"REPEAT: {nxt}")

    def action_shuffle_queue(self) -> None:
        if isinstance(self.focused, Input) or len(self.queue) <= 1:
            return
        current = self.queue[self.queue_idx] if 0 <= self.queue_idx < len(self.queue) else None
        rest = [x for i, x in enumerate(self.queue) if i != self.queue_idx]
        random.shuffle(rest)
        self.queue = ([current] + rest) if current else rest
        self.queue_idx = 0 if current else -1
        self.refresh_queue_table()
        self.set_status("QUEUE SHUFFLED.")

    def action_favorite_current(self) -> None:
        if isinstance(self.focused, Input):
            return
        if 0 <= self.queue_idx < len(self.queue):
            added = toggle_favorite(self.queue[self.queue_idx])
            self.set_status(f"{'FAVORITED' if added else 'UNFAVORITED'}: {self.queue[self.queue_idx]['title'][:45]}")

    def action_toggle_save(self, key: str) -> None:
        if isinstance(self.focused, Input):
            return
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
        self.set_status(f"{prefix}: {'ON -> ' + folder if self.settings[key] else 'OFF'}")

    def load_offline_music(self) -> None:
        tracks = [it for it in scan_offline_library() if it["kind"] == "MUSIC"]
        if not tracks:
            self.set_status("NO OFFLINE SONGS IN ~/Music YET.")
            return
        self.queue = tracks
        if not self.minimal_mode:
            self.action_switch_tab("tab-np")
        self.play_queue_index(0)

    def _get_yt_selected_or_pasted(self) -> tuple[str, str]:
        pasted = self.query_one("#yt-input", Input).value.strip()
        t = self.query_one("#yt-table", DataTable)
        if self.yt_rows and 0 <= t.cursor_row < len(self.yt_rows):
            title, _, _, url = self.yt_rows[t.cursor_row]
            if is_url(pasted) and extract_vid_id(pasted) and extract_vid_id(pasted) != extract_vid_id(url):
                return pasted, normalize_url(pasted)
            return title, url
        if is_url(pasted):
            return pasted, normalize_url(pasted)
        return "", ""

    def _play_offline_item(self, item: dict) -> None:
        if item["kind"] == "MUSIC":
            self.queue = [item]
            if not self.minimal_mode:
                self.action_switch_tab("tab-np")
            self.play_queue_index(0)
        else:
            subprocess.Popen(["mpv", "--no-terminal", "--really-quiet", item["local_path"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.set_status(f"[OFFLINE {item['kind']}] {item['title'][:50]}")

    # --- INPUTS ---
    @on(Input.Submitted, "#min-quick")
    def on_min_quick_submit(self, event: Input.Submitted):
        val = event.value.strip()
        inp = self.query_one("#min-quick", Input)
        inp.value = ""
        inp.remove_class("open")
        self.set_focus(None)
        if val:
            self.load_and_play_url(val)

    @on(Input.Submitted, "#yt-input")
    def on_yt_submit(self, event: Input.Submitted):
        self.load_yt(event.value.strip())

    @on(Input.Submitted, "#music-input")
    def on_music_submit(self, event: Input.Submitted):
        val = event.value.strip()
        self.load_and_play_url(val) if is_url(val) else self.load_music(val)

    @on(Input.Submitted, "#anime-input")
    def on_anime_submit(self, event: Input.Submitted):
        self.load_anime(event.value.strip())

    @on(Input.Submitted, "#movie-input")
    def on_movie_submit(self, event: Input.Submitted):
        self.load_movies(event.value.strip())

    @on(Input.Submitted, "#scrape-input")
    def on_scrape_submit(self, event: Input.Submitted):
        val = event.value.strip()
        if val:
            self.run_scrape(val, playlists_only=True)

    @on(Input.Changed, "#offline-filter")
    def on_offline_filter(self, event: Input.Changed):
        self.refresh_offline_table(event.value)

    # --- BUTTONS ---
    @on(Button.Pressed)
    def on_button(self, event: Button.Pressed):
        bid = event.button.id
        if bid == "ctl-pause": self.mpv.toggle_pause()
        elif bid == "ctl-next": self.action_next_track()
        elif bid == "ctl-prev": self.action_prev_track()
        elif bid == "ctl-back": self.action_seek_back()
        elif bid == "ctl-fwd": self.action_seek_fwd()
        elif bid == "ctl-repeat": self.action_cycle_repeat()
        elif bid == "ctl-minimal": self._apply_minimal_mode(True)
        elif bid == "ctl-fav": self.action_favorite_current()
        elif bid == "ctl-save-music": self.action_toggle_save("auto_save_music")
        elif bid == "ctl-save-video": self.action_toggle_save("auto_save_video")
        elif bid == "ctl-save-anime": self.action_toggle_save("auto_save_anime")
        elif bid == "ctl-save-movies": self.action_toggle_save("auto_save_movies")
        elif bid == "q-shuffle": self.action_shuffle_queue()
        elif bid == "q-favs":
            favs = load_favorites()
            if favs:
                self.queue = favs
                self.play_queue_index(0)
            else:
                self.set_status("NO FAVORITES YET (PRESS 'f' WHILE PLAYING).")
        elif bid == "q-del":
            t = self.query_one("#queue-table", DataTable)
            if self.queue and 0 <= t.cursor_row < len(self.queue):
                idx = t.cursor_row
                self.queue.pop(idx)
                if idx < self.queue_idx: self.queue_idx -= 1
                elif idx == self.queue_idx: self.mpv.stop(); self.queue_idx = -1
                self.refresh_queue_table()
        elif bid == "q-clear":
            self.mpv.stop()
            self.queue = []
            self.queue_idx = -1
            self.is_playing_audio = False
            self.refresh_queue_table()
            self.query_one("#np-thumb", Static).update(idle_artwork())
            self.query_one("#np-title", Static).update("Nothing Playing")
            self.query_one("#np-author", Static).update("Author: --")
            self.query_one("#np-eq", Static).update("▁" * 44)
            self.query_one("#min-thumb", Static).update(mini_idle_artwork())
            self.query_one("#min-title", Static).update("Nothing Playing")
            self.query_one("#min-author", Static).update("Press [/] to play a song, or [v] to open Full Mode")
            self.query_one("#min-info", Static).update("0:00 / 0:00  ·  IDLE")
        elif bid == "yt-search":
            self.load_yt(self.query_one("#yt-input", Input).value.strip())
        elif bid == "yt-home":
            self.query_one("#yt-input", Input).value = ""
            self.load_yt("")
        elif bid in ("yt-play-audio", "yt-play-win", "yt-dl"):
            title, url = self._get_yt_selected_or_pasted()
            if not url:
                self.set_status("SELECT A VIDEO OR PASTE A URL FIRST.")
                return
            if bid == "yt-play-audio": self.load_and_play_url(url)
            elif bid == "yt-play-win": self.spawn_detached_video(url, title=title)
            elif bid == "yt-dl": self.download_in_background(url, audio_only=False)
        elif bid == "music-search":
            val = self.query_one("#music-input", Input).value.strip()
            self.load_and_play_url(val) if is_url(val) else self.load_music(val)
        elif bid == "music-home":
            self.query_one("#music-input", Input).value = ""
            self.load_music("")
        elif bid in ("music-offline", "offline-play-all"):
            self.load_offline_music()
        elif bid in ("music-play", "music-queue", "music-dl"):
            pasted = self.query_one("#music-input", Input).value.strip()
            t = self.query_one("#music-table", DataTable)
            url = self.music_rows[t.cursor_row][3] if (self.music_rows and 0 <= t.cursor_row < len(self.music_rows)) else (pasted if is_url(pasted) else "")
            if not url:
                self.set_status("SELECT A TRACK OR PASTE A URL FIRST.")
                return
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
            self.set_status(f"OFFLINE VAULT: {len(self.offline_rows)} FILES")
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
        elif bid in ("scrape-pl", "scrape-full"):
            v = self.query_one("#scrape-input", Input).value.strip()
            if v:
                self.run_scrape(v, playlists_only=(bid == "scrape-pl"))
        elif bid == "scrape-stop":
            self.scrape_cancel.set()
            self.set_status("STOPPING SCRAPE...")
        elif bid in ("sf-all", "sf-playlists", "sf-videos", "sf-shorts"):
            self.scrape_filter = bid.split("-", 1)[1].upper()
            self.refresh_scrape_table()
            self.set_status(f"FILTER: {self.scrape_filter} ({len(self.scrape_rows)} ITEMS)")
        elif bid == "scrape-queue" and self.scrape_rows:
            self.queue = [dict(x) for x in self.scrape_rows]
            self.action_switch_tab("tab-np")
            self.play_queue_index(0)
        elif bid == "scrape-watch":
            t = self.query_one("#scrape-table", DataTable)
            if self.scrape_rows and 0 <= t.cursor_row < len(self.scrape_rows):
                r = self.scrape_rows[t.cursor_row]
                self.spawn_detached_video(r["webpage_url"], title=r["title"])
        elif bid == "scrape-export" and self.scrape_data:
            self.set_status(f"EXPORTED -> {export_channel_scrape(self.scrape_data)}")
        elif bid == "scrape-mp3":
            self.run_archive(True)
        elif bid == "scrape-mkv":
            self.run_archive(False)
        elif bid == "mirror-check":
            self.check_mirrors()

    @on(DataTable.RowSelected)
    def on_row_selected(self, event: DataTable.RowSelected):
        tid, idx = event.data_table.id, event.cursor_row
        if tid == "queue-table" and idx < len(self.queue):
            self.play_queue_index(idx)
        elif tid == "yt-table" and idx < len(self.yt_rows):
            title, _, _, url = self.yt_rows[idx]
            self.spawn_detached_video(url, title=title)
        elif tid == "music-table" and idx < len(self.music_rows):
            self.load_and_play_url(self.music_rows[idx][3])
        elif tid == "anime-table" and idx < len(self.anime_rows):
            self.launch_anime(self.anime_rows[idx][4])
        elif tid == "movie-table" and idx < len(self.movie_rows):
            self.launch_movie(self.movie_rows[idx][4])
        elif tid == "offline-table" and idx < len(self.offline_rows):
            self._play_offline_item(self.offline_rows[idx])
        elif tid == "scrape-table" and idx < len(self.scrape_rows):
            self.queue = [dict(self.scrape_rows[idx])]
            self.action_switch_tab("tab-np")
            self.play_queue_index(0)


def main() -> None:
    parser = argparse.ArgumentParser(prog="sawstream", description="SawStream Terminal Media Hub")
    parser.add_argument("-m", "--minimal", action="store_true", help="Start directly in Minimal Visualizer mode")
    parser.add_argument("target", nargs="?", default="", help="Optional song query or URL to start playing immediately")
    args = parser.parse_args()

    app = SawStreamApp(start_minimal=args.minimal, initial_target=args.target)
    app.run()
