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
  GET  /api/favorites                            list
  POST /api/favorite   {"action":"add|remove","path":...,"label":...,"note":...}
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
from urllib.parse import parse_qs, unquote, urlparse

MOUNT = os.environ.get("FV_MOUNT", "/mnt/family-media")
INDEX = os.environ.get("FV_INDEX", os.path.expanduser("~/projects/family-videos/index.txt"))
FAV = os.environ.get("FV_FAVORITES", os.path.expanduser("~/projects/family-videos/favorites.tsv"))
GEO_PY = os.path.expanduser("~/projects/family-videos/geo-venv/bin/python")
CACHE = os.path.expanduser("~/.cache/family-videos-meta.json")
BAKE_DIR = os.path.expanduser("~/.cache/family-videos-bake")
BAKE_CAP_BYTES = 2 * 1024 * 1024 * 1024   # LRU cap for baked files
APP_DIR = os.environ.get("FV_APP_DIR", "")  # set in dev to serve the tvapp dir
PORT = int(os.environ.get("FV_PORT", "8123"))
LOG = os.environ.get("FV_LOG", "/tmp/family-videos-app.log")
# WOL target MAC (env override; empty disables WOL)
HP_MAC = os.environ.get("FV_HP_MAC", "")

_lock = threading.Lock()
_meta = {}
_meta_dirty = False

_last_video_hit = 0.0


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write("%s fv-app: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), msg))
    except OSError:
        pass


# ---------------------------------------------------------------- HP wake
def wol():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        magic = b"\xff" * 6 + bytes.fromhex(HP_MAC.replace(":", "")) * 16
        for port in (9, 7):
            s.sendto(magic, ("10.0.0.255", port))
        s.close()
    except OSError:
        pass


def mount_ok():
    try:
        return os.path.isdir(MOUNT) and len(os.listdir(MOUNT)) > 0
    except OSError:
        return False


def ensure_mount():
    if mount_ok():
        return True
    log("mount down -> sudo mount")
    subprocess.run(["sudo", "mount", MOUNT], capture_output=True, timeout=60)
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

def annotations():
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

def set_annotation(rel, text):
    """Add/update/remove an annotation for rel; text '' removes it."""
    ann = annotations()
    if text:
        ann[rel] = text
    else:
        ann.pop(rel, None)
    lines = ["# path<TAB>annotation (one per video)"]
    lines += ["%s\t%s" % (p, t) for p, t in sorted(ann.items())]
    tmp = ANN_TSV + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, ANN_TSV)
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


def ffprobe(rel):
    """One ffprobe call per file: (creation_time, iso6709, duration, disp_w, disp_h).

    disp_w/disp_h are ROTATION-AWARE display dimensions: iPhone portrait videos
    are often encoded 1280x720 (etc.) with a rotate/rotation side-data of ±90 —
    the browser reports the encoded size, so the client needs the true display
    size from here to letterbox correctly."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:format_tags=creation_time,com.apple.quicktime.location.ISO6709"
             ":stream=width,height:stream_side_data=rotation",
             "-of", "json", os.path.join(MOUNT, rel)],
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
_bake_slots = threading.Semaphore(2)   # hardware encoder: serialize bakes

def bake_video(rel):
    """Transcode rel with rotation baked in; returns baked path or None."""
    out = bake_path(rel)
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    with _bake_lock:
        if _bake_active.get(rel):
            return None        # another thread is baking it
        _bake_active[rel] = True
    try:
        os.makedirs(BAKE_DIR, exist_ok=True)
        tmp = out + ".part"
        try:
            with _bake_slots:
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "error",
                     "-i", os.path.join(MOUNT, rel),
                     "-c:v", "h264_v4l2m2m", "-b:v", "6M",
                     "-c:a", "copy", "-movflags", "+faststart",
                     "-f", "mp4", tmp],
                    timeout=1800, capture_output=True)
            if os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, out)
                trim_bakes()
                log("baked: %s -> %s" % (rel, os.path.getsize(out)))
                return out
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        log("bake FAILED: %s" % rel)
        return None
    finally:
        with _bake_lock:
            _bake_active.pop(rel, None)

def trim_bakes():
    try:
        files = [os.path.join(BAKE_DIR, f) for f in os.listdir(BAKE_DIR)
                 if f.endswith(".mp4")]
        total = sum(os.path.getsize(f) for f in files)
        if total <= BAKE_CAP_BYTES:
            return
        files.sort(key=os.path.getmtime)
        for f in files:
            if total <= BAKE_CAP_BYTES:
                break
            total -= os.path.getsize(f)
            os.remove(f)
    except OSError:
        pass

def _kick_bakes(rels):
    for r in rels:
        try:
            m = _meta.get(r)
            if not (m and m.get("rot")):
                continue
            if os.path.isfile(bake_path(r)):
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


def meta_for(rels):
    """Build {rel: {date, location, duration}} for a list of index paths, cached."""
    global _meta_dirty
    with _lock:
        # entries missing disp_w/rot predate rotation-aware probing: re-probe them
        res = {r: dict(_meta[r]) for r in rels
               if r in _meta and _meta[r].get("disp_w") and "rot" in _meta[r]}
    missing = [r for r in rels if r not in res]
    if not missing:
        _kick_bakes(rels)
        return res

    coords = {}
    for rel in missing:
        created, iso, dur, dw, dh, rot = ffprobe(rel)
        date = pretty_date(rel, created)
        ll = ""
        m = ISO6709_RE.search(iso or "")
        if m:
            ll = "%s %s" % (m.group(1), m.group(2))
        res[rel] = {"date": date, "duration": dur, "disp_w": dw, "disp_h": dh,
                    "rot": rot}
        if ll:
            coords[rel] = ll

    if coords:
        for rel, name in batch_locations(coords).items():
            res[rel]["location"] = name

    with _lock:
        for rel, info in res.items():
            _meta[rel] = info
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
            _meta = json.load(f)
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


def is_favorite(path):
    return any(p == path for _, p, _ in read_favorites())


def write_favorite(action, path, label, note):
    """Thread-safe favorites.tsv update. Returns (ok, message)."""
    label = (label or "").strip() or os.path.splitext(os.path.basename(path))[0][:40]
    note = (note or "").strip()
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
    except ValueError:
        n = 25

    if mode == "favorites":
        picks = [p for _, p, _ in read_favorites()]
    elif label:
        picks = [p for l, p, _ in read_favorites() if l == label]
        if not picks:
            picks = sorted(e for e in entries if label in e)
    elif date:
        picks = sorted(e for e in entries if date in e)
    elif arg:
        picks = [p for l, p, _ in read_favorites() if l == arg]
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
            "favorite": is_favorite(rel),
        })
    return out


# ---------------------------------------------------------------- HTTP
def safe_path(rel):
    rel = unquote(rel).lstrip("/")
    root = os.path.realpath(MOUNT)
    real = os.path.realpath(os.path.join(MOUNT, rel))
    if real == root or not real.startswith(root + os.sep):
        return None
    if not os.path.isfile(real):
        return None
    return real


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        log("http: %s %s" % (self.client_address[0], format % args))

    # -- helpers ------------------------------------------------------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # CORS preflight (dispatched by BaseHTTPRequestHandler)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

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
            rel = unquote(qs.get("p", [""])[0])
            info = meta_for([rel]).get(rel)
            info = dict(info or {"date": "", "location": "", "duration": ""})
            info["kids"] = info.get("kids") or kids_for_date(info.get("date", ""))
            info["note"] = annotations().get(rel, "")
            return self.send_json(info)

        if path == "/api/favorites":
            return self.send_json({"items": [
                {"label": l, "path": p, "note": n} for l, p, n in read_favorites()]})

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
                    out = bake_path(rel)
                    if os.path.isfile(out) and os.path.getsize(out) > 0:
                        use = out
                    elif _bake_active.get(rel):
                        for _ in range(24):        # warmed by prefetch: wait ≤12s
                            time.sleep(0.5)
                            if os.path.isfile(bake_path(rel)):
                                use = out
                                break
                        else:
                            log("bake wait timed out: %s" % rel)
                    else:
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
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                log("tv: %s" % body.get("msg", "?"))
            except (ValueError, OSError):
                pass
            return self.send_json({"ok": True})
        if parsed.path == "/api/note":
            # {"path": rel, "note": text} — set/clear an annotation
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                real = safe_path(body.get("path", ""))
                if real is None:
                    return self.send_json({"error": "bad path"}, 400)
                # store keys in the same relative form meta/playlist use
                rel = os.path.relpath(real, os.path.realpath(MOUNT))
                set_annotation(rel, str(body.get("note", "")).strip())
                return self.send_json({"ok": True})
            except (ValueError, OSError) as e:
                return self.send_json({"error": str(e)}, 400)
        if parsed.path != "/api/favorite":
            return self.send_json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return self.send_json({"error": "bad request"}, 400)
        ok, msg = write_favorite(
            body.get("action", "add"), body.get("path", ""),
            body.get("label", ""), body.get("note", ""))
        return self.send_json({"ok": ok, "message": msg}, 200 if ok else 400)

    # -- streaming ----------------------------------------------------
    def stream_video(self, real):
        size = os.path.getsize(real)
        log("video: %s range=%s size=%d" % (os.path.basename(real), self.headers.get("Range", "-"), size))
        ctype = "video/mp4"
        ext = os.path.splitext(real)[1].lower()
        # NOTE: serve .mov as video/mp4 — the quicktime MIME makes browsers
        # (including Tizen's) refuse the stream even though iPhone .mov files
        # are ISO-BMFF and decode fine as mp4.
        if ext in (".webm",):
            ctype = "video/webm"
        elif ext in (".mkv",):
            ctype = "video/x-matroska"

        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:  # suffix range: last N bytes
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206

        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()

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
        except (BrokenPipeError, ConnectionResetError):
            pass  # client seek/stop — normal

    def static_file(self, rel):
        base = os.path.realpath(APP_DIR)
        real = os.path.realpath(os.path.join(APP_DIR, rel))
        if not real.startswith(base + os.sep) or not os.path.isfile(real):
            return self.send_json({"error": "not found"}, 404)
        exts = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                ".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json"}
        ctype = exts.get(os.path.splitext(real)[1], "application/octet-stream")
        with open(real, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    load_meta()
    threading.Thread(target=keepalive_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log("listening on :%d (app dir: %s)" % (PORT, APP_DIR or "-"))
    srv.serve_forever()


if __name__ == "__main__":
    main()
