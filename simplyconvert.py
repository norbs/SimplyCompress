#!/usr/bin/env python3
"""SimplyConvert — PC companion of the SimplyPlay Android app.

Applies the SAME two engines as the app to a PC music folder:

  compress   Re-encode the library to Ogg/Opus (libopus, 160/180/256 kbps),
             with the same rules as "Compress my music":
               - lossless sources always convert
               - lossy sources only convert if the result is smaller
                 (pre-filter: a lossy source already at/below target+slack
                 is skipped — unless auto-volume is on, mirroring the app);
                 when the Opus result is BIGGER than the original, the
                 original is copied as-is to the output tree instead
               - the ORIGINALS ARE NEVER TOUCHED and never re-tagged;
                 results land in a separate output tree that mirrors the
                 source layout, named "Title.ogg" (existing destination ->
                 skipped)
               - ReplayGain analysis (same math as the app) is written as
                 REPLAYGAIN_TRACK_GAIN/PEAK comments on the copy
  identify   Fingerprint each track (chromaprint, like the app) and fill
             artist/title/album/date/cover from AcoustID + MusicBrainz +
             Cover Art Archive. Tags are written ONLY on the output-tree
             copies (converted files and copied originals) — source files
             are read-only for this tool, ever.

  compressidentify  compress + identify in one pass (tagging + compression).

Requires: ffmpeg/ffprobe, python3-numpy, python3-mutagen, libchromaprint.
"""

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import numpy as np
from mutagen import File as MutagenFile, MutagenError
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4Cover
from mutagen.oggopus import OggOpus

# ----------------------------------------------------------------------------
# Constants shared with the Kotlin app (ReplayGain.kt / CompressionFlow.kt /
# MusicScanner.kt / TagFetcher.kt) — keep in sync when the app evolves.
# ----------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {
    "mp3", "mp2",                    # MPEG audio layers 2 & 3
    "ogg", "flac", "m4a", "aac", "alac",
    "oga", "ogx", "opus",
    "wav", "riff", "bwf",
    "ac3", "eac3",
    "mp4", "m4b", "m4p", "m4r",
}
LOSSLESS_EXTENSIONS = {"flac", "wav", "riff", "bwf", "alac"}

# Fichiers non-audio recopiés tels quels (même nom, même arbre relatif,
# écrasement si la destination existe) — voir cmd_compress.
COPY_AS_IS_EXTENSIONS = {
    "jpg", "jpeg", "txt", "doc", "docx",
    "odt", "xls", "xlsx", "ods",
}

RG_REFERENCE_RMS = 6537.0        # s16 RMS of the 89 dB reference (app value)
MAX_GAIN = 3.981                 # +12 dB amplification ceiling
MIN_GAIN = 0.178                 # -15 dB attenuation floor
CLIP_GUARD = 0.98                # peak headroom after amplification

DEFAULT_BITRATE_KBPS = 160

ACOUSTID_CLIENT = "cSpUJKpD"     # same client key as the app
ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"
DURATION_SLACK_SEC = 5           # duration-consistent candidates win
FINGERPRINT_SECONDS = 120        # fpcalc default analysis length
MB_UA = "SimplyConvert/1.0 (https://github.com/norbs/SimplyPlay)"
MB_RATE_LIMIT_SEC = 1.1

MANIFEST_NAME = ".simplyplay_manifest.json"
RG_CACHE_NAME = ".simplyconvert_rg_cache.json"

# Cover-art scratch files land in the OS temp dir (not a hardcoded /tmp —
# that path does not exist on Windows).
_TMP_DIR = tempfile.gettempdir()


def log(msg: str) -> None:
    # Windows consoles default to cp1252, which cannot encode glyphs like '→'
    # (UnicodeEncodeError would abort the whole run). Replace un-encodable
    # characters instead of crashing.
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode(sys.stdout.encoding or "ascii", "replace").decode(
            sys.stdout.encoding or "ascii"), flush=True)


# ----------------------------------------------------------------------------
# Library walk
# ----------------------------------------------------------------------------

def walk_library(src_root: str, out_root: str):
    """Yield (abs_path, rel_dir, stem, ext) for every supported file."""
    out_real = os.path.realpath(out_root) if out_root else ""
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".")
            and (not out_real or os.path.realpath(os.path.join(dirpath, d)) != out_real)
        )
        rel_dir = os.path.relpath(dirpath, src_root)
        if rel_dir == ".":
            rel_dir = ""
        for fn in sorted(filenames):
            stem, ext = os.path.splitext(fn)
            if ext[1:].lower() in SUPPORTED_EXTENSIONS:
                yield os.path.join(dirpath, fn), rel_dir, stem, ext[1:].lower()


def _subprocess_flags() -> int:
    """Windows: never let helper processes open console windows (the GUI runs
    under pythonw.exe — a console flash per ffmpeg call would be ugly)."""
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def ffprobe_json(path: str) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=_subprocess_flags())
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def is_lossless(path: str, ext: str, probe: dict) -> bool:
    if ext in LOSSLESS_EXTENSIONS:
        return True
    for st in probe.get("streams", []):
        codec = (st.get("codec_name") or "").lower()
        if st.get("codec_type") == "audio":
            return codec in {"alac", "flac", "pcm_s16le", "pcm_s24le",
                             "pcm_s32le", "pcm_f32le", "pcm_u8"}
    return False


def duration_seconds(probe: dict) -> int:
    try:
        return int(float(probe.get("format", {}).get("duration", 0)))
    except (TypeError, ValueError):
        return 0


def destination_exists(out_dir: str, stem: str) -> bool:
    """True when "Title.ogg" (previous conversion or copy of the original)
    already occupies the output name — the file must be skipped, never
    suffixed into a Title-2.ogg duplicate."""
    return os.path.exists(os.path.join(out_dir, f"{stem}.ogg"))


# ----------------------------------------------------------------------------
# ReplayGain — same math as ReplayGain.kt
# ----------------------------------------------------------------------------

class RgCache:
    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, json.JSONDecodeError):
            self.data = {}
        self.dirty = False

    def key(self, src: str) -> str:
        st = os.stat(src)
        return f"{os.path.realpath(src)}::{st.st_size}::{int(st.st_mtime)}"

    def get(self, src: str):
        return self.data.get(self.key(src))

    def put(self, src: str, gain: float, peak: float):
        self.data[self.key(src)] = {"gain": round(gain, 6), "peak": round(peak, 6)}
        self.dirty = True

    def save(self):
        if self.dirty:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
            self.dirty = False


def measure_replaygain(src: str) -> tuple:
    """Whole-track (RMS, peak) in s16 units, native rate/channels.

    Mirrors the app: mean of the per-channel mean-square energies, peak is
    the global maximum absolute sample.
    """
    cmd = ["ffmpeg", "-v", "error", "-i", src, "-map", "0:a:0",
           "-c:a", "pcm_s16le", "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=_subprocess_flags())
    sq_sum = 0.0
    sample_count = 0
    peak = 0
    channels = None
    try:
        while True:
            chunk = proc.stdout.read(1 << 20)
            if not chunk:
                break
            arr = np.frombuffer(chunk, dtype=np.int16)
            usable = (len(arr) // 2) * 2
            if usable == 0:
                continue
            arr = arr[:usable].astype(np.float64)
            peak = max(peak, int(np.max(np.abs(arr[:usable].astype(np.int32)))))
            if channels is None:
                channels = 2  # refined below via ffprobe if needed
            sq_sum += float(np.sum(arr * arr))
            sample_count += usable
    finally:
        proc.stdout.close()
        proc.wait()
    if proc.returncode != 0 or sample_count == 0 or channels is None:
        err = proc.stderr.read().decode(errors="replace")[:200]
        raise RuntimeError(f"decode failed: {err}")
    # Interleaved stereo assumed for the mean-of-energies; multi-channel
    # files average the same way in the app (per-channel energies, meaned).
    mean_sq_per_sample = sq_sum / sample_count
    rms = (mean_sq_per_sample * channels) ** 0.5  # per-channel energy mean
    return rms, peak


def compute_gain(rms: float, peak: float) -> float:
    """ReplayGain.kt computeGain: loudness target, anti-clip, clamps."""
    if rms <= 0:
        return 1.0
    by_loudness = RG_REFERENCE_RMS / rms
    by_clip = (32767.0 * CLIP_GUARD) / peak if peak > 0 else float("inf")
    return max(MIN_GAIN, min(MAX_GAIN, min(by_loudness, by_clip)))


# ----------------------------------------------------------------------------
# Conversion
# ----------------------------------------------------------------------------

def convert_to_ogg(src: str, dst_tmp: str, kbps: int):
    """Decode -> libopus Ogg. Tags/cover are copied afterwards by mutagen."""
    # -map_metadata -1: without it ffmpeg copies EVERY source comment into the
    # new file, and mutagen then adds the contract's canonical spelling next to
    # it — that is where `publisher`+`label`, `album artist`+`albumartist`,
    # `description`+`comment` and a dozen MP3-only frames (TLEN, CDBB DISCID,
    # PERFORMER…) came from. The output must carry exactly what the tag writer
    # decides (TAG_CONTRACT §8.2).
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", src, "-map", "0:a:0",
           "-map_metadata", "-1", "-vn", "-c:a", "libopus", "-b:a", f"{kbps}k",
           "-f", "ogg", dst_tmp]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       creationflags=_subprocess_flags())
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-300:]}")


def dedupe(values):
    seen, out = set(), []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _tag_values(val) -> list:
    """Normalize one source value into the list written to the Opus copy.

    TAG_CONTRACT §9: trim only — a value stays a value. No ';' splitting, no
    dedupe, no reformatting: Android copies fields verbatim, and any transform
    here is a guaranteed divergence between the two pipelines.
    """
    if val is None:
        return []
    if isinstance(val, (list, tuple)):
        items = list(val)
    else:
        # mutagen ID3 frames carry their text in `.text`, not in `str(frame)`.
        text = getattr(val, "text", None)
        items = list(text) if isinstance(text, (list, tuple)) else [val]
    out = []
    for item in items:
        s = str(item).strip()
        if s:
            out.append(s)
    return out


def _source_comments(src: str) -> dict:
    """Source tags as stored, lowercased keys (TAG_CONTRACT §3.4).

    ffprobe renames ALBUMARTIST/TRACKNUMBER/DISCNUMBER to its own spelling and
    collapses two spellings of one field into whichever it read last — a value
    is silently lost. mutagen reads the comment block as stored (every spelling
    survives), which is what the alias table and the verbatim copy need. Keys
    come back lowercased, the spelling the contract writes (§8.1).
    """
    try:
        mf = MutagenFile(src)
    except Exception:
        return {}
    tags = getattr(mf, "tags", None) if mf is not None else None
    if tags is None or not hasattr(tags, "keys"):
        return {}
    out = {}
    for k in tags.keys():
        try:
            values = _tag_values(tags[k])
        except Exception:
            continue
        if values:
            out[str(k).strip().lower()] = values
    return out


def _source_picture(src: str):
    """First embedded artwork of *src*, as raw bytes (TAG_CONTRACT §6).

    Straight copy of the bytes already sitting in the file: no decode, no
    re-encode, no temp file.
    """
    try:
        mf = MutagenFile(src)
    except Exception:
        return None
    if mf is None:
        return None
    pictures = getattr(mf, "pictures", None) or []
    if pictures:
        data = getattr(pictures[0], "data", None)
        if data:
            return bytes(data)
    tags = getattr(mf, "tags", None)
    if tags is None:
        return None
    # FLAC / Ogg: METADATA_BLOCK_PICTURE comment
    try:
        raw = tags.get("metadata_block_picture") if hasattr(tags, "get") else None
        if raw:
            pic = Picture(base64.b64decode(raw[0]))
            if pic.data:
                return bytes(pic.data)
    except Exception:
        pass
    # ID3: APIC
    try:
        artwork = tags.getall("APIC") if hasattr(tags, "getall") else []
        if artwork and artwork[0].data:
            return bytes(artwork[0].data)
    except Exception:
        pass
    return None


def _picture_via_ffmpeg(src: str, src_probe: dict):
    """Last resort for an attached picture mutagen cannot see (§6).

    Kept as a fallback only: `ffmpeg -frames:v 1` re-encodes the image, so it
    must never run on a source whose artwork mutagen can read.
    """
    for st in src_probe.get("streams", []):
        if st.get("codec_type") == "video" and st.get("disposition", {}).get("attached_pic"):
            pic_file = os.path.join(_TMP_DIR, f"st_cover_{os.getpid()}.jpg")
            r = subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", src,
                 "-map", f"0:{st['index']}", "-frames:v", "1", pic_file],
                capture_output=True, creationflags=_subprocess_flags())
            if r.returncode == 0 and os.path.getsize(pic_file) > 0:
                with open(pic_file, "rb") as fh:
                    data = fh.read()
                try:
                    os.remove(pic_file)
                except OSError:
                    pass
                return data
            try:
                os.remove(pic_file)
            except OSError:
                pass
            return None
    return None


# Source spelling (lowercase) → contract canonical key (TAG_CONTRACT §3.2,
# plus the extra ID3 frames Android's FieldKey list maps onto the same
# fields — see §11). Every write path goes through this table, so two
# spellings can never produce two keys (§8.2).
_ALIASES = {
    # Vorbis / generic spellings (§3.2)
    "album_artist": "albumartist",
    "album artist": "albumartist",
    "track": "tracknumber",
    "disc": "discnumber",
    "publisher": "label",           # §12 décision 1 : `label`, jamais les deux
    "year": "date",
    "originaldate": "date",
    "description": "comment",
    "catalog number": "catalognumber",
    # ID3 frame ids (§3.2)
    "trck": "tracknumber",
    "tpos": "discnumber",
    "tpub": "label",
    "tyer": "date",
    "tdrc": "date",
    "tcon": "genre",
    "tcom": "composer",
    "comm": "comment",
    "tbpm": "bpm",
    "tcop": "copyright",
    "unsy": "lyrics",
    "talb": "album",
    "tpe1": "artist",
    "tit2": "title",
    # Extra ID3 frames jaudiotagger routes through the same FieldKeys on
    # Android (§11) — without these the value would be dropped on Linux
    # only, e.g. TSRC → isrc (measured on `11. Chicago`, §12).
    "tpe2": "albumartist",
    "tsrc": "isrc",
    "uslt": "lyrics",
    "totaltracks": "tracktotal",
    "totaldiscs": "disctotal",
}

# Exactly what OpusTags.STANDARD_KEYS writes for an ID3-style source: an
# MP3 copy must carry the same (closed) key set as the Android one — no
# TIT1/TIT3/TPE3/MCDI frame may leak through (§4).
_ID3_WRITABLE = {
    "title", "artist", "album", "albumartist", "date", "genre", "tracknumber",
    "tracktotal", "discnumber", "disctotal", "composer", "comment", "bpm",
    "isrc", "copyright", "lyrics", "label", "catalognumber", "barcode",
    "encoder",
}


def _id3_key(key: str) -> str:
    """mutagen ID3 frame id → canonical key: `TXXX:BARCODE` → `barcode`,
    `COMM::eng` → `comment`, `TSRC` → `isrc` (then the §3.2 alias table)."""
    k = key.strip().lower()
    if k.startswith("txxx:"):
        k = k[5:].strip()       # the TXXX description carries the meaning
    else:
        k = k.split(":", 1)[0]  # comm::eng → comm, uslt:... → uslt, apic:cover → apic
    return _ALIASES.get(k, k)


def copy_tags_with_cover(src: str, opus_path: str, rg: tuple):
    """Copy source tags + cover art onto the Opus copy (mutagen), then add
    the ReplayGain comments exactly like OpusTags.writeReplayGainComments."""
    audio = OggOpus(opus_path)
    src_probe = ffprobe_json(src)
    # §3.4: mutagen is the tag reader (every spelling of a field survives);
    # ffprobe stays the technical probe (streams, attached picture) and is
    # used as the tag view only for containers mutagen names in fourcc form
    # (M4A '\xa9nam'…), where its generic lowercase names are the practical
    # spelling. Merging the two views would double every field — ffprobe
    # renames `TSRC`→… while mutagen keeps `tsrc`, and both would alias to
    # `isrc` (§8.2, §9 no-dedupe makes such a copy visible as a duplicate).
    source_ext = os.path.splitext(src)[1].lower()
    is_vorbis_source = source_ext in {".flac", ".ogg", ".oga", ".opus"}
    is_id3_source = source_ext in {".mp3", ".mp2"}
    if is_vorbis_source or is_id3_source:
        view = _source_comments(src)
    else:
        view = {str(k).lower(): _tag_values(v)
                for k, v in (src_probe.get("format", {}).get("tags", {}) or {}).items()}

    # One pass, one canonical key per field: §8.2 (no second spelling of a
    # key already written), §3.3 (values of two spellings of one family are
    # appended — one key, N values, nothing overwritten), §9 (trim only,
    # no ';' split, no dedupe).
    entries: dict = {}
    for key, value in view.items():
        k = str(key).strip().lower()
        if not k or k in {"metadata_block_picture", "coverart", "coverartmime"}:
            continue
        if is_vorbis_source:
            if k == "vendor":  # §4: header string of the comment block, never a comment
                continue
            canon = _ALIASES.get(k, k)
        else:
            canon = _id3_key(k) if is_id3_source else _ALIASES.get(k, k)
            if canon not in _ID3_WRITABLE:
                continue
        vals = _tag_values(value)
        if not vals:
            continue
        if is_id3_source:
            # ID3-style containers are read by jaudiotagger on Android, which
            # resolves ONE value per FieldKey: the first frame of a family
            # wins (two COMM descriptions must not double `comment`), and the
            # ID3 "N/M" number form is split into number + total
            # (FieldKey.TRACK/TRACK_TOTAL) — the app writes `tracknumber=6`
            # + `tracktotal=9`, never `6/9`. Mirror that here so both sides
            # write the same keys with the same values (§5, §8.2).
            if canon in entries:
                continue
            if canon in ("tracknumber", "discnumber"):
                num, _, total = vals[0].partition("/")
                num, total = num.strip(), total.strip()
                if num:
                    entries[canon] = [num]
                if total:
                    entries["tracktotal" if canon == "tracknumber" else "disctotal"] = [total]
                continue
            entries[canon] = vals
        else:
            entries.setdefault(canon, []).extend(vals)

    for canon, vals in entries.items():
        audio[canon] = vals

    # Cover art (§6): copy the source image BYTES. The old path ran ffmpeg
    # `-frames:v 1`, which re-encodes the picture (250,013 o → 42,007 o on the
    # reference file) and discards quality for nothing. mutagen reads the
    # artwork straight out of the file — no decode, no temp file; ffmpeg stays
    # only as a fallback for pictures mutagen cannot see.
    data = _source_picture(src) or _picture_via_ffmpeg(src, src_probe)
    if data:
        pic = Picture()
        pic.type = 3  # front cover
        pic.mime = ("image/png" if data.startswith(b"\x89PNG\r\n\x1a\n")
                    else "image/jpeg")
        pic.desc = ""   # same picture block as Android's (§6, décision 2)
        pic.data = data
        audio["metadata_block_picture"] = base64.b64encode(pic.write()).decode("ascii")

    if rg:
        gain, peak = rg
        db = 20.0 * (np.log10(gain) if gain > 0 else 0.0)
        audio["replaygain_track_gain"] = [f"{db:+.2f} dB"]
        audio["replaygain_track_peak"] = [f"{peak / 32767.0:.6f}"]

    audio.save()


# ----------------------------------------------------------------------------
# compress command
# ----------------------------------------------------------------------------


def _walk_meta_files(root: str, out_root: str):
    """Yield (abs_path, rel_dir, stem, ext) for every file whose extension is
    in COPY_AS_IS_EXTENSIONS. Used by cmd_compress to recopy jpg/png/txt/doc/
    docx/odt/xls/xlsx/ods as-is (no conversion, no retagging, overwrite on clash).
    Mirrors walk_library's exclude rules (skip hidden dirs, and skip the output
    tree when it overlaps the source tree).
    """
    out_real = os.path.realpath(out_root) if out_root else ""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".")
            and (not out_real or os.path.realpath(os.path.join(dirpath, d)) != out_real)
        )
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        for fn in sorted(filenames):
            stem, ext = os.path.splitext(fn)
            if ext[1:].lower() in COPY_AS_IS_EXTENSIONS:
                yield os.path.join(dirpath, fn), rel_dir, stem, ext[1:].lower()


def cmd_compress(args) -> dict:
    src_root = os.path.abspath(args.source)
    out_root = os.path.abspath(args.output)
    os.makedirs(out_root, exist_ok=True)
    rg_cache = RgCache(os.path.join(out_root, RG_CACHE_NAME))
    manifest_path = os.path.join(out_root, MANIFEST_NAME)
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError):
        manifest = {}

    t_start = time.monotonic()
    files = list(walk_library(src_root, out_root))
    log(f"{len(files)} fichier(s) audio à traiter (auto-volume={'on' if args.auto_volume else 'off'}, "
        f"{args.bitrate} kbps, "
        f"{'vitesse maximale' if getattr(args, 'fast', False) else 'priorité basse'})")

    stats = {"converted": 0, "kept": 0, "copied": 0, "failed": 0,
             "dest_exists": 0}
    if not getattr(args, "fast", False):
        try:
            os.nice(10)  # low priority, like the app's MIN_PRIORITY engine
        except (AttributeError, OSError):
            pass

    for i, (src, rel_dir, stem, ext) in enumerate(files, 1):
        rel_src = os.path.relpath(src, src_root)
        out_dir = os.path.join(out_root, rel_dir)
        os.makedirs(out_dir, exist_ok=True)

        # Idempotency: an existing, non-stale output wins.
        existing = manifest.get(rel_src)
        if existing and os.path.exists(os.path.join(out_root, existing)) \
                and os.path.getmtime(os.path.join(out_root, existing)) >= os.path.getmtime(src):
            stats["kept"] += 1
            continue

        # Per-track wall time: everything for this file counts (probe,
        # ReplayGain analysis, encode, tag copy).
        t0 = time.monotonic()
        probe = ffprobe_json(src)
        if not probe:
            log(f"  [{i}/{len(files)}] illisible, ignoré : {rel_src}")
            stats["failed"] += 1
            continue
        lossless = is_lossless(src, ext, probe)

        # No bitrate pre-filter, like the app: encode, then let the size gate
        # below decide. Guessing from a probed bitrate trusts a duration tag
        # that is often wrong (this sample has an MP3 of 278 s announcing
        # 1500 s, which probes as 32 kbps) and cannot know where the encode
        # lands. The manifest and the destination guard keep repeat runs
        # from re-encoding what was already decided.

        # Destination guard: "Title.ogg" (previous conversion) or a previous
        # copy of the original under its own extension already there? Skip —
        # never write a Title-2.ogg / Title-2.mp3 duplicate.
        if destination_exists(out_dir, stem) or os.path.exists(
                os.path.join(out_dir, f"{stem}.{ext}")):
            log(f"  [{i}/{len(files)}] destination déjà présente, sauté : {rel_src}")
            stats["dest_exists"] += 1
            continue

        # ReplayGain analysis (cached) — feeds the tags written on the copy.
        rg = None
        if args.auto_volume:
            cached = rg_cache.get(src)
            if cached is None:
                try:
                    rms, peak = measure_replaygain(src)
                    rg_cache.put(src, compute_gain(rms, peak), peak)
                    rg_cache.save()
                    cached = rg_cache.get(src)
                except RuntimeError as e:
                    log(f"  [{i}/{len(files)}] RG indisponible ({e}) : {rel_src}")
            if cached:
                rg = (cached["gain"], cached["peak"])

        log(f"  [{i}/{len(files)}] conversion : {rel_src}")
        tmp = os.path.join(out_dir, f"{stem}.ogg") + ".part"
        try:
            convert_to_ogg(src, tmp, args.bitrate)
            copy_tags_with_cover(src, tmp, rg)
        except RuntimeError as e:
            log(f"      échec : {e}")
            stats["failed"] += 1
            if os.path.exists(tmp):
                os.remove(tmp)
            continue

        # Size gate: lossy copies must be smaller, like the app — and when
        # the result would be bigger, the ORIGINAL is copied as-is to the
        # output tree so it still lands there and can be identified/tagged.
        new_size = os.path.getsize(tmp)
        if not lossless and new_size >= os.path.getsize(src):
            os.remove(tmp)
            copy_dst = os.path.join(out_dir, f"{stem}.{ext}")
            shutil.copy2(src, copy_dst)
            manifest[rel_src] = os.path.relpath(copy_dst, out_root)
            stats["copied"] += 1
            log(f"      résultat plus gros que l'original — original copié "
                f"({os.path.getsize(src) // 1024} ko)")
            continue

        final = tmp[:-len(".part")]
        os.replace(tmp, final)
        manifest[rel_src] = os.path.relpath(final, out_root)
        saved = os.path.getsize(src) - new_size
        stats["converted"] += 1
        log(f"      → {os.path.relpath(final, out_root)} "
            f"({os.path.getsize(src) // 1024} → {new_size // 1024} ko, "
            f"{'-' if saved >= 0 else '+'}{abs(saved) // 1024} ko, "
            f"{time.monotonic() - t0:.1f} s)")

    # Recopie exacte des fichiers annexes (jpg/jpeg/png/txt/doc/docx/odt/xls/
    # xlsx/ods) dans la sortie, même nom, écrasement si présent.
    meta_list: list[str] = []
    for (src_meta, rel_dir, stem, ext) in _walk_meta_files(src_root, out_root):
        out_dir = os.path.join(out_root, rel_dir)
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, f"{stem}.{ext}")
        shutil.copy2(src_meta, dst)
        meta_list.append(os.path.relpath(dst, out_root))

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)

    total_src = sum(os.path.getsize(os.path.join(src_root, r)) for r in manifest
                    if os.path.exists(os.path.join(src_root, r)))
    total_out = sum(os.path.getsize(os.path.join(out_root, p)) for p in manifest.values()
                    if os.path.exists(os.path.join(out_root, p)))
    elapsed = time.monotonic() - t_start
    log(f"\nRésumé : {stats['converted']} convertis, {stats['copied']} originaux copiés "
        f"(résultat plus gros), {stats['kept']} déjà à jour, "
        f"{stats['dest_exists']} sautés "
        f"(destination présente), {stats['failed']} échecs — {elapsed:.1f} s"
        + (f" ({elapsed / stats['converted']:.1f} s/morceau)" if stats["converted"] else ""))
    log(f"Espace : {total_src // (1024 * 1024)} Mo → {total_out // (1024 * 1024)} Mo "
        f"(gagné : {(total_src - total_out) // (1024 * 1024)} Mo)")

    if meta_list:
        max_show = 50
        shown = sorted(meta_list)[:max_show]
        if len(meta_list) <= max_show:
            log(f"\nFichiers annexe(s) recopié(s) telle quelle(s) : {len(meta_list)} ->\n    "
                + "\n    ".join(shown))
        else:
            log(f"\nFichiers annexe(s) recopié(s) telle quelle(s) : {len(meta_list)} "
                f"(liste tronquée aux 50 premiers) ->\n    "
                + "\n    ".join(shown))

    return {"out_root": out_root, "manifest": manifest}


# ----------------------------------------------------------------------------
# identify command — AcoustID, same lookup/ranking as TagFetcher.kt
# ----------------------------------------------------------------------------

_mb_last_call = 0.0


def http_post(url: str, body: str) -> dict:
    req = urllib.request.Request(
        url, data=body.encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": MB_UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def http_get_json(url: str) -> dict:
    global _mb_last_call
    wait = MB_RATE_LIMIT_SEC - (time.time() - _mb_last_call)
    if wait > 0:
        time.sleep(wait)
    _mb_last_call = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": MB_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def find_fpcalc() -> str:
    """Locate the fpcalc binary (chromaprint fingerprinter).

    Per-OS hints (shutil.which already covers PATH and the .exe suffix on
    Windows):
      - Linux:   package 'libchromaprint-tools', or ~/.local/bin/fpcalc
      - macOS:   'brew install chromaprint' (/opt/homebrew/bin, /usr/local/bin)
      - Windows: 'choco install chromaprint' or a manual fpcalc.exe next to
                 this script / in the script directory
    """
    candidates = [shutil.which("fpcalc")]
    script_dir = os.path.dirname(os.path.abspath(__file__))
    exe_name = "fpcalc.exe" if os.name == "nt" else "fpcalc"
    candidates += [
        os.path.join(script_dir, exe_name),
        os.path.expanduser(os.path.join("~", ".local", "bin", "fpcalc")),
        "/opt/homebrew/bin/fpcalc",        # Apple Silicon Homebrew
        "/usr/local/bin/fpcalc",           # Intel Homebrew / manual install
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    sys.exit("fpcalc introuvable. Installation :\n"
             "  Linux   : sudo apt install libchromaprint-tools\n"
             "  macOS   : brew install chromaprint\n"
             "  Windows : choco install chromaprint (ou fpcalc.exe à côté du script)")


# CHROMAPRINT_ALGORITHM_DEFAULT (TEST2 in libchromaprint 1.5 — fpcalc's choice)
CHROMAPRINT_ALGORITHM_DEFAULT = 1


def fingerprint(src: str, duration: int, fpcalc: str) -> str:
    """Run fpcalc on the file: base64 AcoustID fingerprint + real duration."""
    r = subprocess.run(
        [fpcalc, "-json", src], capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"fpcalc failed: {r.stderr.strip()[:200]}")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("fpcalc returned invalid JSON")
    fp = data.get("fingerprint", "")
    if not fp:
        raise RuntimeError("fpcalc returned no fingerprint")
    return fp


def acoustid_lookup(fp: str, duration: int) -> dict:
    # meta is appended RAW (a literal '+' = space in form encoding), exactly
    # like the app: urlencode would escape it to %2B and the API then returns
    # results WITHOUT any recordings metadata.
    body = (f"client={ACOUSTID_CLIENT}"
            f"&fingerprint={urllib.parse.quote(fp, safe='')}"
            f"&meta=releases+recordings+releasegroups"
            f"&duration={duration}")
    try:
        return http_post(ACOUSTID_URL, body)
    except (OSError, json.JSONDecodeError) as e:
        log(f"      AcoustID indisponible : {e}")
        return {}


def pick_candidate(root: dict, duration: int):
    """Same ranking as TagFetcher.lookupAcoustId: duration-consistent
    candidates (≤5 s) first, then score, then duration delta."""
    cands = []
    for result in root.get("results", []):
        score = result.get("score", 0.0)
        for rec in result.get("recordings", []) or []:
            title = (rec.get("title") or "").strip()
            artists = rec.get("artists") or []
            artist = (artists[0].get("name") or "").strip() if artists else ""
            if not artist or not title:
                continue
            dur = rec.get("duration") or 0
            delta = abs(dur - duration) if dur else 10**9
            cands.append((score, delta, artist, title, rec))
    if not cands:
        return None

    # Exactly TagFetcher.kt: duration-consistent results first, then score,
    # then duration delta. Release-group type does not affect track matching.
    cands.sort(key=lambda c: (0 if c[1] <= DURATION_SLACK_SEC else 1, -c[0], c[1]))
    return cands[0]


def fetch_album_info(
    rec: dict,
    *,
    needs_year: bool = True,
    needs_genre: bool = True,
) -> tuple:
    """Return (album, Android-style year, Android-style genre, cover bytes).

    Selection and enrichment mirror TagFetcher.kt: choose the earliest dated
    non-secondary AcoustID release group (or earliest group of any type if
    there is no primary group), then use that group's MusicBrainz record only
    for missing year/genre fields. The genre selection matches TagFetcher.pickGenre.
    """
    groups = rec.get("releasegroups") or []
    primary = [g for g in groups if not (
        g.get("secondary-types") or g.get("secondarytypes"))]
    pool = primary or groups
    best = min(pool, key=lambda g: g.get("first-release-date") or "9999") \
        if pool else None
    if best is None:
        return None, "", "", None

    group_id = (best.get("id") or "").strip()
    album = (best.get("title") or "").strip()
    raw_date = (best.get("first-release-date") or "").strip()
    year = raw_date[:4] if len(raw_date) >= 4 and raw_date[:4].isdigit() else ""
    genre = ""

    # AcoustID often omits the date and does not carry genres. Android asks
    # MusicBrainz only for fields missing from the source audio.
    if group_id and (needs_year or needs_genre):
        try:
            # Android requests both genre sources even when only year is
            # missing; keep its MusicBrainz endpoint/query identical.
            query = "?fmt=json&inc=genres%2Btags"
            extras = http_get_json(
                "https://musicbrainz.org/ws/2/release-group/"
                f"{urllib.parse.quote(group_id, safe='')}{query}")
            if needs_year:
                extra_date = (extras.get("first-release-date") or "").strip()
                if len(extra_date) >= 4 and extra_date[:4].isdigit():
                    year = extra_date[:4]
            if needs_genre:
                genre = pick_android_genre(extras.get("genres") or [],
                                          extras.get("tags") or [])
        except (OSError, json.JSONDecodeError) as e:
            log(f"      MusicBrainz extras indisponibles : {e}")

    cover = None
    if group_id:
        try:
            req = urllib.request.Request(
                f"https://coverartarchive.org/release-group/{urllib.parse.quote(group_id, safe='')}/front-250",
                headers={"User-Agent": MB_UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status == 200 and resp.headers.get_content_type().startswith("image/"):
                    cover = resp.read()
        except OSError:
            pass
    return album, year, genre, cover


def pick_android_genre(genres: list[dict], tags: list[dict]) -> str:
    """Match TagFetcher.pickGenre: best curated genre, then clean folksonomy."""
    def best_vote(entries: list[dict]) -> str:
        best_name, best_count = "", None
        for entry in entries:
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            count = entry.get("count", 0)
            try:
                count = int(count)
            except (TypeError, ValueError):
                count = 0
            if best_count is None or count > best_count:
                best_name, best_count = name, count
        return best_name

    curated = best_vote(genres)
    if curated:
        return curated

    import re
    decade = re.compile(r"^\d{2,4}'?s?$", re.IGNORECASE)
    usable = []
    for entry in tags:
        name = str(entry.get("name") or "").strip()
        if not name or len(name) > 40:
            continue
        if any(c in name for c in "/\\|") or not any(c.isalpha() for c in name):
            continue
        if decade.fullmatch(name) or len(name.split()) > 3:
            continue
        usable.append(entry)
    return best_vote(usable)


def set_cover(path: str, data: bytes):
    """Embed cover art in a copy, whatever container it uses: MP3 -> APIC
    frame, M4A/MP4 -> covr box, FLAC -> PICTURE block, Ogg/Opus (default) ->
    base64 METADATA_BLOCK_PICTURE comment."""
    pic = Picture()
    pic.type = 3
    pic.mime = "image/jpeg"
    pic.desc = "Cover"
    pic.data = data
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in {"mp3", "mp2"}:
        tags = ID3(path)
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover",
                      data=data))
        tags.save()
    elif ext in {"m4a", "m4b", "m4p", "m4r", "mp4"}:
        audio = MutagenFile(path, easy=True)
        audio["covr"] = [MP4Cover(data, MP4Cover.FORMAT_JPEG)]
        audio.save()
    elif ext == "flac":
        audio = FLAC(path)
        audio.add_picture(pic)
        audio.save()
    else:
        audio = OggOpus(path)
        audio["METADATA_BLOCK_PICTURE"] = base64.b64encode(pic.write()).decode(
            "ascii")
        audio.save()


def needs_identification(path: str) -> bool:
    """True when "identify" has something to fill in on this file.

    Exact mirror of Track.needsIdentification() on Android: identification
    writes artist, title, album, year and genre, so a file counts as pending
    as soon as ONE of them is missing. A fully tagged file is therefore
    never sent to AcoustID/MusicBrainz — no fingerprint, no request — which
    is what keeps repeat runs offline (parity with the app's "Identify
    songs" target filter).
    """
    try:
        audio = MutagenFile(path, easy=True)
        tags = audio.tags if audio is not None else None
    except (MutagenError, OSError):
        tags = None
    if not tags:
        return True

    def present(*keys):
        for k in keys:
            v = tags.get(k)
            if v is None:
                continue
            items = v if isinstance(v, (list, tuple)) else [v]
            if any(str(it).strip() for it in items):
                return True
        return False

    return not (present("title") and present("artist") and present("album")
                and present("date", "year") and present("genre"))


def cmd_identify(args, compress_result=None):
    """Identify + tag. ONLY the compressed copies are ever written."""
    if compress_result:
        out_root = compress_result["out_root"]
        manifest = compress_result["manifest"]
    else:
        out_root = os.path.abspath(args.output)
        try:
            with open(os.path.join(out_root, MANIFEST_NAME), encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, json.JSONDecodeError):
            manifest = {}
    files = [os.path.join(out_root, rel) for rel in manifest.values()]
    files = [f for f in files if os.path.exists(f)]
    if not files:
        # No manifest, or a manifest that points nowhere (output tree moved
        # or wiped). The output tree is the only thing we may ever tag, so
        # identify whatever audio copies exist there (originaux jamais touchés).
        files = [os.path.join(dirpath, fn)
                 for dirpath, _, fns in os.walk(out_root)
                 for fn in fns
                 if os.path.splitext(fn)[1].lower().lstrip(".")
                 in SUPPORTED_EXTENSIONS]
        files.sort()
        if not files:
            log("Aucune copie à identifier dans le dossier de sortie : rien n'a "
                "été converti ni copié (déjà compressé, ou compress jamais "
                "lancé). Les originaux ne sont jamais retagués.")
            return
        log("(manifeste absent ou vide — identification de toutes les copies "
            "Ogg/Opus trouvées dans le dossier de sortie)")
    # Offline short-circuit (mirror of Track.needsIdentification): a copy
    # that already carries artist/title/album/year/genre needs nothing from
    # the network. Filtered BEFORE find_fpcalc, so a fully tagged tree
    # requires neither fpcalc nor connectivity at all.
    pending = [f for f in files if needs_identification(f)]
    already_tagged = len(files) - len(pending)
    if already_tagged:
        log(f"\n{already_tagged} copie(s) déjà taguée(s) — ignorées, "
            "aucun téléchargement")
    if not pending:
        log("Rien à identifier : toutes les copies portent déjà artiste, "
            "titre, album, année et genre.")
        return

    fpcalc = find_fpcalc()
    t_start = time.monotonic()
    log(f"\nIdentification de {len(pending)} copie(s) compressée(s)…")

    ok = no_match = failed = 0
    for i, path in enumerate(pending, 1):
        name = os.path.relpath(path, out_root)
        probe = ffprobe_json(path)
        dur = duration_seconds(probe)
        log(f"  [{i}/{len(pending)}] {name}")
        t0 = time.monotonic()
        try:
            fp = fingerprint(path, dur, fpcalc)
        except RuntimeError as e:
            log(f"      empreinte impossible : {e}")
            failed += 1
            continue
        root = acoustid_lookup(fp, dur)
        if root.get("status") != "ok":
            failed += 1
            continue
        best = pick_candidate(root, dur)
        if not best:
            log("      aucune correspondance")
            no_match += 1
            continue
        score, delta, artist, title, rec = best
        # Android only requests MusicBrainz year/genre data when those fields
        # are missing on the source. Keep existing values when absent online.
        try:
            current_audio = MutagenFile(path, easy=True)
            current_tags = current_audio.tags or {} if current_audio is not None else {}
        except (MutagenError, OSError):
            current_tags = {}
        needs_year = not bool(current_tags.get("date") or current_tags.get("year"))
        needs_genre = not bool(current_tags.get("genre"))
        album, year, genre, cover = fetch_album_info(
            rec, needs_year=needs_year, needs_genre=needs_genre,
        )
        if args.dry_run:
            log(f"      (dry-run) « {title} » — {artist}"
                + (f" — {album} ({year})" if album else "")
                + (f" — {genre}" if genre else ""))
            ok += 1
            continue
        try:
            audio = MutagenFile(path, easy=True)
            if audio is None:
                raise MutagenError("format non géré")
            if audio.tags is None:
                audio.add_tags()
            audio["title"] = title
            audio["artist"] = artist
            if album:
                audio["album"] = album
            if year:
                audio["date"] = year
            if genre:
                audio["genre"] = genre
            audio.save()
        except MutagenError as e:
            log(f"      balises impossibles : {e}")
            failed += 1
            continue
        # Android updates embedded artwork only for Opus, whose custom writer
        # is Android-safe. Its jaudiotagger path intentionally writes text only
        # for MP3/FLAC/Vorbis; mirror that here rather than altering their art.
        if cover and isinstance(audio, OggOpus):
            try:
                set_cover(path, cover)
            except (MutagenError, OSError) as e:
                log(f"      pochette non écrite : {e}")
        log(f"      « {title} » — {artist}" + (f" — {album} ({year})" if album else "")
            + (f" — {genre}" if genre else "")
            + f"  (score {score:.2f}, Δ{delta}s, {time.monotonic() - t0:.1f} s)")
        ok += 1
    summary = (f"\nIdentification : {ok} tagués, {no_match} sans correspondance, "
               f"{failed} échecs")
    if already_tagged:
        summary += f", {already_tagged} déjà tagués ignorés"
    log(summary
        + (f" — {time.monotonic() - t_start:.1f} s" if ok + no_match + failed else ""))


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="SimplyConvert — companion PC de SimplyPlay")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("source", help="dossier musical à traiter")
        p.add_argument("-o", "--output", help="dossier de sortie (copies Ogg)")

    p = sub.add_parser("compress", help="compresser la bibliothèque en Ogg/Opus")
    add_common(p)
    p.add_argument("--bitrate", type=int, default=DEFAULT_BITRATE_KBPS,                    choices=[128, 160, 180, 256])
    p.add_argument("--auto-volume", dest="auto_volume", action="store_true", default=True)
    p.add_argument("--no-auto-volume", dest="auto_volume", action="store_false")
    p.add_argument("--fast", action="store_true",
                   help="exécuter à pleine vitesse (pas de priorité basse)")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("identify", help="identifier et taguer les copies compressées")
    add_common(p)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("compressidentify", help="compress puis identify (taggage + compression)")
    add_common(p)
    p.add_argument("--bitrate", type=int, default=DEFAULT_BITRATE_KBPS,                    choices=[128, 160, 180, 256])
    p.add_argument("--auto-volume", dest="auto_volume", action="store_true", default=True)
    p.add_argument("--no-auto-volume", dest="auto_volume", action="store_false")
    p.add_argument("--fast", action="store_true",
                   help="exécuter à pleine vitesse (pas de priorité basse)")
    p.add_argument("--dry-run", action="store_true")

    args = ap.parse_args()
    if args.cmd == "compress":
        cmd_compress(args)
    elif args.cmd == "identify":
        cmd_identify(args)
    elif args.cmd == "compressidentify":
        res = cmd_compress(args)
        cmd_identify(args, compress_result=res)


if __name__ == "__main__":
    main()
