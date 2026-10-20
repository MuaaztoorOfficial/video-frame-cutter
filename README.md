# FrameTrim v2 (Windows)

Local, manual, frame-accurate video trimmer. Step frame by frame, set start/end, then **Save as NEW file** or **Replace ORIGINAL** (backup is kept).

## Requirements
- Windows 10/11, Python 3.10+ (no pip packages needed)
- FFmpeg with `libx264`. If it is missing, the app offers to download it automatically into its own folder. Manual option: copy `ffmpeg.exe` + `ffprobe.exe` next to `frametrim.py` (or into `.\ffmpeg`, `.\bin`, `.\ffmpeg-*\bin`), or add them to PATH.

## Run
Double-click `run_frametrim.bat` (or `py frametrim.py`). You can also drag a video onto the .bat.

## Use
1. **Open Clip** / **Open Folder** (Previous/Next switch clips).
2. Navigate: ←/→ = 1 frame, Shift+←/→ = 10, Home/End, slider, or type a frame number + Enter.
3. **I** (or "Set start") and **O** (or "Set end") mark the range. End is exclusive: start 10, end 100 keeps frames 10-99. The preview shows KEPT / TRIMMED OUT.
4. **Save as NEW file** (safe) or **Replace ORIGINAL**. Cancel is available during export.

## What Replace ORIGINAL does
Exports to a temp file, verifies the frame count, renames the original to `.bak.original` (unique name, never overwritten), then moves the new file in. If any step fails the original is restored. Non-MP4 originals (mkv, mov, ts...) become `name.mp4`; the original stays as the backup.

## Notes
- Output is re-encoded (H.264 CRF 16, AAC 192k, MP4): very high quality, not bit-for-bit lossless. 10-bit/HDR sources are converted to 8-bit SDR yuv420p.
- Variable-frame-rate clips (phone videos) are flagged; frame numbers are approximate there, so check the result.
- Errors are logged to `frametrim_error.log` next to the app.
