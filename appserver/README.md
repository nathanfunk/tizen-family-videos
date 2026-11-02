# appserver — Pi backend

Stdlib-only Python 3 server for the TV app. Runs anywhere on the LAN that can
read your video archive (we run it on a Raspberry Pi 4 against a read-only SMB
mount).

## Run

```
python3 server.py
```

Environment variables (all optional):

| var | default | meaning |
|---|---|---|
| `FV_MOUNT` | `/mnt/family-media` | root of the video archive (served paths are confined here) |
| `FV_INDEX` | `~/projects/family-videos/index.txt` | cached playlist index (one relative path per line) |
| `FV_FAVORITES` | `~/projects/family-videos/favorites.tsv` | `label<TAB>path<TAB>note` favorites |
| `FV_PORT` | `8123` | listen port |
| `FV_LOG` | `/tmp/family-videos-app.log` | log file (also receives the TV app's breadcrumbs) |
| `FV_HP_MAC` | *(empty)* | storage-PC MAC for wake-on-LAN keep-alive; empty disables WOL |
| `FV_APP_DIR` | *(empty)* | set to a `tvapp/` dir to also serve it for desktop testing |

systemd unit (example):

```ini
[Service]
ExecStart=/usr/bin/python3 /opt/family-videos/server.py
Environment=FV_PORT=8123
Environment=FV_HP_MAC=aa:bb:cc:dd:ee:ff
Restart=always
RestartSec=5
```

## Endpoints

- `GET /api/playlist?mode=random&n=25` (also `mode=favorites`, `label=<lbl>`, `date=<path-prefix>`)
- `GET /api/video?p=<index-relative-path>` — HTTP Range streaming; rotated
  files are served from a rotation-baked cached transcode when available
- `GET /api/meta?p=<path>` — date, duration, location (offline reverse
  geocoding), kids' ages, note, display dimensions
- `POST /api/favorite` `{"action":"add|remove","path":...,"label":...}`
- `POST /api/note` `{"path":...,"note":...}`
- `GET /api/favorites`, `GET /api/health`
- `POST /api/log` `{"msg":...}` — the TV app's breadcrumb endpoint

The meta cache lives at `~/.cache/family-videos-meta.json`; baked (rotation-
fixed) transcodes at `~/.cache/family-videos-bake/` with a 2GB LRU cap.
