"""Void Labs main site: public page + /admin dashboard. Python standard library only.

Environment variables (set them in the bot-hosting.net panel):
  SERVER_PORT     port to listen on (set by the panel)
  ADMIN_PASSWORD  REQUIRED to enable /admin (min 10 characters)
  ADMIN_USERNAME  optional, default "admin"
  SESSION_SECRET  optional extra secret used to sign login cookies
"""
import hashlib, hmac, html, json, os, re, secrets, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.getenv("SERVER_PORT") or 8080)
DATA_FILE = os.getenv("DATA_FILE") or os.path.join(BASE, "data.json")
ADMIN_USER = os.getenv("ADMIN_USERNAME") or "admin"
ADMIN_PASS = os.getenv("ADMIN_PASSWORD") or ""
ADMIN_ON = len(ADMIN_PASS) >= 10
# Cookie-signing key is independent of the password. Without SESSION_SECRET a random key is
# generated at startup (everyone is logged out whenever the server restarts).
_SS = os.getenv("SESSION_SECRET") or ""
SECRET = hashlib.sha256(("vl|" + _SS).encode()).digest() if len(_SS) >= 16 else secrets.token_bytes(32)
SESSION_TTL = 8 * 3600

STATUSES = [("online", "Online"), ("maintenance", "Maintenance"), ("offline", "Offline")]
STATUS_LABEL = dict(STATUSES)

DEFAULTS = {
    "name": "Void",
    "about": (
        "I'm a developer who builds Discord bots, dashboards and tools for communities.\n\n"
        "Donut and Pixel are my two biggest projects. Donut is a dashboard and Discord bot "
        "built around the Donut network, and Pixel is a full community bot with tickets, "
        "giveaways, moderation and staff tools. Both are built, run and updated by me."
    ),
    "email": "",
    "discord": "",
    "donut_status": "online",
    "pixel_status": "online",
}

_lock = threading.Lock()
_fails = {}  # ip -> (count, locked_until)


# ---------- storage ----------
def load():
    data = dict(DEFAULTS)
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for k in DEFAULTS:
                if isinstance(saved.get(k), str):
                    data[k] = saved[k]
    except (OSError, ValueError):
        pass
    for k in ("donut_status", "pixel_status"):
        if data[k] not in STATUS_LABEL:
            data[k] = "online"
    return data


def save(data):
    tmp = DATA_FILE + ".tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, DATA_FILE)


# ---------- validation ----------
EMAIL_RE = re.compile(r"^[^@\s<>\"'\\]+@[^@\s<>\"'\\]+\.[^@\s<>\"'\\]{2,}$")
DISCORD_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:discord\.gg/|discord\.com/invite/)([A-Za-z0-9-]{2,32})/?$")


def validate(form):
    """Returns (clean_values, error_message_or_None)."""
    g = lambda k: (form.get(k, [""])[0] or "").replace("\r\n", "\n").strip()
    out = {
        "name": g("name"), "about": g("about"), "email": g("email"), "discord": g("discord"),
        "donut_status": g("donut_status"), "pixel_status": g("pixel_status"),
    }
    if not out["name"] or len(out["name"]) > 40:
        return out, "Name must be 1 to 40 characters."
    if len(out["about"]) > 1500:
        return out, "About text is too long (max 1500 characters)."
    if out["email"] and (len(out["email"]) > 120 or not EMAIL_RE.match(out["email"])):
        return out, "That doesn't look like a valid email address."
    if out["discord"]:
        m = DISCORD_RE.match(out["discord"])
        if not m:
            return out, "Discord link must look like discord.gg/yourcode or discord.com/invite/yourcode."
        out["discord"] = "https://discord.gg/" + m.group(1)
    for k in ("donut_status", "pixel_status"):
        if out[k] not in STATUS_LABEL:
            return out, "Pick Online, Maintenance or Offline for each site."
    return out, None


# ---------- sessions ----------
def _sig(msg):
    return hmac.new(SECRET, msg.encode(), hashlib.sha256).hexdigest()


def make_session():
    exp = str(int(time.time()) + SESSION_TTL)
    return exp + "." + _sig("s" + exp)


def check_session(token):
    try:
        exp, sig = token.split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(sig, _sig("s" + exp))
    except (ValueError, AttributeError):
        return False


def csrf_for(token):
    return _sig("c" + token)


def same(a, b):
    return hmac.compare_digest(a.encode(), b.encode())


# ---------- pages ----------
CSS = """
:root{
--paper:#f6f5f1;--card:#ffffff;--ink:#15140f;--muted:#6b665b;--faint:#989284;
--line:#e2ded5;--line-2:#cdc7b9;--accent:#1d4d3e;--accent-soft:rgba(29,77,62,.1);
--donut:#a44d68;--pixel:#33637a;--ok:#2f7d54;--warn:#9a6b1a;--bad:#a8453c;
--sans:"Inter",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box;margin:0;padding:0}
html{background:var(--paper);-webkit-text-size-adjust:100%}
body{font-family:var(--sans);color:var(--ink);background:var(--paper);line-height:1.55;font-size:16px;
min-height:100vh;display:flex;flex-direction:column;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
::selection{background:var(--accent);color:#fff}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:2px}
main{flex:1}
.wrap{width:100%;max-width:1040px;margin:0 auto;padding-left:clamp(1.25rem,5vw,2.5rem);padding-right:clamp(1.25rem,5vw,2.5rem)}
header{position:sticky;top:0;z-index:20;background:var(--paper);border-bottom:1px solid var(--line)}
header .wrap{display:flex;align-items:center;justify-content:space-between;gap:1rem;height:60px}
.mark{display:inline-flex;align-items:center;gap:.55rem;font-size:.9375rem;font-weight:600;letter-spacing:-.01em;text-decoration:none}
.mark .dot{width:.7rem;height:.7rem;border-radius:50%;border:1.5px solid var(--ink);flex:none}
nav.top{display:flex;gap:clamp(.9rem,2.5vw,1.75rem)}
nav.top a{font:500 10.5px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
padding:.4rem 0;border-bottom:1px solid transparent;transition:color .2s,border-color .2s}
nav.top a:hover{color:var(--ink);border-color:var(--line-2)}
.hero{border-top:0;padding-top:clamp(3.5rem,9vw,6.5rem);padding-bottom:clamp(2.5rem,6vw,4rem)}
.eyebrow{display:flex;align-items:center;gap:.75rem;font:500 10.5px/1 var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}
.eyebrow::before{content:"";width:1.75rem;height:1px;background:var(--line-2)}
h1{margin-top:1.5rem;font-size:clamp(2.6rem,7.4vw,5.5rem);font-weight:600;letter-spacing:-.045em;line-height:.96;max-width:18ch;text-wrap:balance}
.lede{margin-top:1.6rem;max-width:54ch;color:var(--muted);font-size:clamp(1rem,1.6vw,1.125rem);line-height:1.65}
.hero-meta{margin-top:clamp(2rem,5vw,3.25rem);padding-top:1rem;border-top:1px solid var(--line);
display:flex;flex-wrap:wrap;gap:.4rem 2.5rem;font:400 10.5px/1.6 var(--mono);letter-spacing:.09em;text-transform:uppercase;color:var(--faint)}
section{border-top:1px solid var(--line);padding:clamp(2.5rem,6vw,4.5rem) 0}
.sec-head{display:flex;align-items:baseline;justify-content:space-between;gap:1.5rem;margin-bottom:clamp(1.5rem,3vw,2.5rem)}
h2{font-size:clamp(1.4rem,2.6vw,1.75rem);font-weight:600;letter-spacing:-.03em;line-height:1.2}
.sec-label{font:500 10.5px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--faint);white-space:nowrap}
.projects{display:flex;flex-direction:column;border-top:1px solid var(--line-2)}
.project{display:grid;grid-template-columns:3rem minmax(0,1fr) auto;gap:clamp(1rem,3vw,2.5rem);align-items:start;
padding:clamp(1.5rem,3vw,2rem) .75rem;margin:0 -.75rem;border-bottom:1px solid var(--line);border-radius:8px;transition:background .25s ease}
.project:hover,.project:focus-visible{background:var(--card)}
.idx{font:400 10.5px/1.7 var(--mono);letter-spacing:.08em;color:var(--faint);padding-top:.35rem}
.project-head{display:flex;align-items:center;gap:.7rem;flex-wrap:wrap}
.badge{width:1.5rem;height:1.5rem;flex:none}
.donut{border-radius:50%;background:radial-gradient(circle at 50% 50%,var(--paper) 0 26%,var(--donut) 28% 100%)}
.pixel{display:grid;grid-template-columns:repeat(4,1fr);gap:2px}
.pixel b{background:var(--pixel);opacity:.16;border-radius:1px}.pixel b.on{opacity:1}
.name{font-size:clamp(1.2rem,2vw,1.4rem);font-weight:600;letter-spacing:-.025em}
.status{display:inline-flex;align-items:center;gap:.35rem;font:500 9.5px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase}
.status::before{content:"";width:4px;height:4px;border-radius:50%;background:currentColor}
.s-online{color:var(--ok)}.s-maintenance{color:var(--warn)}.s-offline{color:var(--bad)}
.desc{margin-top:.5rem;color:var(--muted);font-size:.9375rem;line-height:1.6;max-width:56ch}
.meta{margin-top:.75rem;font:400 10.5px/1.7 var(--mono);letter-spacing:.04em;color:var(--faint);text-transform:uppercase}
.host{display:inline-flex;align-items:center;gap:.4rem;font:400 11px/1 var(--mono);color:var(--muted);padding-top:.35rem;white-space:nowrap}
.arrow{display:inline-block;transition:transform .25s ease}
.project:hover .arrow{transform:translate(2px,-2px)}
.sec-grid{display:grid;grid-template-columns:10rem minmax(0,1fr);gap:clamp(1.5rem,4vw,3rem);align-items:start}
.about h2{max-width:26ch}
.about p{margin-top:1rem;color:var(--muted);font-size:1rem;line-height:1.7;max-width:64ch}
.about p strong{color:var(--ink);font-weight:600}
.contact h2{max-width:22ch}
.contact p{margin-top:1rem;color:var(--muted);max-width:56ch}
.links{margin-top:1.25rem;display:flex;flex-direction:column;border-top:1px solid var(--line)}
.links a{display:flex;align-items:baseline;justify-content:space-between;gap:1.5rem;padding:.9rem 0;border-bottom:1px solid var(--line);font-size:.9375rem;transition:color .2s}
.links a span{font:400 10.5px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--faint);flex:none}
.links a:hover{color:var(--accent)}
footer{margin-top:auto;border-top:1px solid var(--line);padding:1.5rem 0 2.5rem}
footer .wrap{display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap;font:400 10.5px/1.7 var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--faint)}
.progress{position:fixed;top:0;left:0;right:0;height:2px;z-index:50;pointer-events:none;opacity:0;transition:opacity .4s ease}
.progress i{display:block;height:100%;width:100%;background:var(--accent);transform:scaleX(0);transform-origin:left}
.progress.on{opacity:1}
.js [data-reveal]{opacity:0;transform:translateY(10px);
transition:opacity .5s ease,transform .65s cubic-bezier(.22,.61,.36,1);transition-delay:var(--d,0ms)}
.js [data-reveal].is-in{opacity:1;transform:none}
.cursor{position:fixed;left:0;top:0;z-index:60;width:24px;height:24px;pointer-events:none;color:var(--ink);
opacity:0;transition:opacity .35s ease;will-change:transform;filter:drop-shadow(0 2px 2px rgba(21,20,15,.22))}
.cursor.on{opacity:1}
.cursor svg{display:block;width:22px;height:22px;transform-origin:4px 2px;transition:transform .12s ease}
.cursor.down svg{transform:scale(.82)}
.cursor.grab svg{transform:scale(.9)}
.cursor path{fill:currentColor;stroke:var(--paper);stroke-width:1.3;stroke-linejoin:round}
.cursor .pulse{position:absolute;left:1px;top:1px;width:12px;height:12px;border-radius:50%;border:1.5px solid var(--accent);opacity:0;transform:scale(.3)}
.cursor .pulse.go{animation:cursorPulse .55s ease-out}
@keyframes cursorPulse{0%{opacity:.85;transform:scale(.3)}100%{opacity:0;transform:scale(3)}}
@media(pointer:coarse){.cursor{display:none}}
@media(max-width:720px){.sec-grid{grid-template-columns:1fr;gap:.75rem}.sec-grid .sec-label{display:block;margin-bottom:.75rem}}
@media(max-width:640px){
.project{grid-template-columns:2rem minmax(0,1fr);padding-inline:.5rem;margin-inline:-.5rem}
.host{grid-column:1/-1;padding-top:0;font-size:10.5px}
.sec-head{flex-direction:column;gap:.35rem}.sec-head .sec-label{order:-1}}
@media(prefers-reduced-motion:reduce){
.js [data-reveal]{opacity:1;transform:none;transition:none}
.progress,.cursor{display:none}.arrow{transition:none}}
/* admin */
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:clamp(1.25rem,3vw,1.75rem);max-width:620px}
.card h1{font-size:1.5rem;letter-spacing:-.02em;line-height:1.2;margin-bottom:1.25rem;max-width:none}
label{display:block;font-weight:600;font-size:.875rem;margin:1rem 0 .35rem}
.hint{color:var(--faint);font-size:.78rem;font-weight:400}
input,textarea,select{width:100%;background:#fff;color:var(--ink);border:1px solid var(--line-2);border-radius:6px;
padding:.6rem .7rem;font:inherit;font-size:.9375rem;transition:border-color .2s,box-shadow .2s}
input:focus,textarea:focus,select:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 3px var(--accent-soft)}
textarea{min-height:10rem;resize:vertical}
.row{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
@media(max-width:560px){.row{grid-template-columns:1fr}}
button{margin-top:1.25rem;background:var(--ink);color:var(--paper);border:0;border-radius:6px;
padding:.65rem 1.15rem;font:inherit;font-weight:600;font-size:.9375rem;cursor:pointer;transition:background .2s}
button:hover{background:var(--accent)}
button.ghost{background:transparent;color:var(--muted);border:1px solid var(--line-2);margin-top:0}
button.ghost:hover{color:var(--ink);border-color:var(--ink);background:transparent}
.msg{border-radius:6px;padding:.6rem .8rem;margin-bottom:1rem;font-weight:500;font-size:.875rem}
.msg.ok{background:#e8f2ec;color:#1d5c3f}.msg.err{background:#fbebe9;color:#93392f}
.bar{display:flex;justify-content:space-between;align-items:center;max-width:620px;margin-bottom:1rem;gap:1rem}
.bar form{display:inline}
"""

HEAD = """<!DOCTYPE html><html lang="en" class="js"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>{meta}
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='44' fill='%2315140f'/><circle cx='50' cy='50' r='16' fill='%23f6f5f1'/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{css}</style></head>"""

E = html.escape

PIXEL_CELLS = "".join('<b class="on"></b>' if i in (1, 3, 4, 6, 9, 11, 12, 14) else "<b></b>" for i in range(16))

# Served at /app.js (kept as a constant so the site stays a single deployable file).
SCRIPT = r"""
(function () {
  "use strict";

  var reduce = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  var fine = !!(window.matchMedia && window.matchMedia("(pointer: fine)").matches);
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";

  var items = Array.prototype.slice.call(document.querySelectorAll("[data-reveal]"));
  var bar = document.querySelector(".progress");
  var fill = bar ? bar.querySelector("i") : null;
  var cursor = document.querySelector(".cursor");

  function reveal(el) { if (el) el.classList.add("is-in"); }
  function revealAll() { for (var i = 0; i < items.length; i++) reveal(items[i]); }

  function paint() {
    if (!bar) return;
    var max = document.documentElement.scrollHeight - window.innerHeight;
    var p = max > 0 ? Math.min(1, Math.max(0, window.pageYOffset / max)) : 0;
    fill.style.transform = "scaleX(" + p + ")";
    bar.classList.toggle("on", window.pageYOffset > 4);
  }
  window.addEventListener("scroll", paint, { passive: true });
  window.addEventListener("resize", paint, { passive: true });
  paint();

  if (reduce) { revealAll(); return; }

  var cancelled = false;
  var raf = 0;

  function stopMotion() {
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
  }

  function clearDragStyles() {
    for (var i = 0; i < items.length; i++) {
      var el = items[i];
      el.style.transition = "";
      el.style.position = "";
      el.style.zIndex = "";
      el.style.opacity = "";
      el.style.transform = "";
    }
  }

  function cancel() {
    if (cancelled) return;
    cancelled = true;
    stopMotion();
    clearDragStyles();
    revealAll();
    if (cursor) {
      cursor.classList.remove("on");
      cursor.classList.remove("grab");
    }
    if (bar) bar.classList.remove("on");
  }

  ["wheel", "touchstart", "pointerdown", "keydown"].forEach(function (type) {
    window.addEventListener(type, cancel, { passive: true });
  });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) cancel();
  });

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function easeInOutCubic(p) {
    return p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
  }

  function easeOutCubic(p) {
    return 1 - Math.pow(1 - p, 3);
  }

  function scrollToY(y, ms) {
    return new Promise(function (resolve) {
      var from = window.pageYOffset;
      var dist = y - from;
      if (Math.abs(dist) < 2) { resolve(); return; }
      var t0 = 0;
      function step(now) {
        if (cancelled) { resolve(); return; }
        if (!t0) t0 = now;
        var p = Math.min(1, (now - t0) / ms);
        window.scrollTo(0, from + dist * easeInOutCubic(p));
        if (p < 1) raf = requestAnimationFrame(step);
        else resolve();
      }
      raf = requestAnimationFrame(step);
    });
  }

  function revealOnScroll() {
    if (!("IntersectionObserver" in window)) { revealAll(); return; }
    var io = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (entries[i].isIntersecting) {
          reveal(entries[i].target);
          io.unobserve(entries[i].target);
        }
      }
    }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
    for (var j = 0; j < items.length; j++) io.observe(items[j]);
  }

  function revealPassed() {
    var limit = window.pageYOffset + window.innerHeight * 0.9;
    for (var i = 0; i < items.length; i++) {
      if (!items[i].classList.contains("is-in") &&
          items[i].getBoundingClientRect().top + window.pageYOffset < limit) {
        reveal(items[i]);
      }
    }
  }

  function autoScroll() {
    var max = document.documentElement.scrollHeight - window.innerHeight;
    if (max < 160) return;
    var duration = Math.min(11000, Math.max(3600, max * 4.2));
    var t0 = 0;
    revealPassed();
    function step(now) {
      if (cancelled) return;
      if (!t0) t0 = now;
      var p = Math.min(1, (now - t0) / duration);
      var bottom = document.documentElement.scrollHeight - window.innerHeight;
      window.scrollTo(0, bottom * easeInOutCubic(p));
      revealPassed();
      if (p < 1) raf = requestAnimationFrame(step);
      else if (bar) bar.classList.remove("on");
    }
    setTimeout(function () { if (!cancelled) raf = requestAnimationFrame(step); }, 700);
  }

  /* ---- the cursor that drags the page together ---- */
  var cx = 0;
  var cy = 0;

  function placeCursor(x, y) {
    cx = x;
    cy = y;
    cursor.style.transform = "translate3d(" + x + "px," + y + "px,0)";
  }

  function moveCursor(x, y) {
    return new Promise(function (resolve) {
      var fx = cx;
      var fy = cy;
      var dist = Math.sqrt((x - fx) * (x - fx) + (y - fy) * (y - fy));
      var duration = Math.max(210, Math.min(520, dist * 0.8));
      var t0 = 0;
      function step(now) {
        if (cancelled) { resolve(); return; }
        if (!t0) t0 = now;
        var p = Math.min(1, (now - t0) / duration);
        var e = easeInOutCubic(p);
        var arc = Math.sin(p * Math.PI) * Math.min(26, 7 + dist / 9);
        placeCursor(fx + (x - fx) * e, fy + (y - fy) * e - arc);
        if (p < 1) raf = requestAnimationFrame(step);
        else { placeCursor(x, y); resolve(); }
      }
      raf = requestAnimationFrame(step);
    });
  }

  function releaseCursor() {
    cursor.classList.remove("grab");
    var pulse = cursor.querySelector(".pulse");
    if (pulse) {
      pulse.classList.remove("go");
      void pulse.offsetWidth;
      pulse.classList.add("go");
    }
  }

  function ensureVisible(el) {
    var r = el.getBoundingClientRect();
    var h = window.innerHeight;
    if (r.top >= h * 0.1 && r.bottom <= h * 0.9) return Promise.resolve();
    var max = document.documentElement.scrollHeight - h;
    var target = window.pageYOffset + r.top - h * 0.3;
    return scrollToY(Math.max(0, Math.min(max, target)), 480);
  }

  function dragIntoPlace(el, i) {
    return new Promise(function (resolve) {
      var h = el.getBoundingClientRect().height;
      var dir = i % 2 ? -1 : 1;
      var dx = 66 * dir;
      var dy = 72;
      var rot = 1.1 * dir;
      var gx = 17;
      var gy = Math.min(28, Math.max(10, h / 2));

      el.style.transition = "none";
      el.style.transform = "none";
      var r = el.getBoundingClientRect();

      el.style.position = "relative";
      el.style.zIndex = "5";
      el.style.opacity = "0.3";
      el.style.transform = "translate(" + dx + "px," + dy + "px) rotate(" + rot + "deg)";

      var grabX = r.left + dx + gx;
      var grabY = r.top + dy + gy;

      moveCursor(grabX, grabY).then(function () {
        if (cancelled) { resolve(); return; }
        cursor.classList.add("grab");

        var t0 = 0;
        var duration = 400;
        function step(now) {
          if (cancelled) { resolve(); return; }
          if (!t0) t0 = now;
          var p = Math.min(1, (now - t0) / duration);
          var e = easeOutCubic(p);
          var tx = dx * (1 - e);
          var ty = dy * (1 - e);
          var tr = rot * (1 - e);
          el.style.transform = "translate(" + tx + "px," + ty + "px) rotate(" + tr + "deg)";
          el.style.opacity = String(0.3 + 0.7 * e);
          placeCursor(r.left + tx + gx, r.top + ty + gy);
          if (p < 1) {
            raf = requestAnimationFrame(step);
          } else {
            releaseCursor();
            reveal(el);
            el.style.transition = "";
            el.style.position = "";
            el.style.zIndex = "";
            el.style.opacity = "";
            el.style.transform = "";
            resolve();
          }
        }
        raf = requestAnimationFrame(step);
      });
    });
  }

  async function play() {
    placeCursor(window.innerWidth * 0.6, window.innerHeight * 0.6);
    await sleep(220);
    if (cancelled) return;
    cursor.classList.add("on");
    await sleep(220);

    for (var i = 0; i < items.length; i++) {
      if (cancelled) return;
      var el = items[i];
      await ensureVisible(el);
      if (cancelled) return;
      await dragIntoPlace(el, i);
      if (cancelled) return;
      await sleep(90);
    }

    if (cancelled) return;
    var max = document.documentElement.scrollHeight - window.innerHeight;
    await scrollToY(max, 650);
    if (cancelled) return;
    await moveCursor(window.innerWidth - 70, window.innerHeight - 70);
    if (cancelled) return;
    await sleep(220);
    if (cancelled) return;
    cursor.classList.remove("on");
    if (bar) bar.classList.remove("on");
  }

  if (location.hash) {
    revealOnScroll();
    return;
  }

  window.scrollTo(0, 0);

  if (!cursor || !fine) {
    revealOnScroll();
    autoScroll();
    return;
  }

  play().catch(function () { cancel(); });
})();
"""


def project(idx, url, host, badge, name, status, desc, chips):
    metas = " &middot; ".join(E(c) for c in chips)
    return (
        f'<a class="project" data-reveal href="{url}" style="--d:{idx * 80}ms">'
        f'<span class="idx">{idx + 1:02d}</span>'
        f'<div class="project-body"><div class="project-head">{badge}'
        f'<span class="name">{name}</span>'
        f'<span class="status s-{status}">{E(STATUS_LABEL[status])}</span></div>'
        f'<p class="desc">{desc}</p><p class="meta">{metas}</p></div>'
        f'<span class="host">{host} <span class="arrow" aria-hidden="true">&#8599;</span></span></a>'
    )


def home_page(s):
    paras = "".join(f"<p>{E(p.strip())}</p>" for p in s["about"].split("\n\n") if p.strip())
    has_contact = bool(s["email"] or s["discord"])
    links = ""
    if s["email"]:
        links += f'<a href="mailto:{E(s["email"])}"><span>Email</span>{E(s["email"])}</a>'
    if s["discord"]:
        links += f'<a href="{E(s["discord"])}" rel="noopener" target="_blank"><span>Discord</span>{E(s["discord"].replace("https://", ""))}</a>'
    donut = project(
        0, "https://donut.voidlabs.fun", "donut.voidlabs.fun",
        '<span class="badge donut" aria-hidden="true"></span>', "Donut", s["donut_status"],
        "A Discord bot and web dashboard for the Donut network. Player stats, payments, chat logs and admin tooling, all in one place.",
        ["Player stats", "Payments", "AI chat", "Chat logs", "Admin dashboard"],
    )
    pixel = project(
        1, "https://pixel.voidlabs.fun", "pixel.voidlabs.fun",
        f'<span class="badge pixel" aria-hidden="true">{PIXEL_CELLS}</span>', "Pixel", s["pixel_status"],
        "An all-in-one community bot with tickets, giveaways, applications, moderation and a full web dashboard.",
        ["Tickets", "Giveaways", "Applications", "Moderation", "Staff tools"],
    )
    nav = '<a href="#projects">Projects</a><a href="#about">About</a>' + ('<a href="#contact">Contact</a>' if has_contact else "")
    contact = ""
    if has_contact:
        contact = (
            '<section id="contact"><div class="wrap sec-grid">'
            '<div data-reveal><span class="sec-label">03 &mdash; Contact</span></div>'
            '<div class="contact" data-reveal style="--d:90ms"><h2>Get in touch</h2>'
            "<p>Questions, bug reports or ideas? Reach me here.</p>"
            f'<div class="links">{links}</div></div></div></section>'
        )
    body = (
        '<body><div class="cursor" aria-hidden="true">'
        '<svg viewBox="0 0 24 24"><path d="M5 2.6 19.3 11l-6.5 1.4-2.9 6.7z"/></svg>'
        '<span class="pulse"></span></div>'
        '<div class="progress" aria-hidden="true"><i></i></div>'
        f'<header><div class="wrap"><a class="mark" href="#top"><span class="dot" aria-hidden="true"></span>Void Labs</a>'
        f'<nav class="top" aria-label="Page sections">{nav}</nav></div></header>'
        '<main id="top">'
        '<section class="hero"><div class="wrap">'
        '<p class="eyebrow" data-reveal>Independent software studio</p>'
        '<h1 data-reveal style="--d:70ms">Things built in the void.</h1>'
        '<p class="lede" data-reveal style="--d:150ms">Discord bots and web dashboards for online communities. Designed, built and maintained in-house.</p>'
        '<div class="hero-meta" data-reveal style="--d:230ms"><span>Two products live</span>'
        '<span>Discord &amp; web</span><span>Built in-house</span></div>'
        '</div></section>'
        '<section id="projects"><div class="wrap">'
        '<div class="sec-head" data-reveal><h2>Projects</h2>'
        '<span class="sec-label">01 &mdash; Selected work</span></div>'
        f'<div class="projects">{donut}{pixel}</div>'
        '</div></section>'
        '<section id="about"><div class="wrap sec-grid">'
        '<div data-reveal><span class="sec-label">02 &mdash; About</span></div>'
        f'<div class="about" data-reveal style="--d:90ms"><h2>A one-person studio.</h2>'
        f'<p><strong>Hi, I&rsquo;m {E(s["name"])}.</strong></p>{paras}</div>'
        '</div></section>'
        f'{contact}'
        '</main>'
        f'<footer><div class="wrap"><span>&copy; {time.gmtime().tm_year} Void Labs</span>'
        '<span>Independent software studio</span></div></footer>'
        '<script src="/app.js"></script>'
        '<noscript><style>.js [data-reveal]{opacity:1;transform:none}</style></noscript>'
        '</body></html>'
    )
    return HEAD.format(title="Void Labs", css=CSS, meta='<meta name="description" content="Void Labs builds Discord bots and web dashboards. Home of Donut and Pixel.">') + body


def admin_login_page(msg=""):
    m = f'<div class="msg err">{E(msg)}</div>' if msg else ""
    body = (
        '<body><main><div class="wrap" style="padding:3rem 0"><div class="card"><h1>Admin login</h1>'
        f'{m}<form method="post" action="/admin/login" autocomplete="on">'
        '<label for="u">Username</label><input id="u" name="username" autocomplete="username" required>'
        '<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>'
        '<button type="submit">Log in</button></form></div></div></main></body></html>'
    )
    return HEAD.format(title="Admin login", css=CSS, meta='<meta name="robots" content="noindex,nofollow">') + body


def admin_page(s, csrf, msg="", kind="ok"):
    def opts(cur):
        return "".join(f'<option value="{v}"{" selected" if v == cur else ""}>{l}</option>' for v, l in STATUSES)
    m = f'<div class="msg {kind}">{E(msg)}</div>' if msg else ""
    body = (
        '<body><main><div class="wrap" style="padding:2rem 0 3rem">'
        '<div class="bar"><a class="mark" href="/"><span class="dot" aria-hidden="true"></span>View site</a>'
        f'<form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{csrf}">'
        '<button class="ghost" type="submit">Log out</button></form></div>'
        f'<div class="card"><h1>Site settings</h1>{m}'
        f'<form method="post" action="/admin/save"><input type="hidden" name="csrf" value="{csrf}">'
        '<div class="row"><div><label for="ds">Donut status</label>'
        f'<select id="ds" name="donut_status">{opts(s["donut_status"])}</select></div>'
        '<div><label for="ps">Pixel status</label>'
        f'<select id="ps" name="pixel_status">{opts(s["pixel_status"])}</select></div></div>'
        '<label for="n">Your name <span class="hint">(shown in "Hi, I&rsquo;m ...")</span></label>'
        f'<input id="n" name="name" maxlength="40" value="{E(s["name"])}" required>'
        '<label for="e">Contact email <span class="hint">(leave empty to hide)</span></label>'
        f'<input id="e" name="email" type="email" maxlength="120" value="{E(s["email"])}" placeholder="you@example.com">'
        '<label for="d">Support Discord <span class="hint">(leave empty to hide)</span></label>'
        f'<input id="d" name="discord" maxlength="80" value="{E(s["discord"])}" placeholder="discord.gg/yourcode">'
        '<label for="a">About me <span class="hint">(blank line = new paragraph)</span></label>'
        f'<textarea id="a" name="about" maxlength="1500">{E(s["about"])}</textarea>'
        '<button type="submit">Save changes</button></form></div></div></main></body></html>'
    )
    return HEAD.format(title="Admin", css=CSS, meta='<meta name="robots" content="noindex,nofollow">') + body


NOT_ENABLED = HEAD.format(title="Admin disabled", css=CSS, meta='<meta name="robots" content="noindex">') + (
    '<body><main><div class="wrap" style="padding-top:3rem"><div class="card"><h1>Admin is off</h1>'
    "<p>Set an <strong>ADMIN_PASSWORD</strong> environment variable (at least 10 characters) and restart the server to turn it on.</p>"
    "</div></div></main></body></html>"
)


# ---------- http ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "VoidLabs"
    sys_version = ""
    timeout = 15  # drop stalled / slow connections

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)

    # helpers
    def ip(self):
        # Use the LAST entry: that one is appended by the hosting proxy, the earlier ones
        # can be forged by the client.
        xff = self.headers.get("X-Forwarded-For")
        return xff.split(",")[-1].strip() if xff else self.client_address[0]

    def secure(self):
        return self.headers.get("X-Forwarded-Proto", "") == "https"

    def send(self, code, body, ctype="text/html; charset=utf-8", extra=None, private=False):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        if private:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex, nofollow")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def redirect(self, to, extra=None):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()

    def session(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "vl_session" and check_session(v):
                return v
        return None

    def cookie(self, value, max_age):
        flags = f"vl_session={value}; Path=/admin; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        return ("Set-Cookie", flags + ("; Secure" if self.secure() else ""))

    def form(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n < 0 or n > 20000:
            return None
        return parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True)

    # routes
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/":
            return self.send(200, home_page(load()), extra=[("Cache-Control", "public, max-age=30")])
        if path == "/healthz":
            return self.send(200, "ok", "text/plain")
        if path == "/app.js":
            return self.send(200, SCRIPT, "text/javascript; charset=utf-8",
                             extra=[("Cache-Control", "public, max-age=300")])
        if path == "/admin":
            if not ADMIN_ON:
                return self.send(503, NOT_ENABLED, private=True)
            tok = self.session()
            if not tok:
                return self.send(200, admin_login_page(), private=True)
            return self.send(200, admin_page(load(), csrf_for(tok)), private=True)
        self.send(404, HEAD.format(title="Not found", css=CSS, meta="") +
                  '<body><main><div class="wrap" style="padding-top:4rem"><h1>Not found</h1>'
                  '<p class="lede"><a href="/">Back to Void Labs</a></p></div></main></body></html>')

    do_HEAD = do_GET

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if not path.startswith("/admin") or not ADMIN_ON:
            return self.send(404, "Not found", "text/plain")
        form = self.form()
        if form is None:
            return self.send(413, "Too large", "text/plain")

        if path == "/admin/login":
            ip, now = self.ip(), time.time()
            if len(_fails) > 1000:
                for k in [k for k, (_, u) in _fails.items() if u < now]:
                    _fails.pop(k, None)
            count, until = _fails.get(ip, (0, 0))
            if until and until <= now:
                count, until = 0, 0  # lock expired: start fresh
            if until > now:
                return self.send(429, admin_login_page("Too many attempts. Try again in a few minutes."), private=True)
            u, p = form.get("username", [""])[0], form.get("password", [""])[0]
            ok_u, ok_p = same(u, ADMIN_USER), same(p, ADMIN_PASS)
            if ok_u and ok_p:
                _fails.pop(ip, None)
                return self.redirect("/admin", [self.cookie(make_session(), SESSION_TTL)])
            count += 1
            _fails[ip] = (count, now + 300 if count >= 5 else 0)
            time.sleep(1)
            return self.send(401, admin_login_page("Wrong username or password."), private=True)

        tok = self.session()
        if not tok:
            return self.redirect("/admin")
        if not same(form.get("csrf", [""])[0], csrf_for(tok)):
            return self.send(403, "Bad request token. Reload the page and try again.", "text/plain")

        if path == "/admin/logout":
            return self.redirect("/admin", [self.cookie("", 0)])
        if path == "/admin/save":
            clean, err = validate(form)
            if err:
                return self.send(400, admin_page(clean, csrf_for(tok), err, "err"), private=True)
            save(clean)
            return self.send(200, admin_page(clean, csrf_for(tok), "Saved. The public site is updated.", "ok"), private=True)
        self.send(404, "Not found", "text/plain")


if __name__ == "__main__":
    if not ADMIN_ON:
        print("WARNING: ADMIN_PASSWORD missing or shorter than 10 characters. /admin is disabled.", flush=True)
    if len(_SS) < 16:
        print("NOTE: SESSION_SECRET missing or under 16 chars. Using a random key; logins reset on restart.", flush=True)
    print(f"Void Labs site running on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
