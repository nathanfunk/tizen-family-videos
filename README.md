# tizen-family-videos

A family-video player for 2016–2019 Samsung smart TVs (Tizen 3/4), built for a TV
that refuses to let you sideload "properly". The TV app plays a playlist streamed
over HTTP from a Raspberry Pi backend — with a remote-controllable queue,
favorites, date/location overlays, and automatic handling of iPhone portrait
videos that Tizen's video plane renders wrong.

The app is deliberately simple. The interesting parts are the **[docs/](docs/)**,
which document the undocumented: installing and signing custom web apps on a
*retail* Tizen 4.0 TV with no Samsung developer account, and the video-plane
quirks (mis-applied `object-fit`, rotation-metadata tiles, style-write glitches)
we had to reverse-engineer along the way.

## Hardware this was built on

- **TV**: Samsung UN50NU7100 (2018, Tizen 4.0), retail firmware, Developer Mode enabled
- **Backend**: Raspberry Pi 4 (Debian 13), stdlib-only Python server
- **Storage**: a read-only SMB share on a desktop PC (the video archive never leaves the LAN)

## Pieces

- `tvapp/` — the Tizen web app (ES5 + XHR only, per Tizen 4.0 rules). Channel
  Up/Down = prev/next video, Enter = pause, Left/Right = seek ±10s, Down = action
  menu, transport Rewind/FF = jump prev/next. Server discovery tries a hostname
  then fallback IPs on the LAN.
- `appserver/server.py` — Pi backend (systemd unit, port 8123, stdlib only):
  `/api/playlist`, `/api/video` (HTTP Range), `/api/meta`, `/api/favorite(s)`,
  `/api/note`, `/api/health`. Metadata comes from ffprobe (embedded creation
  dates, GPS reverse-geocoded **offline** — family coordinates never leave the
  LAN). Rotated videos are transparently transcoded once ("baked") with the Pi's
  hardware h264 encoder. WOL keeps the storage PC awake while serving.
- `deploy.sh` + `scripts/sign_famvids3.py` — the sign → install → relaunch loop
  (see [docs/tizen4-retail-install.md](docs/tizen4-retail-install.md)).

## Quick start

1. Enable Developer Mode on the TV (Apps → type `12345` → Enter).
2. Serve `tvapp/` from any LAN web server, and run the backend on the Pi.
3. Package and install: see the docs — either Apps2Samsung (GUI, easiest) or the
   Tizen CLI loop in this repo (`deploy.sh`; fill in `deploy.local.sh` from
   `deploy.local.sh.example`).

## Docs

- **[Installing custom apps on a retail Tizen 4.0 TV](docs/tizen4-retail-install.md)** —
  error 118019 ("Parsing error") decoded, signing without a Samsung certificate,
  retail `sdb` limits, the deploy script pattern.
- **[Tizen 4.0 video playback quirks](docs/tizen4-video-playback.md)** — the
  hardware video plane ignores `object-fit`, renders rotation-metadata streams as
  a tile, glitches on no-op style writes, and how to debug a TV with no console.

## Privacy

Nothing leaves the home network: the archive is streamed from LAN storage, and
GPS coordinates are reverse-geocoded on-device with an offline geocoder.

## License

[MIT](LICENSE)
