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
# Design system: Klein-blue "void" field, pink (Donut) and sun-yellow (Pixel) product panels.
# Spacing scale: 4 8 12 16 24 32 48 72 112 (--s1..--s9). Radii: pill for actions, 6px for inputs, 0 for panels.
CSS = """
:root{
--blue:#2a2fe0;--blue-deep:#1a1eb0;--ink:#0c0d2e;--pale:#eceefa;--paper:#ffffff;
--pink:#ff9dbb;--sun:#ffd13d;--muted:#454766;--line:rgba(12,13,46,.22);
--ok:#0a7a45;--warn:#9a6200;--bad:#b3261e;--focus:var(--blue);
--display:"Bricolage Grotesque","Arial Narrow",system-ui,sans-serif;
--body:"Figtree",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--s1:.25rem;--s2:.5rem;--s3:.75rem;--s4:1rem;--s5:1.5rem;--s6:2rem;--s7:3rem;--s8:4.5rem;--s9:7rem;
--gutter:clamp(1.25rem,4vw,3rem);--bar:4rem}
*{box-sizing:border-box;margin:0;padding:0}
html{background:var(--blue);-webkit-text-size-adjust:100%;scroll-padding-top:var(--bar)}
@media(prefers-reduced-motion:no-preference){html{scroll-behavior:smooth}}
body{font-family:var(--body);color:var(--ink);background:var(--pale);line-height:1.6;font-size:1.0625rem;
min-height:100vh;min-height:100svh;display:flex;flex-direction:column;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
::selection{background:var(--sun);color:var(--ink)}
:focus-visible{outline:3px solid var(--focus);outline-offset:3px}
.on-dark{--focus:var(--sun)}
main{flex:1}
.wrap{width:100%;max-width:80rem;margin:0 auto;padding-inline:var(--gutter)}
.skip{position:absolute;left:var(--s4);top:-4rem;z-index:100;background:var(--sun);color:var(--ink);padding:var(--s2) var(--s4);font-weight:700;border-radius:999px}
.skip:focus{top:var(--s3)}

/* header */
.site-head{position:sticky;top:0;z-index:30;background:var(--blue);color:var(--pale);height:var(--bar)}
.site-head .wrap{height:100%;display:flex;align-items:center;justify-content:space-between;gap:var(--s4)}
.mark{display:inline-flex;align-items:center;gap:var(--s3);font:700 1.25rem/1 var(--display);letter-spacing:-.02em}
.mark svg{width:1.75rem;height:1.75rem;flex:none;transition:transform .5s cubic-bezier(.3,1.4,.5,1)}
.mark:hover svg{transform:rotate(180deg)}
.site-head nav{display:flex;gap:var(--s1)}
.site-head nav a{font-weight:600;font-size:1rem;padding:.55rem 1rem;border-radius:999px;transition:background .2s,color .2s}
.site-head nav a:hover{background:var(--pale);color:var(--blue-deep)}

/* hero */
.hero{position:relative;overflow:hidden;background:var(--blue);color:var(--pale);
min-height:calc(100vh - var(--bar));min-height:calc(100svh - var(--bar));display:flex;align-items:flex-end}
.hero-in{position:relative;z-index:2;padding-block:var(--s8) var(--s7)}
h1{font:800 clamp(3.5rem,12.5vw,11.5rem)/.86 var(--display);letter-spacing:-.045em;font-variation-settings:"opsz" 96}
h1 .ln{display:block;overflow:hidden;padding-bottom:.08em;margin-bottom:-.08em}
h1 .ln>span{display:block}
.hero-foot{margin-top:var(--s7);display:grid;grid-template-columns:minmax(0,28rem) 1fr;gap:var(--s6) var(--s8);align-items:end;
border-top:2px solid var(--pale);padding-top:var(--s5)}
.lede{font-size:clamp(1.125rem,1.7vw,1.375rem);line-height:1.5;max-width:30em}
.live{list-style:none;display:flex;flex-wrap:wrap;gap:var(--s2) var(--s6);justify-content:flex-end}
.live li{display:flex;align-items:center;gap:var(--s2);font-weight:600}
.live li span{opacity:.82;font-weight:500}
.disc-wrap{position:absolute;z-index:1;right:-9vw;top:50%;width:min(66vh,54vw);aspect-ratio:1;margin-top:calc(min(66vh,54vw)/-2 - 2rem);will-change:transform}
.disc{position:absolute;inset:0;border-radius:50%;background:var(--ink)}
.disc::after{content:"";position:absolute;inset:-6%;border-radius:50%;border:2px solid rgba(236,238,250,.35)}
.orb{position:absolute;inset:0;animation:spin 46s linear infinite}
.orb::after{content:"";position:absolute;left:50%;top:-1%;width:11%;aspect-ratio:1;margin-left:-5.5%}
.o-donut::after{border-radius:50%;background:radial-gradient(circle,var(--blue) 0 26%,var(--pink) 28%)}
.o-pixel{inset:-13%;animation-duration:70s;animation-direction:reverse}
.o-pixel::after{background:var(--sun);width:7%;margin-left:-3.5%}
@keyframes spin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:no-preference){
.disc{animation:discIn 1.3s cubic-bezier(.2,.8,.2,1) both}
h1 .ln>span{animation:rise .9s cubic-bezier(.2,.8,.2,1) both}
h1 .ln:nth-child(2)>span{animation-delay:.1s}h1 .ln:nth-child(3)>span{animation-delay:.2s}
.hero-foot{animation:fade .8s .6s both}}
@keyframes rise{from{transform:translateY(105%)}}
@keyframes discIn{from{transform:scale(.55);opacity:0}}
@keyframes fade{from{opacity:0}}

/* status marks: shape + word, never colour alone */
.st{display:inline-block;width:.8rem;height:.8rem;flex:none;border-radius:50%;background:var(--ok);box-shadow:0 0 0 2px var(--paper)}
.st-maintenance{background:var(--warn);border-radius:1px;transform:rotate(45deg)}
.st-offline{background:transparent;border:2px solid var(--bad);box-shadow:none}
.on-dark .st{background:#6be3a0;box-shadow:none}.on-dark .st-maintenance{background:var(--sun)}.on-dark .st-offline{background:transparent;border-color:#ff8f86}
.admin-side .st{background:var(--ok)}

/* projects */
.projects{background:var(--pale)}
.sec-title{padding-block:var(--s8) var(--s7)}
h2{font:800 clamp(2.25rem,5.6vw,4.75rem)/.95 var(--display);letter-spacing:-.035em;max-width:14ch}
.project{display:block;position:relative;overflow:hidden}
.p-donut{background:var(--pink)}.p-pixel{background:var(--sun)}
.project-in{max-width:80rem;margin:0 auto;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:var(--s7) var(--s8);align-items:center;padding:var(--s8) var(--gutter)}
.p-pixel .p-art{order:-1}
.p-status{display:inline-flex;align-items:center;gap:var(--s2);font-weight:700;font-size:1rem}
h3{font:800 clamp(3.5rem,9vw,8rem)/.88 var(--display);letter-spacing:-.045em;margin:var(--s4) 0 var(--s5)}
.desc{font-size:1.1875rem;line-height:1.55;max-width:30em}
.feats{list-style:none;margin-top:var(--s6);display:grid;grid-template-columns:1fr 1fr;column-gap:var(--s5);max-width:30rem}
.feats li{border-top:2px solid var(--ink);padding:var(--s3) 0 var(--s3);font-weight:600}
.btn{display:inline-flex;align-items:center;gap:var(--s3);margin:0;background:var(--ink);color:var(--pale);
font:700 1rem/1 var(--body);padding:1.05rem 1.5rem;border-radius:999px;border:2px solid var(--ink);cursor:pointer;
transition:background .2s,color .2s,border-color .2s}
.p-copy .btn{margin-top:var(--s6)}
.btn svg{width:1.1rem;height:1.1rem;transition:transform .25s cubic-bezier(.3,1.3,.5,1)}
.project:hover .btn,.btn:hover{background:var(--blue);border-color:var(--blue)}
.project:hover .btn svg,.btn:hover svg{transform:translate(3px,-3px)}
.project:focus-visible{outline-offset:-6px;outline-color:var(--ink)}
.p-art{display:flex;justify-content:center;align-items:center}
.ring{width:min(100%,32rem);height:auto;overflow:visible}
.ring .spin{transform-origin:220px 220px;animation:spin 80s linear infinite}
.project:hover .ring .spin{animation-duration:24s}
.ring text{font:700 21px var(--body);fill:var(--ink)}
.px{display:grid;grid-template-columns:repeat(7,1fr);gap:clamp(4px,1vw,10px);width:min(100%,26rem);aspect-ratio:1}
.px b{background:var(--ink);transition:opacity .3s}
.px b.off{opacity:.13}
.p-pixel:hover .px b.off{animation:blink 1.6s steps(1) infinite;animation-delay:calc(var(--i)*61ms)}
@keyframes blink{0%{opacity:.13}50%{opacity:.7}}

/* about + contact */
.about{background:var(--ink);color:var(--pale)}
.about-in{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:var(--s7) var(--s8);padding-block:var(--s9) var(--s8)}
.about h2{color:var(--pale);max-width:9ch}
.about .copy p{font-size:1.1875rem;line-height:1.65;max-width:34em}
.about .copy p+p{margin-top:var(--s5)}
.about .copy p:first-child{font:600 clamp(1.4rem,2.3vw,1.9rem)/1.3 var(--display);letter-spacing:-.015em}
.contact{background:var(--ink);color:var(--pale);border-top:2px solid rgba(236,238,250,.28)}
.contact-in{padding-block:var(--s7) var(--s9);display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:var(--s5) var(--s8)}
.contact h2{font-size:clamp(1.75rem,3vw,2.5rem);letter-spacing:-.025em;max-width:none}
.contact p{margin-top:var(--s3);color:#c9ccee;max-width:26em}
.links a{display:flex;justify-content:space-between;align-items:baseline;gap:var(--s5);padding:var(--s4) 0;border-bottom:2px solid rgba(236,238,250,.28);
font:600 clamp(1.25rem,2.4vw,1.875rem)/1.2 var(--display);letter-spacing:-.02em;overflow-wrap:anywhere;transition:color .2s,padding .25s}
.links a:first-child{border-top:2px solid rgba(236,238,250,.28)}
.links a small{font:600 1rem var(--body);color:#c9ccee;flex:none;letter-spacing:0}
.links a:hover{color:var(--pink);padding-left:var(--s3)}

/* footer */
.site-foot{background:var(--blue);color:var(--pale);overflow:hidden}
.foot-row{display:flex;justify-content:space-between;gap:var(--s4);flex-wrap:wrap;padding-block:var(--s5);font-weight:500}
.foot-word{font:800 21.2vw/.74 var(--display);letter-spacing:-.055em;white-space:nowrap;padding:var(--s5) 0 0 calc(var(--gutter) - .04em);user-select:none;overflow:hidden}

/* admin + utility pages */
.panel{background:var(--paper);border:2px solid var(--ink);padding:clamp(1.25rem,3vw,2rem)}
.admin-grid{display:grid;grid-template-columns:minmax(0,18rem) minmax(0,1fr);gap:var(--s7);padding-block:var(--s7) var(--s8);align-items:start}
.admin-side{position:sticky;top:calc(var(--bar) + var(--s5))}
.admin-side h1{font-size:clamp(2.5rem,5vw,4rem);line-height:.92;letter-spacing:-.04em;margin-bottom:var(--s5)}
.admin-side p{color:var(--muted);margin-bottom:var(--s4);max-width:22em}
.admin-side ul{list-style:none;margin:var(--s5) 0}
.admin-side li{display:flex;align-items:center;gap:var(--s3);padding:var(--s3) 0;border-top:2px solid var(--ink);font-weight:600}
.admin-side li:last-child{border-bottom:2px solid var(--ink)}
.admin-side li span:not(.st){margin-left:auto;font-weight:500;color:var(--muted)}
.admin-side .st{box-shadow:0 0 0 2px var(--pale)}
fieldset{border:0;margin:0 0 var(--s7)}
legend{font:800 1.75rem/1.1 var(--display);letter-spacing:-.025em;margin-bottom:var(--s4);padding:0}
label{display:block;font-weight:700;font-size:1rem;margin:var(--s5) 0 var(--s2)}
.hint{color:var(--muted);font-size:.9375rem;font-weight:500}
input,textarea,select{width:100%;background:var(--paper);color:var(--ink);border:2px solid var(--ink);border-radius:6px;
padding:.75rem .9rem;font:inherit;font-size:1rem;transition:box-shadow .15s}
input:focus,textarea:focus,select:focus{outline:3px solid var(--blue);outline-offset:2px}
textarea{min-height:12rem;resize:vertical;line-height:1.55}
.row{display:grid;grid-template-columns:1fr 1fr;gap:var(--s5)}
.row label:first-child{margin-top:0}
.actions{display:flex;gap:var(--s3);align-items:center;flex-wrap:wrap}
.btn.ghost{background:transparent;color:var(--ink)}
.btn.ghost:hover{background:var(--ink);color:var(--pale);border-color:var(--ink)}
.on-dark .btn.ghost{color:var(--pale);border-color:var(--pale)}
.on-dark .btn.ghost:hover{background:var(--pale);color:var(--blue-deep)}
.btn.sm{padding:.7rem 1.1rem}
.msg{border-left:6px solid;padding:var(--s3) var(--s4);margin-bottom:var(--s5);font-weight:600}
.msg.ok{background:#ddf3e6;color:#0a5430;border-color:var(--ok)}
.msg.err{background:#fbe3e0;color:#8c1c15;border-color:var(--bad)}
.gate{background:var(--blue);min-height:100vh;min-height:100svh;display:grid;place-items:center;padding:var(--gutter)}
.gate .panel{width:100%;max-width:26rem}
.gate h1{font-size:clamp(2.5rem,9vw,3.5rem);line-height:.92;margin-bottom:var(--s5)}
.gate .mark{color:var(--pale);margin-bottom:var(--s5)}
.gate .btn{margin-top:var(--s6);width:100%;justify-content:center}
.gate p{margin-bottom:var(--s3)}
.lost{background:var(--blue);color:var(--pale);flex:1;display:flex;align-items:center}
.lost h1{font-size:clamp(5rem,22vw,16rem)}
.lost p{font-size:1.25rem;margin:var(--s5) 0}
.lost .btn{background:var(--pale);color:var(--blue-deep);border-color:var(--pale)}
.lost .btn:hover{background:var(--sun);color:var(--ink);border-color:var(--sun)}

@media(max-width:60rem){
.hero-foot{grid-template-columns:1fr}.live{justify-content:flex-start}
.project-in,.about-in,.contact-in{grid-template-columns:1fr}
.p-pixel .p-art{order:0}
.p-art{justify-content:flex-start}.ring{width:min(100%,24rem)}.px{width:min(100%,18rem)}
.admin-grid{grid-template-columns:1fr}.admin-side{position:static}
}
@media(max-width:40rem){
:root{--bar:3.5rem}
.site-head nav a{padding:.5rem .7rem;font-size:.9375rem}
.disc-wrap{width:92vw;right:-38vw;top:30%;margin-top:0;opacity:.9}
.hero{align-items:flex-end}
.feats{grid-template-columns:1fr}
.row{grid-template-columns:1fr}
.links a small{display:none}
}
@media(prefers-reduced-motion:reduce){.orb,.ring .spin{animation:none}.p-pixel:hover .px b.off{animation:none}}
"""

HEAD = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>{meta}
<meta name="theme-color" content="#2a2fe0">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='50' fill='%232a2fe0'/><circle cx='50' cy='54' r='24' fill='%230c0d2e'/><circle cx='50' cy='17' r='8' fill='%23ffd13d'/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600..800&family=Figtree:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{css}</style></head>"""

E = html.escape

LOGO = ('<svg viewBox="0 0 28 28" aria-hidden="true"><circle cx="14" cy="14" r="13" fill="none" stroke="currentColor" stroke-width="2"/>'
        '<circle cx="14" cy="16" r="6.5" fill="currentColor"/><circle cx="14" cy="5.5" r="2.4" fill="#ffd13d"/></svg>')
ARROW = ('<svg viewBox="0 0 20 20" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="square">'
         '<path d="M5 15 15 5M7 5h8v8"/></svg>')

# 7x7 pixel "P" glyph for the Pixel panel
_P = ("1111100", "1000010", "1000010", "1111100", "1000000", "1000000", "1000000")
PIXEL_CELLS = "".join(
    f'<b class="{"on" if c == "1" else "off"}" style="--i:{r * 7 + i}"></b>'
    for r, row in enumerate(_P) for i, c in enumerate(row)
)

RING_TEXT = "Player stats / Payments / AI chat / Chat logs / Admin dashboard / "
DONUT_ART = (
    '<svg class="ring" viewBox="0 0 440 440" aria-hidden="true"><defs><path id="rp" d="M220 220m-192 0a192 192 0 1 1 384 0a192 192 0 1 1-384 0"/></defs>'
    '<g class="spin"><text><textPath href="#rp" textLength="1200" lengthAdjust="spacing">' + E(RING_TEXT) + '</textPath></text></g>'
    '<circle cx="220" cy="220" r="118" fill="none" stroke="#2a2fe0" stroke-width="86"/>'
    '<circle cx="220" cy="220" r="75" fill="#ff9dbb"/></svg>'
)

# Served at /app.js. Only job: a light parallax on the hero disc (pointer devices, motion allowed).
SCRIPT = r"""
(function () {
  "use strict";
  var q = function (m) { return window.matchMedia && window.matchMedia(m).matches; };
  var hero = document.querySelector(".hero");
  var disc = document.querySelector(".disc-wrap");
  if (!hero || !disc || q("(prefers-reduced-motion: reduce)") || !q("(pointer: fine)")) return;
  var tx = 0, ty = 0, x = 0, y = 0, raf = 0;
  function tick() {
    x += (tx - x) * 0.08; y += (ty - y) * 0.08;
    disc.style.transform = "translate3d(" + x.toFixed(2) + "px," + y.toFixed(2) + "px,0)";
    raf = (Math.abs(tx - x) > 0.1 || Math.abs(ty - y) > 0.1) ? requestAnimationFrame(tick) : 0;
  }
  function go() { if (!raf) raf = requestAnimationFrame(tick); }
  hero.addEventListener("pointermove", function (e) {
    var b = hero.getBoundingClientRect();
    tx = ((e.clientX - b.left) / b.width - 0.5) * -44;
    ty = ((e.clientY - b.top) / b.height - 0.5) * -44;
    go();
  });
  hero.addEventListener("pointerleave", function () { tx = 0; ty = 0; go(); });
})();
"""


def status_mark(status):
    return f'<span class="st st-{status}" aria-hidden="true"></span>'


def project(cls, url, host, art, name, status, desc, chips):
    feats = "".join(f"<li>{E(c)}</li>" for c in chips)
    return (
        f'<a class="project {cls}" href="{url}">'
        '<div class="project-in"><div class="p-copy">'
        f'<p class="p-status">{status_mark(status)}{E(STATUS_LABEL[status])}</p>'
        f'<h3>{name}</h3><p class="desc">{desc}</p><ul class="feats">{feats}</ul>'
        f'<span class="btn">Open {host}{ARROW}</span></div>'
        f'<div class="p-art" aria-hidden="true">{art}</div></div></a>'
    )


def page_shell(nav, body, foot):
    return (
        '<body><a class="skip" href="#top">Skip to content</a>'
        f'<header class="site-head on-dark"><div class="wrap"><a class="mark" href="/" aria-label="Void Labs home">{LOGO}Void Labs</a>'
        f'<nav aria-label="Page sections">{nav}</nav></div></header>'
        f'<main id="top">{body}</main>{foot}'
        '<script src="/app.js"></script></body></html>'
    )


def home_page(s):
    paras = "".join(f"<p>{E(p.strip())}</p>" for p in s["about"].split("\n\n") if p.strip())
    has_contact = bool(s["email"] or s["discord"])
    links = ""
    if s["email"]:
        links += f'<a href="mailto:{E(s["email"])}"><small>Email</small>{E(s["email"])}</a>'
    if s["discord"]:
        links += f'<a href="{E(s["discord"])}" rel="noopener" target="_blank"><small>Discord</small>{E(s["discord"].replace("https://", ""))}</a>'
    donut = project(
        "p-donut", "https://donut.voidlabs.fun", "donut.voidlabs.fun", DONUT_ART, "Donut", s["donut_status"],
        "A Discord bot and web dashboard for the Donut network. Player stats, payments, chat logs and admin tooling, all in one place.",
        ["Player stats", "Payments", "AI chat", "Chat logs", "Admin dashboard"],
    )
    pixel = project(
        "p-pixel", "https://pixel.voidlabs.fun", "pixel.voidlabs.fun", f'<div class="px">{PIXEL_CELLS}</div>', "Pixel", s["pixel_status"],
        "An all-in-one community bot with tickets, giveaways, applications, moderation and a full web dashboard.",
        ["Tickets", "Giveaways", "Applications", "Moderation", "Staff tools"],
    )
    nav = '<a href="#projects">Projects</a><a href="#about">About</a>' + ('<a href="#contact">Contact</a>' if has_contact else "")
    live = "".join(
        f"<li>{status_mark(s[k])}{n} <span>{E(STATUS_LABEL[s[k]])}</span></li>"
        for n, k in (("Donut", "donut_status"), ("Pixel", "pixel_status"))
    )
    contact = ""
    if has_contact:
        contact = (
            '<section class="contact on-dark" id="contact"><div class="wrap contact-in">'
            '<div><h2>Get in touch</h2><p>Questions, bug reports or ideas? Reach me here.</p></div>'
            f'<div class="links">{links}</div></div></section>'
        )
    body = (
        '<section class="hero on-dark"><div class="disc-wrap" aria-hidden="true"><div class="disc"></div>'
        '<div class="orb o-donut"></div><div class="orb o-pixel"></div></div>'
        '<div class="wrap hero-in"><h1><span class="ln"><span>Things</span></span><span class="ln"><span>built in</span></span>'
        '<span class="ln"><span>the void.</span></span></h1>'
        '<div class="hero-foot"><p class="lede">Discord bots and web dashboards for online communities. Designed, built and maintained in-house.</p>'
        f'<ul class="live" aria-label="Product status">{live}</ul></div></div></section>'
        '<section class="projects" id="projects"><div class="wrap sec-title"><h2>Two products, built end to end.</h2></div>'
        f'{donut}{pixel}</section>'
        '<section class="about on-dark" id="about"><div class="wrap about-in">'
        f'<h2>Hi, I&rsquo;m {E(s["name"])}.</h2><div class="copy">{paras}</div></div></section>'
        f'{contact}'
    )
    foot = (
        '<footer class="site-foot on-dark"><div class="wrap"><div class="foot-row">'
        f'<span>&copy; {time.gmtime().tm_year} Void Labs</span><span>Independent software studio</span></div></div>'
        '<div class="foot-word" aria-hidden="true">Void Labs</div></footer>'
    )
    return HEAD.format(title="Void Labs", css=CSS, meta='<meta name="description" content="Void Labs builds Discord bots and web dashboards. Home of Donut and Pixel.">') + page_shell(nav, body, foot)


def admin_login_page(msg=""):
    m = f'<div class="msg err" role="alert">{E(msg)}</div>' if msg else ""
    body = (
        f'<body><main class="gate on-dark"><div><a class="mark" href="/">{LOGO}Void Labs</a><div class="panel"><h1>Admin login</h1>'
        f'{m}<form method="post" action="/admin/login" autocomplete="on">'
        '<label for="u" style="margin-top:0">Username</label><input id="u" name="username" autocomplete="username" required>'
        '<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>'
        '<button class="btn" type="submit">Log in</button></form></div></div></main></body></html>'
    )
    return HEAD.format(title="Admin login", css=CSS, meta='<meta name="robots" content="noindex,nofollow">') + body


def admin_page(s, csrf, msg="", kind="ok"):
    def opts(cur):
        return "".join(f'<option value="{v}"{" selected" if v == cur else ""}>{l}</option>' for v, l in STATUSES)
    m = f'<div class="msg {kind}" role="{"alert" if kind == "err" else "status"}">{E(msg)}</div>' if msg else ""
    live = "".join(
        f'<li>{status_mark(s[k])}{n}<span>{E(STATUS_LABEL[s[k]])}</span></li>'
        for n, k in (("Donut", "donut_status"), ("Pixel", "pixel_status"))
    )
    body = (
        '<body class="adm"><a class="skip" href="#top">Skip to content</a>'
        f'<header class="site-head on-dark"><div class="wrap"><a class="mark" href="/">{LOGO}View site</a>'
        f'<form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{csrf}">'
        '<button class="btn ghost sm" type="submit">Log out</button></form></div></header>'
        '<main id="top"><div class="wrap admin-grid">'
        f'<aside class="admin-side"><h1>Site settings</h1><p>Changes go live on the public site as soon as you save.</p><ul aria-label="Current status">{live}</ul></aside>'
        f'<div>{m}<form method="post" action="/admin/save"><input type="hidden" name="csrf" value="{csrf}">'
        '<fieldset><legend>Product status</legend><div class="row"><div><label for="ds">Donut status</label>'
        f'<select id="ds" name="donut_status">{opts(s["donut_status"])}</select></div>'
        '<div><label for="ps">Pixel status</label>'
        f'<select id="ps" name="pixel_status">{opts(s["pixel_status"])}</select></div></div></fieldset>'
        '<fieldset><legend>Public profile</legend>'
        '<label for="n" style="margin-top:0">Your name <span class="hint">(shown in &ldquo;Hi, I&rsquo;m &hellip;&rdquo;)</span></label>'
        f'<input id="n" name="name" maxlength="40" value="{E(s["name"])}" required>'
        '<label for="e">Contact email <span class="hint">(leave empty to hide)</span></label>'
        f'<input id="e" name="email" type="email" maxlength="120" value="{E(s["email"])}" placeholder="you@example.com">'
        '<label for="d">Support Discord <span class="hint">(leave empty to hide)</span></label>'
        f'<input id="d" name="discord" maxlength="80" value="{E(s["discord"])}" placeholder="discord.gg/yourcode">'
        '<label for="a">About me <span class="hint">(blank line = new paragraph)</span></label>'
        f'<textarea id="a" name="about" maxlength="1500">{E(s["about"])}</textarea></fieldset>'
        '<div class="actions"><button class="btn" type="submit">Save changes</button></div></form></div></div></main></body></html>'
    )
    return HEAD.format(title="Admin", css=CSS, meta='<meta name="robots" content="noindex,nofollow">') + body


NOT_ENABLED = HEAD.format(title="Admin disabled", css=CSS, meta='<meta name="robots" content="noindex">') + (
    f'<body><main class="gate on-dark"><div><a class="mark" href="/">{LOGO}Void Labs</a><div class="panel"><h1>Admin is off</h1>'
    "<p>Set an <strong>ADMIN_PASSWORD</strong> environment variable (at least 10 characters) and restart the server to turn it on.</p>"
    "</div></div></main></body></html>"
)

NOT_FOUND = HEAD.format(title="Not found", css=CSS, meta="") + (
    f'<body><header class="site-head on-dark"><div class="wrap"><a class="mark" href="/">{LOGO}Void Labs</a></div></header>'
    '<main class="lost on-dark" id="top"><div class="wrap"><h1>404</h1><p>This page is not here.</p>'
    f'<a class="btn" href="/">Back to Void Labs{ARROW}</a></div></main></body></html>'
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
        self.send(404, NOT_FOUND)

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
