import atexit
import json
import os
import socket
import subprocess
import threading
import time

from sawstream.storage import commit_partial_recording


class HeadlessMPV:
    """Silent headless mpv engine controlled strictly over Unix JSON IPC (zero terminal output)."""

    def __init__(self, initial_volume: int = 100):
        self.sock_path = f"/tmp/sawstream_mpv_{os.getpid()}.sock"
        self.proc = None
        self.initial_volume = initial_volume
        self._lock = threading.Lock()
        self._req_id = 1
        self._active_partial = ""
        self._active_final = ""
        atexit.register(self.shutdown)

    def ensure_started(self) -> None:
        if self.proc and self.proc.poll() is None and os.path.exists(self.sock_path):
            return
        self.shutdown()
        self.proc = subprocess.Popen(
            [
                "mpv",
                "--no-video",
                "--no-terminal",
                "--really-quiet",
                "--idle=yes",
                "--force-window=no",
                "--cache=yes",
                "--demuxer-max-bytes=64MiB",
                f"--volume={self.initial_volume}",
                f"--input-ipc-server={self.sock_path}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if os.path.exists(self.sock_path):
                break
            time.sleep(0.04)

    def command(self, *args):
        with self._lock:
            self.ensure_started()
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(1.2)
                s.connect(self.sock_path)
                req_id = self._req_id
                self._req_id += 1
                payload = json.dumps({"command": list(args), "request_id": req_id}) + "\n"
                s.sendall(payload.encode("utf-8"))
                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                s.close()
                for line in buf.decode("utf-8", "ignore").splitlines():
                    try:
                        msg = json.loads(line)
                        if msg.get("request_id") == req_id:
                            return msg.get("data")
                    except Exception:
                        continue
            except Exception:
                return None
        return None

    def get_prop(self, prop: str):
        return self.command("get_property", prop)

    def set_prop(self, prop: str, value):
        return self.command("set_property", prop, value)

    def finalize_recording(self, completed: bool) -> bool:
        """Flushes stream-record and atomically commits or deletes the partial file."""
        if self.proc and self.proc.poll() is None:
            self.set_prop("stream-record", "")
            time.sleep(0.08)
        saved = commit_partial_recording(self._active_partial, self._active_final, completed=completed)
        self._active_partial = ""
        self._active_final = ""
        return saved

    def play_source(self, source: str, partial_path: str = "", final_path: str = "", prev_completed: bool = False) -> bool:
        saved_prev = self.finalize_recording(completed=prev_completed)
        if partial_path and final_path:
            self._active_partial = partial_path
            self._active_final = final_path
            self.set_prop("stream-record", partial_path)
        self.command("loadfile", source, "replace")
        self.set_prop("pause", False)
        return saved_prev

    def toggle_pause(self) -> None:
        self.command("cycle", "pause")

    def toggle_mute(self) -> None:
        self.command("cycle", "mute")

    def seek(self, seconds: int) -> None:
        self.command("seek", seconds, "relative")

    def adjust_volume(self, delta: int) -> int:
        vol = self.get_prop("volume")
        vol = self.initial_volume if vol is None else int(vol)
        new_vol = max(0, min(150, vol + delta))
        self.set_prop("volume", new_vol)
        return new_vol

    def snapshot_state(self) -> dict:
        if not self.proc or self.proc.poll() is not None:
            return {"idle": True}
        return {
            "idle": self.get_prop("idle-active"),
            "pos": self.get_prop("time-pos") or 0.0,
            "dur": self.get_prop("duration") or 0.0,
            "paused": bool(self.get_prop("pause")),
            "muted": bool(self.get_prop("mute")),
            "vol": self.get_prop("volume"),
            "cache": self.get_prop("demuxer-cache-duration") or 0.0,
            "bitrate": self.get_prop("audio-bitrate") or 0,
            "codec": self.get_prop("audio-codec-name") or "",
        }

    def stop(self, completed: bool = False) -> None:
        if self.proc and self.proc.poll() is None:
            self.finalize_recording(completed=completed)
            self.command("stop")

    def shutdown(self) -> None:
        try:
            self.finalize_recording(completed=False)
        except Exception:
            pass
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1.5)
            except Exception:
                self.proc.kill()
        self.proc = None
        if os.path.exists(self.sock_path):
            try:
                os.remove(self.sock_path)
            except OSError:
                pass
