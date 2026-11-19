import atexit
import math
import shutil
import struct
import subprocess
import threading
import time
from collections import deque

from rich.style import Style
from rich.text import Text

# 512-point pure-Python Radix-2 FFT (~2.2ms/frame, 43Hz bass resolution, zero numpy needed)
FFT_N = 512
FFT_BITS = 9
SAMPLE_RATE = 22050
_HANN = [0.5 * (1.0 - math.cos(2.0 * math.pi * i / (FFT_N - 1))) for i in range(FFT_N)]
_BITREV = [int(f"{i:0{FFT_BITS}b}"[::-1], 2) for i in range(FFT_N)]
_TWIDDLES = []
_sz = 2
while _sz <= FFT_N:
    _half = _sz // 2
    _step = -2.0 * math.pi / _sz
    _TWIDDLES.append([(math.cos(_step * k), math.sin(_step * k)) for k in range(_half)])
    _sz *= 2

SUB_BLOCKS = " ▁▂▃▄▅▆▇█"
ASCII_RAMP = " .:-=+*#%@"
BIT_RAMP = " ·01▓█"
HEX_RAMP = " ·∙∘◇◈◆❖"

STYLES = (
    "SPEAK",
    "BITS",
    "SPECTRO",
    "SCOPE",
    "LISSAJOUS",
    "ASCII",
    "ORBIT",
    "DNA",
    "WAVEFILL",
    "PULSE",
    "MATRIX",
    "RAIN",
    "RADAR",
    "HEXGRID",
)

THEMES = ("CRIMSON", "CYAN", "EMERALD", "GOLD", "VIOLET", "MONO")

_THEME_STOPS = {
    "CRIMSON": ((100, 8, 24), (255, 38, 62), (255, 145, 130), (255, 240, 245)),
    "CYAN":    ((8, 45, 95),  (0, 195, 255), (110, 240, 255), (235, 252, 255)),
    "EMERALD": ((8, 70, 32),  (29, 215, 96), (125, 255, 165), (235, 255, 242)),
    "GOLD":    ((95, 40, 6),  (255, 145, 15), (255, 215, 85), (255, 250, 225)),
    "VIOLET":  ((65, 12, 110), (195, 60, 255), (255, 120, 225), (255, 235, 252)),
    "MONO":    ((45, 48, 54), (135, 142, 152), (210, 216, 224), (255, 255, 255)),
}

_ORBIT_LUT = [
    (
        math.cos((2.0 * math.pi * i) / 128 - math.pi / 2.0),
        math.sin((2.0 * math.pi * i) / 128 - math.pi / 2.0),
    )
    for i in range(128)
]

_BRAILLE_BITS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)


def _interp_rgb(stops, t: float, flash: float = 0.0) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    c0, c1, c2, c3 = stops
    if t < 0.42:
        u = t / 0.42
        a, b = c0, c1
    elif t < 0.80:
        u = (t - 0.42) / 0.38
        a, b = c1, c2
    else:
        u = (t - 0.80) / 0.20
        a, b = c2, c3
    r = int(a[0] + (b[0] - a[0]) * u)
    g = int(a[1] + (b[1] - a[1]) * u)
    bl = int(a[2] + (b[2] - a[2]) * u)
    if flash > 0.0:
        r = min(255, int(r + (255 - r) * flash))
        g = min(255, int(g + (250 - g) * flash))
        bl = min(255, int(bl + (250 - bl) * flash))
    return r, g, bl


# Precompute 32-step Truecolor Style & RGB palettes per theme (zero per-frame allocations!)
_PALETTES: dict[str, tuple[list[Style], list[Style], list[tuple[int, int, int]]]] = {}
for _tname, _stops in _THEME_STOPS.items():
    _norm = []
    _flsh = []
    _rgbs = []
    for _i in range(32):
        _t = _i / 31.0
        _rgb = _interp_rgb(_stops, _t, 0.0)
        _frgb = _interp_rgb(_stops, _t, 0.45)
        _rgbs.append(_rgb)
        _norm.append(Style(color=f"rgb({_rgb[0]},{_rgb[1]},{_rgb[2]})"))
        _flsh.append(Style(color=f"rgb({_frgb[0]},{_frgb[1]},{_frgb[2]})"))
    _PALETTES[_tname] = (_norm, _flsh, _rgbs)

_STYLE_TAG = Style(color="#565d68")
_STYLE_DIM = Style(color="#282c34")
_STYLE_AXIS = Style(color="#2a0e14")
_STYLE_PEAK = Style(color="#ffd8de", bold=True)
_STYLE_WHITE = Style(color="#ffffff", bold=True)

# Cache for spectrogram half-block fg/bg styles (quantized to 16 levels -> max 256 styles total)
_SPECTRO_STYLE_CACHE: dict[tuple[str, int, int], Style] = {}


def _get_spectro_style(theme: str, top_q: int, bot_q: int) -> Style:
    key = (theme, top_q, bot_q)
    st = _SPECTRO_STYLE_CACHE.get(key)
    if st is None:
        rgbs = _PALETTES[theme][2]
        if top_q <= 0:
            fg = "rgb(4,4,6)"
        else:
            r1, g1, b1 = rgbs[min(31, top_q * 2)]
            fg = f"rgb({r1},{g1},{b1})"
        if bot_q <= 0:
            bg = "rgb(4,4,6)"
        else:
            r2, g2, b2 = rgbs[min(31, bot_q * 2)]
            bg = f"rgb({r2},{g2},{b2})"
        st = Style(color=fg, bgcolor=bg)
        _SPECTRO_STYLE_CACHE[key] = st
    return st


def _pick_style(theme: str, norm: float, beat_flash: float = 0.0) -> Style:
    norm_pal, flash_pal, _ = _PALETTES.get(theme, _PALETTES["CRIMSON"])
    idx = max(0, min(31, int(norm * 31)))
    return flash_pal[idx] if beat_flash > 0.42 else norm_pal[idx]


def _fft_mags(samples: list[float]) -> list[float]:
    re = [samples[_BITREV[i]] * _HANN[_BITREV[i]] for i in range(FFT_N)]
    im = [0.0] * FFT_N
    size = 2
    stage = 0
    while size <= FFT_N:
        half = size // 2
        tw = _TWIDDLES[stage]
        for start in range(0, FFT_N, size):
            for k in range(half):
                wr, wi = tw[k]
                j = start + k + half
                rj = re[j]
                ij = im[j]
                tr = wr * rj - wi * ij
                ti = wr * ij + wi * rj
                i = start + k
                ri = re[i]
                ii = im[i]
                re[j] = ri - tr
                im[j] = ii - ti
                re[i] = ri + tr
                im[i] = ii + ti
        size *= 2
        stage += 1
    return [math.hypot(re[i], im[i]) for i in range(1, FFT_N // 2)]


class AudioVisualizerEngine:
    """
    Zero-lag PipeWire/PulseAudio monitor capture + on-demand 512-pt FFT + triggered PCM oscilloscope +
    rolling spectrogram history + rain/radar state. Zero wasted CPU when paused or hidden.
    """

    def __init__(self, num_bands: int = 64):
        self.num_bands = num_bands
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None

        self.live_capture = False
        self._latest_pcm: bytes = b""
        self._pcm_seq = 0
        self._last_processed_seq = -1
        self._last_audio_ts = 0.0

        self._smooth = [0.0] * num_bands
        self._peaks = [0.0] * num_bands
        self._wave = [0.0] * 160
        self._raw_bits = [0] * 64
        self._spectro_hist: deque[list[float]] = deque(maxlen=160)
        self._rain_hist: deque[list[float]] = deque(maxlen=40)
        self._radar_ring: list[float] = [0.0] * 72
        self._radar_angle = 0

        self._agc = 2.2
        self._wave_agc = 0.25
        self._bass_avg = 0.2
        self._beat_flash = 0.0
        self._bass_energy = 0.0
        self._vocal_energy = 0.0
        self._treble_energy = 0.0
        self._phase = 0.0
        self._bin_ranges = self._build_log_bins(num_bands)
        atexit.register(self.stop)

    @staticmethod
    def _build_log_bins(num_bands: int) -> list[tuple[int, int]]:
        max_bin = (FFT_N // 2) - 2
        ranges = []
        for i in range(num_bands):
            f0 = (max_bin ** (i / num_bands)) - 1
            f1 = (max_bin ** ((i + 1) / num_bands)) - 1
            b0 = max(0, min(max_bin - 1, int(f0)))
            b1 = max(b0 + 1, min(max_bin, int(math.ceil(f1))))
            ranges.append((b0, b1))
        return ranges

    def _spawn_capture(self) -> subprocess.Popen | None:
        candidates = []
        if shutil.which("parec"):
            candidates.append([
                "parec",
                "--format=s16le",
                f"--rate={SAMPLE_RATE}",
                "--channels=1",
                "--latency-msec=20",
                "-d",
                "@DEFAULT_MONITOR@",
            ])
        if shutil.which("pw-cat"):
            candidates.append([
                "pw-cat",
                "--record",
                "--raw",
                "--rate",
                str(SAMPLE_RATE),
                "--channels",
                "1",
                "--format",
                "s16",
                "-P",
                "stream.capture.sink=true",
                "-",
            ])
        for cmd in candidates:
            try:
                p = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=FFT_N * 2,
                )
                time.sleep(0.05)
                if p.poll() is None:
                    return p
                p.kill()
            except Exception:
                continue
        return None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        proc = self._proc
        self._proc = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=0.4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.live_capture = False

    def _capture_loop(self) -> None:
        proc = self._spawn_capture()
        if not proc or not proc.stdout:
            self.live_capture = False
            return
        self._proc = proc
        self.live_capture = True
        chunk_bytes = FFT_N * 2
        try:
            while not self._stop_evt.is_set() and proc.poll() is None:
                raw = proc.stdout.read(chunk_bytes)
                if not raw or len(raw) < chunk_bytes:
                    break
                with self._lock:
                    self._latest_pcm = raw
                    self._pcm_seq += 1
        except Exception:
            pass
        finally:
            self.live_capture = False
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    def _extract_triggered_wave(self, samples: list[float], out_len: int = 160) -> list[float]:
        trigger = 0
        best_slope = -1.0
        limit = FFT_N - out_len - 1
        for i in range(1, min(limit, FFT_N // 2)):
            if samples[i - 1] <= 0.0 < samples[i]:
                slope = samples[i] - samples[i - 1]
                if slope > best_slope:
                    best_slope = slope
                    trigger = i
        seg = samples[trigger : trigger + out_len]
        peak = max((abs(v) for v in seg), default=0.0)
        if peak > self._wave_agc:
            self._wave_agc = self._wave_agc * 0.4 + peak * 0.6
        else:
            self._wave_agc = max(0.08, self._wave_agc * 0.96)
        inv = 1.0 / max(0.08, self._wave_agc)
        return [max(-1.0, min(1.0, v * inv)) for v in seg]

    def step(self, is_playing: bool) -> dict:
        self._phase += 0.24
        now = time.monotonic()

        with self._lock:
            pcm = self._latest_pcm
            seq = self._pcm_seq
            live = self.live_capture

        has_live = False
        target = [0.0] * self.num_bands
        wave = self._wave
        raw_bits = self._raw_bits

        if is_playing and live and pcm and len(pcm) == FFT_N * 2:
            if seq != self._last_processed_seq:
                self._last_processed_seq = seq
                ints = struct.unpack(f"<{FFT_N}h", pcm)
                samples = [v / 32768.0 for v in ints]
                rms = math.sqrt(sum(v * v for v in samples) / FFT_N)
                if rms > 0.0022:
                    self._last_audio_ts = now
                    mags = _fft_mags(samples)
                    bands = []
                    frame_peak = 0.0
                    for idx, (b0, b1) in enumerate(self._bin_ranges):
                        val = sum(mags[b0:b1]) / max(1, b1 - b0)
                        frac = idx / max(1, self.num_bands - 1)
                        eq_curve = 0.90 + 1.55 * (frac ** 0.85)
                        val = math.log1p(val * 4.5 * eq_curve)
                        if val > frame_peak:
                            frame_peak = val
                        bands.append(val)

                    if frame_peak > self._agc:
                        self._agc = self._agc * 0.45 + frame_peak * 0.55
                    else:
                        self._agc = max(0.95, self._agc * 0.985)

                    inv_agc = 1.0 / max(0.75, self._agc)
                    target = [min(1.0, b * inv_agc) for b in bands]
                    wave = self._extract_triggered_wave(samples, 160)
                    # Exact bit-level extraction from raw 16-bit PCM ints (magnitude bit depth per sample)
                    raw_bits = [abs(ints[i * 8]) for i in range(64)]
                    has_live = True
            elif now - self._last_audio_ts < 0.8:
                target = list(self._smooth)
                has_live = True

        if not is_playing:
            target = [0.0] * self.num_bands
            wave = [v * 0.65 for v in self._wave]
            raw_bits = [v >> 1 for v in self._raw_bits]
        elif not has_live and (now - self._last_audio_ts > 1.0):
            p = self._phase
            target = []
            for i in range(self.num_bands):
                x = i / max(1, self.num_bands - 1)
                bass = math.exp(-((x - 0.16) ** 2) / 0.05) * (0.55 + 0.45 * math.sin(p * 1.5))
                mid = math.exp(-((x - 0.50) ** 2) / 0.08) * (0.45 + 0.42 * math.sin(p * 2.3 + i * 0.25))
                hi = math.exp(-((x - 0.82) ** 2) / 0.06) * (0.28 + 0.35 * math.sin(p * 3.5 - i * 0.38))
                target.append(min(0.96, max(0.04, bass + mid + hi)))
            wave = [
                0.65 * math.sin(p * 1.6 + i * 0.14) + 0.35 * math.sin(p * 3.2 - i * 0.29)
                for i in range(160)
            ]
            raw_bits = [int(abs(wave[i * 2]) * 30000) for i in range(64)]

        # Ultra-snappy attack (0.86) + smooth natural decay
        for i in range(self.num_bands):
            t = target[i]
            cur = self._smooth[i]
            if t > cur:
                self._smooth[i] = cur * 0.14 + t * 0.86
            else:
                self._smooth[i] = max(0.0, cur * 0.66 + t * 0.34)
            if self._smooth[i] >= self._peaks[i]:
                self._peaks[i] = self._smooth[i]
            else:
                self._peaks[i] = max(self._smooth[i], self._peaks[i] - 0.036)

        self._wave = wave
        self._raw_bits = raw_bits

        # Push frame into spectrogram & rain histories only when active
        if is_playing or max(self._smooth, default=0.0) > 0.01:
            self._spectro_hist.append(list(self._smooth))
            self._rain_hist.appendleft(list(self._smooth))

        # Update radar sweep ring (phosphor fade + paint active frequency echo at current angle)
        self._radar_angle = (self._radar_angle + 2) % 72
        for i in range(72):
            self._radar_ring[i] *= 0.91
        if is_playing:
            echo = sum(self._smooth[:24]) / 24.0
            self._radar_ring[self._radar_angle] = max(self._radar_ring[self._radar_angle], echo)
            self._radar_ring[(self._radar_angle - 1) % 72] = max(self._radar_ring[(self._radar_angle - 1) % 72], echo * 0.85)

        bass_now = sum(self._smooth[:10]) / 10.0
        vocal_now = sum(self._smooth[12:38]) / 26.0
        treble_now = sum(self._smooth[40:]) / max(1, len(self._smooth) - 40)
        self._bass_energy = bass_now
        self._vocal_energy = vocal_now
        self._treble_energy = treble_now

        if bass_now > self._bass_avg * 1.24 and bass_now > 0.36:
            self._beat_flash = min(1.0, (bass_now - self._bass_avg) * 2.6 + 0.45)
        else:
            self._beat_flash = max(0.0, self._beat_flash - 0.14)
        self._bass_avg = self._bass_avg * 0.88 + bass_now * 0.12

        is_quiet = (
            (not is_playing)
            and max(self._smooth, default=0.0) < 0.008
            and max((abs(v) for v in self._wave), default=0.0) < 0.01
        )
        return {
            "levels": list(self._smooth),
            "peaks": list(self._peaks),
            "wave": list(self._wave),
            "raw_bits": list(self._raw_bits),
            "spectro": self._spectro_hist,
            "rain": self._rain_hist,
            "radar_ring": list(self._radar_ring),
            "radar_angle": self._radar_angle,
            "bass": self._bass_energy,
            "vocal": self._vocal_energy,
            "treble": self._treble_energy,
            "flash": self._beat_flash,
            "phase": self._phase,
            "live": has_live,
            "quiet": is_quiet,
        }


def _resample(values: list[float], count: int) -> list[float]:
    if count <= 0:
        return []
    n = len(values)
    if count == n:
        return values
    out = []
    scale = (n - 1) / max(1, count - 1)
    for i in range(count):
        pos = i * scale
        lo = int(pos)
        hi = min(n - 1, lo + 1)
        frac = pos - lo
        out.append(values[lo] * (1.0 - frac) + values[hi] * frac)
    return out


def _header_line(width: int, header: str) -> Text:
    out = Text()
    if header:
        pad = max(0, width - len(header) - 2)
        out.append(" " * pad + header + "  \n", style=_STYLE_TAG)
    else:
        out.append("\n")
    return out


def _render_braille_canvas(grid: list[bytearray], width: int, height: int, row_styles: list[Style], header: str) -> Text:
    out = _header_line(width, header)
    for y in range(height):
        row = grid[y]
        chars = "".join(chr(0x2800 | b) if b else " " for b in row)
        out.append(chars, style=row_styles[y])
        if y < height - 1:
            out.append("\n")
    return out


# 1. SPEAK — Center-mirrored vocal/voice wave
def _render_speak(frame: dict, width: int, vis_h: int, header: str, theme: str) -> Text:
    out = _header_line(width, header)
    bar_w, gap = (2, 1) if width >= 88 else ((1, 1) if width >= 46 else (1, 0))
    step = bar_w + gap
    num_bars = max(10, (width - 2) // step)
    left_pad = " " * max(0, (width - (num_bars * step - gap)) // 2)
    gap_str = " " * gap

    half_bars = (num_bars + 1) // 2
    half_lv = _resample(frame["levels"][:50], half_bars)
    sym = []
    for i in range(num_bars):
        dist_center = abs(i - (num_bars - 1) / 2.0) / max(1.0, (num_bars - 1) / 2.0)
        idx = min(half_bars - 1, int(dist_center * (half_bars - 1)))
        taper = 0.25 + 0.75 * math.cos(dist_center * math.pi * 0.46)
        boost = 1.0 + 0.22 * frame["flash"] * (1.0 - dist_center)
        sym.append(min(1.0, half_lv[idx] * taper * boost))

    mid = vis_h / 2.0
    flash = frame["flash"]
    full_blk, top_blk, bot_blk, axis_blk, spc_blk = "█" * bar_w, "▀" * bar_w, "▄" * bar_w, "─" * bar_w, " " * bar_w

    for row in range(vis_h):
        dist = abs((row + 0.5) - mid) / max(0.5, mid)
        row_style = _pick_style(theme, dist, flash * (1.0 - dist * 0.5))
        is_center = abs((row + 0.5) - mid) <= 0.55
        buf = [left_pad]
        axis_only = True
        for b in range(num_bars):
            amp = sym[b]
            if amp >= dist + 0.045:
                buf.append(full_blk); axis_only = False
            elif amp >= dist - 0.045 and amp > 0.03:
                buf.append(top_blk if row >= mid else bot_blk); axis_only = False
            elif is_center:
                buf.append(axis_blk)
            else:
                buf.append(spc_blk)
            if b < num_bars - 1 and gap:
                buf.append(gap_str)
        out.append("".join(buf), style=_STYLE_AXIS if (is_center and axis_only) else row_style)
        if row < vis_h - 1:
            out.append("\n")
    return out


# 2. BITS — Literal 1-to-1 sound-bit to ASCII-bit matrix!
def _render_bits(frame: dict, width: int, vis_h: int, header: str, theme: str) -> Text:
    """Every column is a raw 16-bit PCM sample / FFT bin; every row is an exact bit plane (MSB at top, LSB at bottom)."""
    out = _header_line(width, header)
    cols = max(16, (width - 2) // 2)
    left_pad = " " * max(0, (width - cols * 2) // 2)
    lv = _resample(frame["levels"], cols)
    rb = frame["raw_bits"]
    n_rb = len(rb)
    flash = frame["flash"]

    for row in range(vis_h):
        # Row 0 = highest energy / MSB, bottom row = LSB
        rfb = vis_h - 1 - row
        bit_plane = min(14, int((rfb / max(1, vis_h - 1)) * 14))
        bit_mask = 1 << bit_plane
        norm_y = rfb / max(1, vis_h - 1)
        row_style = _pick_style(theme, norm_y, flash * norm_y)

        chars = [left_pad]
        for c in range(cols):
            amp = lv[c]
            sample_int = rb[(c * n_rb) // cols]
            bit_on = (sample_int & bit_mask) != 0
            thresh = norm_y
            if amp >= thresh + 0.08:
                chars.append("1 " if bit_on else "0 ")
            elif amp >= thresh - 0.05 and amp > 0.04:
                chars.append("▓ " if bit_on else "▒ ")
            elif bit_on and amp > 0.18:
                chars.append("· ")
            else:
                chars.append("  ")
        out.append("".join(chars), style=row_style)
        if row < vis_h - 1:
            out.append("\n")
    return out


# 3. SPECTRO — Truecolor scrolling frequency × time waterfall spectrogram (half-block ▀ pixels)
def _render_spectro(frame: dict, width: int, vis_h: int, header: str, theme: str) -> Text:
    out = _header_line(width, header)
    hist = frame["spectro"]
    cols = width
    rows_px = vis_h * 2
    n_hist = len(hist)
    offset = max(0, n_hist - cols)
    pad_left = max(0, cols - n_hist)

    # Pre-resample visible columns into rows_px frequency bins (low freq at bottom, high freq at top)
    col_bins: list[list[int]] = []
    empty_col = [0] * rows_px
    for _ in range(pad_left):
        col_bins.append(empty_col)
    for ci in range(offset, n_hist):
        res = _resample(hist[ci], rows_px)
        col_bins.append([min(15, int(v * 15.9)) for v in res])

    for row in range(vis_h):
        py_top = rows_px - 1 - (row * 2)
        py_bot = py_top - 1
        # Run-length merge identical (top_q, bot_q) cells so Rich Text has minimal spans!
        run_len = 0
        prev_key = (-1, -1)
        for x in range(cols):
            cb = col_bins[x]
            key = (cb[py_top], cb[py_bot])
            if key == prev_key:
                run_len += 1
            else:
                if run_len > 0:
                    out.append("▀" * run_len, style=_get_spectro_style(theme, prev_key[0], prev_key[1]))
                prev_key = key
                run_len = 1
        if run_len > 0:
            out.append("▀" * run_len, style=_get_spectro_style(theme, prev_key[0], prev_key[1]))
        if row < vis_h - 1:
            out.append("\n")
    return out


# 4. SCOPE — 2x4 subpixel Braille triggered PCM oscilloscope
def _render_scope(frame: dict, width: int, vis_h: int, header: str, theme: str) -> Text:
    px_w, px_h = width * 2, vis_h * 4
    grid = [bytearray(width) for _ in range(vis_h)]
    wave = _resample(frame["wave"], px_w)
    mid_y = (px_h - 1) / 2.0
    amp_scale = mid_y * (0.84 + 0.14 * frame["flash"])

    cy = int(mid_y)
    cy_row, cy_sub = divmod(cy, 4)
    for x in range(0, px_w, 6):
        grid[cy_row][x >> 1] |= _BRAILLE_BITS[cy_sub][x & 1]

    prev_y = None
    for x in range(px_w):
        y = max(0, min(px_h - 1, int(round(mid_y - wave[x] * amp_scale))))
        y0, y1 = (y, y) if prev_y is None else ((prev_y, y) if prev_y <= y else (y, prev_y))
        cx, dx = x >> 1, x & 1
        for py in range(y0, y1 + 1):
            grid[py >> 2][cx] |= _BRAILLE_BITS[py & 3][dx]
        if frame["bass"] > 0.35 and y + 1 < px_h:
            grid[(y + 1) >> 2][cx] |= _BRAILLE_BITS[(y + 1) & 3][dx]
        prev_y = y

    mid_row = vis_h / 2.0
    row_styles = [_pick_style(theme, min(1.0, abs((r + 0.5) - mid_row) / max(0.5, mid_row) + 0.25), frame["flash"]) for r in range(vis_h)]
    return _render_braille_canvas(grid, width, vis_h, row_styles, header)


# 5. LISSAJOUS — True X-Y phase-delay oscilloscope (each PCM sample plots (s[t], s[t-lag]))
def _render_lissajous(frame: dict, width: int, vis_h: int, header: str, theme: str) -> Text:
    px_w, px_h = width * 2, vis_h * 4
    grid = [bytearray(width) for _ in range(vis_h)]
    wave = frame["wave"]
    n = len(wave)
    lag = max(4, min(20, int(10 + 8 * frame["vocal"])))
    cx, cy = px_w / 2.0, px_h / 2.0
    rad = min(cx * 0.78, cy * 0.88) * (0.75 + 0.25 * frame["bass"] + 0.1 * frame["flash"])

    # Faint crosshair
    icx, icy = int(cx), int(cy)
    for x in range(max(0, icx - int(rad)), min(px_w, icx + int(rad)), 8):
        grid[icy >> 2][x >> 1] |= _BRAILLE_BITS[icy & 3][x & 1]
    for y in range(max(0, icy - int(rad)), min(px_h, icy + int(rad)), 6):
        grid[y >> 2][icx >> 1] |= _BRAILLE_BITS[y & 3][icx & 1]

    prev = None
    for i in range(lag, n):
        px = int(cx + wave[i] * rad)
        py = int(cy - wave[i - lag] * rad)
        if prev is not None:
            x0, y0 = prev
            steps = max(1, min(14, max(abs(px - x0), abs(py - y0))))
            for s in range(steps + 1):
                u = s / steps
                ix = int(x0 + (px - x0) * u)
                iy = int(y0 + (py - y0) * u)
                if 0 <= ix < px_w and 0 <= iy < px_h:
                    grid[iy >> 2][ix >> 1] |= _BRAILLE_BITS[iy & 3][ix & 1]
        prev = (px, py)

    mid_row = vis_h / 2.0
    row_styles = [_pick_style(theme, min(1.0, 1.0 - abs((r + 0.5) - mid_row) / max(0.5, mid_row) + 0.15), frame["flash"]) for r in range(vis_h)]
    return _render_braille_canvas(grid, width, vis_h, row_styles, header)


# 6. ASCII — Exact 1-to-1 sound-to-ASCII density field (` .:-=+*#%@`)
def _render_ascii(frame: dict, width: int, vis_h: int, header: str, theme: str) -> Text:
    """Maps every character cell (x, y) directly to FFT_bin[x] modulated by live PCM wave[y] + vocal harmonics."""
    out = _header_line(width, header)
    lv = _resample(frame["levels"], width)
    wv = _resample(frame["wave"], vis_h)
    ramp_max = len(ASCII_RAMP) - 1
    mid = vis_h / 2.0
    flash = frame["flash"]

    for row in range(vis_h):
        dist = abs((row + 0.5) - mid) / max(0.5, mid)
        wave_mod = 0.55 + 0.45 * abs(wv[row])
        row_style = _pick_style(theme, 1.0 - dist * 0.75, flash * (1.0 - dist))
        chars = []
        for x in range(width):
            energy = lv[x] * wave_mod - dist * 0.72
            if energy <= 0.02:
                chars.append(" ")
            else:
                idx = min(ramp_max, max(1, int(energy * ramp_max * 1.45)))
                chars.append(ASCII_RAMP[idx])
        out.append("".join(chars), style=row_style)
        if row < vis_h - 1:
            out.append("\n")
    return out


# 7. ORBIT — 2x4 subpixel Braille radial halo
def _render_orbit(frame: dict, width: int, vis_h: int, header: str, theme: str) -> Text:
    px_w, px_h = width * 2, vis_h * 4
    grid = [bytearray(width) for _ in range(vis_h)]
    cx, cy = px_w / 2.0, px_h / 2.0
    max_r = min(cx * 0.85, cy * 0.90)
    base_r = max_r * (0.42 + 0.16 * frame["bass"] + 0.08 * frame["flash"])

    half_lv = _resample(frame["levels"][:48], 64)
    ring_lv = half_lv + list(reversed(half_lv))

    for i, (ca, sa) in enumerate(_ORBIT_LUT):
        amp = ring_lv[i]
        r_out = base_r + amp * (max_r - base_r)
        r_in = max(2.0, base_r - amp * base_r * 0.32)
        span = max(1, int(r_out - r_in))
        for s in range(span + 1):
            r = r_in + (r_out - r_in) * (s / span)
            px, py = int(cx + ca * r), int(cy + sa * r)
            if 0 <= px < px_w and 0 <= py < px_h:
                grid[py >> 2][px >> 1] |= _BRAILLE_BITS[py & 3][px & 1]

    mid_row = vis_h / 2.0
    row_styles = [_pick_style(theme, min(1.0, abs((r + 0.5) - mid_row) / max(0.5, mid_row) + 0.2), frame["flash"]) for r in range(vis_h)]
    return _render_braille_canvas(grid, width, vis_h, row_styles, header)


# 8. DNA — Dual counter-phased Braille double helix driven by bass, vocals & treble
def _render_dna(frame: dict, width: int, vis_h: int, header: str, theme: str) -> Text:
    px_w, px_h = width * 2, vis_h * 4
    grid = [bytearray(width) for _ in range(vis_h)]
    lv = _resample(frame["levels"][:48], px_w)
    mid_y = (px_h - 1) / 2.0
    phase = frame["phase"] * 0.85
    freq = 0.055 + 0.025 * frame["treble"]

    for x in range(px_w):
        amp = (0.22 + 0.72 * lv[x] + 0.12 * frame["flash"]) * mid_y
        s1 = math.sin(x * freq + phase)
        s2 = -s1
        y1 = max(0, min(px_h - 1, int(mid_y - s1 * amp)))
        y2 = max(0, min(px_h - 1, int(mid_y - s2 * amp)))
        cx, dx = x >> 1, x & 1
        # Thicker strands (2px)
        for dy in (0, 1):
            if y1 + dy < px_h:
                grid[(y1 + dy) >> 2][cx] |= _BRAILLE_BITS[(y1 + dy) & 3][dx]
            if y2 + dy < px_h:
                grid[(y2 + dy) >> 2][cx] |= _BRAILLE_BITS[(y2 + dy) & 3][dx]
        # DNA base-pair rungs every 8 subpixels when audio energy is present
        if x % 8 == 0 and lv[x] > 0.14:
            lo, hi = (y1, y2) if y1 <= y2 else (y2, y1)
            for py in range(lo, hi + 1, 2):
                grid[py >> 2][cx] |= _BRAILLE_BITS[py & 3][dx]

    mid_row = vis_h / 2.0
    row_styles = [_pick_style(theme, min(1.0, abs((r + 0.5) - mid_row) / max(0.5, mid_row) + 0.25), frame["flash"]) for r in range(vis_h)]
    return _render_braille_canvas(grid, width, vis_h, row_styles, header)


# 9. WAVEFILL — Smooth liquid mountain-range silhouette + reflection
def _render_wavefill(frame: dict, width: int, vis_h: int, header: str, theme: str) -> Text:
    out = _header_line(width, header)
    # Mirror-fold spectrum for dramatic mountain silhouette
    half = (width + 1) // 2
    lv_half = _resample(frame["levels"][:52], half)
    heights = [lv_half[min(half - 1, abs(x - half))] for x in range(width)]
    horizon = int(vis_h * 0.68)
    flash = frame["flash"]

    for row in range(vis_h):
        if row <= horizon:
            rfb = horizon - row
            norm_y = rfb / max(1, horizon)
            row_style = _pick_style(theme, norm_y, flash * norm_y)
            chars = []
            for x in range(width):
                h_cells = heights[x] * horizon
                diff = h_cells - rfb
                if diff >= 1.0:
                    chars.append("█")
                elif diff > 0.0:
                    chars.append(SUB_BLOCKS[min(8, max(1, int(diff * 8)))])
                else:
                    chars.append(" ")
            out.append("".join(chars), style=row_style)
        else:
            # Dimmed water reflection below horizon
            depth = row - horizon
            max_ref = max(1, vis_h - horizon)
            norm_r = max(0.0, 0.35 - 0.30 * (depth / max_ref))
            row_style = _pick_style(theme, norm_r, 0.0)
            chars = []
            for x in range(width):
                ref_h = heights[x] * max_ref * 0.85
                chars.append("░" if ref_h >= depth else " ")
            out.append("".join(chars), style=row_style)
        if row < vis_h - 1:
            out.append("\n")
    return out


# 10 & 11. PULSE (Cava bars) & MATRIX (LED studio meter)
def _render_bars(frame: dict, width: int, vis_h: int, header: str, theme: str, led: bool = False) -> Text:
    out = _header_line(width, header)
    bar_w, gap = (2, 1) if width >= 88 else ((1, 1) if width >= 46 else (1, 0))
    step = bar_w + gap
    num_bars = max(8, (width - 2) // step)
    left_pad = " " * max(0, (width - (num_bars * step - gap)) // 2)
    gap_str = " " * gap

    lv = _resample(frame["levels"], num_bars)
    pk = _resample(frame["peaks"], num_bars)
    flash = frame["flash"]
    full_ch = ("━" if led else "█") * bar_w
    peak_ch = ("─" if led else "▔") * bar_w
    spc = " " * bar_w

    for row in range(vis_h):
        rfb = vis_h - 1 - row
        norm_y = rfb / max(1, vis_h - 1)
        row_style = _pick_style(theme, norm_y, flash * norm_y)
        peak_style = _STYLE_WHITE if (led or flash > 0.4) else _STYLE_PEAK

        out.append(left_pad)
        run_chars = []
        run_style = row_style

        def flush():
            if run_chars:
                out.append("".join(run_chars), style=run_style)
                run_chars.clear()

        for b in range(num_bars):
            h_cells = lv[b] * vis_h
            p_cell = int(pk[b] * (vis_h - 1))
            diff = h_cells - rfb
            if diff >= 1.0:
                if run_style != row_style:
                    flush(); run_style = row_style
                run_chars.append(full_ch)
            elif (not led) and diff > 0.0:
                if run_style != row_style:
                    flush(); run_style = row_style
                run_chars.append(SUB_BLOCKS[min(8, max(1, int(diff * 8)))] * bar_w)
            elif rfb == p_cell and pk[b] > 0.05:
                if run_style != peak_style:
                    flush(); run_style = peak_style
                run_chars.append(peak_ch)
            else:
                run_chars.append(spc)
            if b < num_bars - 1 and gap:
                run_chars.append(gap_str)
        flush()
        if row < vis_h - 1:
            out.append("\n")
    return out


# 12. RAIN — Frequency-locked vertical sound-cascade rain (each column = 1 exact frequency band)
def _render_rain(frame: dict, width: int, vis_h: int, header: str, theme: str) -> Text:
    out = _header_line(width, header)
    rain = frame["rain"]
    cols = max(12, width // 2)
    left_pad = " " * max(0, (width - cols * 2) // 2)
    n_rain = len(rain)
    glyphs = " ·░▒▓█"
    flash = frame["flash"]

    for row in range(vis_h):
        norm_y = 1.0 - (row / max(1, vis_h - 1))
        row_style = _pick_style(theme, norm_y, flash * norm_y)
        if row < n_rain:
            vals = _resample(rain[row], cols)
            fade = max(0.15, 1.0 - (row / max(1, vis_h)) * 0.72)
            chars = [left_pad]
            for v in vals:
                amp = v * fade
                if amp < 0.14:
                    chars.append("  ")
                else:
                    idx = min(5, max(1, int(amp * 5.8)))
                    chars.append(glyphs[idx] + " ")
            out.append("".join(chars), style=row_style)
        else:
            out.append(" ")
        if row < vis_h - 1:
            out.append("\n")
    return out


# 13. RADAR — Rotating Braille polar sonar painting real frequency echoes
def _render_radar(frame: dict, width: int, vis_h: int, header: str, theme: str) -> Text:
    px_w, px_h = width * 2, vis_h * 4
    grid = [bytearray(width) for _ in range(vis_h)]
    cx, cy = px_w / 2.0, px_h / 2.0
    max_r = min(cx * 0.85, cy * 0.90)

    def plot(px: int, py: int):
        if 0 <= px < px_w and 0 <= py < px_h:
            grid[py >> 2][px >> 1] |= _BRAILLE_BITS[py & 3][px & 1]

    # Outer + inner concentric guide rings (sparse dots)
    for ring_scale in (0.45, 0.95):
        rr = max_r * ring_scale
        for i in range(0, 128, 3):
            ca, sa = _ORBIT_LUT[i]
            plot(int(cx + ca * rr), int(cy + sa * rr))

    # Paint frequency echoes around the 72-sector polar ring
    ring = frame["radar_ring"]
    for i in range(72):
        amp = ring[i]
        if amp < 0.06:
            continue
        lut_i = (i * 128) // 72
        ca, sa = _ORBIT_LUT[lut_i]
        r_echo = max_r * (0.18 + 0.78 * amp)
        plot(int(cx + ca * r_echo), int(cy + sa * r_echo))
        plot(int(cx + ca * (r_echo - 1.5)), int(cy + sa * (r_echo - 1.5)))

    # Bright active sweep arm
    ang_idx = (frame["radar_angle"] * 128) // 72
    ca, sa = _ORBIT_LUT[ang_idx % 128]
    steps = max(4, int(max_r))
    for s in range(steps):
        r = max_r * (s / steps)
        plot(int(cx + ca * r), int(cy + sa * r))

    mid_row = vis_h / 2.0
    row_styles = [_pick_style(theme, min(1.0, 0.95 - abs((r + 0.5) - mid_row) / max(0.5, mid_row) * 0.7), frame["flash"]) for r in range(vis_h)]
    return _render_braille_canvas(grid, width, vis_h, row_styles, header)


# 14. HEXGRID — Geometric diamond lattice (`·∙∘◇◈◆❖`) mapped to sub-bass core, vocal mid-ring & treble outer grid
def _render_hexgrid(frame: dict, width: int, vis_h: int, header: str, theme: str) -> Text:
    out = _header_line(width, header)
    cols = max(12, width // 2)
    left_pad = " " * max(0, (width - cols * 2) // 2)
    cx, cy = (cols - 1) / 2.0, (vis_h - 1) / 2.0
    max_d = max(1.0, math.hypot(cx * 0.65, cy))
    lv = frame["levels"]
    n_lv = len(lv)
    ramp_max = len(HEX_RAMP) - 1
    flash = frame["flash"]

    for row in range(vis_h):
        dy = row - cy
        row_norm = 1.0 - abs(dy) / max(1.0, cy)
        row_style = _pick_style(theme, max(0.1, row_norm), flash * row_norm)
        chars = [left_pad]
        for c in range(cols):
            dx = (c - cx) * 0.65
            d = math.hypot(dx, dy) / max_d  # 0 at center -> ~1 at edge
            if d > 1.05:
                chars.append("  ")
                continue
            # Center = sub-bass bins, mid-ring = vocals, outer ring = treble
            b_idx = min(n_lv - 1, int(d * (n_lv - 1)))
            amp = lv[b_idx] * (1.15 - d * 0.45) + flash * 0.18 * (1.0 - d)
            if amp < 0.10:
                chars.append("  ")
            else:
                g_idx = min(ramp_max, max(1, int(amp * ramp_max * 1.15)))
                chars.append(HEX_RAMP[g_idx] + " ")
        out.append("".join(chars), style=row_style)
        if row < vis_h - 1:
            out.append("\n")
    return out


def render_visualizer(
    frame: dict,
    width: int,
    height: int,
    style_name: str = "SPEAK",
    theme: str = "CRIMSON",
    header_tag: str = "",
) -> Text:
    width = max(24, width)
    height = max(6, height)
    vis_h = max(4, height - 1)
    if theme not in _PALETTES:
        theme = "CRIMSON"

    if style_name == "SPEAK":
        return _render_speak(frame, width, vis_h, header_tag, theme)
    if style_name == "BITS":
        return _render_bits(frame, width, vis_h, header_tag, theme)
    if style_name == "SPECTRO":
        return _render_spectro(frame, width, vis_h, header_tag, theme)
    if style_name == "SCOPE":
        return _render_scope(frame, width, vis_h, header_tag, theme)
    if style_name == "LISSAJOUS":
        return _render_lissajous(frame, width, vis_h, header_tag, theme)
    if style_name == "ASCII":
        return _render_ascii(frame, width, vis_h, header_tag, theme)
    if style_name == "ORBIT":
        return _render_orbit(frame, width, vis_h, header_tag, theme)
    if style_name == "DNA":
        return _render_dna(frame, width, vis_h, header_tag, theme)
    if style_name == "WAVEFILL":
        return _render_wavefill(frame, width, vis_h, header_tag, theme)
    if style_name == "MATRIX":
        return _render_bars(frame, width, vis_h, header_tag, theme, led=True)
    if style_name == "RAIN":
        return _render_rain(frame, width, vis_h, header_tag, theme)
    if style_name == "RADAR":
        return _render_radar(frame, width, vis_h, header_tag, theme)
    if style_name == "HEXGRID":
        return _render_hexgrid(frame, width, vis_h, header_tag, theme)
    return _render_bars(frame, width, vis_h, header_tag, theme, led=False)
