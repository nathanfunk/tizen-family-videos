#!/usr/bin/env python3
"""family-videos app server — playlist + Range-capable video streaming for the TV app.

Runs on the Pi. Serves from the SMB mount (//HP/PicturesArchive at /mnt/family-media),
reusing the same date/GPS metadata logic as cast-random.sh (ffprobe creation_time,
offline reverse geocode via geo-venv). Keeps the HP awake with periodic WOL while
videos are being served.

Endpoints (all CORS-enabled — the TV widget calls these cross-origin):
  GET  /api/health
  GET  /api/playlist?mode=random&n=25            (also: mode=favorites | label=<lbl> | date=<prefix>)
  GET  /api/video?p=<index-relative-path>        HTTP Range stream
  GET  /api/meta?p=<path>                        date/location/duration/display size
  GET  /api/favorites                            list
  POST /api/favorite   {"action":"add|remove","path":...,"label":...,"note":...}
  POST /api/note       {"path":...,"note":...}   annotation for a clip
  POST /api/log        {"msg":...}               TV breadcrumb
  GET  /api/preview                              Smart Hub Preview JSON
  GET  /api/thumb?p=<path>                       640x360 JPEG (placeholder on failure)
  GET  /*  static files from APP_DIR (dev/test serving of the TV app)

Test from a desktop browser: http://rpi:8123/  (APP_DIR must be set)
"""

import calendar
import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

MOUNT = os.environ.get("FV_MOUNT", "/mnt/family-media")
INDEX = os.environ.get("FV_INDEX", os.path.expanduser("~/projects/family-videos/index.txt"))
FAV = os.environ.get("FV_FAVORITES", os.path.expanduser("~/projects/family-videos/favorites.tsv"))
GEO_PY = os.path.expanduser("~/projects/family-videos/geo-venv/bin/python")
CACHE = os.path.expanduser("~/.cache/family-videos-meta.json")
BAKE_DIR = os.path.expanduser("~/.cache/family-videos-bake")
BAKE_CAP_BYTES = 2 * 1024 * 1024 * 1024   # LRU cap for baked files
BAKE_RETRY_SECONDS = 300                  # back off after a failed bake
APP_DIR = os.environ.get("FV_APP_DIR", "")  # set in dev to serve the tvapp dir
PORT = int(os.environ.get("FV_PORT", "8123"))
MAX_BODY = 64 * 1024                      # POST bodies are tiny JSON documents
# Smart Hub Preview: launcher strips + thumbnails (personal — LAN only)
THUMB_DIR = os.environ.get("FV_THUMBS", os.path.expanduser("~/.cache/family-videos-thumbs"))
THUMB_CAP_BYTES = int(os.environ.get("FV_THUMB_CAP_BYTES", str(256 * 1024 * 1024)))
PREVIEW_URL = os.environ.get("FV_PREVIEW_URL", "")  # advertised in config.xml
# 640x360 mid-gray JPEG: served when a thumbnail can't be generated so the
# launcher's strip never aborts on a 404
PLACEHOLDER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "placeholder.jpg")

def _load_placeholder():
    try:
        with open(PLACEHOLDER_FILE, "rb") as f:
            return f.read()
    except OSError:
        return b""

PLACEHOLDER_JPEG = _load_placeholder()

LOG = os.environ.get("FV_LOG", "/tmp/family-videos-app.log")
# WOL target MAC (env override; empty disables WOL)
HP_MAC = os.environ.get("FV_HP_MAC", "")

_lock = threading.Lock()
_meta = {}
_meta_dirty = False

_last_video_hit = 0.0


def log(msg):
    # keep every entry on one line: some messages include request/client data
    msg = str(msg).replace("\r", "\\r").replace("\n", "\\n")
    try:
        with open(LOG, "a") as f:
            f.write("%s fv-app: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), msg))
    except OSError:
        pass


# ---------------------------------------------------------------- HP wake
def wol():
    if not HP_MAC:
        return
    try:
        magic = b"\xff" * 6 + bytes.fromhex(HP_MAC.replace(":", "")) * 16
    except ValueError:
        return
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for port in (9, 7):
                s.sendto(magic, ("10.0.0.255", port))
    except OSError:
        pass


def mount_ok():
    try:
        return os.path.isdir(MOUNT) and len(os.listdir(MOUNT)) > 0
    except OSError:
        return False


_mount_lock = threading.Lock()


def ensure_mount():
    if mount_ok():
        return True
    # Only one request should run sudo mount; everyone else waits for it.
    with _mount_lock:
        if mount_ok():
            return True
        log("mount down -> sudo mount")
        try:
            subprocess.run(["sudo", "mount", MOUNT], capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            log("mount command failed: %r" % e)
            return False
        time.sleep(3)
        return mount_ok()


def keepalive_loop():
    global _last_video_hit
    while True:
        time.sleep(300)
        if time.time() - _last_video_hit < 660:
            wol()


# ---------------------------------------------------------------- metadata
MONTHS = ["", "January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

# Kids' birth months (day unknown) — ages shown next to the video date
KIDS = [("Calvin", 2011, 8), ("Nadine", 2013, 6)]

def kids_ages(y, m=None):
    """'Calvin 6 · Nadine 4' — ages at the video's date (year+month precision;
    month-only dates treated as end-of-month). Under 2 shows months."""
    out = []
    for name, by, bm in KIDS:
        vm = m if m else 12
        if (y, vm) < (by, bm):
            continue
        months = (y - by) * 12 + (vm - bm)
        if months >= 24:
            out.append("%s %d" % (name, months // 12))
        elif months >= 0:
            out.append("%s %d mo" % (name, months) if months else "%s (newborn)" % name)
    return " \u00b7 ".join(out)

# ---------------------------------------------------------------- annotations
# Human-added captions, one per video: path<TAB>text. Authored by Nathan via
# Hermes chat ("add 'Calvin singing' to the video playing now") or by hand;
# the server picks up file changes by mtime, no restart needed.
ANN_TSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "annotations.tsv")
_ann_cache = {"mtime": None, "data": {}}
_ann_lock = threading.Lock()
_TSV_BAD_RE = re.compile(r"[\t\r\n]+")


def _tsv_safe(value):
    """Keep tabs/newlines from corrupting TSV files (annotations/favorites)."""
    return _TSV_BAD_RE.sub(" ", str(value)).strip()


def _read_annotations():
    try:
        mt = os.path.getmtime(ANN_TSV)
    except OSError:
        return {}
    if _ann_cache["mtime"] == mt:
        return _ann_cache["data"]
    data = {}
    try:
        with open(ANN_TSV) as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#") or "\t" not in line:
                    continue
                p, txt = line.split("\t", 1)
                data[p.strip()] = txt.strip()
    except OSError:
        pass
    _ann_cache["mtime"] = mt
    _ann_cache["data"] = data
    return data


def annotations():
    with _ann_lock:
        return _read_annotations()


def set_annotation(rel, text):
    """Add/update/remove an annotation for rel; text '' removes it."""
    rel = _tsv_safe(rel)
    text = _tsv_safe(text)
    with _ann_lock:
        ann = dict(_read_annotations())
        if text:
            ann[rel] = text
        else:
            ann.pop(rel, None)
        lines = ["# path<TAB>annotation (one per video)"]
        lines += ["%s\t%s" % (p, t) for p, t in sorted(ann.items())]
        tmp = ANN_TSV + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(tmp, ANN_TSV)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        _ann_cache["mtime"] = None   # force reload
    log("annotation set: %s -> %s" % (rel, text[:60]))

DATE_FULL_RE = re.compile(r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), (January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2}), (\d{4})")
DATE_MY_RE = re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December) (\d{4})$")

def kids_for_date(date_str):
    if not date_str:
        return ""
    m = DATE_FULL_RE.search(date_str)
    if m:
        return kids_ages(int(m.group(3)), MONTHS.index(m.group(1)))
    m = DATE_MY_RE.search(date_str.strip())
    if m:
        return kids_ages(int(m.group(2)), MONTHS.index(m.group(1)))
    m = re.match(r"^(\d{4})$", date_str.strip())
    if m:
        return kids_ages(int(m.group(1)))
    return ""


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

def fmt_date(y, m=None, d=None):
    if m and d:
        try:  # include the weekday: "Tuesday, December 26, 2017"
            # calendar.weekday: 0=Monday..6=Sunday; WEEKDAYS is Monday-first
            return "%s, %s %d, %d" % (WEEKDAYS[calendar.weekday(int(y), int(m), int(d))], MONTHS[int(m)], int(d), int(y))
        except Exception:
            return "%s %d, %d" % (MONTHS[int(m)], int(d), int(y))
    if m:
        return "%s %d" % (MONTHS[int(m)], int(y))
    return "%d" % int(y)


def ffprobe(rel, real=None):
    """One ffprobe call per file: (creation_time, iso6709, duration, disp_w, disp_h).

    disp_w/disp_h are ROTATION-AWARE display dimensions: iPhone portrait videos
    are often encoded 1280x720 (etc.) with a rotate/rotation side-data of ±90 —
    the browser reports the encoded size, so the client needs the true display
    size from here to letterbox correctly."""
    try:
        if real is None:
            real = safe_path(rel)
        if real is None:          # never probe outside MOUNT, even from /api/meta
            return "", "", "", 0, 0, 0
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:format_tags=creation_time,com.apple.quicktime.location.ISO6709"
             ":stream=width,height:stream_side_data=rotation",
             "-of", "json", real],
            capture_output=True, timeout=30).stdout
        data = json.loads(out)
        fmt = data.get("format", {})
        tags = fmt.get("tags", {})
        dw = dh = 0
        for s in data.get("streams", []):
            if "width" not in s or "height" not in s:
                continue
            w, h = int(s["width"]), int(s["height"])
            rot = 0
            for sd in s.get("side_data_list") or []:
                if "rotation" in sd:
                    rot = int(sd["rotation"]) % 360
            if not rot and "rotate" in (s.get("tags") or {}):
                rot = int(s["tags"]["rotate"]) % 360
            dw, dh = w, h
            if rot % 180 == 90:       # portrait stored as landscape
                dw, dh = h, w
            break
        baked = 1 if rot % 180 == 90 else 0
        return (tags.get("creation_time", ""),
                tags.get("com.apple.quicktime.location.ISO6709", ""),
                fmt.get("duration", ""), dw, dh, baked)
    except Exception:
        return "", "", "", 0, 0, 0


# ------------------------------------------------------- rotation baking (Tizen)
# The UN50NU7100's video plane renders streams with rotation metadata as a
# small tile, while natively-encoded portrait files play fine. So for rotated
# files we bake the rotation into the pixels once (hardware h264 encoder,
# ~2.3x realtime) and serve that instead.

def bake_path(rel):
    h = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
    return os.path.join(BAKE_DIR, h + ".mp4")

_bake_lock = threading.Lock()
_bake_active = {}
_bake_failed = {}      # rel -> time before which a failed bake is not retried
_bake_slots = threading.Semaphore(1)   # one hardware encoder concurrency
_trim_lock = threading.Lock()


def _touch(path):
    try:
        os.utime(path, None)
    except OSError:
        pass


def _bake_ready(rel):
    out = bake_path(rel)
    try:
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
    except OSError:
        pass
    return None


def _bake_is_active(rel):
    with _bake_lock:
        return bool(_bake_active.get(rel))


def _bake_blocked(rel):
    with _bake_lock:
        return bool(_bake_active.get(rel)) or _bake_failed.get(rel, 0) > time.time()


def bake_video(rel):
    """Transcode rel with rotation baked in; returns baked path or None."""
    out = bake_path(rel)
    if _bake_ready(rel):
        return out
    with _bake_lock:
        if _bake_active.get(rel) or _bake_failed.get(rel, 0) > time.time():
            return None        # another thread is baking it, or recent failure
        _bake_active[rel] = True
    err = ""
    ok = False
    try:
        src = safe_path(rel)
        if src is None:
            err = " (bad path)"
            return None
        os.makedirs(BAKE_DIR, exist_ok=True)
        tmp = out + ".part"
        try:
            with _bake_slots:
                proc = subprocess.run(
                    ["ffmpeg", "-y", "-v", "error",
                     "-i", src,
                     "-vf", "format=yuv420p",   # HEVC sources decode to a
                                                # 10-bit/yuv420p10le format the
                                                # v4l2m2m encoder rejects
                     "-c:v", "h264_v4l2m2m", "-b:v", "6M",
                     "-c:a", "copy", "-movflags", "+faststart",
                     "-f", "mp4", tmp],
                    timeout=1800, capture_output=True)
            if os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, out)
                _touch(out)
                trim_bakes()
                ok = True
                log("baked: %s -> %s" % (rel, os.path.getsize(out)))
                return out
            err = " (ffmpeg rc=%d)" % proc.returncode
            if proc.stderr:
                err += " " + proc.stderr.decode("utf-8", "replace")[-300:].strip()
        except Exception as e:
            err = " (%r)" % e
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return None
    except Exception as e:
        err = " (%r)" % e
        return None
    finally:
        with _bake_lock:
            _bake_active.pop(rel, None)
            if ok:
                _bake_failed.pop(rel, None)
            else:
                _bake_failed[rel] = time.time() + BAKE_RETRY_SECONDS
        if err:
            log("bake FAILED: %s%s" % (rel, err))


def trim_bakes():
    """Keep baked files under BAKE_CAP_BYTES, oldest (least recently served) first."""
    with _trim_lock:
        try:
            names = [f for f in os.listdir(BAKE_DIR) if f.endswith(".mp4")]
        except OSError:
            return
        files = []
        for name in names:
            path = os.path.join(BAKE_DIR, name)
            try:
                files.append((os.path.getmtime(path), os.path.getsize(path), path))
            except OSError:
                pass
        total = sum(size for _, size, _ in files)
        if total <= BAKE_CAP_BYTES:
            return
        files.sort()
        while total > BAKE_CAP_BYTES and len(files) > 1:
            _, size, path = files.pop(0)
            try:
                os.remove(path)
            except OSError:
                pass
            total -= size


def _kick_bakes(rels):
    for r in rels:
        try:
            with _lock:
                m = _meta.get(r)
            if not (m and m.get("rot")):
                continue
            if _bake_ready(r) or _bake_blocked(r):
                continue
            threading.Thread(target=bake_video, args=(r,), daemon=True).start()
        except Exception:
            pass

def needs_bake(rel):
    """True when rel has rotation metadata the TV plane renders wrong."""
    try:
        m = meta_for([rel]).get(rel) or {}
        return bool(m.get("rot"))
    except Exception:
        return False


def pretty_date(rel, created, mtime_fallback=True):
    base = os.path.basename(rel)
    d = os.path.dirname(rel)
    m1 = re.search(r"^(\d{4})-(\d{2})-(\d{2})", base)
    if not m1:
        m1 = re.search(r"(\d{4})-(\d{2})-(\d{2})", d)
    if m1:
        return fmt_date(int(m1.group(1)), int(m1.group(2)), int(m1.group(3)))
    m2 = re.search(r"(?:^|/)(\d{4})-(\d{2})(?:/|$)", d)
    if m2:
        return fmt_date(int(m2.group(1)), int(m2.group(2)))
    m3 = re.search(r"(?:^|/)(\d{4})(?:/|$)", d)
    if m3:
        if created:
            try:
                t = time.strptime(created[:10], "%Y-%m-%d")
                return fmt_date(t.tm_year, t.tm_mon, t.tm_mday)
            except ValueError:
                pass
        return "%d" % int(m3.group(1))
    if mtime_fallback:
        try:
            st = os.stat(os.path.join(MOUNT, rel))
            t = time.localtime(st.st_mtime)
            return fmt_date(t.tm_year, t.tm_mon, t.tm_mday)
        except OSError:
            pass
        if created:
            try:
                t = time.strptime(created[:10], "%Y-%m-%d")
                return fmt_date(t.tm_year, t.tm_mon, t.tm_mday)
            except ValueError:
                pass
    return ""


ISO6709_RE = re.compile(r"([+-]\d+\.\d+)([+-]\d+\.\d+)")


def batch_locations(coords):
    """{rel: latlon} -> {rel: 'City, Region'} via ONE geo-venv subprocess."""
    out = {}
    if not coords or not os.path.isfile(GEO_PY):
        return out
    pairs = list(coords.items())
    try:
        inp = "\n".join("%d %s" % (i, ll) for i, (rel, ll) in enumerate(pairs)) + "\n"
        code = (
            "import sys\n"
            "import reverse_geocoder as rg\n"
            "lines = [l for l in sys.stdin.read().splitlines() if l.strip()]\n"
            "ll = [tuple(float(x) for x in l.split()[1:]) for l in lines]\n"
            "for idx, r in zip([int(l.split()[0]) for l in lines], rg.search(ll)):\n"
            "    print('%s|%s, %s' % (idx, r['name'], r['admin1']))\n"
        )
        p = subprocess.run([GEO_PY, "-c", code], input=inp, capture_output=True,
                           text=True, timeout=120)
        for line in p.stdout.splitlines():
            if "|" not in line:
                continue
            idx, name = line.split("|", 1)
            out[pairs[int(idx)][0]] = name
    except Exception as e:
        log("geo batch failed: %r" % e)
    return out


_probe_locks = {}
_probe_locks_guard = threading.Lock()


def _probe_lock(rel):
    with _probe_locks_guard:
        lock = _probe_locks.get(rel)
        if lock is None:
            lock = threading.Lock()
            _probe_locks[rel] = lock
        return lock


def _meta_complete(info):
    # New entries always carry disp_w+rot (possibly 0); old ones lack rot.
    return bool(info) and "disp_w" in info and "rot" in info


def meta_for(rels):
    """Build {rel: {date, location, duration}} for a list of index paths, cached."""
    global _meta_dirty
    with _lock:
        res = {r: dict(_meta[r]) for r in rels
               if _meta_complete(_meta.get(r))}
    missing = [r for r in rels if r not in res]
    if not missing:
        _kick_bakes(rels)
        return res

    coords = {}
    changed = False
    for rel in missing:
        real = safe_path(rel)
        if real is None:       # reject traversal / missing files without probing
            res[rel] = {"date": "", "duration": "", "disp_w": 0, "disp_h": 0,
                        "rot": 0}
            continue
        # Coalesce concurrent probes of the same file (playlist + on-demand meta).
        with _probe_lock(rel):
            with _lock:
                cached = _meta.get(rel)
                if _meta_complete(cached):
                    res[rel] = dict(cached)
                    continue
            created, iso, dur, dw, dh, rot = ffprobe(rel, real)
            date = pretty_date(rel, created)
            ll = ""
            m = ISO6709_RE.search(iso or "")
            if m:
                ll = "%s %s" % (m.group(1), m.group(2))
            info = {"date": date, "duration": dur, "disp_w": dw, "disp_h": dh,
                    "rot": rot}
            with _lock:
                _meta[rel] = info
            res[rel] = info
            changed = True
            if ll:
                coords[rel] = ll

    if coords:
        for rel, name in batch_locations(coords).items():
            with _lock:
                if rel in _meta:
                    _meta[rel]["location"] = name
                if rel in res:
                    res[rel]["location"] = name
            changed = True

    if changed:
        with _lock:
            _meta_dirty = True
            _save_meta_locked()
    _kick_bakes(rels)
    return res


def _save_meta_locked():
    global _meta_dirty
    if not _meta_dirty:
        return
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_meta, f)
        os.replace(tmp, CACHE)
        _meta_dirty = False
    except OSError as e:
        log("meta save failed: %r" % e)


def load_meta():
    global _meta
    try:
        with open(CACHE) as f:
            data = json.load(f)
        _meta = ({k: v for k, v in data.items()
                  if isinstance(k, str) and isinstance(v, dict)}
                 if isinstance(data, dict) else {})
    except (OSError, ValueError):
        _meta = {}


# ---------------------------------------------------------------- playlist
def read_index():
    try:
        with open(INDEX, "rb") as f:
            data = f.read()
        if data[:3] == b"\xef\xbb\xbf":
            data = data[3:]
        return [l.decode("utf-8", "replace").strip() for l in data.replace(b"\r\n", b"\n").split(b"\n") if l.strip()]
    except OSError:
        return []


def read_favorites():
    """[(label, path, note), ...]"""
    out = []
    try:
        with open(FAV, "rb") as f:
            data = f.read()
        if data[:3] == b"\xef\xbb\xbf":
            data = data[3:]
        for line in data.replace(b"\r\n", b"\n").split(b"\n"):
            line = line.decode("utf-8", "replace").rstrip("\n")
            if not line.strip():
                continue
            parts = line.split("\t")
            label = parts[0].strip() if len(parts) > 0 else ""
            path = parts[1].strip() if len(parts) > 1 else ""
            note = parts[2].strip() if len(parts) > 2 else ""
            if path:
                out.append((label, path, note))
    except OSError:
        pass
    return out


# ------------------------------------------------- Smart Hub Preview (Tizen)
# The launcher fetches a JSON file when its app tile is hovered/focused and
# renders "recommended" strips above the bottom row; clicking a tile launches
# the app with that tile's action_data as an ApplicationControlData (PAYLOAD).

def thumb_path(rel):
    return os.path.join(THUMB_DIR, hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16] + ".jpg")


_thumb_locks = {}
_thumb_locks_guard = threading.Lock()
_thumb_slots = threading.Semaphore(2)   # bound concurrent ffmpeg processes
_thumb_trim_lock = threading.Lock()


def _thumb_lock(rel):
    with _thumb_locks_guard:
        lock = _thumb_locks.get(rel)
        if lock is None:
            lock = threading.Lock()
            _thumb_locks[rel] = lock
        return lock


def _thumb_ok(path):
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def make_thumb(rel):
    """640x360 JPEG (16:9, ≤360KB per Samsung spec), cached in THUMB_DIR."""
    out = thumb_path(rel)
    if _thumb_ok(out):
        return out
    real = safe_path(rel)
    if real is None:
        return None
    # Per-file lock: the launcher double-fetches the same tile, but different
    # tiles must not wait behind each other's ffmpeg run.
    with _thumb_lock(rel):
        if _thumb_ok(out):
            return out
        try:
            os.makedirs(THUMB_DIR, exist_ok=True)
        except OSError as e:
            log("thumb dir failed: %r" % e)
            return None
        tmp = out + ".part"
        err = ""
        try:
            # -ss 3 is nicer than the first frame, but clips shorter than 3s
            # produce no frame at all; retry from the start.
            for ss in ("3", "0"):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                try:
                    with _thumb_slots:
                        proc = subprocess.run(
                            ["ffmpeg", "-y", "-v", "error", "-ss", ss, "-i", real,
                             "-frames:v", "1",
                             "-vf", "scale=640:360:force_original_aspect_ratio=decrease,"
                                    "pad=640:360:(ow-iw)/2:(oh-ih)/2",
                             "-q:v", "4", "-f", "image2", tmp],
                            timeout=90, capture_output=True)
                except Exception as e:
                    err = "%r" % e
                    continue
                if _thumb_ok(tmp):
                    try:
                        os.replace(tmp, out)
                    except OSError as e:
                        err = "%r" % e
                        continue
                    _touch(out)
                    trim_thumbs(out)
                    return out
                err = "ffmpeg rc=%d" % proc.returncode
                if proc.stderr:
                    err += " " + proc.stderr.decode("utf-8", "replace")[-200:].strip()
            log("thumb failed %s: %s" % (rel, err))
            return None
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass


def trim_thumbs(keep=None):
    """LRU-cap the thumbnail cache; never evict the tile just generated."""
    with _thumb_trim_lock:
        try:
            names = [f for f in os.listdir(THUMB_DIR) if f.endswith(".jpg")]
        except OSError:
            return
        files = []
        for name in names:
            path = os.path.join(THUMB_DIR, name)
            try:
                files.append((os.path.getmtime(path), os.path.getsize(path), path))
            except OSError:
                pass
        total = sum(size for _, size, _ in files)
        if total <= THUMB_CAP_BYTES:
            return
        files.sort()
        candidates = [f for f in files if f[2] != keep]
        while total > THUMB_CAP_BYTES and candidates:
            _, size, path = candidates.pop(0)
            try:
                os.remove(path)
            except OSError:
                pass
            total -= size


def preview_candidates():
    """[(title, rel)] for the preview: labeled favorites, then most recent."""
    out = []
    seen = set()
    for label, path, _ in read_favorites():
        if label and path and path not in seen and safe_path(path):
            out.append((label, path))
            seen.add(path)
    dated = sorted((e for e in read_index()
                    if re.match(r"^\d{4}/\d{2}-\d{2}", e) and e not in seen),
                   reverse=True)[:8]
    with _lock:
        cache = dict(_meta)
    for rel in dated:
        info = cache.get(rel) or {}
        title = info.get("date") or os.path.basename(rel)
        if safe_path(rel):
            out.append((title, rel))
            seen.add(rel)
    return out[:14]


HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?"
    r"|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")


def _preview_base(host):
    """Absolute origin for launcher image URLs; ignore malformed Host headers."""
    host = (host or "").strip()
    if host and HOST_RE.match(host):
        return "http://" + host
    if PREVIEW_URL:
        return PREVIEW_URL if "://" in PREVIEW_URL else "http://" + PREVIEW_URL
    return ""


def preview_json(base=""):
    """Smart Hub Preview JSON. base = absolute origin of the preview request —
    Samsung's launcher does NOT resolve relative image_url values (strip never
    renders); the sample JSON uses absolute URLs."""
    # TEST PHASE: minimal known-good payload — 1 section, 2 tiles, exact sample
    # schema. Once the strip renders, expand back to full favorites/recent.
    if not base:
        base = _preview_base("")
    tiles = []
    for label, rel in preview_candidates()[:2]:
        # cached metadata only: the launcher's fetch must return fast; missing
        # entries just show an empty subtitle (meta gets probed on playback)
        with _lock:
            info = dict(_meta.get(rel) or {})
        tiles.append({
            "title": label,
            "subtitle": info.get("date", ""),
            "image_url": base + "/api/thumb?p=" + quote(rel, safe=""),
            "image_ratio": "16by9",
            "is_playable": True,
            "action_data": json.dumps({"path": rel}),
        })
    sections = []
    if tiles:
        sections.append({"title": "Family Videos", "tiles": tiles})
    # refresh at most every 6h (spec caps at 1 week; short keeps it fresh)
    return {"expires": int(time.time()) + 6 * 3600, "sections": sections}


def warm_previews():
    """Pre-generate thumbnails so the launcher's first fetch never stalls."""
    for _, rel in preview_candidates():
        try:
            make_thumb(rel)
        except Exception:
            pass
    log("preview thumbs warmed")


def write_favorite(action, path, label, note):
    """Thread-safe favorites.tsv update. Returns (ok, message)."""
    action = str(action or "")
    path = _tsv_safe(path or "")
    label = _tsv_safe(label or "").strip()
    note = _tsv_safe(note or "").strip()
    if not path:
        return False, "missing path"
    label = label or os.path.splitext(os.path.basename(path))[0][:40]
    with _lock:
        favs = read_favorites()
        if action == "add":
            favs = [(l, p, n) for (l, p, n) in favs if p != path]
            favs.append((label, path, note))
        elif action == "remove":
            favs = [(l, p, n) for (l, p, n) in favs if p != path]
        else:
            return False, "unknown action"
        tmp = FAV + ".tmp"
        try:
            os.makedirs(os.path.dirname(FAV), exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(("\n".join("%s\t%s\t%s" % (l, p, n) for l, p, n in favs) + "\n").encode("utf-8"))
            os.replace(tmp, FAV)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            log("favorite write failed: %r" % e)
            return False, "write failed"
    return True, "ok"


def build_playlist(qs):
    mode = (qs.get("mode", ["random"])[0] or "random").lower()
    entries = read_index()
    picks = []
    label = qs.get("label", [""])[0]
    arg = qs.get("arg", [""])[0]
    date = qs.get("date", [""])[0]
    try:
        n = max(1, min(200, int(qs.get("n", ["25"])[0])))
    except (ValueError, TypeError):
        n = 25

    favs = read_favorites()
    fav_paths = set(p for _, p, _ in favs)
    if mode == "favorites":
        picks = [p for _, p, _ in favs]
    elif label:
        picks = [p for l, p, _ in favs if l == label]
        if not picks:
            picks = sorted(e for e in entries if label in e)
    elif date:
        picks = sorted(e for e in entries if date in e)
    elif arg:
        picks = [p for l, p, _ in favs if l == arg]
        if not picks:
            picks = sorted(e for e in entries if arg in e)
    else:  # random
        import random
        random.shuffle(entries)
        picks = entries[:n]

    picks = [p for p in picks if p and not p.startswith("#")]
    # Instant response: only cached meta goes inline; per-item meta is fetched
    # on demand by the client via /api/meta when each video starts.
    with _lock:
        cache = dict(_meta)
    out = []
    ann = annotations()
    for rel in picks:
        info = cache.get(rel, {})
        kids = info.get("kids")
        if kids is None:            # older cache entries lack the field
            kids = kids_for_date(info.get("date", ""))
        out.append({
            "path": rel,
            "title": os.path.splitext(os.path.basename(rel))[0],
            "date": info.get("date", ""),
            "location": info.get("location", ""),
            "duration": info.get("duration", ""),
            "kids": kids,
            "note": ann.get(rel, ""),
            "favorite": rel in fav_paths,
        })
    return out


# ---------------------------------------------------------------- HTTP
def safe_path(rel):
    """Resolve an index-relative path, refusing anything outside MOUNT.

    Callers pass an already-decoded value (parse_qs/JSON decode exactly once);
    do not unquote here or literal '%'-containing filenames break.
    """
    if not isinstance(rel, str):
        return None
    rel = rel.lstrip("/")
    try:
        root = os.path.realpath(MOUNT)
        real = os.path.realpath(os.path.join(MOUNT, rel))
        if real == root or not real.startswith(root + os.sep):
            return None
        if not os.path.isfile(real):
            return None
        return real
    except (OSError, ValueError):
        return None


RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 65   # don't leak threads on dead/stalled keep-alive connections

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        log("http: %s %s" % (self.client_address[0], format % args))

    # -- helpers ------------------------------------------------------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")

    def send_json(self, obj, code=200, no_store=False):
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if no_store:
                self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.close_connection = True

    def read_json_body(self, limit=MAX_BODY):
        """Read a small JSON object body; None means malformed/oversized."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            self.close_connection = True
            return None
        if length < 0 or length > limit:
            self.close_connection = True   # don't reuse a socket with unread body
            return None
        try:
            raw = self.rfile.read(length)
        except OSError:
            return None
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def do_OPTIONS(self):  # CORS preflight (dispatched by BaseHTTPRequestHandler)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_HEAD(self):
        # the launcher may probe resources with HEAD; stdlib answers 501
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/api/preview":
            host = self.headers.get("Host", "")
            try:
                body = json.dumps(preview_json(_preview_base(host))).encode("utf-8")
            except Exception:
                body = b"{}"
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
            except OSError:
                self.close_connection = True
            return
        if parsed.path == "/api/thumb":
            rel = qs.get("p", [""])[0]
            out = make_thumb(rel)
            size = len(PLACEHOLDER_JPEG)
            if out is not None:
                try:
                    size = os.path.getsize(out)
                except OSError:
                    pass
            try:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "max-age=604800")
                self._cors()
                self.end_headers()
            except OSError:
                self.close_connection = True
            return
        if parsed.path == "/api/video":
            real = safe_path(qs.get("p", [""])[0])
            if real is None:
                try:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self._cors()
                    self.end_headers()
                except OSError:
                    self.close_connection = True
                return
            return self._video_response(real, head_only=True)
        try:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()
        except OSError:
            self.close_connection = True

    # -- routes -------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/health":
            return self.send_json({"ok": True, "mount": mount_ok()})

        if path == "/api/playlist":
            ensure_mount()
            try:
                return self.send_json({"items": build_playlist(qs)})
            except Exception as e:
                log("playlist error: %r" % e)
                return self.send_json({"error": str(e)}, 500)

        if path == "/api/meta":
            ensure_mount()
            rel = qs.get("p", [""])[0]
            info = meta_for([rel]).get(rel)
            info = dict(info or {"date": "", "location": "", "duration": ""})
            info["kids"] = info.get("kids") or kids_for_date(info.get("date", ""))
            info["note"] = annotations().get(rel, "")
            return self.send_json(info)

        if path == "/api/preview":
            # Smart Hub Preview: the LAUNCHER (not the app) fetches this —
            # log it so we can see the TV pull the strip. Absolute image URLs
            # are built from the Host the TV used to reach us.
            host = self.headers.get("Host", "")
            log("preview fetch from %s (host=%s ua=%r accept=%r)"
                % (self.client_address[0], host,
                   (self.headers.get("User-Agent") or "")[:40],
                   self.headers.get("Accept")))
            ensure_mount()
            return self.send_json(preview_json(_preview_base(host)), no_store=True)

        if path == "/api/thumb":
            rel = qs.get("p", [""])[0]
            real = safe_path(rel)
            # Full request forensics: the launcher gets a different result than
            # identical local fetches — see exactly what it sends.
            log("thumb req ip=%s rel=%r safe=%s ua=%s hdrs={Range:%r Accept:%r Host:%r}"
                % (self.client_address[0], rel[:60], real is not None,
                   (self.headers.get("User-Agent") or "")[:40],
                   self.headers.get("Range"), self.headers.get("Accept"),
                   self.headers.get("Host")))
            out = make_thumb(rel)   # validates the path internally
            body = PLACEHOLDER_JPEG
            if out is not None:
                try:
                    with open(out, "rb") as f:
                        body = f.read()
                    _touch(out)
                except OSError:
                    body = PLACEHOLDER_JPEG   # cache entry vanished mid-request
            try:
                # never 404: a failed tile must not abort the launcher's strip
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=604800")
                self._cors()
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self.close_connection = True
            return

        if path == "/api/favorites":
            return self.send_json({"items": [
                {"label": l, "path": p, "note": n} for l, p, n in read_favorites()]},
                no_store=True)

        if path == "/api/video":
            rel = qs.get("p", [""])[0]
            real = safe_path(rel)
            if real is None:
                return self.send_json({"error": "not found"}, 404)
            global _last_video_hit
            _last_video_hit = time.time()
            wol()  # cheap; keeps HP awake while the family watches
            # Rotation-baked playback: rotated iPhone files render as a tile on
            # the TV plane — serve the baked (portrait-pixel) copy when ready.
            use = real
            try:
                if needs_bake(rel):
                    out = _bake_ready(rel)
                    if out:
                        use = out
                        _touch(out)
                    elif _bake_is_active(rel):
                        for _ in range(24):        # warmed by prefetch: wait ≤12s
                            time.sleep(0.5)
                            out = _bake_ready(rel)
                            if out:
                                use = out
                                _touch(out)
                                break
                        else:
                            log("bake wait timed out: %s" % rel)
                    elif not _bake_blocked(rel):
                        threading.Thread(target=bake_video, args=(rel,),
                                         daemon=True).start()
                        log("baking on demand: %s (serving original)" % rel)
            except Exception as e:
                log("bake routing error %r" % e)
            return self.stream_video(use)

        if APP_DIR:
            if path == "/" or path == "/index.html":
                return self.static_file("index.html")
            if path.startswith("/api/"):
                return self.send_json({"error": "unknown endpoint"}, 404)
            return self.static_file(path.lstrip("/"))
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/log":
            # TV app breadcrumb endpoint: {"msg": "..."} -> app log line
            body = self.read_json_body()
            if body is not None:
                log("tv: %s" % str(body.get("msg", "?"))[:2000])
            return self.send_json({"ok": True})
        if parsed.path == "/api/note":
            # {"path": rel, "note": text} — set/clear an annotation
            body = self.read_json_body()
            if body is None:
                return self.send_json({"error": "bad request"}, 400)
            real = safe_path(body.get("path", ""))
            if real is None:
                return self.send_json({"error": "bad path"}, 400)
            try:
                # store keys in the same relative form meta/playlist use
                rel = os.path.relpath(real, os.path.realpath(MOUNT))
                set_annotation(rel, body.get("note", ""))
            except OSError as e:
                return self.send_json({"error": str(e)}, 400)
            return self.send_json({"ok": True})
        if parsed.path != "/api/favorite":
            return self.send_json({"error": "not found"}, 404)
        body = self.read_json_body()
        if body is None:
            return self.send_json({"error": "bad request"}, 400)
        ok, msg = write_favorite(
            body.get("action", "add"), body.get("path", ""),
            body.get("label", ""), body.get("note", ""))
        return self.send_json({"ok": ok, "message": msg}, 200 if ok else 400)

    # -- streaming ----------------------------------------------------
    def stream_video(self, real):
        self._video_response(real, head_only=False)

    def _video_response(self, real, head_only=False):
        try:
            size = os.path.getsize(real)
        except OSError:
            return self.send_json({"error": "not found"}, 404)
        log("video: %s range=%s size=%d" % (os.path.basename(real),
            (self.headers.get("Range") or "-")[:80], size))
        ctype = "video/mp4"
        ext = os.path.splitext(real)[1].lower()
        # NOTE: serve .mov as video/mp4 — the quicktime MIME makes browsers
        # (including Tizen's) refuse the stream even though iPhone .mov files
        # are ISO-BMFF and decode fine as mp4.
        if ext in (".webm",):
            ctype = "video/webm"
        elif ext in (".mkv",):
            ctype = "video/x-matroska"

        start, end = 0, size - 1
        code = 200
        rng = self.headers.get("Range")
        if rng:
            m = RANGE_RE.match(rng.strip())
            if m and (m.group(1) or m.group(2)):
                bad = False
                try:
                    if m.group(1):
                        start = int(m.group(1))
                        if m.group(2):
                            end = min(int(m.group(2)), size - 1)
                    else:  # suffix range: last N bytes
                        start = max(0, size - int(m.group(2)))
                    if start > end or start >= size:
                        bad = True
                except (ValueError, OverflowError):
                    bad = True
                if bad:
                    try:
                        self.send_response(416)
                        self.send_header("Content-Range", "bytes */%d" % size)
                        self.send_header("Content-Length", "0")
                        self.send_header("Accept-Ranges", "bytes")
                        self._cors()
                        self.end_headers()
                    except OSError:
                        self.close_connection = True
                    return
                code = 206

        length = end - start + 1
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if code == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
        except OSError:
            self.close_connection = True
            return
        if head_only:
            return

        try:
            with open(real, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True   # client seek/stop — normal
        except OSError as e:
            log("stream failed %s: %r" % (os.path.basename(real), e))
            self.close_connection = True

    def static_file(self, rel):
        base = os.path.realpath(APP_DIR)
        real = os.path.realpath(os.path.join(APP_DIR, rel))
        if not real.startswith(base + os.sep) or not os.path.isfile(real):
            return self.send_json({"error": "not found"}, 404)
        exts = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".svg": "image/svg+xml", ".ico": "image/x-icon",
                ".json": "application/json", ".txt": "text/plain",
                ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf"}
        ctype = exts.get(os.path.splitext(real)[1].lower(), "application/octet-stream")
        try:
            with open(real, "rb") as f:
                body = f.read()
        except OSError:
            return self.send_json({"error": "not found"}, 404)
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.close_connection = True


def main():
    load_meta()
    threading.Thread(target=keepalive_loop, daemon=True).start()
    threading.Thread(target=warm_previews, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log("listening on :%d (app dir: %s)" % (PORT, APP_DIR or "-"))
    srv.serve_forever()


if __name__ == "__main__":
    main()
