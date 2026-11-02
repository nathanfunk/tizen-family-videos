/* Family Videos — TV player (Tizen 4.0 web app). v2: instrumented boot.
 * Rules per samsung-tv-control skill: ES5-era JS, XHR only (no fetch), no
 * classList / padStart / Promises. Remote keys arrive as keydown codes:
 *   427/428 = CHUP/CHDOWN (prev/next video), 13 Enter = pause,
 *   37/39 Left/Right = seek 10s, 38 Up = favorite, 10009 Return = exit.
 * Every boot step and media event is reported to the Pi (/api/log) so boot
 * progress is visible server-side; all errors also show on screen.
 */

var SERVERS = ['http://rpi:8123', 'http://10.0.0.172:8123', 'http://10.0.0.175:8123'];

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
  lastStallLog: 0
};

function $(id) { return document.getElementById(id); }

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
      loadPlaylist();
    } else {
      tvlogAt(SERVERS[0], 'boot: server ' + base + ' failed status=' + status);
      tryServer(i + 1);
    }
  });
}

function loadPlaylist() {
  $('boot-status').innerHTML = 'Loading videos&hellip;';
  var t0 = Date.now();
  xhrGet(g.server + '/api/playlist?mode=random&n=25', 15000, function (status, text) {
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
  xhrGet(g.server + '/api/playlist?mode=random&n=25', 15000, function (st, text) {
    extending = false;
    if (st !== 200 || !text) return;
    var data = null;
    try { data = JSON.parse(text); } catch (e) { data = null; }
    if (!data || !data.items) return;
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
  });
}

function refreshQueue() {
  extending = true;
  xhrGet(g.server + '/api/playlist?mode=random&n=25', 15000, function (st, text) {
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
  if (i >= g.items.length) return;
  var it = g.items[i];
  if (it.metaDone) { prefetchMeta(i + 1); return; }
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
  if (!it) return;
  var txt = it.date || '';
  if (it.location) txt += (txt ? ' \u2014 ' : '') + it.location;
  if (it.kids) txt += (txt ? ' \u2014 ' : '') + it.kids;
  if (it.note) txt += (txt ? '<br>' : '') + '\u201C' + it.note + '\u201D';
  if (txt) { $('meta-line').innerHTML = txt; $('meta-line').className = ''; }
  else { $('meta-line').className = 'hidden'; }
}

function showHud() {
  var it = current();
  if (!it) return;
  updateMetaLine();
  $('hud-pos').innerHTML = (g.idx + 1) + ' / ' + g.items.length;
  $('hud-title').innerHTML = it.title;
  var meta = it.date || '';
  if (it.location) meta += (meta ? ' \u2014 ' : '') + it.location;
  $('hud-meta').innerHTML = meta;
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
function seekBy(sec) {
  var v = $('player');
  if (!v.duration || g.seekBusy) return;
  g.seekBusy = true;
  var t = v.currentTime + sec;
  if (t < 0) t = 0;
  if (t > v.duration - 1) t = v.duration - 1;
  v.currentTime = t;
  showHud();
  setTimeout(function () { g.seekBusy = false; }, 250);
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
  { label: '\u25B6\u25B6 Next video', act: function () { closeMenu(); next(); } },
  { label: '\u25C0\u25C0 Previous video', act: function () { closeMenu(); prev(); } },
  { label: '\u21BB Restart video', act: function () {
      closeMenu();
      try { $('player').currentTime = 0; status('\u21BB Restarted', 1500); showHud(); } catch (e2) {}
  } },
  { label: '\u2605 Favorite', act: function () { closeMenu(); toggleFavorite(); } },
  { label: '\u2715 Exit app', act: function () { closeMenu(); exitApp(); } }
];

var menuSel = 0;

function openMenu() {
  try { $('player').pause(); } catch (e) {}
  menuSel = 0;
  renderMenu();
  $('menu-title').innerHTML = current() ? current().title : '';
  $('menu').className = '';
  tvlog('menu open');
}

function renderMenu() {
  var html = '';
  for (var i = 0; i < MENU_ACTIONS.length; i++) {
    html += '<div class="menu-item' + (i === menuSel ? ' sel' : '') + '">' + MENU_ACTIONS[i].label + '</div>';
  }
  $('menu-items').innerHTML = html;
}

function closeMenu() {
  $('menu').className = 'hidden';
  try { $('player').play(); } catch (e) {}
}

function menuOpen() { return $('menu').className !== 'hidden'; }

function onKeyDown(e) {
  var code = e.keyCode;
  tvlog('key ' + code);                        // learn what the remote really sends
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
    case 13: togglePause(); break;             // Enter
    case 37: seekBy(-10); break;               // Left
    case 39: seekBy(10); break;                // Right
    case 38: toggleFavorite(); break;          // Up
    case 10009: exitApp(); break;              // Return
    case 461: exitApp(); break;                // Return (older maps)
    default:
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
    setTimeout(next, 2500);
  });
  v.addEventListener('stalled', function () {
    var now = Date.now();
    if (now - g.lastStallLog > 5000) {
      g.lastStallLog = now;
      tvlog('stalled ct=' + (v.currentTime || 0));
    }
  });
}

/* ---------------------------------------------------------------- go */
document.onkeydown = onKeyDown;
if (window.addEventListener) window.addEventListener('tizenhwkey', onTizenHwKey, false);
wireVideo();
setInterval(updateProgress, 500);   // progress bar + time readout
tvlog('boot v1.0.14 ua=' + (navigator.userAgent || '').slice(-60));
boot();
