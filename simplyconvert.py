#!/usr/bin/env python3
"""SimplyConvert — PC companion of the SimplyPlay Android app.

Applies the SAME two engines as the app to a PC music folder:

  compress   Re-encode the library to Ogg/Opus (libopus, 160/180/320 kbps),
             with the same rules as "Compress my music":
               - lossless sources always convert
               - lossy sources only convert if the result is smaller
                 (pre-filter: a lossy source already at/below target+slack
                 is skipped — unless auto-volume is on, mirroring the app)
               - the ORIGINALS ARE NEVER TOUCHED and never re-tagged;
                 results land in a separate output tree that mirrors the
                 source layout, named "Title.ogg" (collision -> Title-2.ogg)
               - ReplayGain analysis (same math as the app) is written as
                 REPLAYGAIN_TRACK_GAIN/PEAK comments on the copy
  identify   Fingerprint each track (chromaprint, like the app) and fill
             artist/title/album/date/cover from AcoustID + MusicBrainz +
             Cover Art Archive. Tags are written ONLY on the compressed
             copies — originals are read-only for this tool, ever.

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
from mutagen.flac import Picture
from mutagen.oggopus import OggOpus

# ----------------------------------------------------------------------------
# Constants shared with the Kotlin app (ReplayGain.kt / CompressionFlow.kt /
# MusicScanner.kt / TagFetcher.kt) — keep in sync when the app evolves.
# ----------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {
    "mp3", "ogg", "flac", "m4a", "aac", "alac",
    "oga", "ogx", "opus",
    "wav", "riff", "bwf",
    "ac3", "eac3",
    "mp4", "m4b", "m4p", "m4r",
}
LOSSLESS_EXTENSIONS = {"flac", "wav", "riff", "bwf", "alac"}

RG_REFERENCE_RMS = 6537.0        # s16 RMS of the 89 dB reference (app value)
MAX_GAIN = 3.981                 # +12 dB amplification ceiling
MIN_GAIN = 0.178                 # -15 dB attenuation floor
CLIP_GUARD = 0.98                # peak headroom after amplification

DEFAULT_BITRATE_KBPS = 160
BITRATE_SLACK = 24_000           # bit/s of tolerance in the lossy pre-filter

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


def source_bitrate(probe: dict):
    br = probe.get("format", {}).get("bit_rate")
    try:
        return int(br) if br else None
    except ValueError:
        return None


def duration_seconds(probe: dict) -> int:
    try:
        return int(float(probe.get("format", {}).get("duration", 0)))
    except (TypeError, ValueError):
        return 0


def resolve_out_name(out_dir: str, stem: str) -> str:
    """Title.ogg, or Title-2.ogg / Title-3.ogg… when taken (app rule)."""
    candidate = os.path.join(out_dir, f"{stem}.ogg")
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(out_dir, f"{stem}-{n}.ogg")
        n += 1
    return candidate


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
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", src, "-map", "0:a:0",
           "-vn", "-c:a", "libopus", "-b:a", f"{kbps}k", "-f", "ogg", dst_tmp]
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


def copy_tags_with_cover(src: str, opus_path: str, rg: tuple):
    """Copy source tags + cover art onto the Opus copy (mutagen), then add
    the ReplayGain comments exactly like OpusTags.writeReplayGainComments."""
    audio = OggOpus(opus_path)
    src_probe = ffprobe_json(src)
    fmt_tags = src_probe.get("format", {}).get("tags", {}) or {}

    def pick(*keys):
        for k in keys:
            v = fmt_tags.get(k)
            if v:
                return v
        return None

    mapping = {
        "TITLE": ("title", "TITLE"),
        "ARTIST": ("artist", "ARTIST"),
        "ALBUM": ("album", "ALBUM"),
        "ALBUMARTIST": ("album_artist", "ALBUMARTIST", "ALBUM ARTIST"),
        "DATE": ("date", "DATE", "year", "TYER"),
        "TRACKNUMBER": ("track", "TRCK", "track"),
        "DISCNUMBER": ("disc", "TPOS", "disc"),
        "GENRE": ("genre", "TCON"),
        "COMPOSER": ("composer", "TCOM"),
        "PUBLISHER": ("publisher", "TPUB", "label"),
    }
    for dst_key, sources in mapping.items():
        val = pick(*sources)
        if val:
            vals = dedupe([v.strip() for v in str(val).split(";") if v.strip()])
            if vals:
                audio[dst_key] = vals

    # Cover art: first attached picture of the source, via ffmpeg → tmp jpg.
    pic_file = None
    for st in src_probe.get("streams", []):
        if st.get("codec_type") == "video" and st.get("disposition", {}).get("attached_pic"):
            pic_file = os.path.join(_TMP_DIR, f"st_cover_{os.getpid()}.jpg")
            r = subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", src,
                 "-map", f"0:{st['index']}", "-frames:v", "1", pic_file],
                capture_output=True, creationflags=_subprocess_flags())
            if r.returncode != 0:
                pic_file = None
            break
    if pic_file and os.path.getsize(pic_file) > 0:
        with open(pic_file, "rb") as f:
            data = f.read()
        pic = Picture()
        pic.type = 3  # front cover
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.data = data
        audio["METADATA_BLOCK_PICTURE"] = base64.b64encode(pic.write()).decode("ascii")
        try:
            os.remove(pic_file)
        except OSError:
            pass

    if rg:
        gain, peak = rg
        db = 20.0 * (np.log10(gain) if gain > 0 else 0.0)
        audio["REPLAYGAIN_TRACK_GAIN"] = [f"{db:+.2f} dB"]
        audio["REPLAYGAIN_TRACK_PEAK"] = [f"{peak / 32767.0:.6f}"]

    audio.save()


# ----------------------------------------------------------------------------
# compress command
# ----------------------------------------------------------------------------

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

    stats = {"converted": 0, "kept": 0, "prefilter": 0, "bigger": 0, "failed": 0}
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

        # Pre-filter (app rule): lossy at/below target+slack can only grow…
        if not lossless and not args.auto_volume:
            br = source_bitrate(probe)
            if br and br <= args.bitrate * 1000 + BITRATE_SLACK:
                log(f"  [{i}/{len(files)}] déjà compressé, sauté : {rel_src}")
                stats["prefilter"] += 1
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
        tmp = resolve_out_name(out_dir, stem) + ".part"
        try:
            convert_to_ogg(src, tmp, args.bitrate)
            copy_tags_with_cover(src, tmp, rg)
        except RuntimeError as e:
            log(f"      échec : {e}")
            stats["failed"] += 1
            if os.path.exists(tmp):
                os.remove(tmp)
            continue

        # Size gate: lossy copies must be smaller, like the app.
        new_size = os.path.getsize(tmp)
        if not lossless and new_size >= os.path.getsize(src):
            os.remove(tmp)
            log(f"      résultat plus gros que l'original — sauté")
            stats["bigger"] += 1
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

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)

    total_src = sum(os.path.getsize(os.path.join(src_root, r)) for r in manifest
                    if os.path.exists(os.path.join(src_root, r)))
    total_out = sum(os.path.getsize(os.path.join(out_root, p)) for p in manifest.values()
                    if os.path.exists(os.path.join(out_root, p)))
    elapsed = time.monotonic() - t_start
    log(f"\nRésumé : {stats['converted']} convertis, {stats['kept']} déjà à jour, "
        f"{stats['prefilter']} sautés (déjà compressés), {stats['bigger']} plus gros, "
        f"{stats['failed']} échecs — {elapsed:.1f} s"
        + (f" ({elapsed / stats['converted']:.1f} s/morceau)" if stats["converted"] else ""))
    log(f"Espace : {total_src // (1024 * 1024)} Mo → {total_out // (1024 * 1024)} Mo "
        f"(gagné : {(total_src - total_out) // (1024 * 1024)} Mo)")
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

    def has_primary(rec: dict) -> bool:
        # Any release-group AcoustID does not flag as compilation/live/etc.
        # (missing type data counts as unknown, i.e. possibly primary).
        return any(not (g.get("secondary-types") or g.get("secondarytypes"))
                   for g in rec.get("releasegroups", []) or [])

    cands.sort(key=lambda c: (0 if c[1] <= DURATION_SLACK_SEC else 1, -c[0],
                              0 if has_primary(c[4]) else 1, c[1]))
    return cands[0]


def fetch_album_info(rec: dict, acoustid_root: dict | None = None) -> tuple:
    """(album, date, cover_bytes|None) via MusicBrainz + Cover Art Archive.

    The release-groups AcoustID nests in a recording are unreliable for old
    hits: mostly compilations, dates almost never filled, order arbitrary —
    taking the first "non-secondary" one once tagged Joe Dassin's L'Été
    indien with a random compilation (« A French Affair ») instead of the
    1974 single. So instead:
      1. collect the groups of every recording of the best result (the
         original single/album often hangs on a sibling recording, and
         MusicBrainz's own browse sometimes only exposes compilations),
      2. merge the groups MusicBrainz itself links to the recording
         (authoritative types/dates) with the AcoustID ones,
      3. verify the primary-looking ones on MusicBrainz one by one
         (release-group lookup: real types + first-release-date) and keep
         the first that is neither compilation nor live/etc. — the original
         release — falling back to the earliest dated group of any kind when
         the track only ever appeared on compilations (same rule as
         TagFetcher.kt).
    """
    rec_id = rec.get("id") or ""
    acoustid_groups, seen_ids = [], set()
    recs = [rec]
    if acoustid_root:
        recs += [r for res in acoustid_root.get("results", []) or []
                 for r in res.get("recordings", []) or []]
    for r in recs:
        for g in r.get("releasegroups", []) or []:
            if g.get("id") and g["id"] not in seen_ids:
                seen_ids.add(g["id"])
                acoustid_groups.append(g)

    browse_groups = []
    if rec_id:
        try:
            data = http_get_json(
                f"https://musicbrainz.org/ws/2/release?recording={rec_id}"
                f"&inc=release-groups&fmt=json&limit=100")
            seen = set()
            for rel in data.get("releases", []) or []:
                g = dict(rel.get("release-group") or {})
                gid = g.get("id")
                if not gid or gid in seen:
                    continue
                seen.add(gid)
                g["secondary-types"] = g.get("secondary-types") or []
                browse_groups.append(g)
        except (OSError, json.JSONDecodeError):
            pass  # MusicBrainz unreachable: fall back to the AcoustID groups

    def sec_of(g: dict) -> list:
        return g.get("secondary-types") or g.get("secondarytypes") or []

    def primary_type_ok(info: dict) -> bool:
        # MusicBrainz exposes primary-type (str) and secondary-types (list).
        pt = info.get("primary-types") or ([info["primary-type"]]
                                           if info.get("primary-type") else [])
        return not pt or any(t in ("Album", "Single", "EP") for t in pt)

    # Groups worth verifying: those AcoustID does not already flag as
    # compilation/live/etc. AcoustID's type is often absent, so rank known
    # albums first, unknown next, singles last; dated groups (rare) first
    # within a class, then MusicBrainz browse entries before AcoustID ones.
    def rank(g: dict):
        order = {"Album": 0, "Single": 2}.get(g.get("type") or "", 1)
        return (order,
                0 if g.get("first-release-date") else 1,
                g.get("first-release-date") or "",
                0 if g in browse_groups else 1)

    cand_ids, candidates = set(), []
    for g in browse_groups + acoustid_groups:
        gid = g.get("id")
        if not gid or gid in cand_ids or sec_of(g):
            continue
        cand_ids.add(gid)
        candidates.append(g)
    candidates.sort(key=rank)

    winner = None
    verified = []  # MB info of everything checked — compilation-only fallback
    for g in candidates[:8]:
        try:
            info = http_get_json(
                f"https://musicbrainz.org/ws/2/release-group/{g['id']}?fmt=json")
        except (OSError, json.JSONDecodeError):
            continue
        verified.append(info)
        if not (info.get("secondary-types") or []) and primary_type_ok(info):
            winner = info
            break

    if winner is not None:
        gid = winner.get("id")
        date = winner.get("first-release-date") or None
        winner_primary = (winner.get("primary-types")
                          or ([winner["primary-type"]] if winner.get("primary-type") else []))
        if winner_primary == ["Single"]:
            # A-side/B-side single titles are noisy (« X (Y) / Z »): a
            # standalone single reads better under the track's own name.
            album = (rec.get("title") or winner.get("title") or "").strip()
        else:
            album = (winner.get("title") or "").strip()
    else:
        # Nothing verifiable turned out non-compilation: earliest dated group
        # of any kind (canonical album, like the app); list order when no date.
        pool = [info for info in verified if info.get("id")]
        pool += browse_groups + acoustid_groups
        best = min(pool, key=lambda g: g.get("first-release-date") or "9999") \
            if pool else None
        if not best:
            return None, None, None
        gid = best.get("id")
        album = best.get("title")
        date = best.get("first-release-date") or None

    cover = None
    if gid:
        try:
            req = urllib.request.Request(
                f"https://coverartarchive.org/release-group/{gid}/front-500",
                headers={"User-Agent": MB_UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status == 200 and resp.headers.get_content_type().startswith("image/"):
                    cover = resp.read()
        except OSError:
            pass
    return album, date, cover


def set_cover(opus_path: str, data: bytes):
    audio = OggOpus(opus_path)
    pic = Picture()
    pic.type = 3
    pic.mime = "image/jpeg"
    pic.desc = "Cover"
    pic.data = data
    audio["METADATA_BLOCK_PICTURE"] = base64.b64encode(pic.write()).decode("ascii")
    audio.save()


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
        # No manifest, a manifest that points nowhere (output tree moved or
        # wiped), or compress skipped everything as "bigger" so nothing was
        # written yet. The output tree is the only thing we may ever tag,
        # so identify whatever Ogg copies exist there (originaux jamais touchés).
        files = [os.path.join(dirpath, fn)
                 for dirpath, _, fns in os.walk(out_root)
                 for fn in fns if fn.lower().endswith((".opus", ".ogg"))]
        files.sort()
        if not files:
            log("Aucune copie à identifier dans le dossier de sortie : rien n'a "
                "été converti (déjà compressé, plus gros que l'original, ou "
                "compress jamais lancé). Les originaux ne sont jamais retagués.")
            return
        log("(manifeste absent ou vide — identification de toutes les copies "
            "Ogg/Opus trouvées dans le dossier de sortie)")
    fpcalc = find_fpcalc()
    t_start = time.monotonic()
    log(f"\nIdentification de {len(files)} copie(s) compressée(s)…")

    ok = no_match = failed = 0
    for i, path in enumerate(files, 1):
        name = os.path.relpath(path, out_root)
        probe = ffprobe_json(path)
        dur = duration_seconds(probe)
        existing_title = (probe.get("format", {}).get("tags", {}) or {}).get("title", "")
        log(f"  [{i}/{len(files)}] {name}")
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
        album, date, cover = fetch_album_info(rec, root)
        if args.dry_run:
            log(f"      (dry-run) « {title} » — {artist}"
                + (f" — {album} ({date})" if album else ""))
            ok += 1
            continue
        audio = OggOpus(path)
        audio["TITLE"] = [title]
        audio["ARTIST"] = [artist]
        if album:
            audio["ALBUM"] = [album]
        if date:
            audio["DATE"] = [date]
        audio.save()
        if cover:
            set_cover(path, cover)
        log(f"      « {title} » — {artist}" + (f" — {album} ({date})" if album else "")
            + f"  (score {score:.2f}, Δ{delta}s, {time.monotonic() - t0:.1f} s)")
        ok += 1
    log(f"\nIdentification : {ok} tagués, {no_match} sans correspondance, {failed} échecs"
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
    p.add_argument("--bitrate", type=int, default=DEFAULT_BITRATE_KBPS,
                   choices=[128, 160, 180, 320])
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
    p.add_argument("--bitrate", type=int, default=DEFAULT_BITRATE_KBPS,
                   choices=[128, 160, 180, 320])
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
