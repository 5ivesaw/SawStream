import atexit
import math
import shutil
import struct
import subprocess
import threading
import time

from rich.style import Style
from rich.text import Text

# Pure-Python 256-point Radix-2 FFT (~1.2ms/frame, zero external dependencies)
FFT_N = 256
SAMPLE_RATE = 22050
_HANN = [0.5 * (1.0 - math.cos(2.0 * math.pi * i / (FFT_N - 1))) for i in range(FFT_N)]
_BITREV = [int(f"{i:08b}"[::-1], 2) for i in range(FFT_N)]
_TWIDDLES = []
_sz = 2
while _sz <= FFT_N:
    _half = _sz // 2
    _step = -2.0 * math.pi / _sz
    _TWIDDLES.append([(math.cos(_step * k), math.sin(_step * k)) for k in range(_half)])
    _sz *= 2

SUB_BLOCKS = " ▁▂▃▄▅▆▇█"
STYLES = ("BARS", "MIRROR", "LED")


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
                tr = wr * re[j] - wi * im[j]
                ti = wr * im[j] + wi * re[j]
                i = start + k
                re[j] = re[i] - tr
                im[j] = im[i] - ti
                re[i] += tr
                im[i] += ti
        size *= 2
        stage += 1
    return [math.hypot(re[i], im[i]) for i in range(1, FFT_N // 2)]


def _gradient_color(norm_y: float) -> str:
    """Smooth vertical gradient from deep crimson (0.0) -> neon red (0.55) -> warm coral/white (1.0)."""
    t = max(0.0, min(1.0, norm_y))
    if t < 0.5:
        u = t / 0.5
        r = int(110 + (255 - 110) * u)
        g = int(10 + (46 - 10) * u)
        b = int(28 + (67 - 28) * u)
    else:
        u = (t - 0.5) / 0.5
        r = 255
        g = int(46 + (215 - 46) * u)
        b = int(67 + (225 - 67) * u)
    return f"rgb({r},{g},{b})"


class AudioVisualizerEngine:
    """
    Captures live desktop/mpv audio from PipeWire/PulseAudio monitor via parec/pw-cat,
    runs real-time logarithmic FFT binning with AGC & gravity peaks, and falls back
    smoothly to a reactive synth wave if system capture is unavailable.
    """

    def __init__(self, num_bands: int = 48):
        self.num_bands = num_bands
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None

        self.live_capture = False
        self._raw_bands = [0.0] * num_bands
        self._smooth = [0.0] * num_bands
        self._peaks = [0.0] * num_bands
        self._agc = 2.5
        self._last_audio_ts = 0.0
        self._phase = 0.0
        self._bin_ranges = self._build_log_bins(num_bands)
        atexit.register(self.stop)

    @staticmethod
    def _build_log_bins(num_bands: int) -> list[tuple[int, int]]:
        max_bin = (FFT_N // 2) - 1  # 127
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
                "--latency-msec=30",
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
                time.sleep(0.06)
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
                proc.wait(timeout=0.5)
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
        unpack_fmt = f"<{FFT_N}h"

        try:
            while not self._stop_evt.is_set() and proc.poll() is None:
                raw = proc.stdout.read(chunk_bytes)
                if not raw or len(raw) < chunk_bytes:
                    break
                ints = struct.unpack(unpack_fmt, raw)
                samples = [v / 32768.0 for v in ints]
                mags = _fft_mags(samples)

                # Logarithmic frequency bands with slight treble boost so highs stay lively
                bands = []
                frame_peak = 0.0
                for idx, (b0, b1) in enumerate(self._bin_ranges):
                    val = sum(mags[b0:b1]) / max(1, b1 - b0)
                    boost = 0.85 + 1.35 * (idx / max(1, self.num_bands - 1))
                    val = math.log1p(val * 3.8 * boost)
                    if val > frame_peak:
                        frame_peak = val
                    bands.append(val)

                # Auto-gain control
                if frame_peak > self._agc:
                    self._agc = self._agc * 0.6 + frame_peak * 0.4
                else:
                    self._agc = max(1.1, self._agc * 0.992)

                norm = [min(1.0, b / max(0.8, self._agc)) for b in bands]
                with self._lock:
                    self._raw_bands = norm
                    if frame_peak > 0.04:
                        self._last_audio_ts = time.monotonic()
        except Exception:
            pass
        finally:
            self.live_capture = False
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    def step_levels(self, is_playing: bool) -> tuple[list[float], list[float], bool]:
        """Returns smoothed (levels, peaks, using_live_pcm) at UI frame rate."""
        self._phase += 0.22
        now = time.monotonic()
        with self._lock:
            raw = list(self._raw_bands)
            has_live_signal = self.live_capture and (now - self._last_audio_ts < 1.2)

        if not is_playing:
            target = [0.0] * self.num_bands
        elif has_live_signal:
            target = raw
        else:
            # Smooth multi-harmonic fallback wave when PipeWire monitor is unavailable
            p = self._phase
            target = []
            for i in range(self.num_bands):
                x = i / max(1, self.num_bands - 1)
                bass_env = math.exp(-((x - 0.18) ** 2) / 0.06) * (0.55 + 0.45 * math.sin(p * 1.45))
                mid_env = math.exp(-((x - 0.52) ** 2) / 0.08) * (0.45 + 0.40 * math.sin(p * 2.1 + i * 0.25))
                hi_env = math.exp(-((x - 0.82) ** 2) / 0.06) * (0.28 + 0.32 * math.sin(p * 3.3 - i * 0.4))
                ripple = 0.08 * (1.0 + math.sin(p * 1.9 + i * 0.55))
                target.append(min(0.96, max(0.05, bass_env + mid_env + hi_env + ripple)))

        # Smooth rise, natural gravity decay, floating peak caps
        for i in range(self.num_bands):
            t = target[i]
            cur = self._smooth[i]
            self._smooth[i] = cur * 0.38 + t * 0.62 if t > cur else max(0.0, cur * 0.78 + t * 0.22)
            if self._smooth[i] >= self._peaks[i]:
                self._peaks[i] = self._smooth[i]
            else:
                self._peaks[i] = max(self._smooth[i], self._peaks[i] - 0.028)

        return list(self._smooth), list(self._peaks), has_live_signal


def _resample(values: list[float], count: int) -> list[float]:
    if count <= 0:
        return []
    n = len(values)
    if count == n:
        return values
    out = []
    for i in range(count):
        pos = i * (n - 1) / max(1, count - 1)
        lo = int(pos)
        hi = min(n - 1, lo + 1)
        frac = pos - lo
        out.append(values[lo] * (1.0 - frac) + values[hi] * frac)
    return out


def render_visualizer(
    levels: list[float],
    peaks: list[float],
    width: int,
    height: int,
    style_name: str = "BARS",
    header_tag: str = "",
) -> Text:
    """Renders a crisp, full-canvas Truecolor audio visualizer (BARS, MIRROR, or LED)."""
    width = max(20, width)
    height = max(6, height)

    # Reserve 1 top row for a super clean subtle corner tag
    vis_h = max(4, height - 1)
    out = Text()
    if header_tag:
        pad = max(0, width - len(header_tag) - 2)
        out.append(" " * pad + header_tag + "  \n", style="#5a606c")
    else:
        out.append("\n")

    # Compute bar count + spacing so bars look thick and clean
    if width >= 90:
        bar_w, gap = 2, 1
    elif width >= 50:
        bar_w, gap = 1, 1
    else:
        bar_w, gap = 1, 0

    step = bar_w + gap
    num_bars = max(8, (width - 2) // step)
    left_pad = max(0, (width - (num_bars * step - gap)) // 2)

    lv = _resample(levels, num_bars)
    pk = _resample(peaks, num_bars)
    gap_str = " " * gap

    if style_name == "MIRROR":
        mid = vis_h / 2.0
        for row in range(vis_h):
            dist = abs((row + 0.5) - mid) / max(0.5, mid)  # 0 at center, 1 at top/bottom edge
            col_style = Style(color=_gradient_color(dist))
            out.append(" " * left_pad)
            for b in range(num_bars):
                amp = lv[b]
                if amp >= dist + 0.04:
                    out.append("█" * bar_w, style=col_style)
                elif amp >= dist - 0.06 and amp > 0.03:
                    ch = "▀" if row >= mid else "▄"
                    out.append(ch * bar_w, style=col_style)
                else:
                    if abs((row + 0.5) - mid) < 0.6:
                        out.append("─" * bar_w, style="#2a080d")
                    else:
                        out.append(" " * bar_w)
                if b < num_bars - 1 and gap:
                    out.append(gap_str)
            if row < vis_h - 1:
                out.append("\n")
        return out

    if style_name == "LED":
        for row in range(vis_h):
            row_from_bottom = vis_h - 1 - row
            norm_y = row_from_bottom / max(1, vis_h - 1)
            bar_style = Style(color=_gradient_color(norm_y))
            peak_style = Style(color="#ffffff", bold=True)
            out.append(" " * left_pad)
            for b in range(num_bars):
                h_cells = lv[b] * vis_h
                p_cell = int(pk[b] * (vis_h - 1))
                if row_from_bottom < int(h_cells):
                    out.append("━" * bar_w, style=bar_style)
                elif row_from_bottom == p_cell and pk[b] > 0.04:
                    out.append("─" * bar_w, style=peak_style)
                else:
                    out.append(" " * bar_w)
                if b < num_bars - 1 and gap:
                    out.append(gap_str)
            if row < vis_h - 1:
                out.append("\n")
        return out

    # Default: BARS (Cava-style smooth sub-cell bars with floating peak caps)
    for row in range(vis_h):
        row_from_bottom = vis_h - 1 - row
        norm_y = row_from_bottom / max(1, vis_h - 1)
        bar_style = Style(color=_gradient_color(norm_y))
        peak_style = Style(color="#ffd6dc")
        out.append(" " * left_pad)
        for b in range(num_bars):
            h_cells = lv[b] * vis_h
            p_cell = int(pk[b] * (vis_h - 1))
            diff = h_cells - row_from_bottom
            if diff >= 1.0:
                out.append("█" * bar_w, style=bar_style)
            elif diff > 0.0:
                idx = min(8, max(1, int(diff * 8)))
                out.append(SUB_BLOCKS[idx] * bar_w, style=bar_style)
            elif row_from_bottom == p_cell and pk[b] > 0.05:
                out.append("▔" * bar_w, style=peak_style)
            else:
                out.append(" " * bar_w)
            if b < num_bars - 1 and gap:
                out.append(gap_str)
        if row < vis_h - 1:
            out.append("\n")
    return out
