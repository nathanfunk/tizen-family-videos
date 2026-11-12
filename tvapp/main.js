/* Family Videos — TV player (Tizen 4.0 web app). v2: instrumented boot.
 * Rules per samsung-tv-control skill: ES5-era JS, XHR only (no fetch), no
 * classList / padStart / Promises. Remote keys arrive as keydown codes:
 *   427/428 = CHUP/CHDOWN (prev/next video), 13 Enter = pause,
 *   37/39 Left/Right = seek 10s, 38 Up = favorite, 10009 Return = exit.
 * Every boot step and media event is reported to the Pi (/api/log) so boot
 * progress is visible server-side; all errors also show on screen.
 */

var SERVERS = ['http://rpi:8123'];   // add your Pi's LAN IP(s) here if mDNS isn't available

var g = {
  server: '',          // winning base URL
  items: [],           // playlist [{path,title,date,location,duration,favorite}]
  idx: -1,
  hudTimer: null,
  statusTimer: null,
  seekBusy: false,
  exitDone: false,
  bootT: 0,            // Date.now() at boot
  playSeen: false,     // first 'playing' event since last src change
  watchdog: null,
  skipTimer: null,
  pendingNext: false,
  pendingLaunch: null,
  loadToken: 0,
  lastStallLog: 0
};

function $(id) { return document.getElementById(id); }

function esc(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function safeDecode(value) {
  try { return decodeURIComponent(value); } catch (e) { return value; }
}

/* ---------------------------------------------------------------- logging */
// NOTE: text/plain body keeps these "simple" CORS requests (no preflight),
// which the old Tizen engine handles more reliably. The server reads the
// JSON payload regardless of the request Content-Type.
function tvlog(msg) {
  tvlogAt(g.server || 'http://rpi:8123', msg);
}

function tvlogAt(base, msg) {
  try {
    var x = new XMLHttpRequest();
    x.open('POST', base + '/api/log', true);
    x.setRequestHeader('Content-Type', 'text/plain');
    x.timeout = 4000;
    x.send(JSON.stringify({ msg: msg }));
  } catch (e) { /* ignore */ }
}

window.onerror = function (msg, src, line) {
  tvlog('jserror: ' + msg + ' @' + (src || '?') + ':' + (line || '?'));
  return false;
};

/* ---------------------------------------------------------------- XHR */
function xhrGet(url, timeout, cb) {
  var x = new XMLHttpRequest();
  x.open('GET', url, true);
  x.timeout = timeout;
  x.onload = function () { cb(x.status, x.responseText); };
  x.onerror = function () { cb(-1, null); };
  x.ontimeout = function () { cb(-2, null); };
  x.send();
}

function xhrPost(url, obj, cb) {
  var x = new XMLHttpRequest();
  x.open('POST', url, true);
  x.setRequestHeader('Content-Type', 'text/plain'); // no CORS preflight on Tizen 4
  x.timeout = 8000;
  x.onload = function () { cb(x.status, x.responseText); };
  x.onerror = function () { cb(-1, null); };
  x.ontimeout = function () { cb(-2, null); };
  x.send(JSON.stringify(obj));
}

/* ---------------------------------------------------------------- boot */
function boot() {
  g.bootT = Date.now();
  $('boot-status').innerHTML = 'Looking for the video server&hellip;';
  tryServer(0);
}

/* ------------------------------------------------- Smart Hub Preview deep link */
/* Launcher preview tiles launch (or poke) the app with the tile's action_data
   as an ApplicationControlData under the PAYLOAD key. We send it as a JSON
   string: {"path": "<index-relative-path>"}. */
function readDeepLink() {
  try {
    if (!window.tizen || !tizen.application) return null;
    var ctrl = tizen.application.getCurrentApplication().getRequestedAppControl();
    if (!ctrl) return null;
    // correct Tizen shape: ctrl.appControl.data (getAppControlData() is not
    // part of the API — it always returned null)
    var data = (ctrl.appControl && ctrl.appControl.data) || [];
    for (var i = 0; i < data.length; i++) {
      if (data[i] && data[i].key === 'PAYLOAD' && data[i].value && data[i].value.length) {
        var raw = data[i].value[0];
        // the launcher wraps payloads: {"values": "<url-encoded json>"}
        // (per the Samsung sample: JSON.parse(value[0]).values, then parse)
        try {
          var wrap = JSON.parse(raw);
          if (wrap && typeof wrap.values === 'string') {
            raw = safeDecode(wrap.values);
          }
        } catch (e1) { /* payload was already plain JSON */ }
        return raw;
      }
    }
  } catch (e) {
    tvlog('deeplink read error ' + e);
  }
  return null;
}

function deepPlay(payload) {
  var path = null;
  if (payload && typeof payload === 'object') {
    if (payload.path) path = payload.path;
  } else {
    try {
      var p = JSON.parse(payload);
      if (p && p.path) path = p.path;
    } catch (e1) {
      if (typeof payload === 'string' && payload.indexOf('/') >= 0) {
        path = payload;   // raw path fallback
      }
    }
  }
  if (!path) return false;
  path = String(path);
  tvlog('deep link play: ' + path);
  abortLoads();   // a boot/queue fetch must not overwrite this single-item queue
  g.items = [{ path: path, title: path.split('/').pop() || 'Video' }];
  g.idx = 0;
  play(0, true);
  return true;
}

function handleLaunchPayload() {
  var payload = readDeepLink();
  if (!payload) return false;
  return deepPlay(payload);
}

/* While the app is already running, an appcontrol event carries the same
   payload (only fires when relaunch is disabled in config.xml). */
function onAppControl() {
  try {
    var payload = readDeepLink();
    if (!payload) return;
    if (!g.server) { g.pendingLaunch = payload; return; }   // still booting
    deepPlay(payload);
  } catch (e) { tvlog('appcontrol event error ' + e); }
}

function tryServer(i) {
  if (i >= SERVERS.length) {
    $('boot-status').innerHTML = 'No video server found on the network.<br>Check that the Pi is up.';
    tvlogAt(SERVERS[0], 'boot: all servers failed');
    return;
  }
  var base = SERVERS[i];
  xhrGet(base + '/api/health', 4000, function (status) {
    if (status === 200) {
      g.server = base;
      tvlogAt(base, 'boot: server ok ' + base + ' (' + (Date.now() - g.bootT) + 'ms)');
      // Launcher preview tile click: play THAT video instead of the shuffle
      if (g.pendingLaunch) {
        var pending = g.pendingLaunch;
        g.pendingLaunch = null;
        if (deepPlay(pending)) return;
      }
      if (handleLaunchPayload()) return;
      loadPlaylist();
    } else {
      tvlogAt(SERVERS[0], 'boot: server ' + base + ' failed status=' + status);
      tryServer(i + 1);
    }
  });
}

function abortLoads() {
  g.loadToken++;
  extending = false;
}

function loadPlaylist() {
  $('boot-status').innerHTML = 'Loading videos&hellip;';
  var t0 = Date.now();
  var token = ++g.loadToken;
  xhrGet(g.server + '/api/playlist?mode=random&n=25', 15000, function (status, text) {
    if (token !== g.loadToken) return;   // superseded by a deep link / refresh
    if (status !== 200) {
      $('boot-status').innerHTML = 'Could not load the video list (server error ' + status + ').';
      tvlog('boot: playlist failed status=' + status);
      return;
    }
    var data = null;
    try { data = JSON.parse(text); } catch (e) { data = null; }
    if (!data || !data.items || data.items.length === 0) {
      $('boot-status').innerHTML = 'The server returned no videos.';
      tvlog('boot: playlist empty');
      return;
    }
    g.items = data.items;
    g.idx = 0;
    tvlog('boot: playlist ' + g.items.length + ' items (' + (Date.now() - t0) + 'ms)');
    play(0, true);
    prefetchMeta(0);   // background: warm date/location cache for the queue
  });
}

/* ---------------------------------------------------------------- play */
function current() { return g.items[g.idx]; }

function play(i, fromBoot) {
  if (i < 0 || i >= g.items.length) return;
  g.idx = i;
  var it = current();
  var v = $('player');
  g.playSeen = false;
  g.pendingNext = false;
  g.lastStallLog = 0;
  lastPct = -1;
  if (g.skipTimer) { clearTimeout(g.skipTimer); g.skipTimer = null; }
  if (g.watchdog) clearTimeout(g.watchdog);
  // Watchdog: if the media element never starts, report it server-side
  g.watchdog = setTimeout(function () {
    if (!g.playSeen) {
      tvlog('watchdog: no playing event src=' + (v.currentSrc || v.src) +
            ' err=' + (v.error ? v.error.code : 'null') +
            ' readyState=' + v.readyState + ' networkState=' + v.networkState);
      status('Video did not start (err ' + (v.error ? v.error.code : 'none') + ') — menu via \u25BC for options', 8000);
    }
  }, 25000);
  var url = g.server + '/api/video?p=' + encodeURIComponent(it.path);
  tvlog('play ' + (i + 1) + '/' + g.items.length + ' ' + it.path);
  // Size the plane BEFORE the video loads when the server already knows the
  // display size — avoids style writes while playback is starting.
  fitPlayer();
  v.src = url;
  try { v.play(); } catch (e) { tvlog('play() threw ' + e); }
  showHud();
  updateMetaLine();
  status('Playing ' + (i + 1) + ' of ' + g.items.length, 3000);
  if (fromBoot) {
    $('boot-screen').className = 'hidden';
  }
  // Date/location metadata: compute on demand (cached server-side), update HUD
  // when it arrives so playback never waits on it.
  if (!it.metaDone) {
    it.metaDone = true;
    xhrGet(g.server + '/api/meta?p=' + encodeURIComponent(it.path), 30000, function (st, text) {
      if (st !== 200 || !text) return;
      var m = null;
      try { m = JSON.parse(text); } catch (e2) { m = null; }
      if (!m) return;
      it.date = m.date || it.date || '';
      it.location = m.location || it.location || '';
      it.duration = m.duration || it.duration || '';
      it.kids = m.kids || it.kids || '';
      it.note = m.note || it.note || '';
      it.disp_w = m.disp_w || it.disp_w || 0;   // rotation-aware display size
      it.disp_h = m.disp_h || it.disp_h || 0;
      if (g.idx === i) { showHud(); updateMetaLine(); fitPlayer(); }
    });
  }
}

function next() {
  if (g.idx + 1 < g.items.length) {
    play(g.idx + 1);
    extendQueueSoon();
  } else if (extending) {
    g.pendingNext = true;   // auto-advance when the in-flight batch lands
    status('Loading more videos\u2026', 4000);
  } else {
    // queue fully consumed: refresh and keep playing
    status('Queue finished \u2014 loading a fresh shuffle\u2026', 6000);
    refreshQueue();
  }
}

/* Keep the queue topped up: when only 5 remain, silently append another
 * batch of 25 (deduped). The old 25-video limit was a catt-era default —
 * there are ~3900 videos in the archive. */
function extendQueueSoon() {
  if (g.items.length - (g.idx + 1) > 5) return;
  extendQueue();
}

var extending = false;

function extendQueue() {
  if (extending) return;
  extending = true;
  var t0 = Date.now();
  var token = ++g.loadToken;
  xhrGet(g.server + '/api/playlist?mode=random&n=25', 15000, function (st, text) {
    if (token !== g.loadToken) return;   // superseded by a deep link / refresh
    extending = false;
    if (st !== 200 || !text) {
      if (g.pendingNext) {
        g.pendingNext = false;
        status('Could not load more videos', 4000);
      }
      return;
    }
    var data = null;
    try { data = JSON.parse(text); } catch (e) { data = null; }
    if (!data || !data.items) {
      if (g.pendingNext) {
        g.pendingNext = false;
        status('Could not load more videos', 4000);
      }
      return;
    }
    var seen = {};
    for (var i = 0; i < g.items.length; i++) seen[g.items[i].path] = 1;
    var added = 0;
    for (var j = 0; j < data.items.length; j++) {
      var it = data.items[j];
      if (!seen[it.path]) { g.items.push(it); seen[it.path] = 1; added++; }
    }
    if (added) {
      tvlog('queue extended +' + added + ' (total ' + g.items.length + ') (' + (Date.now() - t0) + 'ms)');
      prefetchMeta(g.items.length - added);   // warm meta for the new batch
    }
    if (g.pendingNext) {
      if (g.idx + 1 < g.items.length) play(g.idx + 1);
      else { g.pendingNext = false; refreshQueue(); }
    }
  });
}

function refreshQueue() {
  extending = true;
  var token = ++g.loadToken;
  xhrGet(g.server + '/api/playlist?mode=random&n=25', 15000, function (st, text) {
    if (token !== g.loadToken) return;   // superseded by a deep link / newer fetch
    extending = false;
    if (st !== 200 || !text) { status('Could not load more videos', 4000); return; }
    var data = null;
    try { data = JSON.parse(text); } catch (e) { data = null; }
    if (!data || !data.items || !data.items.length) { status('Could not load more videos', 4000); return; }
    g.items = data.items;
    g.idx = -1;
    tvlog('queue refreshed with ' + g.items.length + ' fresh items');
    prefetchMeta(0);
    next();
  });
}

function prev() {
  if (g.idx - 1 >= 0) play(g.idx - 1);
  else status('First video in the queue', 3000);
}

/* Background-prefetch date/location for every queued item, one at a time so
 * the geocoder/server aren't flooded. Cached server-side, so repeat boots are
 * instant; a fresh clip's location is ready before it ever plays. */
function prefetchMeta(i) {
  while (i < g.items.length && g.items[i].metaDone) i++;
  if (i >= g.items.length) return;
  var it = g.items[i];
  it.metaDone = true;
  var t0 = Date.now();
  xhrGet(g.server + '/api/meta?p=' + encodeURIComponent(it.path), 30000, function (st, text) {
    if (st === 200 && text) {
      var m = null;
      try { m = JSON.parse(text); } catch (e) { m = null; }
      if (m) {
        it.date = m.date || it.date || '';
        it.location = m.location || it.location || '';
        it.duration = m.duration || it.duration || '';
        it.kids = m.kids || it.kids || '';
        it.note = m.note || it.note || '';
        it.disp_w = m.disp_w || it.disp_w || 0;   // rotation-aware display size
        it.disp_h = m.disp_h || it.disp_h || 0;
        if (g.idx === i) { showHud(); updateMetaLine(); fitPlayer(); tvlog('meta ready (prefetch) ' + it.path + ' (' + (Date.now() - t0) + 'ms)'); }
      }
    } else {
      it.metaDone = false;   // let play() retry later
    }
    prefetchMeta(i + 1);
  });
}

/* ---------------------------------------------------------------- HUD */
function updateMetaLine() {
  var it = current();
  var el = $('meta-line');
  if (!el || !it) { if (el) el.className = 'hidden'; return; }
  var txt = esc(it.date || '');
  if (it.location) txt += (txt ? ' \u2014 ' : '') + esc(it.location);
  if (it.kids) txt += (txt ? ' \u2014 ' : '') + esc(it.kids);
  if (it.note) txt += (txt ? '<br>' : '') + '\u201C' + esc(it.note) + '\u201D';
  if (txt) { el.innerHTML = txt; el.className = ''; }
  else { el.className = 'hidden'; }
}

function showHud() {
  var it = current();
  if (!it) return;
  updateMetaLine();
  $('hud-pos').innerHTML = (g.idx + 1) + ' / ' + g.items.length;
  // Date is the headline; the filename drops to a small dim line below (it
  // still matters for names like "Boat from cave", just not as the lead).
  // Until /api/meta lands there is no date, so the filename stands in as the
  // headline — the meta callback re-runs showHud() and swaps it over.
  if (it.date) {
    $('hud-title').innerHTML = esc(it.date);
    $('hud-file').innerHTML = esc(it.title || '');
    $('hud-file').className = it.title ? '' : 'hidden';
    var meta = it.location || '';
    if (it.kids) meta += (meta ? ' \u2014 ' : '') + it.kids;
    $('hud-meta').innerHTML = esc(meta);
  } else {
    $('hud-title').innerHTML = esc(it.title || 'Video');
    $('hud-file').className = 'hidden';
    $('hud-meta').innerHTML = esc(it.location || '');
  }
  $('hud-star').className = it.favorite ? '' : 'hidden';
  $('hud').className = '';
  if (g.hudTimer) clearTimeout(g.hudTimer);
  g.hudTimer = setTimeout(hideHud, 6000);
}

function hideHud() { $('hud').className = 'hidden'; }

function status(msg, ms) {
  $('status-line').innerHTML = msg;
  $('status-line').className = '';
  if (g.statusTimer) clearTimeout(g.statusTimer);
  g.statusTimer = setTimeout(function () { $('status-line').className = 'hidden'; }, ms || 2500);
}

/* ---------------------------------------------------------------- favorite */
function toggleFavorite() {
  var it = current();
  if (!it) return;
  var action = it.favorite ? 'remove' : 'add';
  var note = '';
  if (action === 'add' && it.date) note = it.date;
  xhrPost(g.server + '/api/favorite', { action: action, path: it.path, label: it.title, note: note }, function (st, text) {
    if (st === 200) {
      it.favorite = (action === 'add');
      $('hud-star').className = it.favorite ? '' : 'hidden';
      status(it.favorite ? '\u2605 Saved to favorites' : 'Removed from favorites', 2500);
      showHud();
    } else {
      status('Could not save favorite', 2500);
    }
  });
}

/* ---------------------------------------------------------------- keys */
/* Tizen 4.0 quirk: setting currentTime updates the property but the engine may
   ignore the actual stream seek (it never requests the target byte range and
   playback keeps its own position — ct reads back EXACTLY the set value).
   seekBy tries three strategies in order, logging each result:
     A. plain currentTime set (works on some files)
     B. pause -> seek -> resume
     C. media-fragment restart: re-open the stream at #t=<seconds>
   "ignored" is detected by time: after a real seek, ct advances (~+1s); if ct
   still reads exactly the target after 900ms, the engine didn't move. */
function seekIsStuck(v, t) {
  return Math.abs(v.currentTime - t) < 0.15;   // frozen exactly at target
}

function seekBy(sec) {
  var v = $('player');
  if (g.seekBusy) return;
  var it = g.items ? g.items[g.idx] : null;
  if (!it) return;
  g.seekBusy = true;
  var url = g.server + '/api/video?p=' + encodeURIComponent(it.path);
  var dur = v.duration;
  var end = (dur && isFinite(dur)) ? dur - 1 : null;
  try {
    if (!end && v.seekable && v.seekable.length) {
      end = v.seekable.end(v.seekable.length - 1) - 1;
    }
  } catch (e0) {}
  var from = v.currentTime;
  var t = from + sec;
  if (t < 0) t = 0;
  if (end !== null && t > end) t = end;
  var wasPlaying = !v.paused;
  tvlog('seek req ' + sec + 's ' + from.toFixed(1) + ' -> ' + t.toFixed(1) +
        ' (dur=' + dur + ')');
  // Keep the HUD hidden during seeks (Nathan wants a clear view of the
  // progress bar jumping) — a small status line carries the feedback.
  var mk = Math.floor(Math.abs(t) / 60), sk = Math.floor(Math.abs(t) % 60);
  status((sec > 0 ? '\u25B6\u25B6 +' : '\u25C0\u25C0 \u2212') + Math.abs(sec) +
         's \u2192 ' + mk + ':' + (sk < 10 ? '0' : '') + sk, 1800);

  function attemptB() {
    tvlog('seek B: pause-seek-resume');
    try { v.pause(); } catch (e) {}
    try { v.currentTime = t; } catch (e1) {}
    setTimeout(function () {
      try { if (wasPlaying) v.play(); } catch (e2) {}
      setTimeout(function () {
        tvlog('seek B result ct=' + v.currentTime.toFixed(1) +
              (seekIsStuck(v, t) ? ' STUCK' : ' moved'));
        if (seekIsStuck(v, t)) attemptC();
        else finish(true);
      }, 900);
    }, 500);
  }

  function attemptC() {
    tvlog('seek C: fragment restart #t=' + t.toFixed(1));
    v.src = url + '#t=' + t.toFixed(1);
    try { if (wasPlaying) v.play(); } catch (e3) {}
    setTimeout(function () {
      tvlog('seek C result ct=' + (v.currentTime || 0).toFixed(1) +
            ' readyState=' + v.readyState);
      finish(true);
    }, 1500);
  }

  function finish(ok) {
    g.seekBusy = false;
    // no showHud(): a seek should not re-summon the info overlay
  }

  // Attempt A
  try { v.currentTime = t; } catch (err) {
    tvlog('seek A threw ' + err);
    attemptB();
    return;
  }
  setTimeout(function () {
    var moved = !seekIsStuck(v, t);
    tvlog('seek A result ct=' + v.currentTime.toFixed(1) + (moved ? ' moved' : ' STUCK'));
    if (moved) finish(true);
    else if (wasPlaying) attemptB();
    else attemptC();   // paused video: ct frozen is normal while paused — go straight to C
  }, 900);
}

function togglePause() {
  var v = $('player');
  if (v.paused) { v.play(); status('\u25B6 Play', 1200); }
  else { v.pause(); status('\u275A Paused', 100000); }
  showHud();
}

function exitApp() {
  if (g.exitDone) return;
  g.exitDone = true;
  $('player').pause();
  tvlog('exit');
  try {
    if (window.tizen && tizen.application) {
      tizen.application.getCurrentApplication().exit();
      return;
    }
  } catch (e) { /* no privilege — fall through */ }
  try { window.close(); return; } catch (e2) { /* dev browser */ }
  status('Press HOME to leave', 4000); // dev fallback
}

/* ---------------------------------------------------------------- menu */
var MENU_ACTIONS = [
  { label: '\u25C0\u25C0 Previous', act: function () { closeMenu(); prev(); } },
  // button text follows playback state: shows the action the key WILL take
  { label: function () { return $('player').paused ? '\u25B6 Play' : '\u23F8 Pause'; },
    act: function () { closeMenu(); togglePause(); } },
  { label: '\u25B6\u25B6 Next', act: function () { closeMenu(); next(); } },
  { label: '\u21BB Restart video', act: function () {
      closeMenu();
      try { $('player').currentTime = 0; status('\u21BB Restarted', 1500); showHud(); } catch (e2) {}
  } },
  { label: '\u2605 Favorite', act: function () { closeMenu(); toggleFavorite(); } }
];

var menuSel = 1;   // default: Pause/Play (center of Previous|Pause|Next)

function openMenu() {
  menuSel = 1;
  renderMenu();
  $('menu-title').innerHTML = current() ? esc(current().title) : '';
  $('menu').className = '';
  tvlog('menu open (playing=' + !$('player').paused + ')');
}

function renderMenu() {
  var html = '';
  for (var i = 0; i < MENU_ACTIONS.length; i++) {
    var label = MENU_ACTIONS[i].label;
    if (typeof label === 'function') label = label();   // state-aware (Pause/Play)
    html += '<div class="menu-item' + (i === menuSel ? ' sel' : '') + '">' + esc(label) + '</div>';
  }
  $('menu-items').innerHTML = html;
}

function closeMenu() {
  $('menu').className = 'hidden';
  // no auto-play: the menu never paused the video (Nathan's preference)
}

function menuOpen() { return $('menu').className !== 'hidden'; }

/* Media keys only reach the app if registered (Tizen drops unregistered hwkeys).
   Samsung remotes send the combined play/pause button as MediaPlayPause (10252);
   dedicated play/pause as MediaPlay (415) / MediaPause (19). registerKey fails
   atomically if ANY name is unsupported on this TV, so ask getSupportedKeys()
   first and register only what exists (per-key, so one bad name can't kill the
   rest). This TV's WebKit doesn't set e.keyName, so map its real keyCodes to
   canonical names and use that in onKeyDown. */
function registerMediaKeys() {
  if (!window.tizen || !tizen.tvinputdevice) return;   // dev browser
  g.mediaByCode = {};                                  // keyCode -> canonical name
  var wanted = { MediaPlayPause: 1, MediaPlay: 1, MediaPause: 1, MediaStop: 1,
                 MediaRewind: 1, MediaFastForward: 1, MediaTrackPrevious: 1, MediaTrackNext: 1 };
  try {
    var sup = tizen.tvinputdevice.getSupportedKeys() || [];
    var names = [], unsupported = [];
    for (var i = 0; i < sup.length; i++) {
      var k = sup[i];
      var kc = (k.keyCode !== undefined) ? k.keyCode : k.code;   // Tizen 4 hides keyCode
      if (wanted[k.name]) { names.push(k.name); if (kc !== undefined) g.mediaByCode[kc] = k.name; }
    }
    if (sup.length && sup[0].keyCode === undefined) {
      tvlog('getSupportedKeys entry sample: ' + JSON.stringify(sup[0]));
    }
    for (var n in wanted) if (!nameIn(n, names)) unsupported.push(n);
    for (var j = 0; j < names.length; j++) {
      try {
        tizen.tvinputdevice.registerKey(names[j]);
      } catch (e2) {
        tvlog('registerKey ' + names[j] + ' failed: ' + (e2.message || e2));
        // drop its code from the name map so a failed key can't fire actions
        for (var c in g.mediaByCode) if (g.mediaByCode[c] === names[j]) delete g.mediaByCode[c];
      }
    }
    tvlog('media keys: supported=' + names.join(',') + ' codes=' + JSON.stringify(g.mediaByCode) +
          (unsupported.length ? ' NOT-supported=' + unsupported.join(',') : ''));
  } catch (e) { tvlog('getSupportedKeys failed: ' + (e.message || e)); }
}
function nameIn(name, list) {
  for (var i = 0; i < list.length; i++) if (list[i] === name) return true;
  return false;
}

function onKeyDown(e) {
  var code = e.keyCode;
  var kn = e.keyName || (g.mediaByCode ? g.mediaByCode[code] : '') || '';
  tvlog('key ' + code + (kn ? ' (' + kn + ')' : ''));  // learn what the remote really sends
  /* media keys work whether or not the action menu is open */
  if (kn === 'MediaPlayPause' || code === 10252 || code === 179) { togglePause(); e.preventDefault(); return; }
  if (kn === 'MediaPlay'     || code === 415)  { if ($('player').paused) togglePause(); e.preventDefault(); return; }
  if (kn === 'MediaPause'    || code === 19)   { if (!$('player').paused) togglePause(); e.preventDefault(); return; }
  if (kn === 'MediaStop'     || code === 413)  { if (!$('player').paused) togglePause(); return; }
  if (menuOpen()) {
    if (code === 37) {                         // Left
      menuSel = (menuSel + MENU_ACTIONS.length - 1) % MENU_ACTIONS.length;
      renderMenu();
    } else if (code === 39) {                  // Right
      menuSel = (menuSel + 1) % MENU_ACTIONS.length;
      renderMenu();
    } else if (code === 13) {                  // Enter
      MENU_ACTIONS[menuSel].act();
    } else if (code === 40 || code === 10009) { // Down or Return closes
      closeMenu();
      if (code === 10009) exitApp();
    }
    if (code === 40 || code === 10009) e.preventDefault();
    return;
  }
  switch (code) {
    case 40: openMenu(); break;                // Down opens the action menu
    case 412: prev(); break;                   // Rewind button = previous video
    case 417: next(); break;                   // FastForward = next video
    case 427: prev(); break;                   // CHUP (some maps)
    case 428: next(); break;                   // CHDOWN
    case 10232: prev(); break;                 // MediaTrackPrevious (long-press Rewind)
    case 10233: next(); break;                 // MediaTrackNext (long-press FastForward)
    case 13: togglePause(); break;             // Enter
    case 37: seekBy(-10); break;               // Left
    case 39: seekBy(10); break;                // Right
    case 38: toggleFavorite(); break;          // Up
    case 10009: exitApp(); break;              // Return
    case 461: exitApp(); break;                // Return (older maps)
    default:
      if (kn === 'MediaRewind' || kn === 'MediaTrackPrevious') { prev(); break; }
      if (kn === 'MediaFastForward' || kn === 'MediaTrackNext') { next(); break; }
      if (code >= 4097 && code <= 4107) exitApp(); // color keys on some TVs
      break;
  }
  if (code === 13 || code === 37 || code === 38 || code === 39 || code === 40) e.preventDefault();
}

/* Tizen packaged apps also fire a dedicated tizenhwkey event */
function onTizenHwKey(e) {
  if (e.keyName === 'Return' || e.keyName === 'XF86Exit') exitApp();
}

/* ---------------------------------------------------------------- progress */
function fmtTime(sec) {
  var s = Math.max(0, Math.floor(sec || 0));
  var m = Math.floor(s / 60);
  s = s % 60;
  return m + ':' + (s < 10 ? '0' : '') + s;
}

var lastPct = -1;

function updateProgress() {
  var v = $('player');
  if (!v.duration || v.duration === Infinity || isNaN(v.duration)) return;
  var pct = Math.min(100, (v.currentTime / v.duration) * 100);
  var fill = $('progress-fill');
  // Big change = new video or seek: snap without animation.
  if (lastPct < 0 || pct < lastPct - 0.5 || pct > lastPct + 6) {
    fill.className = 'jump';
    fill.style.width = pct + '%';
    void fill.offsetWidth;          // force reflow so the snap applies
    fill.className = '';
  } else {
    fill.style.width = pct + '%';   // CSS transition smooths the step
  }
  lastPct = pct;
  var t = $('progress-time');
  t.innerHTML = fmtTime(v.currentTime) + ' / ' + fmtTime(v.duration);
  t.className = '';
}

/* ------------------------------------------------- explicit contain-fit (Tizen) */
/* Tizen 4.0's WebKit renders <video> on a hardware plane that mis-applies
   object-fit: contain — portrait videos came out small and top-anchored.
   Compute the letterbox box in pixels and position the element ourselves.
   Dims come from the server when available (rotation-aware — iPhone portrait
   videos report landscape-encoded sizes to the browser), else videoWidth. */
var lastFit = '';
function fitPlayer() {
  var v = $('player');
  var W = window.innerWidth, H = window.innerHeight;
  var it = g.items ? g.items[g.idx] : null;
  var vw = (it && it.disp_w) || v.videoWidth;
  var vh = (it && it.disp_h) || v.videoHeight;
  if (!W || !H || !vw || !vh) return;   // no metadata yet: keep full-screen default
  var s = Math.min(W / vw, H / vh);
  var w = Math.round(vw * s), h = Math.round(vh * s);
  var x = Math.round((W - w) / 2), y = Math.round((H - h) / 2);
  // Identical writes can still make Tizen re-composite (and glitch) the video
  // plane — skip no-ops entirely.
  var sig = w + 'x' + h + '@' + x + ',' + y;
  if (sig === lastFit) return;
  lastFit = sig;
  v.style.width = w + 'px';
  v.style.height = h + 'px';
  v.style.left = x + 'px';
  v.style.top = y + 'px';
  tvlog('fit ' + sig + ' src=' + vw + 'x' + vh);
}

/* ---------------------------------------------------------------- video events */
function wireVideo() {
  var v = $('player');
  v.addEventListener('loadedmetadata', function () {
    var r = null;
    try { r = v.getBoundingClientRect(); } catch (e) {}
    tvlog('meta ' + v.videoWidth + 'x' + v.videoHeight +
          ' viewport ' + window.innerWidth + 'x' + window.innerHeight +
          ' rect ' + (r ? Math.round(r.left) + ',' + Math.round(r.top) + ' ' +
                              Math.round(r.width) + 'x' + Math.round(r.height) : '?'));
    fitPlayer();
  });
  window.addEventListener('resize', fitPlayer);
  v.addEventListener('playing', function () {
    g.playSeen = true;
    fitPlayer();   // self-heal if the plane lost its box (Tizen glitches)
    $('status-line').className = 'hidden';
    tvlog('playing t=+' + (Date.now() - g.bootT) + 'ms ct=' + (v.currentTime || 0));
  });
  v.addEventListener('ended', function () { tvlog('ended idx=' + g.idx); next(); });
  // keep the open menu's Pause/Play label honest if state changes underneath
  function menuRefresh() { if (menuOpen()) renderMenu(); }
  v.addEventListener('play', menuRefresh);
  v.addEventListener('pause', menuRefresh);
  v.addEventListener('waiting', function () {
    var now = Date.now();
    if (now - g.lastStallLog > 5000) {
      g.lastStallLog = now;
      tvlog('waiting ct=' + (v.currentTime || 0));
      status('Buffering\u2026', 20000);
    }
  });
  v.addEventListener('error', function () {
    var code = v.error ? (v.error.code + '/' + (v.error.message || '')) : 'null';
    tvlog('video ERROR ' + code + ' src=' + (v.currentSrc || v.src));
    status('Cannot play this file (' + (v.error ? v.error.code : '?') + ') — skipping\u2026', 4000);
    if (g.skipTimer) clearTimeout(g.skipTimer);
    g.skipTimer = setTimeout(function () { g.skipTimer = null; next(); }, 2500);
  });
  v.addEventListener('stalled', function () {
    var now = Date.now();
    if (now - g.lastStallLog > 5000) {
      g.lastStallLog = now;
      tvlog('stalled ct=' + (v.currentTime || 0));
    }
  });
  v.addEventListener('seeking', function () {
    tvlog('seeking -> ' + (v.currentTime || 0).toFixed(1));
  });
  v.addEventListener('seeked', function () {
    tvlog('seeked -> ' + (v.currentTime || 0).toFixed(1));
  });
}

/* ---------------------------------------------------------------- go */
document.onkeydown = onKeyDown;
if (window.addEventListener) {
  window.addEventListener('tizenhwkey', onTizenHwKey, false);
  window.addEventListener('appcontrol', onAppControl, false);   // preview tiles while running
}
registerMediaKeys();
wireVideo();
setInterval(updateProgress, 500);   // progress bar + time readout
tvlog('boot v1.0.34 ua=' + (navigator.userAgent || '').slice(-60));
boot();
