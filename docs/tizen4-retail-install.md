# Installing custom apps on a retail Tizen 4.0 TV (Samsung UN50NU7100)

Everything below was learned the hard way on a 2018 UN50NU7100 on retail
firmware with Developer Mode enabled. Most of it is documented nowhere else.

## Developer Mode

- Enable: on the TV, open **Apps**, type `12345`, press Enter, toggle Developer
  Mode on, set the **Host PC IP** to the PC that will install.
- Verify: port **26101** open on the TV (that's `sdb`), 8002/8001 for the
  WebSocket control API.

## Retail `sdb` limits

- `sdb shell` is blocked — every command prints `closed`, rc=1. `/proc` file
  pulls return 0 bytes. Do not waste time diagnosing storage or processes via
  `sdb shell`.
- What works: `sdb connect/devices`, `sdb push/pull` (app partition), and app
  installs.
- `sdb` allows ONE host connection. If `sdb connect` is refused while the TV is
  otherwise reachable, another tool (Apps2Samsung, TizenBrewInstaller) is
  holding the slot — close it first.
- The `sdb` server on your machine dies between sessions: run
  `sdb connect <TV IP>` before any install or you get "There is no connected
  target".

## Error 118019 ("Parsing error") — it's the manifest shape

`118019` (a.k.a. `118,-19`) on Tizen 4.0 means the installer could not parse
`config.xml` — a MANIFEST-SHAPE failure, not a privileges/certs/app-id problem.
It fails instantly with no `installing[N]` progress steps. The fix is to mirror
the manifest shape used by apps that are known to install (TizenBrew):

```xml
<tizen:profile name="tv-samsung"/>   <!-- NOT "tv" -->
<tizen:application id="XXXXXXXXXX.famvids" package="XXXXXXXXXX" required_version="3.0"/>
<!-- package = bare 10-char token; app id = package.suffix; required_version 3.0 -->
<widget id="https://example.local">   <!-- bare URL, no path suffix -->
<feature name="http://tizen.org/feature/screen.size.all"/>
<access origin="*" subdomains="true"/>
<tizen:setting hwkey-event="enable" .../>
```

Privileges that the old folklore calls "restricted" (e.g. `application.launch`,
`internet`) install fine. The real culprit was the manifest shape.

## Signing WITHOUT a Samsung developer account (Tizen 4.0)

Tizen 7+ demands a Samsung distributor certificate. Tizen 4.0 does not:

1. Create an author cert with the Tizen Studio CLI:
   `tizen.sh certificate -a <name> -n "Your Name" -p <pw> -c US ...`
   → `~/tizen-studio-data/keystore/author/author.p12`
2. Register a security profile:
   ```
   tizen.sh security-profiles add -n <name> \
     -a <ABSOLUTE author.p12> -p <pw> \
     -d <ABSOLUTE distributor.p12> -dp <pw> -- <profiles dir>
   ```
   Use the SDK default distributor:
   `tools/certificate-generator/certificates/distributor/tizen-distributor-signer.p12`
   (password `tizenpkcs12passfordsigner`; openssl needs `-legacy` for it).
   Cert paths MUST be absolute.

### profiles.xml traps

- `tizen cli-config add profiles.profile...` is broken in the CLI — do not use it.
- If `profiles.xml` is missing, profile-add errors with "Not found a file of
  security profiles" — seed it first with `<profiles active="" version="3.1"/>`.
- NEVER hand-write profiles.xml with plaintext passwords: the store keeps
  passwords machine-encrypted, and a hand-written entry makes every signing
  attempt die with "Both an author and a first distributor must be required".
  If you see that error, delete profiles.xml and re-run `security-profiles add`.
- On WSL the keyring roundtrip is broken: blank the `password="..."` attrs in
  profiles.xml before each re-sign so the CLI prompts on the pty instead.

### Non-interactive signing via a pty driver

`security-profiles add` and `package` prompt interactively, and background-pty
answering is flaky. The reliable pattern (see `scripts/sign_famvids3.py`) is a
**foreground** Python pty driver: `pty.fork()` the CLI and answer each prompt in
a state machine as its text appears (Author password → pw; `Yes: (Y), No: (N)`
→ N; Distributor1 password → pw; N again). Pre-feeding all answers up front does
NOT work — the CLI mis-consumes buffered lines.

## Install loop

```
tizen.sh package -t wgt -s <profile> -o out -- tvapp
sdb connect <TV IP>
tizen.sh install -n <file>.wgt -- out
```

- `-o out` resolves relative to the CLI bin dir (`tools/ide/bin/out/`).
- Version-bump the widget on every reinstall attempt — a failed install can
  leave stale package state that only a new version or a new app id clears.
- App/package ids must be lowercase.
- `tizen.sh install ... | tail -3` masks failure (pipe exit status is tail's) —
  grep the output for "successfully installed" before declaring success.
- The TV's own failure popup ("Please wait a moment and try again") is generic;
  take the numeric code from the sdb/Apps2Samsung log line instead.

## Alternatives

- **Apps2Samsung** (free, MIT): GUI that signs + installs custom wgts; it
  creates its own cert set registered to the TV's DUID, reusable for the CLI.
  The WSL AppImage is unreliable under WSLg — use the native Windows build.
- **TizenBrew Installer Desktop**: one-click install of pre-signed apps, no
  account needed.

## Caveats

- A **Smart Hub reset wipes sideloaded apps** — keep the `.wgt` for reinstall.
- The app partition is ~800MB–1GB and stock apps eat most of it. If an install
  fails for space, delete removable apps (firmware-locked ones cannot go).
- Old Tizen MIME gotcha: serve `.mov` files as `video/mp4`, never
  `video/quicktime` — browsers refuse the quicktime MIME even though the h264
  inside decodes fine.

## Debugging without a console

`adb logcat`-style TV debugging doesn't exist here and `sdb shell` is blocked.
The pattern that works: have the web app POST breadcrumbs to your backend
(`POST /api/log` with `{msg}`) — boot steps, keydown codes, media events, a
watchdog on every failure path — and read the log over ssh from the backend.
See [tizen4-video-playback.md](tizen4-video-playback.md) for what to log.
