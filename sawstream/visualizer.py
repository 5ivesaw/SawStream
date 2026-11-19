import atexit
import math
import shutil
import struct
import subprocess
import threading
import time

from rich.style import Style
from rich.text import Text

# 512-point pure-Python Radix-2 FFT (~2.3ms/frame, 43Hz bass bin resolution, zero numpy needed)
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
STYLES = ("SPEAK", "SCOPE", "ORBIT", "PULSE", "MATRIX")

# Braille dot bitmask for (dx in 0..1, dy in 0..3)
_ORBIT_LUT = [(math.cos((2.0 * math.pi * i) / 128 - math.pi / 2.0), math.sin((2.0 * math.pi * i) / 128 - math.pi / 2.0)) for i in range(128)]

_BRAILLE_BITS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)

# Precomputed Truecolor Style palettes (zero per-frame Style object allocation!)
def _build_palette(steps: int = 32, flash: float = 0.0) -> list[Style]:
    out = []
    for i in range(steps):
        t = i / max(1, steps - 1)
        if t < 0.45:
            u = t / 0.45
            r = int(105 + (255 - 105) * u)
            g = int(8 + (38 - 8) * u)
            b = int(26 + (62 - 26) * u)
        elif t < 0.82:
            u = (t - 0.45) / 0.37
            r = 255
            g = int(38 + (135 - 38) * u)
            b = int(62 + (125 - 62) * u)
        else:
            u = (t - 0.82) / 0.18
            r = 255
            g = int(135 + (240 - 135) * u)
            b = int(125 + (245 - 125) * u)
        if flash > 0.0:
            r = min(255, int(r + (255 - r) * flash))
            g = min(255, int(g + (245 - g) * flash))
            b = min(255, int(b + (245 - b) * flash))
        out.append(Style(color=f"rgb({r},{g},{b})"))
    return out


_PAL_NORMAL = _build_palette(32, flash=0.0)
_PAL_FLASH = _build_palette(32, flash=0.45)
_STYLE_TAG = Style(color="#565d68")
_STYLE_AXIS = Style(color="#2a0a10")
_STYLE_PEAK = Style(color="#ffd8de", bold=True)
_STYLE_WHITE = Style(color="#ffffff", bold=True)


def _pick_style(norm: float, beat_flash: float = 0.0) -> Style:
    idx = max(0, min(31, int(norm * 31)))
    return _PAL_FLASH[idx] if beat_flash > 0.42 else _PAL_NORMAL[idx]


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
    Zero-lag PipeWire/PulseAudio monitor capture + on-demand 512-pt FFT + oscilloscope + beat/vocal detector.
    Reader thread only drains raw PCM so audio latency never drifts; FFT runs ONLY at UI tick rate.
    """

    def __init__(self, num_bands: int = 48):
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
        self._wave = [0.0] * 128
        self._agc = 2.2
        self._wave_agc = 0.25
        self._bass_avg = 0.2
        self._beat_flash = 0.0
        self._bass_energy = 0.0
        self._vocal_energy = 0.0
        self._phase = 0.0
        self._bin_ranges = self._build_log_bins(num_bands)
        atexit.register(self.stop)

    @staticmethod
    def _build_log_bins(num_bands: int) -> list[tuple[int, int]]:
        # Map 43Hz..10kHz logarithmically across num_bands (gives vocals & bass rich detail)
        max_bin = (FFT_N // 2) - 2  # 254
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
        """Ultra-lightweight PCM drainer — zero FFT here so pipe latency never builds up."""
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

    def _extract_triggered_wave(self, samples: list[float], out_len: int = 128) -> list[float]:
        """Rising-edge zero-crossing trigger so oscilloscope locks cleanly to vocal/instrument pitch."""
        # Find strongest rising zero-crossing in the first half
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
        """Runs FFT + onset detection at UI frame rate. Returns frame state dict."""
        self._phase += 0.24
        now = time.monotonic()

        with self._lock:
            pcm = self._latest_pcm
            seq = self._pcm_seq
            live = self.live_capture

        has_live = False
        target = [0.0] * self.num_bands
        wave = self._wave

        if is_playing and live and pcm and len(pcm) == FFT_N * 2:
            if seq != self._last_processed_seq:
                self._last_processed_seq = seq
                ints = struct.unpack(f"<{FFT_N}h", pcm)
                samples = [v / 32768.0 for v in ints]
                rms = math.sqrt(sum(v * v for v in samples) / FFT_N)
                if rms > 0.0025:
                    self._last_audio_ts = now
                    mags = _fft_mags(samples)
                    bands = []
                    frame_peak = 0.0
                    for idx, (b0, b1) in enumerate(self._bin_ranges):
                        val = sum(mags[b0:b1]) / max(1, b1 - b0)
                        # Emphasize vocal presence (mids) and crisp treble sparkle
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
                    wave = self._extract_triggered_wave(samples, 128)
                    has_live = True
            elif now - self._last_audio_ts < 0.8:
                # Re-use previous smooth target briefly between PCM frames
                target = list(self._smooth)
                has_live = True

        if not is_playing:
            target = [0.0] * self.num_bands
            wave = [v * 0.65 for v in self._wave]
        elif not has_live and (now - self._last_audio_ts > 1.0):
            # Reactive synth fallback if PipeWire monitor is silent/unavailable
            p = self._phase
            target = []
            for i in range(self.num_bands):
                x = i / max(1, self.num_bands - 1)
                bass = math.exp(-((x - 0.16) ** 2) / 0.05) * (0.55 + 0.45 * math.sin(p * 1.5))
                mid = math.exp(-((x - 0.50) ** 2) / 0.08) * (0.45 + 0.42 * math.sin(p * 2.3 + i * 0.28))
                hi = math.exp(-((x - 0.82) ** 2) / 0.06) * (0.28 + 0.35 * math.sin(p * 3.5 - i * 0.4))
                target.append(min(0.96, max(0.04, bass + mid + hi)))
            wave = [
                0.65 * math.sin(p * 1.6 + i * 0.16) + 0.35 * math.sin(p * 3.2 - i * 0.31)
                for i in range(128)
            ]

        # Ultra-snappy attack (0.88) so vocals/drums speak IMMEDIATELY, crisp decay (0.64)
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

        # Separate Bass (kick), Vocal (mids), and Beat Onset Flash
        bass_now = sum(self._smooth[:8]) / 8.0
        vocal_now = sum(self._smooth[10:30]) / 20.0
        self._bass_energy = bass_now
        self._vocal_energy = vocal_now

        if bass_now > self._bass_avg * 1.24 and bass_now > 0.38:
            self._beat_flash = min(1.0, (bass_now - self._bass_avg) * 2.6 + 0.45)
        else:
            self._beat_flash = max(0.0, self._beat_flash - 0.14)
        self._bass_avg = self._bass_avg * 0.88 + bass_now * 0.12

        is_quiet = (not is_playing) and max(self._smooth, default=0.0) < 0.008 and max((abs(v) for v in self._wave), default=0.0) < 0.01
        return {
            "levels": list(self._smooth),
            "peaks": list(self._peaks),
            "wave": list(self._wave),
            "bass": self._bass_energy,
            "vocal": self._vocal_energy,
            "flash": self._beat_flash,
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


def _render_braille_canvas(grid: list[bytearray], width: int, height: int, row_styles: list[Style], header: str) -> Text:
    out = Text()
    if header:
        pad = max(0, width - len(header) - 2)
        out.append(" " * pad + header + "  \n", style=_STYLE_TAG)
    else:
        out.append("\n")

    for y in range(height):
        row = grid[y]
        chars = "".join(chr(0x2800 | b) if b else " " for b in row)
        out.append(chars, style=row_styles[y])
        if y < height - 1:
            out.append("\n")
    return out


def _render_speak(frame: dict, width: int, vis_h: int, header: str) -> Text:
    """Symmetric vocal-centered dual wave that literally breathes & speaks the song."""
    out = Text()
    pad = max(0, width - len(header) - 2)
    out.append(" " * pad + header + "  \n", style=_STYLE_TAG)

    bar_w, gap = (2, 1) if width >= 88 else ((1, 1) if width >= 46 else (1, 0))
    step = bar_w + gap
    num_bars = max(10, (width - 2) // step)
    left_pad = " " * max(0, (width - (num_bars * step - gap)) // 2)
    gap_str = " " * gap

    # Mirror spectrum horizontally from center outward (bass/vocals strong in the middle like a voice!)
    half_bars = (num_bars + 1) // 2
    half_lv = _resample(frame["levels"][:40], half_bars)
    # Apply gentle Hanning voice-envelope so edges taper smoothly like a living mouth/voice wave
    sym = []
    for i in range(num_bars):
        dist_center = abs(i - (num_bars - 1) / 2.0) / max(1.0, (num_bars - 1) / 2.0)
        idx = min(half_bars - 1, int(dist_center * (half_bars - 1)))
        taper = 0.25 + 0.75 * math.cos(dist_center * math.pi * 0.46)
        boost = 1.0 + 0.22 * frame["flash"] * (1.0 - dist_center)
        sym.append(min(1.0, half_lv[idx] * taper * boost))

    mid = vis_h / 2.0
    flash = frame["flash"]
    full_blk = "█" * bar_w
    top_blk = "▀" * bar_w
    bot_blk = "▄" * bar_w
    axis_blk = "─" * bar_w
    spc_blk = " " * bar_w

    for row in range(vis_h):
        # Distance from horizontal centerline (0 at center -> 1 at outer top/bottom edge)
        dist = abs((row + 0.5) - mid) / max(0.5, mid)
        row_style = _pick_style(dist, flash * (1.0 - dist * 0.5))
        is_center = abs((row + 0.5) - mid) <= 0.55

        buf = [left_pad]
        has_axis_only = True
        for b in range(num_bars):
            amp = sym[b]
            if amp >= dist + 0.045:
                buf.append(full_blk)
                has_axis_only = False
            elif amp >= dist - 0.045 and amp > 0.03:
                buf.append(top_blk if row >= mid else bot_blk)
                has_axis_only = False
            elif is_center:
                buf.append(axis_blk)
            else:
                buf.append(spc_blk)
            if b < num_bars - 1 and gap:
                buf.append(gap_str)

        line_str = "".join(buf)
        out.append(line_str, style=_STYLE_AXIS if (is_center and has_axis_only) else row_style)
        if row < vis_h - 1:
            out.append("\n")
    return out


def _render_scope(frame: dict, width: int, vis_h: int, header: str) -> Text:
    """2x4 subpixel Unicode Braille real-time triggered PCM oscilloscope."""
    px_w = width * 2
    px_h = vis_h * 4
    grid = [bytearray(width) for _ in range(vis_h)]
    wave = _resample(frame["wave"], px_w)

    mid_y = (px_h - 1) / 2.0
    amp_scale = mid_y * (0.84 + 0.14 * frame["flash"])

    # Faint center horizon line in Braille
    cy = int(mid_y)
    cy_row, cy_sub = divmod(cy, 4)
    for x in range(0, px_w, 6):
        grid[cy_row][x >> 1] |= _BRAILLE_BITS[cy_sub][x & 1]

    prev_y = None
    for x in range(px_w):
        y = int(round(mid_y - wave[x] * amp_scale))
        y = max(0, min(px_h - 1, y))
        if prev_y is None:
            y0 = y1 = y
        else:
            y0, y1 = (prev_y, y) if prev_y <= y else (y, prev_y)
        cx = x >> 1
        dx = x & 1
        for py in range(y0, y1 + 1):
            grid[py >> 2][cx] |= _BRAILLE_BITS[py & 3][dx]
        # Thicken waveform by 1 subpixel on strong bass/vocal hits so it looks bold
        if frame["bass"] > 0.35 and y + 1 < px_h:
            grid[(y + 1) >> 2][cx] |= _BRAILLE_BITS[(y + 1) & 3][dx]
        prev_y = y

    mid_row = vis_h / 2.0
    row_styles = [
        _pick_style(min(1.0, abs((r + 0.5) - mid_row) / max(0.5, mid_row) + 0.25), frame["flash"])
        for r in range(vis_h)
    ]
    return _render_braille_canvas(grid, width, vis_h, row_styles, header)


def _render_orbit(frame: dict, width: int, vis_h: int, header: str) -> Text:
    """2x4 subpixel Braille radial halo that pulses with bass and ripples with frequency spectrum."""
    px_w = width * 2
    px_h = vis_h * 4
    grid = [bytearray(width) for _ in range(vis_h)]

    cx = px_w / 2.0
    cy = px_h / 2.0
    # Terminal character aspect ratio is ~1:2 (2 braille cols x 4 braille rows is already ~square!)
    max_r = min(cx * 0.85, cy * 0.90)
    base_r = max_r * (0.42 + 0.16 * frame["bass"] + 0.08 * frame["flash"])

    half_lv = _resample(frame["levels"][:36], 64)
    ring_lv = half_lv + list(reversed(half_lv))

    for i, (ca, sa) in enumerate(_ORBIT_LUT):
        amp = ring_lv[i]
        r_out = base_r + amp * (max_r - base_r)
        r_in = max(2.0, base_r - amp * base_r * 0.32)

        # Draw ring point + radial spoke outward
        span = max(1, int(r_out - r_in))
        for s in range(span + 1):
            r = r_in + (r_out - r_in) * (s / span)
            px = int(cx + ca * r)
            py = int(cy + sa * r)
            if 0 <= px < px_w and 0 <= py < px_h:
                grid[py >> 2][px >> 1] |= _BRAILLE_BITS[py & 3][px & 1]

    mid_row = vis_h / 2.0
    row_styles = [
        _pick_style(min(1.0, abs((r + 0.5) - mid_row) / max(0.5, mid_row) + 0.2), frame["flash"])
        for r in range(vis_h)
    ]
    return _render_braille_canvas(grid, width, vis_h, row_styles, header)


def _render_bars(frame: dict, width: int, vis_h: int, header: str, led: bool = False) -> Text:
    """Ultra-snappy Cava-style spectrum (PULSE) or segmented studio LED VU (MATRIX)."""
    out = Text()
    pad = max(0, width - len(header) - 2)
    out.append(" " * pad + header + "  \n", style=_STYLE_TAG)

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
        row_style = _pick_style(norm_y, flash * norm_y)
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
                    flush()
                    run_style = row_style
                run_chars.append(full_ch)
            elif (not led) and diff > 0.0:
                if run_style != row_style:
                    flush()
                    run_style = row_style
                idx = min(8, max(1, int(diff * 8)))
                run_chars.append(SUB_BLOCKS[idx] * bar_w)
            elif rfb == p_cell and pk[b] > 0.05:
                if run_style != peak_style:
                    flush()
                    run_style = peak_style
                run_chars.append(peak_ch)
            else:
                run_chars.append(spc)
            if b < num_bars - 1 and gap:
                run_chars.append(gap_str)
        flush()
        if row < vis_h - 1:
            out.append("\n")
    return out


def render_visualizer(frame: dict, width: int, height: int, style_name: str = "SPEAK", header_tag: str = "") -> Text:
    width = max(24, width)
    height = max(6, height)
    vis_h = max(4, height - 1)

    if style_name == "SPEAK":
        return _render_speak(frame, width, vis_h, header_tag)
    if style_name == "SCOPE":
        return _render_scope(frame, width, vis_h, header_tag)
    if style_name == "ORBIT":
        return _render_orbit(frame, width, vis_h, header_tag)
    if style_name == "MATRIX":
        return _render_bars(frame, width, vis_h, header_tag, led=True)
    return _render_bars(frame, width, vis_h, header_tag, led=False)
