import os
import sys
import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NAME = "FrameTrim"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts", ".m2ts", ".ts", ".wmv", ".flv", ".mpg", ".mpeg"}

def find_binary(name):
    here = Path(__file__).resolve().parent
    candidates = [here / (name + ".exe"), here / "ffmpeg" / (name + ".exe")]
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which(name)

def ffprobe_json(path):
    ffprobe = find_binary("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe nahi mila. FFmpeg Windows build ko app folder mein rakho ya PATH mein add karo.")
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,duration,nb_frames",
           "-show_entries", "format=duration,format_name", "-of", "json", str(path)]
    p = subprocess.run(cmd, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "Video read nahi ho saki.")
    return json.loads(p.stdout)

def parse_rate(rate):
    try:
        a, b = rate.split("/")
        return float(a) / float(b) if float(b) else 0.0
    except Exception:
        try: return float(rate)
        except Exception: return 0.0

class FrameTrimApp:
    def __init__(self, root):
        self.root = root
        root.title("FrameTrim — manual frame-accurate clip trimmer")
        root.geometry("900x650")
        root.minsize(780, 560)
        self.path = None
        self.meta = None
        self.fps = 30.0
        self.duration = 0.0
        self.start_frame = 0
        self.end_frame = 0
        self.preview_proc = None
        self.busy = False
        self.folder_files = []
        self.folder_idx = -1
        self.status = tk.StringVar(value="Open a clip to begin.")
        self.info = tk.StringVar(value="No clip loaded")
        self.start_var = tk.StringVar(value="0")
        self.end_var = tk.StringVar(value="0")
        self._build()

    def _build(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        ttk.Button(top, text="Open Clip", command=self.open_file).pack(side="left")
        ttk.Button(top, text="Open Folder", command=self.open_folder).pack(side="left", padx=6)
        ttk.Button(top, text="Previous", command=lambda: self.step_file(-1)).pack(side="left", padx=(18, 3))
        ttk.Button(top, text="Next", command=lambda: self.step_file(1)).pack(side="left")
        ttk.Button(top, text="Preview Current Frame", command=self.preview_frame).pack(side="right")

        ttk.Label(self.root, textvariable=self.info, padding=(12, 2), wraplength=860).pack(fill="x")
        self.canvas = tk.Canvas(self.root, bg="#181818", height=270, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=12, pady=8)
        self.canvas.create_text(450, 130, text="Open a video to see frame previews", fill="#dddddd",
                                font=("Segoe UI", 15), tags="placeholder")

        nav = ttk.LabelFrame(self.root, text="Frame navigation", padding=10)
        nav.pack(fill="x", padx=12, pady=5)
        ttk.Button(nav, text="◀ 1 frame", command=lambda: self.move_frame(-1)).grid(row=0, column=0, padx=4)
        ttk.Button(nav, text="1 frame ▶", command=lambda: self.move_frame(1)).grid(row=0, column=1, padx=4)
        ttk.Button(nav, text="◀ 10", command=lambda: self.move_frame(-10)).grid(row=0, column=2, padx=4)
        ttk.Button(nav, text="10 ▶", command=lambda: self.move_frame(10)).grid(row=0, column=3, padx=4)
        ttk.Label(nav, text="Current frame:").grid(row=0, column=4, padx=(18, 4))
        self.current_var = tk.StringVar(value="0")
        self.current_entry = ttk.Entry(nav, textvariable=self.current_var, width=10)
        self.current_entry.grid(row=0, column=5)
        ttk.Button(nav, text="Go", command=self.go_frame).grid(row=0, column=6, padx=4)
        ttk.Label(nav, text="(Keyboard: ←/→ one frame, Shift+←/→ ten frames)").grid(row=0, column=7, padx=8)
        nav.columnconfigure(7, weight=1)

        cuts = ttk.LabelFrame(self.root, text="Trim range (inclusive start, exclusive end)", padding=10)
        cuts.pack(fill="x", padx=12, pady=5)
        ttk.Label(cuts, text="Start frame:").grid(row=0, column=0, sticky="w")
        ttk.Entry(cuts, textvariable=self.start_var, width=12).grid(row=0, column=1, padx=5)
        ttk.Button(cuts, text="Set start = current", command=self.set_start).grid(row=0, column=2, padx=5)
        ttk.Label(cuts, text="End frame (exclusive):").grid(row=0, column=3, padx=(16, 0))
        ttk.Entry(cuts, textvariable=self.end_var, width=12).grid(row=0, column=4, padx=5)
        ttk.Button(cuts, text="Set end = current + 1", command=self.set_end).grid(row=0, column=5, padx=5)
        ttk.Button(cuts, text="Reset range", command=self.reset_range).grid(row=0, column=6, padx=5)

        bottom = ttk.Frame(self.root, padding=10)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Save as NEW file…", command=self.save_new).pack(side="left")
        ttk.Button(bottom, text="Replace ORIGINAL (backup first)", command=self.replace_original).pack(side="left", padx=8)
        ttk.Label(bottom, textvariable=self.status, foreground="#245a2b", wraplength=480).pack(side="right", fill="x", expand=True)

        self.root.bind("<Left>", lambda e: self.move_frame(-1))
        self.root.bind("<Right>", lambda e: self.move_frame(1))
        self.root.bind("<Shift-Left>", lambda e: self.move_frame(-10))
        self.root.bind("<Shift-Right>", lambda e: self.move_frame(10))
        self.root.bind("<space>", lambda e: self.preview_frame())

    def require_tools(self):
        if not find_binary("ffmpeg") or not find_binary("ffprobe"):
            messagebox.showerror("FFmpeg required",
                "FFmpeg aur ffprobe nahi mile.\n\nSetup:\n1) Download Windows build from https://ffmpeg.org/download.html\n2) ffmpeg.exe aur ffprobe.exe ko FrameTrim ke folder mein rakho.\n3) App dobara open karo.")
            return False
        return True

    def open_file(self):
        path = filedialog.askopenfilename(title="Choose video clip", filetypes=[
            ("Video files", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mts *.m2ts *.ts *.wmv *.flv *.mpg *.mpeg"),
            ("All files", "*.*")])
        if path:
            self.load_path(Path(path))

    def open_folder(self):
        folder = filedialog.askdirectory(title="Choose folder with video clips")
        if not folder: return
        self.folder_files = sorted([p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS])
        if not self.folder_files:
            messagebox.showinfo("No videos", "Is folder mein supported video files nahi milin.")
            return
        self.folder_idx = 0
        self.load_path(self.folder_files[0])

    def step_file(self, delta):
        if not self.folder_files:
            messagebox.showinfo("Open folder first", "Previous/Next ke liye pehle Open Folder use karo.")
            return
        new_idx = self.folder_idx + delta
        if 0 <= new_idx < len(self.folder_files):
            self.folder_idx = new_idx
            self.load_path(self.folder_files[new_idx])

    def load_path(self, path):
        if not self.require_tools(): return
        try:
            data = ffprobe_json(path)
            streams = data.get("streams", [])
            if not streams: raise RuntimeError("Video stream nahi mili.")
            stream = streams[0]
            self.path = Path(path)
            self.meta = data
            self.fps = parse_rate(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "30/1") or 30.0
            self.duration = float(data.get("format", {}).get("duration") or stream.get("duration") or 0)
            frames = int(round(self.duration * self.fps)) if self.duration else int(stream.get("nb_frames") or 0)
            self.start_frame, self.end_frame = 0, max(1, frames)
            self.start_var.set("0")
            self.end_var.set(str(self.end_frame))
            self.current_var.set("0")
            codec = stream.get("codec_name", "unknown")
            size = f'{stream.get("width", "?")}×{stream.get("height", "?")}'
            self.info.set(f"{self.path.name}  |  {size}  |  codec: {codec}  |  {self.fps:.3f} fps  |  duration: {self.duration:.3f}s")
            self.status.set("Clip loaded. Preview a frame with Space.")
            self.preview_frame()
        except Exception as e:
            messagebox.showerror("Cannot open clip", str(e))

    def current_frame(self):
        try: return max(0, int(self.current_var.get()))
        except Exception: return 0

    def move_frame(self, delta):
        if not self.path: return
        max_frame = max(0, int(self.duration * self.fps) - 1)
        self.current_var.set(str(max(0, min(max_frame, self.current_frame() + delta))))
        self.preview_frame()

    def go_frame(self):
        self.preview_frame()

    def preview_frame(self):
        if not self.path: return
        if not self.require_tools(): return
        frame = self.current_frame()
        timestamp = frame / self.fps
        ffmpeg = find_binary("ffmpeg")
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                jpg = tmp.name
            cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                   "-i", str(self.path), "-ss", f"{timestamp:.9f}", "-frames:v", "1", "-q:v", "2", jpg]
            p = subprocess.run(cmd, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if p.returncode != 0 or not os.path.exists(jpg) or os.path.getsize(jpg) == 0:
                raise RuntimeError(p.stderr.strip() or "Frame preview failed.")
            # Tkinter PhotoImage supports GIF/PNG, not JPEG; convert via Pillow if installed.
            try:
                from PIL import Image, ImageTk
                img = Image.open(jpg)
                img.thumbnail((860, 245))
                self.photo = ImageTk.PhotoImage(img)
                self.canvas.delete("all")
                self.canvas.create_image(450, 132, image=self.photo, anchor="center")
            except ImportError:
                png = jpg + ".png"
                p2 = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", jpg, png],
                                   capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if p2.returncode != 0: raise RuntimeError("Install Pillow with: py -m pip install pillow")
                self.photo = tk.PhotoImage(file=png)
                self.canvas.delete("all")
                self.canvas.create_image(450, 132, image=self.photo, anchor="center")
                try: os.unlink(png)
                except: pass
            self.current_var.set(str(frame))
            self.status.set(f"Showing frame {frame} (~{timestamp:.4f}s).")
        except Exception as e:
            messagebox.showerror("Preview error", str(e))
        finally:
            try: os.unlink(jpg)
            except: pass

    def set_start(self):
        frame = self.current_frame()
        self.start_var.set(str(frame))
        self.status.set(f"Start set to frame {frame}.")

    def set_end(self):
        frame = self.current_frame() + 1
        self.end_var.set(str(frame))
        self.status.set(f"Exclusive end set to frame {frame}.")

    def reset_range(self):
        if not self.path: return
        self.start_var.set("0")
        self.end_var.set(str(max(1, int(self.duration * self.fps))))
        self.status.set("Range reset to full clip.")

    def get_range(self):
        try:
            start = int(self.start_var.get())
            end = int(self.end_var.get())
        except ValueError:
            raise ValueError("Start/end frame integers hone chahiye.")
        total = max(1, int(round(self.duration * self.fps)))
        if start < 0 or end > total or start >= end:
            raise ValueError(f"Invalid range. Start 0 ya zyada, end {total} ya kam, aur start < end hona chahiye.")
        return start, end

    def export_clip(self, dest, start, end):
        ffmpeg = find_binary("ffmpeg")
        # Re-encode to honor arbitrary frame boundaries. High-quality H.264 video + AAC audio.
        # This deliberately supports broad input formats, but is not bit-for-bit lossless.
        start_time = start / self.fps
        duration = (end - start) / self.fps
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-i", str(self.path), "-ss", f"{start_time:.9f}", "-t", f"{duration:.9f}",
               "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium",
               "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", str(dest)]
        p = subprocess.run(cmd, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip() or "Export failed. Is this FFmpeg build equipped with libx264?")
        if not Path(dest).exists() or Path(dest).stat().st_size == 0:
            raise RuntimeError("Export file nahi bani.")

    def save_new(self):
        if not self.path: return
        if not self.require_tools(): return
        try: start, end = self.get_range()
        except Exception as e:
            messagebox.showerror("Invalid range", str(e)); return
        dest = filedialog.asksaveasfilename(title="Save trimmed clip as new file",
            initialdir=str(self.path.parent), initialfile=f"{self.path.stem}_trimmed.mp4",
            defaultextension=".mp4", filetypes=[("MP4 video", "*.mp4")])
        if not dest: return
        try:
            self.status.set("Exporting… please wait.")
            self.root.update_idletasks()
            self.export_clip(dest, start, end)
            self.status.set("Export complete.")
            messagebox.showinfo("Done", f"Trimmed video saved:\n{dest}\n\nNote: output is re-encoded for frame-level cuts.")
        except Exception as e:
            self.status.set("Export failed.")
            messagebox.showerror("Export failed", str(e))

    def replace_original(self):
        if not self.path: return
        if not self.require_tools(): return
        try: start, end = self.get_range()
        except Exception as e:
            messagebox.showerror("Invalid range", str(e)); return
        if start == 0 and end >= max(1, int(self.duration * self.fps)):
            messagebox.showwarning("No trim selected", "Range poori clip hai. Start/end ko adjust karo.")
            return
        if self.path.suffix.lower() != ".mp4":
            messagebox.showwarning("MP4 originals only",
                "Safe replacement mode abhi sirf MP4 originals ke liye hai, kyun ke output MP4/H.264 mein encode hoti hai.\n\nIs clip ke liye Save as NEW file use karo.")
            return
        ok = messagebox.askyesno("Replace original?",
            "Yeh action original clip ko trimmed version se replace karega.\n\n"
            "Safety backup .bak.original naam se banegi. Export successful hone ke baad hi original replace hoga.\n\n"
            "Video re-encode hogi; quality bohat high rakhi gayi hai, lekin bit-for-bit identical nahi hogi.\n\nContinue?")
        if not ok: return
        source = self.path
        suffix = source.suffix.lower()
        temp = source.with_name(source.stem + ".frametrim_temp.mp4")
        backup = source.with_name(source.name + ".bak.original")
        if temp.exists() or backup.exists():
            messagebox.showerror("Safety stop", f"Temporary ya backup file pehle se mojood hai:\n{temp if temp.exists() else backup}\nRename/delete it manually after checking.")
            return
        try:
            self.status.set("Creating temporary trimmed file…")
            self.root.update_idletasks()
            self.export_clip(temp, start, end)
            # Verify that FFprobe can read the output and it has a video stream.
            check = ffprobe_json(temp)
            if not check.get("streams") or float(check.get("format", {}).get("duration") or 0) <= 0:
                raise RuntimeError("Temporary output verification failed; original was not changed.")
            shutil.copy2(source, backup)
            os.replace(temp, source)
            self.load_path(source)
            self.status.set("Original replaced; backup retained.")
            messagebox.showinfo("Replacement complete",
                f"Original replaced successfully.\n\nBackup retained at:\n{backup}\n\nPlease play the new clip before deleting the backup.")
        except Exception as e:
            messagebox.showerror("Replacement failed", f"{e}\n\nOriginal should remain untouched unless replacement completed. Check temporary/backup files before trying again.")
            self.status.set("Replacement failed; inspect files.")
        finally:
            try:
                if temp.exists(): temp.unlink()
            except Exception: pass

if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    app = FrameTrimApp(root)
    root.mainloop()
