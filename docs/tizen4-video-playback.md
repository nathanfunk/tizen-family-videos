# Tizen 4.0 video playback quirks (and fixes)

The UN50NU7100's Tizen 4.0 browser composites `<video>` on a hardware plane that
behaves differently from a desktop browser. All of the below was observed and
verified on a real TV; the fixes are in this repo's `tvapp/main.js` and
`appserver/server.py`.

## 1. `object-fit` is unreliable — compute the fit yourself

A full-viewport `<video>` with `object-fit: contain` mis-renders: landscape
videos happened to look fine, but portrait videos rendered small and
top-anchored despite the CSS being exactly right.

**Fix**: on `loadedmetadata`, compute the contain-fit in JS and set explicit
pixel `width/height/left/top` on the element:

```js
var s = Math.min(innerWidth / vw, innerHeight / vh);
el.style.width  = Math.round(vw * s) + 'px';
el.style.height = Math.round(vh * s) + 'px';
el.style.left   = Math.round((innerWidth  - vw * s) / 2) + 'px';
el.style.top    = Math.round((innerHeight - vh * s) / 2) + 'px';
```

Keep `object-fit: contain` in CSS as a fallback for engines that honor it.
Verify with `getBoundingClientRect()` logged at the same event — that's how fit
correctness is checked with no TV console.

## 2. Rotation-metadata streams render as a tile

iPhone portrait videos are usually **encoded landscape** (e.g. 1280x720) with a
±90 rotation in the stream (`side_data_list[].rotation` in ffprobe, or a
`rotate` tag). The TV browser reports the ENCODED size to JS (`videoWidth`), and
the hardware plane renders the rotated stream wrong:

- with a full-screen element: rotated content is center-cropped;
- with a correctly portrait-sized element: the video shows as a small tile.

Meanwhile files that are **natively encoded portrait** (no rotation matrix) play
perfectly. So the fix is to remove the rotation from the equation — bake it into
the pixels server-side, once per file:

```
ffmpeg -y -i rotated.mov -c:v h264_v4l2m2m -b:v 6M -c:a copy \
       -movflags +faststart -f mp4 baked.mp4
```

- ffmpeg's autorotate applies the display matrix during decode, so the output
  pixels are upright and **no rotation metadata is written**.
- The Pi's `h264_v4l2m2m` hardware encoder does ~2.3x realtime for 720p.
- Cache the result per source file, serve the baked copy from the streaming
  endpoint when it exists; while the first bake is in flight, serve the
  original (it glitches on first play, replays clean).
- Serialize bakes: `v4l2m2m` is a single hardware instance — parallel encodes
  fail.
- `-f mp4` is required if the temp output has a non-media extension (`.part`
  fails muxer selection instantly).

Probing rotation (what the server does per file):

```
ffprobe -v error -show_entries stream=width,height \
        -show_entries stream_side_data=rotation -of json FILE
```

swap width/height for display when `rotation % 180 == 90`.

## 3. Style writes can glitch the plane

Applying inline styles to the video element mid-playback can re-composite the
hardware plane badly — we saw one video render as a tiny noise square mid-play.

**Fixes**: size the element BEFORE `src` is set (when dimensions are already
known from metadata), skip no-op writes (track the last applied box and return
early if identical), and re-fit on every `playing` event to self-heal.

## 4. Key codes

Return = 10009 (also 461 on some maps), Rewind = 412, FastForward = 417,
Down = 40. `tizenhwkey` also fires with `e.keyName === 'Return'`. Documented
codes may not match the actual remote: log every keydown server-side before
trusting a mapping. Also note: driving the app programmatically over the
WebSocket (`send-key`) can fail with `ConnectionFailure: touchDisable` even when
`app-run`/`app-status` work on the same connection — a sent key is only proven
delivered if a `key <code>` breadcrumb shows up in your log.

## 5. Debugging checklist (no console available)

Log from the app to your backend (`POST /api/log`):

- boot: version + `navigator.userAgent` tail (tells you the engine class)
- server discovery steps and timings
- every keydown code
- media events: `loadedmetadata` (with `videoWidth/videoHeight`, viewport size,
  pre-fit `getBoundingClientRect`), `playing` (with `currentTime`), `waiting`,
  `stalled`, `error` (with `error.code`)
- a 25s watchdog if no `playing` ever fires (log `readyState`/`networkState`)

Streaming side: verify the backend serves correct `206` Range responses with
curl before blaming the TV. Headless test browsers often have NO media stack —
even a known-good mp4 stalls at `readyState 0` — so real decode can only be
verified on the TV itself.
