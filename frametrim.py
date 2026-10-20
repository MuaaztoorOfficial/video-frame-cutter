"""
FrameTrim v2 - local, manual, frame-accurate video trimmer (Windows-first, Tkinter + FFmpeg).

No third-party Python packages needed (Pillow is no longer required).
FFmpeg is looked up next to this file, in .\\ffmpeg, .\\bin, .\\ffmpeg-*\\bin, or on PATH.
On Windows the app can also download FFmpeg for you the first time.
"""
import base64
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import OrderedDict, deque
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NAME = "FrameTrim"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts", ".m2ts", ".ts",
              ".wmv", ".flv", ".mpg", ".mpeg"}
CRF = "16"
X264_PRESET = "medium"
AUDIO_BITRATE = "192k"
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROGRESS_RE = re.compile(
    r"^(frame|fps|stream_\d+_\d+_q|bitrate|total_size|out_time_us|out_time_ms|out_time|"
    r"dup_frames|drop_frames|speed|progress)=")


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- helpers
def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def log_exception(exc_info=None):
    """Append the traceback to frametrim_error.log next to the app (never raises)."""
    try:
        with open(app_dir() / "frametrim_error.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("\n[%Y-%m-%d %H:%M:%S]\n"))
            f.write("".join(traceback.format_exception(*(exc_info or sys.exc_info()))))
    except Exception:
        pass


_bin_cache = {}


def find_binary(name):
    if _bin_cache.get(name):
        if os.path.isfile(_bin_cache[name]):
            return _bin_cache[name]
    exe = name + (".exe" if os.name == "nt" else "")
    here = app_dir()
    cands = [here / exe, here / "ffmpeg" / exe, here / "bin" / exe, here / "ffmpeg" / "bin" / exe]
    cands += sorted(here.glob("ffmpeg*/bin/" + exe), reverse=True)
    found = None
    for c in cands:
        if c.is_file():
            found = str(c)
            break
    if not found:
        found = shutil.which(name)
    if found:
        _bin_cache[name] = found
    return found


def run_capture(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=timeout)


_encoder_cache = {}


def has_encoder(name):
    if _encoder_cache.get(name):
        return True
    ffmpeg = find_binary("ffmpeg")
    if not ffmpeg:
        return False
    try:
        p = run_capture([ffmpeg, "-hide_banner", "-encoders"], timeout=20)
        ok = re.search(r"\b" + re.escape(name) + r"\b", p.stdout or "") is not None
    except Exception:
        ok = False
    if ok:
        _encoder_cache[name] = True
    return ok


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def parse_rate(rate):
    try:
        a, b = str(rate).split("/")
        a, b = float(a), float(b)
        return a / b if b else 0.0
    except Exception:
        return to_float(rate)


def fmt_time(sec):
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def count_video_packets(path):
    """Fast (no decode) frame count for files whose header has no nb_frames."""
    ffprobe = find_binary("ffprobe")
    try:
        p = run_capture([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
                         "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)])
        m = re.search(r"\d+", p.stdout or "")
        return int(m.group(0)) if m else 0
    except Exception:
        return 0


def first_frame_origin(path, with_audio=False):
    """Timestamp of the first video frame on FFmpeg's own timeline (the one -ss uses).
    Many files (mkv, some ts/mp4) do not start at exactly 0; without this offset frame N would be
    shown/cut up to a frame too early or late. With audio mapped (export) the timeline can differ from
    video-only (preview) on some TS files, so both variants are measured."""
    ffmpeg = find_binary("ffmpeg")
    try:
        maps = ["-map", "0:v:0", "-map", "0:a?", "-t", "1"] if with_audio else ["-an", "-sn"]
        p = run_capture([ffmpeg, "-nostdin", "-hide_banner", "-v", "info", "-i", str(path)] + maps +
                        ["-frames:v", "1", "-vf", "showinfo", "-f", "null", "-"], timeout=60)
        m = re.search(r"pts_time:\s*(-?[0-9.]+)", p.stderr or "")
        origin = float(m.group(1)) if m else 0.0
        return origin if -1.0 <= origin <= 30.0 else 0.0
    except Exception:
        return 0.0


def seek_args(ss, margin=3.0):
    """Hybrid seek: fast coarse seek (input side) landing safely BEFORE the target, then an exact
    decode-accurate seek (output side). Input-only seeking is off by several frames on MPEG-TS."""
    coarse = max(0.0, ss - margin)
    return ["-ss", f"{coarse:.6f}"], ["-ss", f"{ss - coarse:.6f}"]


def probe(path):
    ffprobe = find_binary("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found. Put FFmpeg next to the app or add it to PATH.")
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,duration,nb_frames:"
           "format=duration,format_name", "-of", "json", str(path)]
    p = run_capture(cmd)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "Could not read this video.")
    data = json.loads(p.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError("No video stream found in this file.")
    s = streams[0]
    fmt = data.get("format") or {}
    avg = parse_rate(s.get("avg_frame_rate"))
    real = parse_rate(s.get("r_frame_rate"))
    fps = avg or real or 30.0
    vfr = bool(avg and real and abs(avg - real) / real > 0.01)
    duration = to_float(s.get("duration")) or to_float(fmt.get("duration"))
    frames = to_int(s.get("nb_frames")) or count_video_packets(path)
    if not frames and duration:
        frames = int(round(duration * fps))
    if frames < 1:
        raise RuntimeError("Could not determine the number of frames.")
    if not duration:
        duration = frames / fps
    return {"fps": fps, "frames": frames, "duration": duration, "vfr": vfr,
            "origin": first_frame_origin(path), "origin_av": first_frame_origin(path, True),
            "codec": s.get("codec_name", "unknown"), "width": s.get("width", "?"),
            "height": s.get("height", "?")}


def retry_replace(src, dst, tries=8, delay=0.4):
    """os.replace with retries: Windows briefly locks files (antivirus, preview process)."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(delay)


def unique_backup(src):
    cand = src.with_name(src.name + ".bak.original")
    n = 2
    while cand.exists():
        cand = src.with_name(f"{src.name}.bak{n}.original")
        n += 1
    return cand


def same_file(a, b):
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def build_export_cmd(ffmpeg, src, dest, start, end, fps, total, origin=0.0):
    # Frame n sits at origin + n/fps. Seeking slightly BEFORE the first wanted frame (0.05 frame) and
    # cutting a little BEFORE the first unwanted frame (0.25 frame) is robust against float /
    # timestamp rounding: no dropped first frame, no extra last frame, no duplicated frame.
    ss = max(0.0, origin + (start - 0.05) / fps)
    pre, post = seek_args(ss)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-progress", "pipe:1", "-nostats"] + pre + ["-i", str(src)] + post
    if end < total:
        cmd += ["-t", f"{(end - start - 0.25) / fps:.6f}"]
    cmd += ["-map", "0:v:0", "-map", "0:a?",
            # libx264 + yuv420p need even dimensions; odd-sized sources would otherwise fail.
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-preset", X264_PRESET, "-crf", CRF, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-movflags", "+faststart", str(dest)]
    return cmd


def run_ffmpeg_progress(cmd, expected_seconds, on_progress, proc_setter):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                         creationflags=NO_WINDOW)
    proc_setter(p)
    tail = deque(maxlen=25)
    last = 0.0
    try:
        for raw in p.stdout:
            line = raw.strip()
            if not line:
                continue
            m = PROGRESS_RE.match(line)
            if m:
                if m.group(1) in ("out_time_us", "out_time_ms"):
                    try:
                        us = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    now = time.time()
                    if expected_seconds > 0 and now - last > 0.1:
                        last = now
                        on_progress(min(0.99, max(0.0, us / 1e6 / expected_seconds)))
                continue
            tail.append(line)
    finally:
        rc = p.wait()
        proc_setter(None)
    return rc, "\n".join(tail)


def download_ffmpeg(dest_dir, on_progress, cancel_check):
    import urllib.request
    import zipfile
    part = dest_dir / "ffmpeg_download.zip.part"
    try:
        req = urllib.request.Request(FFMPEG_ZIP_URL, headers={"User-Agent": "FrameTrim"})
        with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
            total = to_int(r.headers.get("Content-Length"))
            done = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    on_progress(min(0.99, done / total))
                if cancel_check():
                    raise Cancelled()
        got = set()
        with zipfile.ZipFile(part) as z:
            for name in z.namelist():
                base = name.rsplit("/", 1)[-1].lower()
                if base in ("ffmpeg.exe", "ffprobe.exe") and "/bin/" in name:
                    with z.open(name) as s, open(dest_dir / base, "wb") as o:
                        shutil.copyfileobj(s, o)
                    got.add(base)
        if got != {"ffmpeg.exe", "ffprobe.exe"}:
            raise RuntimeError("Downloaded archive did not contain ffmpeg.exe and ffprobe.exe.")
    finally:
        try:
            part.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- preview worker
class PreviewWorker(threading.Thread):
    """Grabs one frame as PNG. Only the newest request matters; older ffmpeg runs are killed,
    so holding an arrow key never queues up dozens of stale ffmpeg processes."""

    def __init__(self, deliver):
        super().__init__(daemon=True)
        self.deliver = deliver
        self.cv = threading.Condition()
        self.req = None
        self.token = 0
        self.proc = None
        self.stopped = False

    def request(self, args):
        with self.cv:
            self.token += 1
            self.req = (self.token, args)
            self._kill_locked()
            self.cv.notify()

    def _kill_locked(self):
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def kill_current(self):
        """Invalidate pending work and make sure no ffmpeg still holds the source file."""
        with self.cv:
            self.token += 1
            self.req = None
            p = self.proc
            self._kill_locked()
        if p is not None:
            try:
                p.wait(timeout=3)
            except Exception:
                pass

    def stop(self):
        with self.cv:
            self.stopped = True
            self.req = None
            self._kill_locked()
            self.cv.notify()

    def run(self):
        while True:
            with self.cv:
                while self.req is None and not self.stopped:
                    self.cv.wait()
                if self.stopped:
                    return
                token, args = self.req
                self.req = None
            data = err = None
            try:
                data = self._grab(token, args)
            except Exception as e:
                err = e
            with self.cv:
                stale = token != self.token
            if not stale:
                self.deliver(args, data, err)

    def _grab(self, token, args):
        path, frame, fps, w, h, _key, origin = args
        ffmpeg = find_binary("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found.")
        ss = max(0.0, origin + (frame - 0.5) / fps)  # first frame with pts >= ss is `frame`
        pre, post = seek_args(ss)
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"] + pre + [
               "-i", str(path)] + post + ["-an", "-sn", "-frames:v", "1",
               "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,format=rgb24",
               "-compression_level", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        with self.cv:
            self.proc = p
            if token != self.token:
                self._kill_locked()
        out, err = p.communicate()
        with self.cv:
            self.proc = None
        if p.returncode != 0 or not out.startswith(b"\x89PNG"):
            raise RuntimeError(err.decode("utf-8", "replace").strip() or "No frame returned.")
        return base64.b64encode(out).decode("ascii")


# --------------------------------------------------------------------------- app
class FrameTrimApp:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME} - manual frame-accurate clip trimmer")
        root.geometry("980x740")
        root.minsize(820, 620)

        self.path = None
        self.mtime = 0
        self.fps = 30.0
        self.total = 0
        self.duration = 0.0
        self.vfr = False
        self.origin = 0.0
        self.origin_av = 0.0
        self.busy = False
        self.committing = False
        self.cancel_flag = False
        self.export_proc = None
        self.job_thread = None
        self.folder_files = []
        self.folder_idx = -1
        self.cache = OrderedDict()
        self.pending_key = None
        self.photo = None
        self._cfg_job = None
        self._scale_lock = False
        self.lock_widgets = []
        self.q = queue.Queue()

        self.status = tk.StringVar(value="Open a clip to begin.")
        self.info = tk.StringVar(value="No clip loaded")
        self.folder_var = tk.StringVar(value="")
        self.start_var = tk.StringVar(value="0")
        self.end_var = tk.StringVar(value="0")
        self.current_var = tk.StringVar(value="0")
        self.keep_var = tk.StringVar(value="")
        self.time_var = tk.StringVar(value="")

        self.preview = PreviewWorker(lambda a, d, e: self.ui(self._on_preview, a, d, e))
        self.preview.start()
        self._build()
        self.start_var.trace_add("write", self._update_keep)
        self.end_var.trace_add("write", self._update_keep)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self.on_tk_exception
        root.after(40, self._pump)
        root.after(300, self._startup_check)

    # ---- thread -> UI bridge
    def ui(self, fn, *args):
        self.q.put((fn, args))

    def _pump(self):
        try:
            while True:
                fn, args = self.q.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    log_exception()
                    self.set_status("Internal error (see frametrim_error.log).", "error")
        except queue.Empty:
            pass
        self.root.after(40, self._pump)

    def on_tk_exception(self, exc, val, tb):
        log_exception((exc, val, tb))
        self.set_status(f"Internal error: {val}", "error")

    # ---- UI construction
    def _btn(self, parent, text, cmd, lock=True):
        b = ttk.Button(parent, text=text, command=cmd, takefocus=False)
        if lock:
            self.lock_widgets.append(b)
        return b

    def _build(self):
        r = self.root
        top = ttk.Frame(r, padding=(10, 10, 10, 2))
        top.pack(fill="x")
        self._btn(top, "Open Clip", self.open_file).pack(side="left")
        self._btn(top, "Open Folder", self.open_folder).pack(side="left", padx=6)
        self._btn(top, "Previous", lambda: self.step_file(-1)).pack(side="left", padx=(18, 3))
        self._btn(top, "Next", lambda: self.step_file(1)).pack(side="left")
        ttk.Label(top, textvariable=self.folder_var).pack(side="left", padx=10)
        self._btn(top, "Refresh preview", self.request_preview, lock=False).pack(side="right")

        info = ttk.Label(r, textvariable=self.info, padding=(12, 2), wraplength=900)
        info.pack(fill="x")
        info.bind("<Configure>", lambda e: e.widget.configure(wraplength=max(200, e.width - 24)))

        self.canvas = tk.Canvas(r, bg="#181818", height=300, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=12, pady=6)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Button-1>", lambda e: self.canvas.focus_set())

        seek = ttk.Frame(r, padding=(12, 0))
        seek.pack(fill="x")
        self.scale = ttk.Scale(seek, from_=0, to=1, orient="horizontal", command=self._on_scale)
        self.scale.pack(side="left", fill="x", expand=True)
        self.lock_widgets.append(self.scale)
        ttk.Label(seek, textvariable=self.time_var, width=14, anchor="e").pack(side="left", padx=(8, 0))

        nav = ttk.LabelFrame(r, text="Frame navigation", padding=8)
        nav.pack(fill="x", padx=12, pady=5)
        self._btn(nav, "◀ 1 frame", lambda: self.move_frame(-1)).grid(row=0, column=0, padx=3)
        self._btn(nav, "1 frame ▶", lambda: self.move_frame(1)).grid(row=0, column=1, padx=3)
        self._btn(nav, "◀ 10", lambda: self.move_frame(-10)).grid(row=0, column=2, padx=3)
        self._btn(nav, "10 ▶", lambda: self.move_frame(10)).grid(row=0, column=3, padx=3)
        ttk.Label(nav, text="Current frame:").grid(row=0, column=4, padx=(16, 4))
        self.current_entry = ttk.Entry(nav, textvariable=self.current_var, width=10)
        self.current_entry.grid(row=0, column=5)
        self.current_entry.bind("<Return>", lambda e: self.go_frame())
        self._btn(nav, "Go", self.go_frame).grid(row=0, column=6, padx=4)
        ttk.Label(nav, text="Keys: ←/→ 1 frame · Shift+←/→ 10 · Home/End · I = set start · O = set end",
                  foreground="#555").grid(row=0, column=7, padx=10, sticky="w")

        cuts = ttk.LabelFrame(r, text="Trim range (start inclusive, end exclusive)", padding=8)
        cuts.pack(fill="x", padx=12, pady=5)
        ttk.Label(cuts, text="Start frame:").grid(row=0, column=0, sticky="w")
        e1 = ttk.Entry(cuts, textvariable=self.start_var, width=10)
        e1.grid(row=0, column=1, padx=4)
        e1.bind("<Return>", lambda e: self.goto_start())
        self._btn(cuts, "Set start = current", self.set_start).grid(row=0, column=2, padx=3)
        self._btn(cuts, "Show start", self.goto_start).grid(row=0, column=3, padx=3)
        ttk.Label(cuts, text="End frame (excl.):").grid(row=0, column=4, padx=(14, 0))
        e2 = ttk.Entry(cuts, textvariable=self.end_var, width=10)
        e2.grid(row=0, column=5, padx=4)
        e2.bind("<Return>", lambda e: self.goto_last_kept())
        self._btn(cuts, "Set end = current + 1", self.set_end).grid(row=0, column=6, padx=3)
        self._btn(cuts, "Show last kept", self.goto_last_kept).grid(row=0, column=7, padx=3)
        self._btn(cuts, "Reset", self.reset_range).grid(row=0, column=8, padx=3)
        ttk.Label(cuts, textvariable=self.keep_var, foreground="#245a2b").grid(
            row=1, column=0, columnspan=9, sticky="w", pady=(6, 0))

        bottom = ttk.Frame(r, padding=(12, 8, 12, 0))
        bottom.pack(fill="x")
        self._btn(bottom, "Save as NEW file…", self.save_new).pack(side="left")
        self._btn(bottom, "Replace ORIGINAL (backup first)", self.replace_original).pack(side="left", padx=8)
        self.progress = ttk.Progressbar(bottom, length=220, maximum=100)
        self.cancel_btn = ttk.Button(bottom, text="Cancel", command=self.cancel_job, takefocus=False)

        self.status_lbl = ttk.Label(r, textvariable=self.status, foreground="#245a2b",
                                    padding=(12, 6, 12, 10), wraplength=900)
        self.status_lbl.pack(fill="x")
        self.status_lbl.bind("<Configure>", lambda e: e.widget.configure(wraplength=max(200, e.width - 24)))

        def key(fn):
            def handler(_e):
                try:
                    w = self.root.focus_get()
                except Exception:
                    w = None
                if isinstance(w, (tk.Entry, ttk.Entry)):
                    return None  # let the user type / move the cursor inside text boxes
                fn()
                return "break"
            return handler

        for seq, fn in (("<Left>", lambda: self.move_frame(-1)), ("<Right>", lambda: self.move_frame(1)),
                        ("<Shift-Left>", lambda: self.move_frame(-10)),
                        ("<Shift-Right>", lambda: self.move_frame(10)),
                        ("<Home>", lambda: self.go_to(0)), ("<End>", lambda: self.go_to(self.total - 1)),
                        ("<space>", self.request_preview),
                        ("<KeyPress-i>", self.set_start), ("<KeyPress-I>", self.set_start),
                        ("<KeyPress-o>", self.set_end), ("<KeyPress-O>", self.set_end)):
            r.bind(seq, key(fn))
        r.bind("<Control-o>", lambda e: self.open_file())
        self._draw_placeholder()

    # ---- status helpers
    def set_status(self, text, kind="ok"):
        color = {"ok": "#245a2b", "error": "#b00020", "warn": "#8a5a00"}.get(kind, "#245a2b")
        self.status_lbl.configure(foreground=color)
        self.status.set(text)

    def _draw_placeholder(self):
        self.canvas.delete("all")
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.canvas.create_text(max(w, 200) // 2, max(h, 100) // 2, text="Open a video to see frame previews",
                                fill="#dddddd", font=("Segoe UI", 15))

    def _update_keep(self, *_):
        if not self.path:
            self.keep_var.set("")
            return
        try:
            s, e = int(self.start_var.get()), int(self.end_var.get())
        except ValueError:
            self.keep_var.set("Start/end must be whole numbers.")
            return
        if 0 <= s < e <= self.total:
            n = e - s
            self.keep_var.set(f"Keeps {n} frames ({n / self.fps:.3f}s): frames {s} to {e - 1}")
        else:
            self.keep_var.set(f"Invalid range - need 0 ≤ start < end ≤ {self.total}")

    # ---- busy / jobs
    def set_busy(self, flag, text=None, determinate=False, cancellable=False):
        self.busy = flag
        for w in self.lock_widgets:
            w.state(["disabled" if flag else "!disabled"])
        if flag:
            self.progress.pack(side="left", padx=(14, 6))
            if determinate:
                self.progress.stop()
                self.progress.configure(mode="determinate", value=0)
            else:
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
            if cancellable:
                self.cancel_btn.state(["!disabled"])
                self.cancel_btn.pack(side="left")
            if text:
                self.set_status(text)
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.cancel_btn.pack_forget()

    def _set_progress(self, frac, label="Working"):
        self.progress["value"] = frac * 100
        self.set_status(f"{label}… {int(frac * 100)}%")

    def _start_job(self, label, work, done, determinate=False, cancellable=False):
        self.cancel_flag = False
        self.set_busy(True, label, determinate, cancellable)

        def runner():
            try:
                res = work()
            except Cancelled:
                self.ui(self._finish, done, "cancel", None)
            except BaseException as e:
                log_exception()
                self.ui(self._finish, done, "error", e)
            else:
                self.ui(self._finish, done, "ok", res)

        self.job_thread = threading.Thread(target=runner, daemon=True)
        self.job_thread.start()

    def _finish(self, done, state, payload):
        self.committing = False
        self.set_busy(False)
        done(state, payload)

    def cancel_job(self):
        self.cancel_flag = True
        p = self.export_proc
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass
        self.set_status("Cancelling…", "warn")

    def _set_export_proc(self, p):
        self.export_proc = p

    def on_close(self):
        if self.committing:
            messagebox.showinfo("Please wait", "FrameTrim is finalizing a file. Please wait a few seconds.")
            return
        if self.busy and self.export_proc is not None:
            if not messagebox.askyesno("Quit", "An export is running. Cancel it and quit?"):
                return
            self.cancel_job()
            if self.job_thread:
                self.job_thread.join(timeout=5)
        self.preview.stop()
        self.root.destroy()

    # ---- ffmpeg availability
    def _startup_check(self):
        if not (find_binary("ffmpeg") and find_binary("ffprobe")):
            self.require_tools()

    def require_tools(self):
        if find_binary("ffmpeg") and find_binary("ffprobe"):
            return True
        if os.name == "nt":
            if messagebox.askyesno(
                    "FFmpeg needed",
                    "FFmpeg (ffmpeg.exe + ffprobe.exe) was not found.\n\n"
                    "Download it automatically now (~80 MB) into the FrameTrim folder?\n\n"
                    "Choose No to install it manually (see README)."):
                self._download_ffmpeg()
        else:
            messagebox.showerror("FFmpeg needed", "ffmpeg/ffprobe not found. Install FFmpeg and add it to PATH.")
        return False

    def _download_ffmpeg(self):
        def work():
            download_ffmpeg(app_dir(), lambda f: self.ui(self._set_progress, f, "Downloading FFmpeg"),
                            lambda: self.cancel_flag)
            return True

        def done(state, payload):
            if state == "ok" and find_binary("ffmpeg") and find_binary("ffprobe"):
                self.set_status("FFmpeg installed. You can open a clip now.")
            elif state == "cancel":
                self.set_status("Download cancelled.", "warn")
            else:
                self.set_status("FFmpeg download failed.", "error")
                messagebox.showerror("Download failed",
                                     f"{payload}\n\nDownload a Windows build manually from ffmpeg.org, "
                                     "and copy ffmpeg.exe and ffprobe.exe next to frametrim.py.")

        self._start_job("Downloading FFmpeg…", work, done, determinate=True, cancellable=True)

    def ensure_x264(self):
        if has_encoder("libx264"):
            return True
        messagebox.showerror("libx264 missing",
                             "This FFmpeg build has no libx264 encoder.\n"
                             "Use a 'full' or 'essentials' build (for example from gyan.dev).")
        return False

    # ---- opening files
    def open_file(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(title="Choose video clip", filetypes=[
            ("Video files", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mts *.m2ts *.ts *.wmv *.flv *.mpg *.mpeg"),
            ("All files", "*.*")])
        if path:
            p = Path(path)
            files = self._list_videos(p.parent)
            self.folder_files = files
            self.folder_idx = next((i for i, f in enumerate(files) if same_file(f, p)), -1)
            self.load_path(p)

    def open_folder(self):
        if self.busy:
            return
        folder = filedialog.askdirectory(title="Choose folder with video clips")
        if not folder:
            return
        files = self._list_videos(Path(folder))
        if not files:
            messagebox.showinfo("No videos", "No supported video files found in this folder.")
            return
        self.folder_files, self.folder_idx = files, 0
        self.load_path(files[0])

    @staticmethod
    def _list_videos(folder):
        try:
            return sorted((p for p in folder.iterdir()
                           if p.is_file() and p.suffix.lower() in VIDEO_EXTS
                           and ".frametrim_temp" not in p.name), key=lambda p: p.name.lower())
        except OSError:
            return []

    def _update_folder_label(self):
        if self.folder_files and 0 <= self.folder_idx < len(self.folder_files):
            self.folder_var.set(f"File {self.folder_idx + 1} of {len(self.folder_files)}")
        else:
            self.folder_var.set("")

    def step_file(self, delta):
        if self.busy:
            return
        if not self.folder_files or self.folder_idx < 0:
            messagebox.showinfo("Open a clip first", "Open a clip or a folder first to use Previous/Next.")
            return
        new_idx = self.folder_idx + delta
        if 0 <= new_idx < len(self.folder_files):
            self.folder_idx = new_idx
            self.load_path(self.folder_files[new_idx])
        else:
            self.set_status("No more clips in that direction.", "warn")

    def load_path(self, path):
        if self.busy:
            return
        path = Path(path)
        if not self.require_tools():
            return

        def work():
            return probe(path), path.stat().st_mtime_ns

        self._start_job("Reading clip…", work, lambda s, p: self._on_loaded(path, s, p))

    def _on_loaded(self, path, state, payload):
        if state != "ok":
            messagebox.showerror("Cannot open clip", str(payload))
            self.set_status("Could not open that clip.", "error")
            return
        info, mtime = payload
        self.path, self.mtime = path, mtime
        self.fps, self.total = info["fps"], info["frames"]
        self.duration, self.vfr = info["duration"], info["vfr"]
        self.origin, self.origin_av = info["origin"], info["origin_av"]
        self.start_var.set("0")
        self.end_var.set(str(self.total))
        self.current_var.set("0")
        self.scale.configure(to=max(1, self.total - 1))
        self._set_scale(0)
        self.cache.clear()
        text = (f"{path.name}  |  {info['width']}×{info['height']}  |  {info['codec']}  |  "
                f"{self.fps:.3f} fps  |  {self.total} frames  |  {fmt_time(self.duration)}")
        if self.vfr:
            text += "  |  VARIABLE frame rate: frame numbers are approximate"
        self.info.set(text)
        self.root.title(f"{APP_NAME} - {path.name}")
        self._update_folder_label()
        self._update_keep()
        if self.vfr:
            self.set_status("Variable-frame-rate clip: frame numbers are approximate. "
                            "Prefer 'Save as NEW file' and check the result.", "warn")
        else:
            self.set_status("Clip loaded. ←/→ step frames, I / O set start / end.")
        self.request_preview()

    # ---- navigation
    def current_frame(self):
        try:
            f = int(self.current_var.get().strip())
        except ValueError:
            f = 0
        return max(0, min(max(0, self.total - 1), f))

    def _set_scale(self, f):
        self._scale_lock = True
        try:
            self.scale.set(f)
        finally:
            self._scale_lock = False

    def _on_scale(self, val):
        if self._scale_lock or not self.path or self.busy:
            return
        f = int(float(val) + 0.5)
        if f != self.current_frame():
            self.current_var.set(str(f))
            self.request_preview()

    def go_to(self, frame):
        if not self.path or self.busy:
            return
        frame = max(0, min(self.total - 1, int(frame)))
        self.current_var.set(str(frame))
        self._set_scale(frame)
        self.request_preview()

    def move_frame(self, delta):
        if self.path and not self.busy:
            self.go_to(self.current_frame() + delta)

    def go_frame(self):
        if not self.path:
            return
        try:
            f = int(self.current_var.get().strip())
        except ValueError:
            self.set_status("Current frame must be a whole number.", "error")
            return
        self.go_to(f)

    def goto_start(self):
        try:
            self.go_to(int(self.start_var.get()))
        except ValueError:
            self.set_status("Start frame must be a whole number.", "error")

    def goto_last_kept(self):
        try:
            self.go_to(int(self.end_var.get()) - 1)
        except ValueError:
            self.set_status("End frame must be a whole number.", "error")

    # ---- preview
    def _on_canvas_configure(self, _e):
        if self._cfg_job:
            self.root.after_cancel(self._cfg_job)
        self._cfg_job = self.root.after(150, self._after_resize)

    def _after_resize(self):
        self._cfg_job = None
        if self.path:
            self.request_preview()
        else:
            self._draw_placeholder()

    def request_preview(self):
        if not self.path or self.busy:
            return
        w = max(160, self.canvas.winfo_width())
        h = max(90, self.canvas.winfo_height())
        frame = self.current_frame()
        key = (str(self.path), self.mtime, frame, w, h)
        self.pending_key = key
        if key in self.cache:
            self.cache.move_to_end(key)
            self._show_frame(frame, self.cache[key])
            return
        self.preview.request((str(self.path), frame, self.fps, w, h, key, self.origin))

    def _on_preview(self, args, data, err):
        key = args[5]
        if key != self.pending_key:
            return
        if err is not None:
            self.set_status(f"Preview failed for frame {args[1]}: {err}", "error")
            return
        self.cache[key] = data
        while len(self.cache) > 40:
            self.cache.popitem(last=False)
        self._show_frame(args[1], data)

    def _in_range(self, frame):
        try:
            return int(self.start_var.get()) <= frame < int(self.end_var.get())
        except ValueError:
            return True

    def _show_frame(self, frame, b64):
        try:
            photo = tk.PhotoImage(data=b64)
        except tk.TclError as e:
            self.set_status(f"Cannot display frame: {e}", "error")
            return
        self.photo = photo
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2, image=photo)
        inside = self._in_range(frame)
        label = (f"Frame {frame} / {self.total - 1}   {fmt_time(frame / self.fps)}   "
                 f"{'KEPT' if inside else 'TRIMMED OUT'}")
        self.canvas.create_text(11, ch - 9, text=label, anchor="sw", fill="black", font=("Segoe UI", 11, "bold"))
        self.canvas.create_text(10, ch - 10, text=label, anchor="sw",
                                fill="#7CFC9A" if inside else "#ff8a8a", font=("Segoe UI", 11, "bold"))
        self.time_var.set(fmt_time(frame / self.fps))
        self.set_status(f"Showing frame {frame} (~{frame / self.fps:.4f}s).")

    # ---- range
    def set_start(self):
        if not self.path or self.busy:
            return
        f = self.current_frame()
        try:
            end = int(self.end_var.get())
        except ValueError:
            end = self.total
        if f >= end:
            self.set_status(f"Start ({f}) must be before the end frame ({end}). Set the end first.", "warn")
            return
        self.start_var.set(str(f))
        self.set_status(f"Start set to frame {f}.")
        self.request_preview()

    def set_end(self):
        if not self.path or self.busy:
            return
        f = self.current_frame() + 1
        try:
            start = int(self.start_var.get())
        except ValueError:
            start = 0
        if f <= start:
            self.set_status(f"End ({f}) must be after the start frame ({start}). Move the start first.", "warn")
            return
        self.end_var.set(str(f))
        self.set_status(f"Exclusive end set to frame {f} (last kept frame: {f - 1}).")
        self.request_preview()

    def reset_range(self):
        if not self.path or self.busy:
            return
        self.start_var.set("0")
        self.end_var.set(str(self.total))
        self.set_status("Range reset to the full clip.")
        self.request_preview()

    def get_range(self):
        try:
            s, e = int(self.start_var.get().strip()), int(self.end_var.get().strip())
        except ValueError:
            raise ValueError("Start and end must be whole numbers.")
        if s < 0 or e > self.total or s >= e:
            raise ValueError(f"Invalid range. Need 0 ≤ start < end ≤ {self.total}.")
        return s, e

    # ---- exporting
    def _check_disk(self, folder, src):
        try:
            if shutil.disk_usage(folder).free < src.stat().st_size:
                return messagebox.askyesno(
                    "Low disk space",
                    "Free space on the target drive is smaller than the source file.\n"
                    "The export may fail. Continue anyway?")
        except OSError:
            pass
        return True

    def _export_tmp(self, src, final, start, end, fps, total, vfr, origin, strict):
        """Render to a temp file next to `final`, verify it, return (tmp, out_frames, expected, ok)."""
        ffmpeg = find_binary("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found.")
        tmp = final.with_name(f"{final.stem}.frametrim_temp_{int(time.time())}.mp4")
        try:
            if self.cancel_flag:
                raise Cancelled()
            cmd = build_export_cmd(ffmpeg, src, tmp, start, end, fps, total, origin)
            rc, tail = run_ffmpeg_progress(
                cmd, (end - start) / fps,
                lambda fr: self.ui(self._set_progress, fr, "Exporting"), self._set_export_proc)
            if self.cancel_flag:
                raise Cancelled()
            if rc != 0:
                raise RuntimeError(tail or "FFmpeg failed to export this clip.")
            if not tmp.exists() or tmp.stat().st_size == 0:
                raise RuntimeError("Export produced no file.")
            expected = end - start
            out = probe(tmp)["frames"]
            tol = 2 if not vfr else max(2, int(expected * 0.05))
            ok = abs(out - expected) <= tol
            if strict and not ok:
                raise RuntimeError(f"Verification failed: output has {out} frames, expected {expected}. "
                                   "The original was NOT changed.")
            return tmp, out, expected, ok
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def save_new(self):
        if not self.path or self.busy or not self.require_tools():
            return
        try:
            start, end = self.get_range()
        except ValueError as e:
            messagebox.showerror("Invalid range", str(e))
            return
        if not self.ensure_x264():
            return
        dest = filedialog.asksaveasfilename(
            title="Save trimmed clip as new file", initialdir=str(self.path.parent),
            initialfile=f"{self.path.stem}_trimmed.mp4", defaultextension=".mp4",
            filetypes=[("MP4 video", "*.mp4")])
        if not dest:
            return
        dest = Path(dest)
        if dest.suffix.lower() != ".mp4":
            dest = dest.with_suffix(".mp4")
            if dest.exists() and not messagebox.askyesno("Overwrite?", f"{dest.name} already exists. Overwrite it?"):
                return
        if same_file(dest, self.path):
            messagebox.showerror("Choose another name", "The new file must not be the same as the source clip.\n"
                                 "Use 'Replace ORIGINAL' if you want to replace it.")
            return
        if not self._check_disk(dest.parent, self.path):
            return
        src, fps, total, vfr, origin = self.path, self.fps, self.total, self.vfr, self.origin_av

        def work():
            tmp, out, exp, ok = self._export_tmp(src, dest, start, end, fps, total, vfr, origin, strict=False)
            try:
                self.committing = True
                self.preview.kill_current()
                retry_replace(tmp, dest)
            except BaseException:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            return dest, out, exp, ok

        def done(state, payload):
            if state == "ok":
                d, out, exp, ok = payload
                self.set_status(f"Saved: {d.name}")
                note = "" if ok else (f"\n\nWarning: output has {out} frames but {exp} were expected "
                                      "(variable frame rate?). Please check it.")
                messagebox.showinfo("Done", f"Trimmed video saved:\n{d}\n\n"
                                    f"Output is re-encoded (H.264 CRF {CRF}) for frame-level cuts.{note}")
                self.request_preview()
            elif state == "cancel":
                self.set_status("Export cancelled. No file was written.", "warn")
            else:
                self.set_status("Export failed.", "error")
                messagebox.showerror("Export failed", self._explain(payload))

        self._start_job("Exporting…", work, done, determinate=True, cancellable=True)

    def replace_original(self):
        if not self.path or self.busy or not self.require_tools():
            return
        try:
            start, end = self.get_range()
        except ValueError as e:
            messagebox.showerror("Invalid range", str(e))
            return
        if start == 0 and end >= self.total:
            messagebox.showwarning("No trim selected", "The range is the whole clip. Adjust start/end first.")
            return
        if not self.ensure_x264():
            return
        src = self.path
        final = src if src.suffix.lower() == ".mp4" else src.with_suffix(".mp4")
        if not same_file(final, src) and final.exists():
            messagebox.showerror("Name already used",
                                 f"{final.name} already exists next to this clip.\nUse 'Save as NEW file' instead.")
            return
        extra = ""
        if not same_file(final, src):
            extra = f"\n\nThe result will be saved as {final.name} (MP4); the original stays as a backup."
        if self.vfr:
            extra += "\n\nWarning: variable frame rate - frame numbers are approximate."
        if not messagebox.askyesno(
                "Replace original?",
                "This replaces the clip with the trimmed version.\n\n"
                "The original is kept as a backup (.bak.original). It is only touched after the new file "
                "has been created and verified.\n\n"
                f"The video is re-encoded (H.264 CRF {CRF}): very high quality, but not bit-for-bit identical."
                f"{extra}\n\nContinue?"):
            return
        if not self._check_disk(src.parent, src):
            return
        fps, total, vfr, origin = self.fps, self.total, self.vfr, self.origin_av

        def work():
            tmp, out, exp, _ok = self._export_tmp(src, final, start, end, fps, total, vfr, origin, strict=True)
            try:
                self.committing = True
                self.ui(self.cancel_btn.state, ["disabled"])
                self.ui(self.set_status, "Finalizing…")
                self.preview.kill_current()
                backup = unique_backup(src)
                retry_replace(src, backup)          # atomic rename: original -> backup (no big copy)
                try:
                    retry_replace(tmp, final)       # new file takes the original's place
                except BaseException as e:
                    try:
                        os.replace(backup, src)     # roll back
                    except Exception:
                        raise RuntimeError(f"Replace failed and rollback failed. Your original is at:\n{backup}") from e
                    raise
            except BaseException:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            return final, backup, out, exp

        def done(state, payload):
            if state == "ok":
                new_path, backup, out, exp = payload
                if 0 <= self.folder_idx < len(self.folder_files):
                    self.folder_files[self.folder_idx] = new_path
                self.set_status("Original replaced; backup kept.")
                messagebox.showinfo("Replacement complete",
                                    f"Done. New clip:\n{new_path}\n\nBackup of the original:\n{backup}\n\n"
                                    "Play the new clip before deleting the backup.")
                self.load_path(new_path)
            elif state == "cancel":
                self.set_status("Cancelled. The original was not changed.", "warn")
            else:
                self.set_status("Replacement failed.", "error")
                messagebox.showerror("Replacement failed", self._explain(payload))

        self._start_job("Creating trimmed file…", work, done, determinate=True, cancellable=True)

    @staticmethod
    def _explain(exc):
        if isinstance(exc, PermissionError):
            return ("The file is in use by another program (media player, explorer preview, antivirus?) "
                    "or the folder is read-only.\nClose it and try again. Nothing was lost.")
        return str(exc)


def enable_dpi_awareness():
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def main():
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
    except tk.TclError:
        pass
    app = FrameTrimApp(root)
    if len(sys.argv) > 1 and Path(sys.argv[1]).is_file():
        arg = Path(sys.argv[1]).resolve()
        app.folder_files = app._list_videos(arg.parent)
        app.folder_idx = next((i for i, f in enumerate(app.folder_files) if same_file(f, arg)), -1)
        root.after(400, lambda: app.load_path(arg))
    root.mainloop()


if __name__ == "__main__":
    main()
