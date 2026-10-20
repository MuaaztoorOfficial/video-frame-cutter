# FrameTrim (Windows prototype)

FrameTrim is a local, manual video trimmer prototype for checking clips frame-by-frame and optionally replacing the original file after creating and verifying a temporary output.

## Requirements
- Windows 10/11
- Python 3.10+ (for running from source)
- FFmpeg Windows build including `ffmpeg.exe`, `ffprobe.exe`, and the `libx264` encoder
- Pillow recommended for JPEG preview: `py -m pip install pillow`

## Setup
1. Download a Windows FFmpeg build from https://ffmpeg.org/download.html (the site links to Windows builds).
2. Extract it.
3. Copy `ffmpeg.exe` and `ffprobe.exe` into the same folder as `frametrim.py`, OR add their `bin` directory to Windows PATH.
4. Install Pillow: `py -m pip install pillow`
5. Run: `py frametrim.py`

## Use
1. Click **Open Clip** or **Open Folder**.
2. Use Left/Right arrows for frame stepping. Shift+Left/Shift+Right moves 10 frames. Space refreshes preview.
3. Set the start and end frames. End is exclusive: for example, start 10 and end 100 keeps frames 10–99.
4. Use **Save as NEW file** first to test.
5. **Replace ORIGINAL** creates a `.bak.original` backup, renders a temporary MP4, verifies it, then replaces the source. It stops if a previous temp/backup exists.

## Important limitations
- This is a prototype, not a signed Windows EXE.
- To make arbitrary frame boundaries practical across mixed codecs, output is re-encoded to H.264/AAC MP4 at CRF 16. This is high quality but NOT bit-for-bit lossless and may change codec/container.
- Frame stepping uses the average frame rate and timestamp seeking, so variable-frame-rate media may not map perfectly to source frame numbers. For critical work, validate the result visually and keep the backup.
- The original file's extension is retained when replaced even though output is MP4; for non-MP4 originals, this can cause a container/extension mismatch. For safety, this prototype should only be used for MP4 originals in Replace Original mode. For other formats use Save as NEW file, or convert/copy originals to MP4 first.
- This prototype's preview is basic and does not include audio waveform or real-time playback.
