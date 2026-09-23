# SimplyConvert — PC companion of SimplyPlay

[![CI](https://github.com/norbs/SimplyCompress/actions/workflows/ci.yml/badge.svg)](https://github.com/norbs/SimplyCompress/actions/workflows/ci.yml)

Applies the same compression (Ogg/Opus with ReplayGain) and online
identification (AcoustID/MusicBrainz) engines as the Android app to a PC
music folder. **Originals are never touched**; results land in a separate
output tree.

## Install

| OS | Steps |
|---|---|
| **Linux** | `sudo apt install ffmpeg python3-numpy python3-mutagen libchromaprint-tools` |
| **macOS** | `brew install ffmpeg chromaprint` then `pip3 install numpy mutagen` |
| **Windows** | Install [Python](https://python.org) (check "Add to PATH"), then `choco install ffmpeg chromaprint` (or put `fpcalc.exe` next to the script), then `pip install numpy mutagen` |

## GUI (recommended)

```bash
python3 simplyconvert_gui.py      # Linux / macOS
python simplyconvert_gui.py       # Windows
```

or launch `run_gui.sh` (Linux/macOS) / `run_gui.bat` (Windows).

One window: source folder, output folder, mode, bitrate, auto-volume,
dry-run — and an **APPLIQUER** button with a live log.

## Command line

```bash
python3 simplyconvert.py compressidentify ~/Musique -o ~/Musique_Ogg
```

- `compress` — convert to Ogg/Opus (lossless always; lossy only if smaller)
- `identify` — fingerprint + tag artist/title/album/date/cover on the **copies**
- `compressidentify` — both in one pass
