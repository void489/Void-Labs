"""Void Labs main site: public page + /admin dashboard. Python standard library only.

Environment variables (set them in the bot-hosting.net panel):
  SERVER_PORT     port to listen on (set by the panel)
  ADMIN_PASSWORD  REQUIRED to enable /admin (min 10 characters)
  ADMIN_USERNAME  optional, default "admin"
  SESSION_SECRET  optional extra secret used to sign login cookies
  DISCORD_WEBHOOK_URL  optional: Discord webhook that gets a message when a site goes down or recovers
  MONITOR_INTERVAL     optional: seconds between health checks (default 60, 0 turns the monitor off)
  MESSAGES_FILE        optional path for the contact inbox (default messages.json next to main.py)
  UPDATES_FILE         optional path for the updates log (default updates.json next to main.py)
  MONITOR_URLS         optional override, e.g. "donut=https://a.example,pixel=https://b.example,site=https://c.example"
"""
import hashlib, hmac, html, json, math, os, re, secrets, ssl, threading, time
import urllib.error, urllib.request
from email.parser import BytesParser
from email.policy import default as _email_policy
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
STATUS_OPTIONS = STATUSES + [("auto", "Auto (follow monitor)")]
PUBLIC_LABEL = {"online": "Online", "maintenance": "Under maintenance", "offline": "Offline"}

DEFAULTS = {
    "name": "Void",
    "about": (
        "I build Discord bots, dashboards and automation for online communities.\n\n"
        "Donut and Pixel are the two biggest. I design them, write them, host them "
        "and fix them when something breaks."
    ),
    "email": "",
    "discord": "",
    "donut_status": "online",
    "pixel_status": "online",
    "banner": "",
    "donut_note": "",
    "pixel_note": "",
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
        if data[k] not in STATUS_LABEL and data[k] != "auto":
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
        "banner": g("banner"), "donut_note": g("donut_note"), "pixel_note": g("pixel_note"),
    }
    if not out["name"] or len(out["name"]) > 40:
        return out, "Name must be 1 to 40 characters."
    if len(out["about"]) > 1500:
        return out, "About text is too long (max 1500 characters)."
    if len(out["banner"]) > 140:
        return out, "Announcement is too long (max 140 characters)."
    if len(out["donut_note"]) > 120 or len(out["pixel_note"]) > 120:
        return out, "Status notes can be 120 characters at most."
    if out["email"] and (len(out["email"]) > 120 or not EMAIL_RE.match(out["email"])):
        return out, "That doesn't look like a valid email address."
    if out["discord"]:
        m = DISCORD_RE.match(out["discord"])
        if not m:
            return out, "Discord link must look like discord.gg/yourcode or discord.com/invite/yourcode."
        out["discord"] = "https://discord.gg/" + m.group(1)
    for k in ("donut_status", "pixel_status"):
        if out[k] not in STATUS_LABEL and out[k] != "auto":
            return out, "Pick Auto, Online, Maintenance or Offline for each site."
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


# ---------- updates log ----------
UPDATES_FILE = os.getenv("UPDATES_FILE") or os.path.join(BASE, "updates.json")
_updlock = threading.Lock()
UPDATES = []  # newest first: {"id","t","title","text","tag"}
UPD_TAGS = {"": "", "donut": "Donut", "pixel": "Pixel", "site": "Void Labs"}


def upd_load():
    try:
        with open(UPDATES_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, list):
            UPDATES[:] = [x for x in d if isinstance(x, dict) and "id" in x][:200]
    except (OSError, ValueError):
        pass


def upd_save():
    tmp = UPDATES_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(UPDATES[:200], f)
        os.replace(tmp, UPDATES_FILE)
    except OSError:
        pass


# ---------- screenshot media ----------
MAX_UPLOAD = 5 * 1024 * 1024


def sniff_image(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if b[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return ".webp"
    return None


def parse_multipart(ctype, body):
    """Returns (fields, files) from a multipart/form-data body. files: name -> bytes."""
    msg = BytesParser(policy=_email_policy).parsebytes(
        b"Content-Type: " + ctype.encode("latin-1", "replace") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    fields, files = {}, {}
    if not msg.is_multipart():
        return fields, files
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            files[name] = payload
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


# ---------- contact inbox ----------
MESSAGES_FILE = os.getenv("MESSAGES_FILE") or os.path.join(BASE, "messages.json")
_msglock = threading.Lock()
_msg_hits = {}  # ip -> [timestamps]
MSGS = []  # newest first: {"id","t","name","reply","msg","read"}


def msgs_load():
    try:
        with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, list):
            MSGS[:] = [x for x in d if isinstance(x, dict) and "id" in x][:500]
    except (OSError, ValueError):
        pass


def msgs_save():
    tmp = MESSAGES_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(MSGS[:500], f)
        os.replace(tmp, MESSAGES_FILE)
    except OSError:
        pass


def unread_count():
    with _msglock:
        return sum(1 for m in MSGS if not m.get("read"))


# ---------- monitor ----------
# Checks each service with a plain HTTP request every MONITOR_INTERVAL seconds. A site counts as up when it answers
# with any status below 500. A state only flips after two checks in a row agree, so one blip never raises an alarm.
SERVICES = [
    ("donut", "Donut", "https://donut.voidlabs.fun/"),
    ("pixel", "Pixel", "https://pixel.voidlabs.fun/"),
    ("site", "Void Labs", "https://voidlabs.fun/"),
]
for _pair in (os.getenv("MONITOR_URLS") or "").split(","):
    _k, _, _u = _pair.strip().partition("=")
    if _u.startswith(("http://", "https://")):
        SERVICES = [(k, n, _u if k == _k else u) for k, n, u in SERVICES]
try:
    MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL") or 60)
except ValueError:
    MONITOR_INTERVAL = 60
MONITOR_FILE = os.getenv("MONITOR_FILE") or os.path.join(BASE, "monitor.json")
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL") or ""
WEBHOOK_OK = WEBHOOK.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/"))
HIST_MAX = 1440  # 24h at one check per minute
UA = "VoidLabsMonitor/1.0 (+https://voidlabs.fun)"

_mlock = threading.Lock()
MON = {k: {"hist": [], "state": None, "since": 0, "streak": 0} for k, _, _ in SERVICES}
INCIDENTS = []  # newest first: {"t", "key", "ok"}


def mon_load():
    try:
        with open(MONITOR_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k in MON:
            h = saved.get("hist", {}).get(k, [])
            if isinstance(h, list):
                MON[k]["hist"] = [x for x in h if isinstance(x, dict)][-HIST_MAX:]
            st = saved.get("state", {}).get(k)
            if isinstance(st, list) and len(st) == 2:
                MON[k]["state"], MON[k]["since"] = st[0], int(st[1])
        inc = saved.get("incidents", [])
        if isinstance(inc, list):
            INCIDENTS[:] = [x for x in inc if isinstance(x, dict)][:50]
    except (OSError, ValueError, TypeError):
        pass


def mon_save():
    tmp = MONITOR_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"hist": {k: v["hist"] for k, v in MON.items()},
                       "state": {k: [v["state"], v["since"]] for k, v in MON.items()},
                       "incidents": INCIDENTS[:50]}, f)
        os.replace(tmp, MONITOR_FILE)
    except OSError:
        pass


def probe(url):
    t0 = time.time()
    code = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8, context=ssl.create_default_context()) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = 0
    return {"t": int(time.time()), "ok": 0 < code < 500, "ms": int((time.time() - t0) * 1000), "code": code}


def post_webhook(text):
    if not WEBHOOK_OK:
        return
    try:
        body = json.dumps({"content": text[:1900], "username": "Void Labs", "allowed_mentions": {"parse": []}}).encode()
        req = urllib.request.Request(WEBHOOK, data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass


def fmt_dur(sec):
    sec = int(sec)
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{sec // 60} min"
    return f"{sec // 3600} h {sec % 3600 // 60} min"


def record(key, res):
    name = {k: n for k, n, _ in SERVICES}[key]
    alert = None
    with _mlock:
        m = MON[key]
        m["hist"].append(res)
        del m["hist"][:-HIST_MAX]
        if m["state"] is None:
            m["state"], m["since"], m["streak"] = res["ok"], res["t"], 0
        elif res["ok"] != m["state"]:
            m["streak"] += 1
            if m["streak"] >= 2:
                was = res["t"] - m["since"]
                m["state"], m["since"], m["streak"] = res["ok"], res["t"], 0
                INCIDENTS.insert(0, {"t": res["t"], "key": key, "ok": res["ok"]})
                del INCIDENTS[50:]
                alert = (f"{name} is back up after {fmt_dur(was)}." if res["ok"]
                         else f"{name} is DOWN ({'HTTP ' + str(res['code']) if res['code'] else 'no response'}).")
        else:
            m["streak"] = 0
    if alert:
        threading.Thread(target=post_webhook, args=(alert,), daemon=True).start()


def check_all():
    out = []
    def one(key, url):
        record(key, probe(url))
    ths = [threading.Thread(target=one, args=(k, u), daemon=True) for k, _, u in SERVICES]
    for t in ths:
        t.start()
    for t in ths:
        t.join(12)
    mon_save()


def monitor_loop():
    mon_load()
    while True:
        check_all()
        time.sleep(max(15, MONITOR_INTERVAL))


def live_state(key):
    m = MON.get(key)
    return m["state"] if m else None


def effective(s):
    """Resolve 'auto' statuses against the monitor. Unknown (no data yet) counts as online."""
    s = dict(s)
    for k in ("donut", "pixel"):
        if s[k + "_status"] == "auto":
            s[k + "_status"] = "offline" if live_state(k) is False else "online"
    return s


# ---------- pages ----------
# Design system: near-black studio, electric violet accent, restrained blue secondary. Flat: no glows, no gradients.
# Spacing scale: 4 8 12 16 24 32 48 72 112 176 (--s1..--s10). One radius (2px). Hairlines carry structure.
# Type: Schibsted Grotesk (display, tight editorial grotesk) / Hanken Grotesk (text). Sentence case throughout.
CSS = r"""/* Void Labs design system: paper, ink, one signal colour.
   Type: Archivo (variable width) + JetBrains Mono for data. Radius 0 everywhere except status dots.
   Rules: 1.5px ink lines carry structure. No shadows, no gradients, no card grids. */
:root{
--paper:#eeeae0;--paper-2:#e2ddd0;--card:#f8f5ed;--ink:#12110e;--ink-2:#4a473f;--hair:#b9b2a1;
--signal:#ff4d1a;--on-ink-2:#aaa496;
--ok:#1b6e37;--warn:#8f5200;--bad:#b0241b;
--donut:#f5a524;--pixel:#2ed47a;
--display:"Archivo","Arial Narrow","Helvetica Neue",Arial,system-ui,sans-serif;
--body:"Archivo","Helvetica Neue",Arial,system-ui,-apple-system,"Segoe UI",sans-serif;
--mono:"JetBrains Mono",ui-monospace,"SF Mono",Menlo,Consolas,"DejaVu Sans Mono",monospace;
--s1:.25rem;--s2:.5rem;--s3:.75rem;--s4:1rem;--s5:1.5rem;--s6:2rem;--s7:3rem;--s8:4.5rem;--s9:7rem;--s10:11rem;
--gutter:clamp(1.25rem,4.5vw,3.5rem);--bar:4rem;--rule:1.5px;--r:0}
*{box-sizing:border-box;margin:0;padding:0}
html{background:var(--paper);color-scheme:light;-webkit-text-size-adjust:100%;scroll-padding-top:calc(var(--bar) + var(--s4))}
@media(prefers-reduced-motion:no-preference){html{scroll-behavior:smooth}}
body{font-family:var(--body);font-stretch:100%;color:var(--ink);background:var(--paper);line-height:1.6;font-size:1.0625rem;min-height:100vh;min-height:100svh;
display:flex;flex-direction:column;-webkit-font-smoothing:antialiased;touch-action:manipulation;-webkit-tap-highlight-color:rgba(255,77,26,.25)}
a{color:inherit;text-decoration:none}
::selection{background:var(--signal);color:var(--ink)}
:focus-visible{outline:3px solid var(--ink);outline-offset:3px}
.on-ink :focus-visible,.site-foot :focus-visible{outline-color:var(--signal)}
main{flex:1}
.wrap{width:100%;max-width:90rem;margin:0 auto;padding-inline:var(--gutter)}
.skip{position:absolute;left:var(--s4);top:-5rem;z-index:100;background:var(--ink);color:var(--paper);padding:var(--s3) var(--s4);font-weight:700}
.skip:focus{top:var(--s3)}
.mono,.kicker,.label{font-family:var(--mono);font-size:.75rem;font-weight:500;letter-spacing:.06em;text-transform:uppercase}
.sq{display:inline-block;width:.17em;height:.17em;background:var(--signal);margin-left:.06em;vertical-align:baseline}

/* header */
.banner{background:var(--signal);color:var(--ink);font:500 .8125rem/1.4 var(--mono);letter-spacing:.04em;text-transform:uppercase;text-align:center;padding:var(--s3) var(--gutter);border-bottom:var(--rule) solid var(--ink)}
.site-head{position:sticky;top:0;z-index:30;height:var(--bar);background:var(--paper);border-bottom:var(--rule) solid var(--ink)}
.site-head .wrap{height:100%;display:flex;align-items:center;justify-content:space-between;gap:var(--s4)}
.mark{display:inline-flex;align-items:center;gap:var(--s3);font:800 1.125rem/1 var(--display);font-stretch:88%;letter-spacing:.02em;text-transform:uppercase;min-height:2.75rem}
.mark svg{width:1.75rem;height:1.75rem;flex:none}
.head-r{display:flex;align-items:center;gap:var(--s6)}
.site-head nav{display:flex;gap:var(--s5)}
.site-head nav a{position:relative;display:inline-flex;align-items:center;min-height:2.75rem;font:500 .8125rem/1 var(--mono);letter-spacing:.06em;text-transform:uppercase}
.site-head nav a::after{content:"";position:absolute;left:0;right:0;bottom:.55rem;height:var(--rule);background:var(--ink);transform:scaleX(0);transform-origin:left;transition:transform .3s}
.site-head nav a:hover::after,.site-head nav a:focus-visible::after{transform:scaleX(1)}
.clock{font:500 .8125rem/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--ink-2);font-variant-numeric:tabular-nums;border-left:var(--rule) solid var(--ink);padding-left:var(--s5)}

/* status marks: shape + word, never colour alone */
.st{display:inline-flex;align-items:center;gap:var(--s2);font:500 .8125rem/1.2 var(--mono);letter-spacing:.06em;text-transform:uppercase}
.st::before{content:"";width:.65rem;height:.65rem;flex:none;border-radius:50%;background:currentColor}
.st-maintenance::before{border-radius:0;transform:rotate(45deg) scale(.9)}
.st-offline::before{background:transparent;border:2px solid currentColor}
.st-online{color:var(--ok)}.st-maintenance{color:var(--warn)}.st-offline{color:var(--bad)}
@media(prefers-reduced-motion:no-preference){
.st-online::before{animation:ping 2.4s ease-out infinite}}
@keyframes ping{0%{box-shadow:0 0 0 0 rgba(27,110,55,.45)}70%,100%{box-shadow:0 0 0 .5rem rgba(27,110,55,0)}}

/* hero */
.hero{border-bottom:var(--rule) solid var(--ink)}
.meta{display:flex;flex-wrap:wrap;border-bottom:var(--rule) solid var(--ink)}
.meta>*{padding:var(--s3) var(--s5) var(--s3) 0;margin-right:var(--s5);display:flex;align-items:center;color:var(--ink-2)}
.meta a{color:var(--ink);margin-left:auto;margin-right:0;padding-right:0;text-decoration:underline;text-underline-offset:.25em}
.meta a:hover{text-decoration-thickness:2px}
.hero-in{display:grid;grid-template-columns:minmax(0,8fr) minmax(0,4fr);gap:var(--s7) clamp(2rem,5vw,5rem);align-items:center;padding-block:clamp(2.5rem,6vw,5.5rem)}
h1{font:800 clamp(2.75rem,8.4vw,8.25rem)/.9 var(--display);font-stretch:80%;letter-spacing:-.035em;text-wrap:balance}
h1 .ln{display:block;overflow:hidden;padding-bottom:.14em;margin-bottom:-.14em}
h1 .ln>span{display:block}
.lede{margin-top:var(--s6);font-size:clamp(1.125rem,1.5vw,1.3125rem);line-height:1.5;max-width:30em}
.lede b{font-weight:700}
.hero-cta{display:flex;gap:var(--s3);flex-wrap:wrap;margin-top:var(--s6)}
.dial{justify-self:end;width:min(100%,25rem)}
.dial svg{display:block;width:100%;height:auto;overflow:visible}
.dial .hand{transform-box:view-box;transform-origin:100px 100px}
.dial-cap{display:flex;justify-content:space-between;gap:var(--s3);margin-top:var(--s4);font:500 .75rem/1.3 var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--ink-2);font-variant-numeric:tabular-nums}
.dial-cap b{color:var(--ink);font-weight:700}
.board{border-top:var(--rule) solid var(--ink)}
.board a{display:grid;grid-template-columns:3.5rem minmax(0,1fr) minmax(0,16rem) 11rem 2.5rem;align-items:center;gap:var(--s4);padding:var(--s5) 0;border-bottom:var(--rule) solid var(--ink);position:relative;isolation:isolate;transition:padding .3s cubic-bezier(.2,.8,.2,1)}
.board a:last-child{border-bottom:0}
.board a::before{content:"";position:absolute;inset:0 calc(var(--gutter)*-1);background:var(--pc);z-index:-1;transform:scaleY(0);transform-origin:bottom;transition:transform .35s cubic-bezier(.2,.8,.2,1)}
.board a:hover::before,.board a:focus-visible::before{transform:scaleY(1)}
.board a:hover .st,.board a:focus-visible .st{color:var(--ink)}
.board a:hover .st::before{animation:none;box-shadow:none}
.board .idx{font:500 .8125rem var(--mono);color:var(--ink-2)}
.board a:hover .idx{color:var(--ink)}
.board .nm{font:800 clamp(2.25rem,6vw,5.5rem)/.95 var(--display);font-stretch:80%;letter-spacing:-.035em}
.board .what{font-size:1rem;line-height:1.35}
.board .go{justify-self:end;width:1.75rem;height:1.75rem;transition:transform .25s}
.board a:hover .go{transform:translate(3px,-3px)}
.board a:hover{padding-left:var(--s3)}
.pc-donut{--pc:var(--donut)}.pc-pixel{--pc:var(--pixel)}
@media(prefers-reduced-motion:no-preference){
h1 .ln>span{animation:rise 1s cubic-bezier(.2,.8,.2,1) both}
h1 .ln:nth-child(2)>span{animation-delay:.09s}h1 .ln:nth-child(3)>span{animation-delay:.18s}
.lede,.hero-cta{animation:fade .9s .4s both}.dial{animation:fade 1.2s .3s both}
.board a{animation:up .8s cubic-bezier(.2,.8,.2,1) both}.board a:nth-child(1){animation-delay:.5s}.board a:nth-child(2){animation-delay:.6s}}
@keyframes rise{from{transform:translateY(105%)}}
@keyframes fade{from{opacity:0}}
@keyframes up{from{opacity:0;transform:translateY(14px)}}

/* buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:var(--s3);min-height:3rem;background:var(--ink);color:var(--paper);font:700 1rem/1 var(--body);
padding:.95rem 1.35rem;border:var(--rule) solid var(--ink);border-radius:var(--r);cursor:pointer;transition:background .2s,color .2s,border-color .2s}
.btn svg{width:1rem;height:1rem;flex:none;transition:transform .25s}
.btn:hover{background:var(--signal);color:var(--ink)}
.btn:hover svg{transform:translate(2px,-2px)}
.btn:active{transform:translateY(1px)}
.btn.ghost{background:transparent;color:var(--ink)}
.btn.ghost:hover{background:var(--ink);color:var(--paper)}
.btn.sm{min-height:2.75rem;padding:.65rem 1rem;font-size:.9375rem}
.btn[disabled]{opacity:.5;cursor:progress}

/* work */
.sec-head{padding-block:var(--s9) var(--s7);display:grid;grid-template-columns:minmax(0,8fr) minmax(0,4fr);gap:var(--s5) var(--s7);align-items:end}
h2{font:800 clamp(2.5rem,7vw,6.5rem)/.92 var(--display);font-stretch:80%;letter-spacing:-.035em}
.sec-head p{max-width:24em}
.project{border-top:var(--rule) solid var(--ink)}
.p-top{display:flex;justify-content:space-between;align-items:center;gap:var(--s4);flex-wrap:wrap;padding-block:var(--s3);border-bottom:var(--rule) solid var(--ink)}
.p-top>span:first-child{color:var(--ink-2)}
.project-in{display:grid;grid-template-columns:minmax(0,4fr) minmax(0,8fr);gap:var(--s7) clamp(2rem,5vw,5rem);align-items:start;padding-block:var(--s7) var(--s9)}
.project.flip .project-in{grid-template-columns:minmax(0,8fr) minmax(0,4fr)}
.project.flip .p-copy{order:2}
.p-copy{position:sticky;top:calc(var(--bar) + var(--s6))}
h3{font:800 clamp(3.5rem,8vw,7.5rem)/.88 var(--display);font-stretch:80%;letter-spacing:-.04em;margin-bottom:var(--s5)}
.desc{font-size:1.125rem;line-height:1.55;max-width:28em}
.p-note{margin-top:var(--s4);padding:var(--s3) var(--s4);background:var(--pc);border:var(--rule) solid var(--ink);font-weight:600;max-width:28em}
.feats{list-style:none;margin:var(--s6) 0;max-width:30rem}
.feats li{display:grid;grid-template-columns:8.5rem 1fr;gap:var(--s4);border-top:1px solid var(--hair);padding:var(--s3) 0;font-weight:700}
.feats li:last-child{border-bottom:1px solid var(--hair)}
.feats li span{font-weight:400;color:var(--ink-2)}
.cta{display:flex;align-items:center;gap:var(--s5);flex-wrap:wrap}
.host{font:500 .8125rem var(--mono);color:var(--ink-2);letter-spacing:.02em}
.slab{background:var(--pc);border:var(--rule) solid var(--ink);padding:clamp(.75rem,2.4vw,2rem) clamp(.75rem,2.4vw,2rem) var(--s3)}
.shot{display:block}
.shot img,.slab .shotimg{display:block;width:100%;height:auto;aspect-ratio:16/10;object-fit:cover;object-position:top left;background:var(--ink);border:var(--rule) solid var(--ink)}
.slab-cap{display:flex;justify-content:space-between;gap:var(--s4);flex-wrap:wrap;padding-top:var(--s3)}
.mark-art{display:flex;justify-content:center;align-items:center;aspect-ratio:16/10;background:var(--ink);border:var(--rule) solid var(--ink)}
.mark-art svg{width:min(46%,18rem);height:auto}
.px{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;width:min(40%,14rem);aspect-ratio:1}
.px b{border:1px solid #5b574d}.px b.on{background:var(--pixel);border-color:var(--pixel)}
@media(prefers-reduced-motion:no-preference){
.js .rv{opacity:0;transform:translateY(22px);transition:opacity .8s ease,transform .9s cubic-bezier(.2,.8,.2,1)}
.js .rv.in{opacity:1;transform:none}}

/* updates (ink band) */
.on-ink{background:var(--ink);color:var(--paper)}
.on-ink .sec-head p{color:var(--on-ink-2)}
.updates-sec{padding-bottom:var(--s9)}
.upd{list-style:none;border-top:var(--rule) solid var(--paper)}
.upd li{display:grid;grid-template-columns:11rem 8rem minmax(0,1fr);gap:var(--s4) var(--s6);padding:var(--s5) 0;border-bottom:1px solid #3d3a33;align-items:baseline}
.upd time{font:500 .8125rem var(--mono);letter-spacing:.04em;color:var(--on-ink-2);text-transform:uppercase;font-variant-numeric:tabular-nums}
.upd h3{font:700 clamp(1.25rem,2.2vw,1.75rem)/1.2 var(--display);font-stretch:90%;letter-spacing:-.01em;margin:0}
.upd .tag{display:inline-block;font:700 .75rem/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;background:var(--signal);color:var(--ink);padding:.3rem .5rem}
.upd p{color:var(--on-ink-2);max-width:40em;white-space:pre-wrap;margin-top:var(--s2)}

/* about */
.about{background:var(--paper-2);border-top:var(--rule) solid var(--ink)}
.about-in{display:grid;grid-template-columns:minmax(0,7fr) minmax(0,5fr);gap:var(--s7) clamp(2rem,6vw,6rem);padding-block:var(--s9)}
.kicker{display:flex;align-items:center;gap:var(--s3);margin-bottom:var(--s5);font-size:.8125rem}
.kicker::before{content:"";width:.65rem;height:.65rem;background:var(--signal)}
.statement{font:800 clamp(2rem,4.6vw,4.5rem)/1.02 var(--display);font-stretch:82%;letter-spacing:-.03em;max-width:15em;text-wrap:balance}
.focus{list-style:none;counter-reset:f;border-top:var(--rule) solid var(--ink)}
.focus li{counter-increment:f;display:grid;grid-template-columns:2.5rem 8.5rem 1fr;gap:var(--s3);border-bottom:var(--rule) solid var(--ink);padding:var(--s4) 0;align-items:baseline}
.focus li::before{content:counter(f,decimal-leading-zero);font:500 .8125rem var(--mono);color:var(--ink-2)}
.focus strong{font:800 1.125rem/1.3 var(--display);font-stretch:90%}
.focus span{color:var(--ink-2)}
.bio{margin-top:var(--s7);max-width:36em}
.bio h3{font:800 1.5rem/1.2 var(--display);font-stretch:90%;letter-spacing:-.015em;margin:0 0 var(--s3)}
.bio p+p{margin-top:var(--s4)}

/* contact (signal block) */
.contact{background:var(--signal);color:var(--ink);border-top:var(--rule) solid var(--ink)}
.contact-in{padding-block:var(--s9)}
.contact-top{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:var(--s6) clamp(2rem,6vw,6rem);align-items:start}
.contact h2{font-size:clamp(3rem,9vw,8.5rem)}
.contact .sub{font-size:1.1875rem;max-width:22em;margin-top:var(--s5);font-weight:500}
.links{border-top:var(--rule) solid var(--ink)}
.links a{display:flex;justify-content:space-between;align-items:baseline;gap:var(--s5);border-bottom:var(--rule) solid var(--ink);padding:var(--s5) var(--s3) var(--s5) 0;
font:800 clamp(1.5rem,3.4vw,3rem)/1.1 var(--display);font-stretch:85%;letter-spacing:-.02em;overflow-wrap:anywhere;transition:background .2s,color .2s,padding .3s}
.links small{font:500 .75rem var(--mono);letter-spacing:.06em;text-transform:uppercase;flex:none}
.links a:hover{background:var(--ink);color:var(--paper);padding-left:var(--s4)}
.links:empty{display:none}
.site-foot{background:var(--ink);color:var(--paper);border-top:var(--rule) solid var(--ink);padding-block:var(--s6)}
.foot-row{display:flex;justify-content:space-between;align-items:center;gap:var(--s5);flex-wrap:wrap;font:500 .8125rem/1.4 var(--mono);letter-spacing:.04em;text-transform:uppercase;color:var(--on-ink-2)}
.foot-row nav{display:flex;gap:var(--s5)}
.foot-row nav a{display:inline-flex;align-items:center;min-height:2.75rem;color:var(--paper)}
.foot-row nav a:hover{color:var(--signal)}

/* forms */
fieldset{border:0;margin:0 0 var(--s7)}
legend{font:800 1.75rem/1.1 var(--display);font-stretch:85%;letter-spacing:-.02em;margin-bottom:var(--s4);padding:0}
label{display:block;font-weight:700;margin:var(--s5) 0 var(--s2)}
.hint{color:var(--ink-2);font-size:.9375rem;font-weight:400}
.contact .hint{color:var(--ink)}
input,textarea,select{width:100%;background:var(--card);color:var(--ink);border:var(--rule) solid var(--ink);border-radius:var(--r);padding:.8rem .9rem;font:inherit;font-size:1rem;min-height:3rem;transition:background .2s,box-shadow .2s}
input::placeholder,textarea::placeholder{color:#6b675d}
input:hover,textarea:hover,select:hover{background:#fff}
input:focus,textarea:focus,select:focus{outline:3px solid var(--ink);outline-offset:2px;background:#fff}
select{appearance:none;background-image:linear-gradient(45deg,transparent 50%,var(--ink) 50%),linear-gradient(135deg,var(--ink) 50%,transparent 50%);background-position:calc(100% - 1.15rem) 55%,calc(100% - .8rem) 55%;background-size:.35rem .35rem;background-repeat:no-repeat;padding-right:2.5rem}
textarea{min-height:12rem;resize:vertical;line-height:1.55}
input[type=file]{padding:.6rem;font-size:.9375rem}
.row{display:grid;grid-template-columns:1fr 1fr;gap:var(--s5)}
.row label:first-child{margin-top:0}
.msg{border:var(--rule) solid var(--ink);border-left-width:8px;padding:var(--s3) var(--s4);margin-bottom:var(--s5);font-weight:600;background:var(--card)}
.msg.ok{border-left-color:var(--ok)}.msg.err{border-left-color:var(--bad)}
.hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}
.cform{margin-top:var(--s8);display:grid;grid-template-columns:1fr 1fr;gap:var(--s5) var(--s6);max-width:60rem}
.cform h3{grid-column:1/-1;font:800 clamp(1.75rem,3vw,2.5rem)/1.1 var(--display);font-stretch:85%;letter-spacing:-.025em;margin:0}
.cform label{margin:0 0 var(--s2)}
.cform .full{grid-column:1/-1}
.cform .btn{justify-self:start;margin:0}
.contact .btn:hover{background:var(--paper);color:var(--ink)}
.flash{grid-column:1/-1;margin:0}.flash.msg{margin-bottom:0}

/* admin + utility */
.panel{background:var(--card);border:var(--rule) solid var(--ink);padding:clamp(1.25rem,3vw,2rem)}
.admin-grid{display:grid;grid-template-columns:minmax(0,19rem) minmax(0,1fr);gap:var(--s7) var(--s8);padding-block:var(--s7) var(--s8);align-items:start}
.admin-side{position:sticky;top:calc(var(--bar) + var(--s5))}
.admin-side h1{font-size:clamp(2.5rem,4.4vw,3.75rem);overflow-wrap:anywhere;line-height:.92;margin-bottom:var(--s5)}
.admin-side p{margin-bottom:var(--s4);max-width:22em;color:var(--ink-2)}
.admin-side ul{list-style:none;margin:var(--s5) 0;border-top:var(--rule) solid var(--ink)}
.admin-side li{display:flex;align-items:center;justify-content:space-between;gap:var(--s3);padding:var(--s3) 0;border-bottom:var(--rule) solid var(--ink);font-weight:700}
.site-head.adm .wrap{gap:var(--s5)}
.admin-nav{display:flex;gap:var(--s5)}
.admin-nav a{display:inline-flex;align-items:center;min-height:2.75rem;font:500 .8125rem/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--ink-2);border-bottom:2px solid transparent;margin-bottom:-2px}
.admin-nav a:hover{color:var(--ink)}.admin-nav a[aria-current]{color:var(--ink);border-color:var(--signal)}
.badge{background:var(--signal);color:var(--ink);font:700 .75rem/1 var(--mono);padding:.2rem .4rem;margin-left:var(--s2);border:1px solid var(--ink)}
.gate{min-height:100vh;min-height:100svh;display:grid;place-items:center;padding:var(--gutter)}
.gate>div{width:100%;max-width:27rem}
.gate .mark{margin-bottom:var(--s5)}
.gate h1{font-size:clamp(2.75rem,10vw,4rem);line-height:.95;margin-bottom:var(--s5)}
.gate .btn{margin-top:var(--s6);width:100%}
.gate p{margin-bottom:var(--s3);color:var(--ink-2)}.gate p strong{color:var(--ink)}
.lost{flex:1;display:flex;align-items:center;border-top:0}
.lost h1{font-size:clamp(7rem,26vw,20rem);line-height:.8}
.lost p{font-size:1.25rem;margin:var(--s5) 0 var(--s6);font-weight:500}
.ops-top{display:flex;justify-content:space-between;align-items:end;gap:var(--s5);flex-wrap:wrap;padding-block:var(--s7) var(--s6);margin-bottom:var(--s2)}
.ops-top h1{font-size:clamp(3rem,6vw,5.5rem);line-height:.9}
.ops-top p{color:var(--ink-2);max-width:36em;margin-top:var(--s3)}
.ops-sec{padding-bottom:var(--s8)}
.ops-sec h2{font:800 1.75rem/1.1 var(--display);font-stretch:85%;letter-spacing:-.02em;margin:var(--s7) 0 var(--s3);max-width:none}
.note{color:var(--ink-2)}
.mrow{border-top:var(--rule) solid var(--ink);padding:var(--s5) 0;display:grid;grid-template-columns:minmax(0,15rem) minmax(0,1fr) auto;gap:var(--s4) var(--s6);align-items:start}
.mrow:last-child{border-bottom:var(--rule) solid var(--ink)}
.mrow .who{color:var(--ink-2);font-size:.9375rem}
.mrow .who strong{display:block;color:var(--ink);font-weight:800;font-size:1.0625rem}
.mrow .who a{color:var(--ink);text-decoration:underline;text-underline-offset:.2em;overflow-wrap:anywhere}
.mrow .body{white-space:pre-wrap;overflow-wrap:anywhere;max-width:44em}
.mrow .acts{display:flex;gap:var(--s2);flex-wrap:wrap}
.mrow:not(.unread){color:var(--ink-2)}
.dot{display:inline-block;width:.6rem;height:.6rem;background:var(--signal);border:1px solid var(--ink);border-radius:50%;margin-right:var(--s2)}
.btn.danger:hover{background:var(--bad);border-color:var(--bad);color:var(--paper)}
.svc{border-top:var(--rule) solid var(--ink);padding:var(--s5) 0;display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,.8fr) minmax(0,1.4fr);gap:var(--s4) var(--s6);align-items:center}
.svc:last-of-type{border-bottom:var(--rule) solid var(--ink)}
.svc h2{font:800 clamp(1.75rem,3vw,2.5rem)/1 var(--display);font-stretch:82%;letter-spacing:-.03em;max-width:none;margin:0}
.svc h2 a:hover{text-decoration:underline;text-underline-offset:.15em}
.svc .url{color:var(--ink-2);font:500 .8125rem var(--mono);overflow-wrap:anywhere;margin-top:var(--s1)}
.stats{display:grid;grid-template-columns:repeat(3,auto);gap:var(--s5);justify-content:start}
.stats dt{font:500 .75rem var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--ink-2)}
.stats dd{font:800 1.5rem/1.2 var(--display);font-stretch:85%;font-variant-numeric:tabular-nums}
.strip{display:flex;align-items:flex-end;justify-content:flex-start;gap:2px;height:2.5rem}
.strip i{flex:1 1 0;min-width:2px;height:100%;background:var(--ok)}
.strip i.bad{height:38%;background:var(--bad)}.strip i.none{height:12%;background:var(--hair)}
.strip-cap{color:var(--ink-2);font:500 .75rem var(--mono);letter-spacing:.04em;text-transform:uppercase;margin-top:var(--s2)}
.ctl{grid-column:1/-1;display:flex;align-items:center;gap:var(--s3);flex-wrap:wrap}
.ctl>span{color:var(--ink-2);font-size:.9375rem;margin-right:var(--s2)}
.seg{display:inline-flex;flex-wrap:wrap;border:var(--rule) solid var(--ink)}
.seg button{background:var(--card);color:var(--ink);border:0;border-right:var(--rule) solid var(--ink);min-height:2.75rem;padding:.55rem 1rem;font:700 .9375rem var(--body);cursor:pointer;transition:background .2s}
.seg button:last-child{border-right:0}
.seg button:hover{background:var(--paper-2)}
.seg button[aria-pressed=true]{background:var(--ink);color:var(--paper)}
.inc{list-style:none;margin-top:var(--s3);border-top:var(--rule) solid var(--ink)}
.inc li{display:grid;grid-template-columns:12rem 1fr;gap:var(--s4);border-bottom:1px solid var(--hair);padding:var(--s3) 0;font-weight:600}
.inc time{font:500 .8125rem var(--mono);color:var(--ink-2);letter-spacing:.03em;text-transform:uppercase}
.sum{display:flex;align-items:center;gap:var(--s4);font:800 clamp(1.5rem,3.2vw,2.5rem)/1.1 var(--display);font-stretch:85%;letter-spacing:-.025em;padding:var(--s5) 0;border-top:var(--rule) solid var(--ink);margin-bottom:var(--s6)}
.sum .st{font-size:.9375rem}
.stat-top{padding-block:var(--s8) var(--s9)}
.stat-top h1{margin-bottom:var(--s6)}
.mgrid{display:grid;grid-template-columns:1fr 1fr;gap:var(--s7);margin-top:var(--s5)}
.mgrid h2{font:800 1.75rem/1.1 var(--display);font-stretch:85%;letter-spacing:-.02em;margin:0 0 var(--s4);max-width:none}
.mgrid img{display:block;width:100%;aspect-ratio:16/10;object-fit:cover;object-position:top left;border:var(--rule) solid var(--ink);margin-bottom:var(--s4)}
.mgrid .none{display:grid;place-items:center;aspect-ratio:16/10;border:var(--rule) dashed var(--ink);color:var(--ink-2);margin-bottom:var(--s4)}
.mgrid form+form{margin-top:var(--s3)}

@media(max-width:62rem){
.site-head.adm{height:auto}.site-head.adm .wrap{flex-wrap:wrap;padding-block:var(--s2)}.admin-nav{order:3;width:100%;overflow-x:auto;gap:var(--s5)}
.hero-in,.sec-head,.project-in,.project.flip .project-in,.about-in,.contact-top,.admin-grid,.mgrid{grid-template-columns:1fr}
.project.flip .p-copy{order:0}.p-copy,.admin-side{position:static}
.dial{justify-self:start;width:min(100%,19rem)}
.board a{grid-template-columns:2.5rem minmax(0,1fr) auto 2rem}.board .what{display:none}
.upd li{grid-template-columns:8.5rem minmax(0,1fr);gap:var(--s2) var(--s5)}.upd li>div{grid-column:1/-1}
.mrow,.cform,.svc,.inc li{grid-template-columns:1fr}.inc li{gap:var(--s1)}
.clock{display:none}
}
@media(max-width:40rem){
:root{--bar:3.75rem}
.mark span{display:none}
.head-r{gap:var(--s4)}.site-head nav{gap:var(--s4)}
.meta>*{margin-right:var(--s4);padding-right:0;font-size:.6875rem}.meta a{margin-left:0}
.board a{grid-template-columns:minmax(0,1fr) auto;gap:var(--s2) var(--s4);padding:var(--s4) 0}.board .idx,.board .go{display:none}
.feats li{grid-template-columns:7rem 1fr;gap:var(--s3)}.focus li{grid-template-columns:1fr;gap:var(--s1)}.focus li::before{display:none}
h1{font-size:clamp(3rem,13.5vw,4.5rem)}
.row,.stats{grid-template-columns:1fr}
.links small{display:none}
.upd li{grid-template-columns:1fr}
.foot-row{flex-direction:column;align-items:flex-start;gap:var(--s2)}
.p-top{font-size:.6875rem}
}
"""

HEAD = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>{meta}
<meta name="theme-color" content="#eeeae0">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 28 28'><rect width='28' height='28' fill='%2312110e'/><circle cx='14' cy='14' r='8' fill='%23eeeae0'/><rect x='17' y='5' width='6' height='6' fill='%23ff4d1a'/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,400..900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>{css}</style><script src="/app.js" defer></script></head>"""

E = html.escape

LOGO = ('<svg viewBox="0 0 28 28" aria-hidden="true"><rect width="28" height="28" fill="#12110e"/>'
        '<circle cx="14" cy="14" r="8" fill="#eeeae0"/><rect x="17" y="5" width="6" height="6" fill="#ff4d1a"/></svg>')
ARROW = ('<svg viewBox="0 0 20 20" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="square">'
         '<path d="M5 15 15 5M7 5h8v8"/></svg>')
SQ = '<i class="sq" aria-hidden="true"></i>'

_P = ("1111100", "1000010", "1000010", "1111100", "1000000", "1000000", "1000000")
PIXEL_ART = '<div class="mark-art"><div class="px">' + "".join(
    f'<b class="{"on" if c == "1" else ""}"></b>' for row in _P for c in row) + "</div></div>"
DONUT_ART = ('<div class="mark-art"><svg viewBox="0 0 200 200" aria-hidden="true" fill="none">'
             '<circle cx="100" cy="100" r="92" stroke="#5b574d"/><circle cx="100" cy="100" r="62" stroke="#f5a524" stroke-width="34"/>'
             '<circle cx="100" cy="100" r="26" stroke="#5b574d"/></svg></div>')

ACCENT = {"donut": "pc-donut", "pixel": "pc-pixel"}


def dial_html():
    """Instrument dial showing server time (UTC). Hands are set here so it is right without JS; app.js keeps it ticking."""
    t = time.gmtime()
    hr, mn, sc = ((t.tm_hour % 12) + t.tm_min / 60) * 30, (t.tm_min + t.tm_sec / 60) * 6, t.tm_sec * 6
    ticks = ""
    for i in range(60):
        major = i % 5 == 0
        r1, r2 = (80, 92) if major else (87, 92)
        a = math.radians(i * 6)
        x1, y1 = 100 + r1 * math.sin(a), 100 - r1 * math.cos(a)
        x2, y2 = 100 + r2 * math.sin(a), 100 - r2 * math.cos(a)
        ticks += f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" stroke-width="{2.4 if major else 1}"/>'
    return (
        '<div class="dial"><svg viewBox="0 0 200 200" role="img" aria-label="Clock showing server time in UTC" fill="none" stroke="#12110e">'
        '<circle cx="100" cy="100" r="97" stroke-width="1.5"/>' + ticks +
        f'<g class="hand" id="h-hr" style="transform:rotate({hr:.1f}deg)"><line x1="100" y1="106" x2="100" y2="56" stroke-width="5"/></g>'
        f'<g class="hand" id="h-min" style="transform:rotate({mn:.1f}deg)"><line x1="100" y1="106" x2="100" y2="30" stroke-width="3"/></g>'
        f'<g class="hand" id="h-sec" style="transform:rotate({sc}deg)" stroke="#ff4d1a"><line x1="100" y1="118" x2="100" y2="18" stroke-width="1.6"/>'
        '<rect x="96" y="12" width="8" height="8" fill="#ff4d1a" stroke="none"/></g>'
        '<circle cx="100" cy="100" r="5.5" fill="#12110e" stroke="none"/></svg>'
        f'<p class="dial-cap"><span>Server time</span><b data-utc>{time.strftime("%H:%M:%S", t)}</b><span>UTC</span></p></div>'
    )


# Served at /app.js: flags JS, reveals sections on scroll, keeps the UTC dial ticking.
SCRIPT = r"""
(function () {
  "use strict";
  var root = document.documentElement;
  root.classList.add("js");
  function two(n) { return (n < 10 ? "0" : "") + n; }
  function tick() {
    var d = new Date(), h = d.getUTCHours(), m = d.getUTCMinutes(), s = d.getUTCSeconds();
    var txt = two(h) + ":" + two(m) + ":" + two(s);
    [].forEach.call(document.querySelectorAll("[data-utc]"), function (el) { el.textContent = txt; });
    [].forEach.call(document.querySelectorAll("[data-utc-short]"), function (el) { el.textContent = two(h) + ":" + two(m) + " UTC"; });
    var set = function (id, deg) { var el = document.getElementById(id); if (el) el.style.transform = "rotate(" + deg + "deg)"; };
    set("h-hr", ((h % 12) + m / 60) * 30);
    set("h-min", (m + s / 60) * 6);
    set("h-sec", s * 6);
  }
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f.querySelector && f.querySelector("button.danger") && !window.confirm("Delete this? This cannot be undone.")) e.preventDefault();
  });
  document.addEventListener("DOMContentLoaded", function () {
    tick();
    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    setInterval(tick, still ? 30000 : 1000);
    var items = [].slice.call(document.querySelectorAll(".rv"));
    if (!items.length) return;
    function show(el) { el.classList.add("in"); }
    if (!("IntersectionObserver" in window)) { items.forEach(show); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { show(e.target); io.unobserve(e.target); } });
    }, { threshold: 0.12 });
    items.forEach(function (s) { io.observe(s); });
    setTimeout(function () { items.forEach(show); }, 6000);
  });
})();
"""

SHOTS_DIR = os.getenv("SHOTS_DIR") or os.path.join(BASE, "shots")
SHOT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def shot_file(key):
    """Real screenshot for a project, if one has been dropped in shots/ (donut.png, pixel.webp ...)."""
    for ext in SHOT_TYPES:
        if os.path.isfile(os.path.join(SHOTS_DIR, key + ext)):
            return key + ext
    return None


def shot_url(key):
    f = shot_file(key)
    if not f:
        return None
    try:
        v = int(os.path.getmtime(os.path.join(SHOTS_DIR, f)))
    except OSError:
        v = 0
    return f"/shots/{f}?v={v}"


def status_html(status):
    return f'<span class="st st-{status}">{E(PUBLIC_LABEL[status])}</span>'


def project(key, flip, url, host, fallback, name, status, desc, chips, note=""):
    feats = "".join(f"<li>{E(n)}<span>{E(d)}</span></li>" for n, d in chips)
    u = shot_url(key)
    idx = {"donut": "01", "pixel": "02"}.get(key, "")
    kind = {"donut": "Minecraft account bot", "pixel": "Community management bot"}.get(key, "Project")
    if u:
        art = (f'<a class="shot" href="{url}" aria-label="Open the {name} dashboard">'
               f'<img src="{u}" alt="{name} dashboard" loading="lazy" decoding="async" width="1600" height="1000"></a>')
    else:
        art = f'<div class="shot" aria-hidden="true">{fallback}</div>'
    return (
        f'<article class="project {ACCENT.get(key, "")}{" flip" if flip else ""}" id="{key}">'
        f'<div class="wrap"><div class="p-top mono"><span>{idx} / {kind}</span>{status_html(status)}</div>'
        f'<div class="project-in"><div class="p-copy">'
        f'<h3>{name}{SQ}</h3><p class="desc">{desc}</p>{f"<p class=p-note>{E(note)}</p>" if note else ""}'
        f'<ul class="feats" aria-label="{name} features">{feats}</ul>'
        f'<div class="cta"><a class="btn" href="{url}">Open dashboard{ARROW}</a><span class="host">{host}</span></div></div>'
        f'<div class="slab rv">{art}<div class="slab-cap mono"><span>Dashboard</span><span>{host}</span></div></div></div></div></article>'
    )


def site_head(nav):
    return (
        f'<header class="site-head"><div class="wrap"><a class="mark" href="/" aria-label="Void Labs home">{LOGO}<span>Void Labs</span></a>'
        f'<div class="head-r"><nav aria-label="Page sections">{nav}</nav>'
        f'<span class="clock" aria-hidden="true"><span data-utc-short>{time.strftime("%H:%M", time.gmtime())} UTC</span></span></div></div></header>'
    )


def site_foot(nav):
    return (
        '<footer class="site-foot"><div class="wrap foot-row">'
        f'<span>&copy; {time.gmtime().tm_year} Void Labs. Built in the void.</span><nav aria-label="Footer">{nav}</nav></div></footer>'
    )


def page_shell(nav, body, foot, banner=""):
    b = f'<div class="banner" role="status">{E(banner)}</div>' if banner else ""
    return (
        '<body><a class="skip" href="#top">Skip to content</a>' + b + site_head(nav) +
        f'<main id="top">{body}</main>{foot}</body></html>'
    )


def home_page(s, notice=""):
    s = effective(s)
    paras = "".join(f"<p>{E(p.strip())}</p>" for p in s["about"].split("\n\n") if p.strip())
    links = ""
    if s["email"]:
        links += f'<a href="mailto:{E(s["email"])}"><span>{E(s["email"])}</span><small>Email</small></a>'
    if s["discord"]:
        links += f'<a href="{E(s["discord"])}" rel="noopener" target="_blank"><span>{E(s["discord"].replace("https://", ""))}</span><small>Discord</small></a>'
    donut = project(
        "donut", False, "https://donut.voidlabs.fun", "donut.voidlabs.fun", DONUT_ART, "Donut", s["donut_status"],
        "Keeps your Minecraft account online while you're away, and tells you what happened. Run it from the web dashboard or from Discord.",
        [("Player stats", "Every player, on the web."), ("Payments", "Alerts when money arrives."),
         ("AI chat", "Built into the bot."), ("Chat logs", "Whispers and messages, recorded."),
         ("Admin dashboard", "Staff controls, online.")], s["donut_note"],
    )
    pixel = project(
        "pixel", True, "https://pixel.voidlabs.fun", "pixel.voidlabs.fun", PIXEL_ART, "Pixel", s["pixel_status"],
        "A community bot that covers the daily work of running a server, with a full web dashboard to manage it.",
        [("Tickets", "Support requests, tracked."), ("Giveaways", "Start, run and draw."),
         ("Applications", "Collect and review them."), ("Moderation", "Tools to keep order."),
         ("Staff tools", "For the people on your team.")], s["pixel_note"],
    )
    has_upd = bool(UPDATES)
    nav = '<a href="#projects">Work</a>' + ('<a href="#updates">Updates</a>' if has_upd else "") + '<a href="#about">About</a><a href="#contact">Contact</a>'
    board = "".join(
        f'<a class="{ACCENT[k]}" href="#{k}" aria-label="{n}, {E(PUBLIC_LABEL[s[k + "_status"]])}. Jump to project">'
        f'<span class="idx">{i}</span><span class="nm">{n}</span><span class="what">{w}</span>'
        f'{status_html(s[k + "_status"])}<svg class="go" viewBox="0 0 20 20" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="square"><path d="M10 3v14M4 11l6 6 6-6"/></svg></a>'
        for i, k, n, w in (("01", "donut", "Donut", "Keeps a Minecraft account online."), ("02", "pixel", "Pixel", "Runs a Discord community."))
    )
    bad = [k for k in ("donut", "pixel") if s[k + "_status"] != "online"]
    summary = "All systems running" if not bad else "Something is not online"
    flash = {"sent": '<p class="flash msg ok" role="status">Sent. Thanks, I&rsquo;ll get back to you.</p>',
             "err1": '<p class="flash msg err" role="alert">Add a way to reach you and a message (up to 1000 characters).</p>',
             "err2": '<p class="flash msg err" role="alert">Too many messages for now. Try again in an hour.</p>',
             "err3": '<p class="flash msg err" role="alert">That took too long or too short. Reload the page and try again.</p>'}.get(notice, "")
    tok = str(int(time.time()))
    tok += "." + _sig("f" + tok)
    form = (
        '<form class="cform" id="message" method="post" action="/contact"><h3>Or send a message</h3>' + flash +
        f'<input type="hidden" name="t" value="{tok}">'
        '<div class="hp" aria-hidden="true"><label>Leave empty<input name="website" tabindex="-1" autocomplete="off"></label></div>'
        '<div><label for="cn">Name <span class="hint">(optional)</span></label><input id="cn" name="name" maxlength="60" autocomplete="name"></div>'
        '<div><label for="cr">How should I reply?</label><input id="cr" name="reply" maxlength="120" required placeholder="Email or Discord username"></div>'
        '<div class="full"><label for="cm">Message</label><textarea id="cm" name="message" maxlength="1000" required></textarea></div>'
        '<button class="btn" type="submit">Send message</button></form>'
    )
    contact = (
        '<section class="contact" id="contact"><div class="wrap contact-in"><div class="contact-top"><div>'
        f'<h2>Say hello{SQ}</h2><p class="sub">Questions, bug reports or an idea for a bot. Email, Discord or the form below.</p></div>'
        f'<div class="links">{links}</div></div>{form}</div></section>'
    )
    foot = site_foot('<a href="#projects">Work</a><a href="#about">About</a><a href="#contact">Contact</a><a href="/status">Status</a>')
    body = (
        '<section class="hero"><div class="wrap">'
        f'<div class="meta mono"><span>Software studio</span><span>Discord bots + dashboards</span><span>One person</span><a href="/status">{summary}</a></div>'
        '<div class="hero-in"><div><h1><span class="ln"><span>Bots and</span></span><span class="ln"><span>dashboards,</span></span>'
        f'<span class="ln"><span>built right{SQ}</span></span></h1>'
        '<p class="lede"><b>Void Labs is a one-person studio.</b> I design, build and run Discord bots and the dashboards behind them.</p>'
        '<div class="hero-cta"><a class="btn" href="#projects">See the work</a><a class="btn ghost" href="#contact">Get in touch</a></div></div>'
        f'{dial_html()}</div>'
        f'<nav class="board" aria-label="Products and their status">{board}</nav></div></section>'
        '<section id="projects"><div class="wrap sec-head">'
        f'<h2>Donut and Pixel{SQ}</h2>'
        '<p>Two Discord bots, each with its own web dashboard. Built, hosted and maintained by me.</p></div>'
        f'{donut}{pixel}</section>'
        f'{updates_html()}'
        '<section class="about" id="about"><div class="wrap about-in"><div>'
        '<h2 class="kicker">About</h2>'
        '<p class="statement">One developer, one studio, and software I&rsquo;m happy to keep running.</p>'
        f'<div class="bio"><h3>Hi, I&rsquo;m {E(s["name"])}.</h3>{paras}</div></div>'
        '<ul class="focus"><li><strong>Software</strong><span>Written, hosted and maintained by me.</span></li>'
        '<li><strong>Discord bots</strong><span>Moderation, tickets, giveaways, applications.</span></li>'
        '<li><strong>Dashboards</strong><span>The web side of every bot: stats, logs, staff controls.</span></li>'
        '<li><strong>Automation</strong><span>The repetitive parts of running a community, handled.</span></li></ul>'
        '</div></section>'
        f'{contact}'
    )
    return HEAD.format(title="Void Labs | Bots and dashboards", css=CSS, meta='<meta name="description" content="Void Labs is a one-person software studio. Discord bots and web dashboards, including Donut and Pixel.">') + page_shell(nav, body, foot, s["banner"])


def admin_login_page(msg=""):
    m = f'<div class="msg err" role="alert">{E(msg)}</div>' if msg else ""
    body = (
        f'<body><main class="gate"><div><a class="mark" href="/">{LOGO}<span>Void Labs</span></a><div class="panel"><h1>Admin</h1>'
        f'{m}<form method="post" action="/admin/login" autocomplete="on">'
        '<label for="u" style="margin-top:0">Username</label><input id="u" name="username" autocomplete="username" required>'
        '<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>'
        '<button class="btn" type="submit">Log in</button></form></div></div></main></body></html>'
    )
    return HEAD.format(title="Admin", css=CSS, meta='<meta name="robots" content="noindex,nofollow">') + body


def admin_head(csrf, active):
    unread = unread_count()
    badge = f'<span class="badge" aria-label="{unread} unread">{unread}</span>' if unread else ""
    cur = lambda n: ' aria-current="page"' if active == n else ""
    return (
        f'<header class="site-head adm"><div class="wrap"><a class="mark" href="/">{LOGO}<span>View site</span></a>'
        f'<nav class="admin-nav" aria-label="Admin"><a href="/admin"{cur("settings")}>Settings</a><a href="/admin/ops"{cur("monitor")}>Monitor</a><a href="/admin/inbox"{cur("inbox")}>Inbox{badge}</a><a href="/admin/updates"{cur("updates")}>Updates</a><a href="/admin/media"{cur("media")}>Media</a></nav>'
        f'<form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{csrf}">'
        '<button class="btn ghost sm" type="submit">Log out</button></form></div></header>'
    )


def admin_page(s, csrf, msg="", kind="ok"):
    def opts(cur):
        return "".join(f'<option value="{v}"{" selected" if v == cur else ""}>{l}</option>' for v, l in STATUS_OPTIONS)
    m = f'<div class="msg {kind}" role="{"alert" if kind == "err" else "status"}">{E(msg)}</div>' if msg else ""
    eff = effective(s)
    live = "".join(
        f'<li>{n}{status_html(eff[k])}</li>' for n, k in (("Donut", "donut_status"), ("Pixel", "pixel_status"))
    )
    body = (
        '<body><a class="skip" href="#top">Skip to content</a>'
        + admin_head(csrf, "settings") +
        '<main id="top"><div class="wrap admin-grid">'
        f'<aside class="admin-side"><h1>Site settings</h1><p>Edits appear on the public site as soon as you save.</p><ul aria-label="Current status">{live}</ul><a class="btn ghost sm" href="/admin/backup">Download backup</a></aside>'
        f'<div>{m}<form method="post" action="/admin/save"><input type="hidden" name="csrf" value="{csrf}">'
        '<fieldset><legend>Product status</legend><div class="row"><div><label for="ds">Donut status</label>'
        f'<select id="ds" name="donut_status">{opts(s["donut_status"])}</select></div>'
        '<div><label for="ps">Pixel status</label>'
        f'<select id="ps" name="pixel_status">{opts(s["pixel_status"])}</select></div></div>'
        '<label for="dn">Donut note <span class="hint">(shown under its status, e.g. &ldquo;Back around 18:00&rdquo;)</span></label>'
        f'<input id="dn" name="donut_note" maxlength="120" value="{E(s["donut_note"])}">'
        '<label for="pn">Pixel note <span class="hint">(optional)</span></label>'
        f'<input id="pn" name="pixel_note" maxlength="120" value="{E(s["pixel_note"])}"></fieldset>'
        '<fieldset><legend>Announcement</legend><label for="bn" style="margin-top:0">Banner <span class="hint">(top of the site, hidden if empty)</span></label>'
        f'<input id="bn" name="banner" maxlength="140" value="{E(s["banner"])}" placeholder="Pixel is updating tonight at 20:00 UTC"></fieldset>'
        '<fieldset><legend>Public profile</legend>'
        '<label for="n" style="margin-top:0">Your name <span class="hint">(shown in &ldquo;Hi, I&rsquo;m &hellip;&rdquo;)</span></label>'
        f'<input id="n" name="name" maxlength="40" value="{E(s["name"])}" required>'
        '<label for="e">Contact email <span class="hint">(hidden if empty)</span></label>'
        f'<input id="e" name="email" type="email" maxlength="120" value="{E(s["email"])}" placeholder="you@example.com">'
        '<label for="d">Support Discord <span class="hint">(hidden if empty)</span></label>'
        f'<input id="d" name="discord" maxlength="80" value="{E(s["discord"])}" placeholder="discord.gg/yourcode">'
        '<label for="a">About me <span class="hint">(blank line = new paragraph)</span></label>'
        f'<textarea id="a" name="about" maxlength="1500">{E(s["about"])}</textarea></fieldset>'
        '<button class="btn" type="submit">Save changes</button></form></div></div></main></body></html>'
    )
    return HEAD.format(title="Admin", css=CSS, meta='<meta name="robots" content="noindex,nofollow">') + body



def inbox_page(csrf, msg=""):
    with _msglock:
        items = list(MSGS)
    rows = ""
    for m in items:
        reply = str(m.get("reply", ""))
        link = (f'<a href="mailto:{E(reply)}">{E(reply)}</a>' if EMAIL_RE.match(reply) else E(reply))
        unread = not m.get("read")
        ident = E(str(m["id"]))
        def btn(act, label, extra=""):
            return (f'<form method="post" action="/admin/inbox/act"><input type="hidden" name="csrf" value="{csrf}">'
                    f'<input type="hidden" name="id" value="{ident}"><button class="btn ghost sm {extra}" name="act" value="{act}" type="submit">{label}</button></form>')
        rows += (
            f'<div class="mrow{" unread" if unread else ""}"><div class="who"><strong>{"<span class=dot></span>" if unread else ""}{E(m.get("name") or "No name")}</strong>'
            f'{link}<br>{E(time.strftime("%d %b, %H:%M UTC", time.gmtime(int(m.get("t", 0)))))}</div>'
            f'<div class="body">{E(str(m.get("msg", "")))}</div>'
            f'<div class="acts">{btn("unread" if not unread else "read", "Mark unread" if not unread else "Mark read")}{btn("delete", "Delete", "danger")}</div></div>'
        )
    empty = '<p class="note">No messages yet. They arrive here when someone uses the contact form.</p>'
    m_ = f'<div class="msg ok" role="status">{E(msg)}</div>' if msg else ""
    body = (
        '<body><a class="skip" href="#top">Skip to content</a>' + admin_head(csrf, "inbox") +
        f'<main id="top"><div class="wrap"><div class="ops-top"><div><h1>Inbox</h1><p>Messages from the contact form on the site. '
        f'{"Discord gets a ping for each new one." if WEBHOOK_OK else "Set DISCORD_WEBHOOK_URL to get a Discord ping for each new one."}</p></div></div>'
        f'{m_}<div class="ops-sec">{rows or empty}</div></div></main></body></html>'
    )
    return HEAD.format(title="Inbox", css=CSS, meta='<meta name="robots" content="noindex,nofollow">') + body


def updates_html(limit=5):
    with _updlock:
        items = list(UPDATES[:limit])
    if not items:
        return ""
    rows = ""
    for u in items:
        t = int(u["t"])
        tag = UPD_TAGS.get(u.get("tag", ""), "")
        tag_html = '<span class="tag">' + E(tag) + "</span>" if tag else ""
        text_html = "<p>" + E(u.get("text", "")) + "</p>" if u.get("text") else ""
        rows += ('<li><time datetime="' + time.strftime("%Y-%m-%d", time.gmtime(t)) + '">' + E(time.strftime("%d %b %Y", time.gmtime(t))) + "</time>"
                 "<span>" + tag_html + "</span><div><h3>" + E(u["title"]) + "</h3>" + text_html + "</div></li>")
    return ('<section id="updates" class="on-ink updates-sec"><div class="wrap sec-head"><h2>Updates' + SQ + '</h2><p>What changed lately, newest first.</p></div>'
            f'<div class="wrap"><ul class="upd">{rows}</ul></div></section>')


def status_page(s):
    eff = effective(s)
    with _mlock:
        snap = {k: (list(v["hist"]), v["state"]) for k, v in MON.items()}
        inc = [i for i in INCIDENTS if i["key"] in ("donut", "pixel")][:10]
    rows, bad, unknown, states = "", 0, 0, []
    for key, name in (("donut", "Donut"), ("pixel", "Pixel")):
        hist, state = snap[key]
        manual = eff[key + "_status"]
        if manual == "maintenance":
            shown = "maintenance"
        elif manual == "offline" or state is False:
            shown = "offline"
        else:
            shown = "online"
        states.append(shown)
        if shown != "online":
            bad += 1
        if state is None and manual == "online":
            unknown += 1
        day = [h for h in hist if h["t"] >= time.time() - 86400]
        up = f'{100 * sum(1 for h in day if h["ok"]) / len(day):.1f}%' if day else "No data yet"
        bars = "".join(
            f'<i class="{"" if h["ok"] else "bad"}" title="{E(time.strftime("%H:%M UTC", time.gmtime(h["t"])))}: {"up" if h["ok"] else "down"}"></i>'
            for h in hist[-60:]) or '<i class="none"></i>'
        note = f'<p class="note">{E(s[key + "_note"])}</p>' if s[key + "_note"] else ""
        rows += (
            f'<div class="svc"><div><h2>{name}</h2>{note}</div><div>{status_html(shown)}</div>'
            f'<div><dl class="stats"><div><dt>Uptime, last 24 h</dt><dd>{up}</dd></div></dl></div>'
            f'<div style="grid-column:1/-1"><div class="strip" role="img" aria-label="{name} recent checks">{bars}</div>'
            f'<p class="strip-cap">Last 60 checks, newest on the right</p></div></div>')
    names = {"donut": "Donut", "pixel": "Pixel"}
    incs = ('<ul class="inc">' + "".join(
        f'<li><time>{E(time.strftime("%d %b, %H:%M UTC", time.gmtime(i["t"])))}</time><span>{names[i["key"]]} {"recovered" if i["ok"] else "went down"}</span></li>'
        for i in inc) + '</ul>') if inc else '<p class="note">No incidents recorded.</p>'
    if bad == 0:
        head = status_html("online") if not unknown else '<span class="st st-maintenance">Checking</span>'
        line = "Everything is running." if not unknown else "Waiting for the first checks."
    else:
        head = status_html("offline" if "offline" in states else "maintenance")
        line = "Something needs attention. Details below."
    body = (
        f'<section><div class="wrap stat-top"><h1>Status{SQ}</h1>'
        f'<div class="sum">{head}<span>{line}</span></div><div>{rows}</div><div class="ops-sec"><h2>Incidents</h2>{incs}</div></div></section>')
    foot = site_foot('<a href="/">Home</a>')
    return HEAD.format(title="Status | Void Labs", css=CSS, meta='<meta name="description" content="Live status of Donut and Pixel.">') + page_shell('<a href="/">Home</a>', body, foot, s["banner"])


def updates_admin_page(csrf, msg=""):
    with _updlock:
        items = list(UPDATES)
    opts = "".join(f'<option value="{k}">{v or "No tag"}</option>' for k, v in UPD_TAGS.items())
    rows = ""
    for u in items:
        tag = UPD_TAGS.get(u.get("tag", ""), "")
        text_html = "<p>" + E(u.get("text", "")) + "</p>" if u.get("text") else ""
        rows += ("<li><time>" + E(time.strftime("%d %b %Y", time.gmtime(int(u["t"])))) + "</time><div><h3>" + E((tag + " " + u["title"]).strip()) + "</h3>"
                 + text_html +
                 '<form method="post" action="/admin/updates/delete" style="margin-top:var(--s3)"><input type="hidden" name="csrf" value="' + csrf + '">'
                 '<input type="hidden" name="id" value="' + E(str(u["id"])) + '"><button class="btn ghost sm danger" type="submit">Delete</button></form></div></li>')
    m = f'<div class="msg ok" role="status">{E(msg)}</div>' if msg else ""
    published = ('<ul class="upd">' + rows + "</ul>") if rows else '<p class="note">Nothing published yet.</p>'
    body = (
        '<body><a class="skip" href="#top">Skip to content</a>' + admin_head(csrf, "updates") +
        '<main id="top"><div class="wrap"><div class="ops-top"><div><h1>Updates</h1><p>Short notes about what changed. The latest five show on the site, newest first. Leave this empty and the section stays hidden.</p></div></div>'
        f'{m}<form method="post" action="/admin/updates/add" class="panel"><input type="hidden" name="csrf" value="{csrf}">'
        '<fieldset style="margin:0"><legend>New update</legend><div class="row"><div><label for="ut" style="margin-top:0">Title</label><input id="ut" name="title" maxlength="80" required placeholder="Giveaway rerolls added"></div>'
        f'<div><label for="ug" style="margin-top:0">About</label><select id="ug" name="tag">{opts}</select></div></div>'
        '<label for="ux">Details <span class="hint">(optional)</span></label><textarea id="ux" name="text" maxlength="400" style="min-height:6rem"></textarea></fieldset>'
        '<button class="btn" type="submit">Publish update</button></form>'
        f'<div class="ops-sec"><h2>Published</h2>{published}</div></div></main></body></html>'
    )
    return HEAD.format(title="Updates", css=CSS, meta='<meta name="robots" content="noindex,nofollow">') + body


def media_page(csrf, msg="", kind="ok"):
    cards = ""
    for key, name in (("donut", "Donut"), ("pixel", "Pixel")):
        u = shot_url(key)
        pic = f'<img src="{u}" alt="Current {name} screenshot">' if u else '<div class="none">No screenshot yet</div>'
        rm = (f'<form method="post" action="/admin/media/remove"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="key" value="{key}">'
              '<button class="btn ghost sm danger" type="submit">Remove</button></form>') if u else ""
        cards += (
            f'<div><h2>{name}</h2>{pic}<form method="post" action="/admin/media/upload" enctype="multipart/form-data">'
            f'<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="key" value="{key}">'
            f'<label for="f-{key}" style="margin-top:0">Replace image <span class="hint">(PNG, JPG or WebP, up to 5 MB)</span></label>'
            f'<input id="f-{key}" type="file" name="file" accept="image/png,image/jpeg,image/webp" required>'
            f'<p class="hint" style="margin:var(--s3) 0">Shown in the hero tile and the {name} section. 1600 by 1000 works best.</p>'
            f'<button class="btn" type="submit">Upload</button></form>{rm}</div>')
    m = f'<div class="msg {kind}" role="{"alert" if kind == "err" else "status"}">{E(msg)}</div>' if msg else ""
    body = (
        '<body><a class="skip" href="#top">Skip to content</a>' + admin_head(csrf, "media") +
        f'<main id="top"><div class="wrap"><div class="ops-top"><div><h1>Media</h1><p>The screenshots shown for Donut and Pixel. Without one, the site shows a simple mark instead.</p></div></div>{m}'
        f'<div class="mgrid ops-sec">{cards}</div></div></main></body></html>'
    )
    return HEAD.format(title="Media", css=CSS, meta='<meta name="robots" content="noindex,nofollow">') + body


def ago(t):
    d = int(time.time() - t)
    return "just now" if d < 5 else fmt_dur(d) + " ago"


def ops_page(s, csrf, msg=""):
    rows = ""
    with _mlock:
        snap = {k: (list(v["hist"]), v["state"]) for k, v in MON.items()}
        inc = list(INCIDENTS[:10])
    names = {k: n for k, n, _ in SERVICES}
    for key, name, url in SERVICES:
        hist, state = snap[key]
        last = hist[-1] if hist else None
        day = [h for h in hist if h["t"] >= time.time() - 86400]
        up = f'{100 * sum(1 for h in day if h["ok"]) / len(day):.1f}%' if day else "No data"
        if state is None:
            badge = '<span class="st st-maintenance" style="color:var(--muted)">Checking</span>'
        else:
            badge = status_html("online" if state else "offline").replace("Online", "Up").replace("Offline", "Down")
        latency = f'{last["ms"]} ms' if last and last["ok"] else ("Failed" if last else "No data")
        code = (f'HTTP {last["code"]}' if last and last["code"] else "No response") if last else "No data"
        bars = "".join(
            f'<i class="{"" if h["ok"] else "bad"}" title="{E(time.strftime("%H:%M UTC", time.gmtime(h["t"])))}: {"up, " + str(h["ms"]) + " ms" if h["ok"] else "down"}"></i>'
            for h in hist[-60:]) or '<i class="none"></i>'
        ctl = ""
        if key in ("donut", "pixel"):
            cur = s[key + "_status"]
            btns = "".join(
                f'<button type="submit" name="status" value="{v}" aria-pressed="{"true" if cur == v else "false"}">{l.split(" (")[0]}</button>'
                for v, l in STATUS_OPTIONS)
            ctl = (f'<form class="ctl" method="post" action="/admin/ops/status"><input type="hidden" name="csrf" value="{csrf}">'
                   f'<input type="hidden" name="key" value="{key}"><span>Public status on the site</span><span class="seg" role="group" aria-label="{name} public status">{btns}</span></form>')
        rows += (
            f'<div class="svc"><div><h2><a href="{url}" rel="noopener" target="_blank">{name}</a></h2><p class="url">{E(url.replace("https://", ""))}</p></div>'
            f'<div>{badge}<p class="note">{ago(last["t"]) if last else "Waiting for first check"}</p></div>'
            f'<div><dl class="stats"><div><dt>Response</dt><dd>{latency}</dd></div><div><dt>Last result</dt><dd>{code}</dd></div><div><dt>Uptime 24h</dt><dd>{up}</dd></div></dl></div>'
            f'<div style="grid-column:1/-1"><div class="strip" role="img" aria-label="Last {len(hist[-60:])} checks">{bars}</div><p class="strip-cap">Last 60 checks, newest on the right</p></div>{ctl}</div>'
        )
    if inc:
        items = "".join(
            f'<li><time>{E(time.strftime("%d %b, %H:%M UTC", time.gmtime(i["t"])))}</time><span>{E(names.get(i["key"], i["key"]))} {"recovered" if i["ok"] else "went down"}</span></li>'
            for i in inc)
        incs = f'<ul class="inc">{items}</ul>'
    else:
        incs = '<p class="note">None recorded yet.</p>'
    m = f'<div class="msg ok" role="status">{E(msg)}</div>' if msg else ""
    hook = ("Discord alerts are on." if WEBHOOK_OK else "Discord alerts are off. Set DISCORD_WEBHOOK_URL to turn them on.")
    on = "" if MONITOR_INTERVAL else " The monitor is turned off (MONITOR_INTERVAL=0), so use Check now."
    body = (
        '<body><a class="skip" href="#top">Skip to content</a>' + admin_head(csrf, "monitor") +
        f'<main id="top"><div class="wrap"><div class="ops-top"><div><h1>Monitor</h1><p>Checked from the server every {fmt_dur(max(15, MONITOR_INTERVAL) if MONITOR_INTERVAL else 60)}. '
        f'A site counts as down after two failed checks in a row. {hook}{on}</p></div>'
        f'<form method="post" action="/admin/ops/check"><input type="hidden" name="csrf" value="{csrf}"><button class="btn" type="submit">Check now</button></form></div>'
        f'{m}<div>{rows}</div><div class="ops-sec"><h2>Incidents</h2>{incs}</div></div></main></body></html>'
    )
    return HEAD.format(title="Monitor", css=CSS, meta='<meta name="robots" content="noindex,nofollow"><meta http-equiv="refresh" content="60">') + body


NOT_ENABLED = HEAD.format(title="Admin disabled", css=CSS, meta='<meta name="robots" content="noindex">') + (
    f'<body><main class="gate"><div><a class="mark" href="/">{LOGO}<span>Void Labs</span></a><div class="panel"><h1>Admin is off</h1>'
    "<p>Set an <strong>ADMIN_PASSWORD</strong> environment variable (at least 10 characters) and restart the server to turn it on.</p>"
    "</div></div></main></body></html>"
)

NOT_FOUND = HEAD.format(title="Not found", css=CSS, meta="") + (
    '<body>' + site_head('<a href="/">Home</a>') +
    '<main class="lost" id="top"><div class="wrap"><h1>404' + SQ + '</h1><p>Nothing here.</p>'
    f'<a class="btn" href="/">Go home{ARROW}</a></div></main></body></html>'
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

    def contact(self, form):
        g = lambda k: (form.get(k, [""])[0] or "").replace("\r\n", "\n").strip()
        if g("website"):  # honeypot: bots fill it, people never see it
            return self.redirect("/?sent=1#contact")
        now = time.time()
        try:
            ts, sig = g("t").split(".", 1)
            if not (hmac.compare_digest(sig, _sig("f" + ts)) and 3 <= now - int(ts) <= 7200):
                return self.redirect("/?err=3#contact")
        except ValueError:
            return self.redirect("/?err=3#contact")
        ip = self.ip()
        hits = [t for t in _msg_hits.get(ip, []) if t > now - 3600]
        if len(hits) >= 3:
            return self.redirect("/?err=2#contact")
        name, reply, msg = g("name")[:60], g("reply"), g("message")
        if not (3 <= len(reply) <= 120) or not (1 <= len(msg) <= 1000):
            return self.redirect("/?err=1#contact")
        _msg_hits[ip] = hits + [now]
        if len(_msg_hits) > 2000:
            for k in [k for k, v in _msg_hits.items() if not v or v[-1] < now - 3600]:
                _msg_hits.pop(k, None)
        with _msglock:
            MSGS.insert(0, {"id": secrets.token_hex(6), "t": int(now), "name": name, "reply": reply, "msg": msg, "read": False})
            del MSGS[500:]
        msgs_save()
        threading.Thread(target=post_webhook, daemon=True,
                         args=(f"New message from {name or 'someone'} ({reply}):\n{msg[:400]}",)).start()
        return self.redirect("/?sent=1#contact")

    def media_upload(self):
        tok = self.session()
        if not tok:
            return self.redirect("/admin")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        ctype = self.headers.get("Content-Type", "")
        if n <= 0 or n > MAX_UPLOAD + 20000 or not ctype.startswith("multipart/form-data"):
            return self.send(413, media_page(csrf_for(tok), "That file is too big. The limit is 5 MB.", "err"), private=True)
        fields, files = parse_multipart(ctype, self.rfile.read(n))
        if not same(fields.get("csrf", ""), csrf_for(tok)):
            return self.send(403, "Bad request token. Reload the page and try again.", "text/plain")
        key, data = fields.get("key", ""), files.get("file", b"")
        ext = sniff_image(data)
        if key not in ("donut", "pixel") or not data or len(data) > MAX_UPLOAD:
            return self.send(400, media_page(csrf_for(tok), "Choose an image up to 5 MB.", "err"), private=True)
        if not ext:
            return self.send(400, media_page(csrf_for(tok), "That file is not a PNG, JPG or WebP image.", "err"), private=True)
        try:
            os.makedirs(SHOTS_DIR, exist_ok=True)
            for e in SHOT_TYPES:
                try:
                    os.remove(os.path.join(SHOTS_DIR, key + e))
                except OSError:
                    pass
            dest = os.path.join(SHOTS_DIR, key + ext)
            with open(dest + ".tmp", "wb") as fh:
                fh.write(data)
            os.replace(dest + ".tmp", dest)
        except OSError:
            return self.send(500, media_page(csrf_for(tok), "Could not save the file on the server.", "err"), private=True)
        return self.send(200, media_page(csrf_for(tok), f"{key.title()} screenshot updated."), private=True)

    # routes
    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/":
            q = parse_qs(urlparse(self.path).query)
            notice = "sent" if "sent" in q else ("err" + q["err"][0] if q.get("err", [""])[0] in ("1", "2", "3") else "")
            return self.send(200, home_page(load(), notice),
                             extra=[("Cache-Control", "no-store" if notice else "public, max-age=30")])
        if path == "/healthz":
            return self.send(200, "ok", "text/plain")
        if path == "/app.js":
            return self.send(200, SCRIPT, "text/javascript; charset=utf-8",
                             extra=[("Cache-Control", "public, max-age=300")])
        if path.startswith("/shots/"):
            name = path[7:]
            ext = os.path.splitext(name)[1].lower()
            if ext in SHOT_TYPES and name in {k + e for k in ("donut", "pixel") for e in SHOT_TYPES} and shot_file(name[:-len(ext)]) == name:
                with open(os.path.join(SHOTS_DIR, name), "rb") as fh:
                    return self.send(200, fh.read(), SHOT_TYPES[ext], extra=[("Cache-Control", "public, max-age=300")])
        if path == "/admin":
            if not ADMIN_ON:
                return self.send(503, NOT_ENABLED, private=True)
            tok = self.session()
            if not tok:
                return self.send(200, admin_login_page(), private=True)
            return self.send(200, admin_page(load(), csrf_for(tok)), private=True)
        if path == "/status":
            return self.send(200, status_page(load()), extra=[("Cache-Control", "public, max-age=30")])
        if path in ("/admin/updates", "/admin/media"):
            if not ADMIN_ON:
                return self.send(503, NOT_ENABLED, private=True)
            tok = self.session()
            if not tok:
                return self.send(200, admin_login_page(), private=True)
            return self.send(200, (updates_admin_page if path == "/admin/updates" else media_page)(csrf_for(tok)), private=True)
        if path in ("/admin/inbox", "/admin/backup"):
            if not ADMIN_ON:
                return self.send(503, NOT_ENABLED, private=True)
            tok = self.session()
            if not tok:
                return self.send(200, admin_login_page(), private=True)
            if path == "/admin/inbox":
                return self.send(200, inbox_page(csrf_for(tok)), private=True)
            with _msglock:
                dump = {"exported": int(time.time()), "settings": load(), "messages": list(MSGS)}
            return self.send(200, json.dumps(dump, indent=2), "application/json",
                             extra=[("Content-Disposition", 'attachment; filename="void-labs-backup.json"')], private=True)
        if path == "/admin/ops":
            if not ADMIN_ON:
                return self.send(503, NOT_ENABLED, private=True)
            tok = self.session()
            if not tok:
                return self.send(200, admin_login_page(), private=True)
            return self.send(200, ops_page(load(), csrf_for(tok)), private=True)
        self.send(404, NOT_FOUND)

    do_HEAD = do_GET

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/contact":
            form = self.form()
            if form is None:
                return self.send(413, "Too large", "text/plain")
            return self.contact(form)
        if not path.startswith("/admin") or not ADMIN_ON:
            return self.send(404, "Not found", "text/plain")
        if path == "/admin/media/upload":
            return self.media_upload()
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
            return self.send(200, admin_page(clean, csrf_for(tok), "Saved. Your changes are live.", "ok"), private=True)
        if path == "/admin/updates/add":
            title, text, tag = (form.get(k, [""])[0].replace("\r\n", "\n").strip() for k in ("title", "text", "tag"))
            if not title or len(title) > 80 or len(text) > 400 or tag not in UPD_TAGS:
                return self.send(400, updates_admin_page(csrf_for(tok), "Add a title (up to 80 characters). Details can be 400 at most."), private=True)
            with _updlock:
                UPDATES.insert(0, {"id": secrets.token_hex(6), "t": int(time.time()), "title": title, "text": text, "tag": tag})
                del UPDATES[200:]
            upd_save()
            return self.send(200, updates_admin_page(csrf_for(tok), "Published."), private=True)
        if path == "/admin/updates/delete":
            ident = form.get("id", [""])[0]
            with _updlock:
                UPDATES[:] = [u for u in UPDATES if u["id"] != ident]
            upd_save()
            return self.send(200, updates_admin_page(csrf_for(tok), "Deleted."), private=True)
        if path == "/admin/media/remove":
            key = form.get("key", [""])[0]
            if key not in ("donut", "pixel"):
                return self.send(400, "Bad request", "text/plain")
            for e in SHOT_TYPES:
                try:
                    os.remove(os.path.join(SHOTS_DIR, key + e))
                except OSError:
                    pass
            return self.send(200, media_page(csrf_for(tok), f"{key.title()} screenshot removed."), private=True)
        if path == "/admin/inbox/act":
            ident, act = form.get("id", [""])[0], form.get("act", [""])[0]
            if act not in ("read", "unread", "delete"):
                return self.send(400, "Bad request", "text/plain")
            with _msglock:
                for i, m in enumerate(MSGS):
                    if m["id"] == ident:
                        if act == "delete":
                            MSGS.pop(i)
                        else:
                            m["read"] = act == "read"
                        break
            msgs_save()
            return self.send(200, inbox_page(csrf_for(tok), "Done."), private=True)
        if path == "/admin/ops/check":
            check_all()
            return self.send(200, ops_page(load(), csrf_for(tok), "Checked just now."), private=True)
        if path == "/admin/ops/status":
            key, val = form.get("key", [""])[0], form.get("status", [""])[0]
            if key not in ("donut", "pixel") or (val not in STATUS_LABEL and val != "auto"):
                return self.send(400, "Bad request", "text/plain")
            data = load()
            data[key + "_status"] = val
            save(data)
            return self.send(200, ops_page(data, csrf_for(tok), f"{key.title()} is now set to {dict(STATUS_OPTIONS)[val].split(' (')[0]}."), private=True)
        self.send(404, "Not found", "text/plain")


if __name__ == "__main__":
    if not ADMIN_ON:
        print("WARNING: ADMIN_PASSWORD missing or shorter than 10 characters. /admin is disabled.", flush=True)
    if len(_SS) < 16:
        print("NOTE: SESSION_SECRET missing or under 16 chars. Using a random key; logins reset on restart.", flush=True)
    msgs_load()
    upd_load()
    if MONITOR_INTERVAL > 0:
        threading.Thread(target=monitor_loop, daemon=True).start()
    else:
        mon_load()
    print(f"Void Labs site running on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
