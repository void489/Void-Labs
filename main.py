"""Void Labs main site: public page + /admin dashboard. Python standard library only.

Environment variables (set them in the bot-hosting.net panel):
  SERVER_PORT     port to listen on (set by the panel)
  ADMIN_PASSWORD  REQUIRED to enable /admin (min 10 characters)
  ADMIN_USERNAME  optional, default "admin"
  SESSION_SECRET  optional extra secret used to sign login cookies
  DISCORD_WEBHOOK_URL  optional: Discord webhook that gets a message when a site goes down or recovers
  MONITOR_INTERVAL     optional: seconds between health checks (default 60, 0 turns the monitor off)
  MESSAGES_FILE        optional path for the contact inbox (default messages.json next to main.py)
  MONITOR_URLS         optional override, e.g. "donut=https://a.example,pixel=https://b.example,site=https://c.example"
"""
import hashlib, hmac, html, json, os, re, secrets, ssl, threading, time
import urllib.error, urllib.request
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
CSS = """
:root{
--void:#0b0b0f;--char:#141419;--raised:#1b1b22;--line:#2a2a33;--line-2:#3a3a46;
--text:#ececf1;--muted:#a1a1b0;--violet:#8f6bff;--violet-text:#a78bfa;--blue:#7fa3ff;
--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;
--display:"Schibsted Grotesk","Helvetica Neue",Arial,system-ui,sans-serif;
--body:"Hanken Grotesk",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--s1:.25rem;--s2:.5rem;--s3:.75rem;--s4:1rem;--s5:1.5rem;--s6:2rem;--s7:3rem;--s8:4.5rem;--s9:7rem;--s10:11rem;
--gutter:clamp(1.25rem,4.5vw,3.5rem);--bar:4.5rem;--r:2px}
*{box-sizing:border-box;margin:0;padding:0}
html{background:var(--void);color-scheme:dark;-webkit-text-size-adjust:100%;scroll-padding-top:var(--bar)}
@media(prefers-reduced-motion:no-preference){html{scroll-behavior:smooth}}
body{font-family:var(--body);color:var(--text);background:var(--void);line-height:1.6;font-size:1.0625rem;min-height:100vh;min-height:100svh;
display:flex;flex-direction:column;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
::selection{background:var(--violet);color:var(--void)}
:focus-visible{outline:2px solid var(--violet-text);outline-offset:4px;border-radius:var(--r)}
main{flex:1}
.wrap{width:100%;max-width:86rem;margin:0 auto;padding-inline:var(--gutter)}
.skip{position:absolute;left:var(--s4);top:-4rem;z-index:100;background:var(--violet);color:var(--void);padding:var(--s2) var(--s4);font-weight:700;border-radius:var(--r)}
.skip:focus{top:var(--s3)}

.site-head{position:sticky;top:0;z-index:30;height:var(--bar);background:var(--void);border-bottom:1px solid var(--line)}
.site-head .wrap{height:100%;display:flex;align-items:center;justify-content:space-between;gap:var(--s4)}
.mark{display:inline-flex;align-items:center;gap:var(--s3);font:700 1.2rem/1 var(--display);letter-spacing:-.01em}
.mark svg{width:1.6rem;height:1.6rem;flex:none}
.site-head nav{display:flex;gap:var(--s6)}
.site-head nav a{position:relative;padding:var(--s2) 0;font-weight:600;color:var(--muted);transition:color .2s}
.site-head nav a::after{content:"";position:absolute;left:0;right:0;bottom:0;height:1px;background:var(--violet);transform:scaleX(0);transform-origin:left;transition:transform .3s}
.site-head nav a:hover{color:var(--text)}.site-head nav a:hover::after{transform:scaleX(1)}

/* hero */
.hero{border-bottom:1px solid var(--line)}
.hero-in{display:grid;grid-template-columns:minmax(0,7fr) minmax(0,5fr);gap:var(--s7) clamp(2rem,5vw,5rem);align-items:end;padding-block:clamp(3.5rem,8vw,7rem) var(--s8)}
h1{font:800 clamp(2.6rem,7.4vw,7rem)/.92 var(--display);letter-spacing:-.045em}
h1 .ln{display:block;overflow:hidden;padding-bottom:.1em;margin-bottom:-.1em}
h1 .ln>span{display:block}
.lede{margin-top:var(--s7);font-size:clamp(1.125rem,1.5vw,1.25rem);line-height:1.55;color:var(--muted);max-width:27em}
.lede b{color:var(--text);font-weight:600}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:var(--s4)}
.tile{display:flex;flex-direction:column;background:var(--char);border:1px solid var(--line-2);border-radius:var(--r);overflow:hidden;transition:border-color .25s,transform .35s cubic-bezier(.2,.8,.2,1)}
.tile:hover{border-color:var(--violet);transform:translateY(-4px)}
.t-art{position:relative;aspect-ratio:4/3;background:var(--void);border-bottom:1px solid var(--line-2);overflow:hidden;display:flex;align-items:center;justify-content:center}
.t-art img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;object-position:top left}
.t-art svg{width:62%;height:auto}
.t-art .px{width:48%}
.t-cap{display:flex;flex-direction:column;gap:var(--s2);padding:var(--s4)}
.t-cap .st{align-items:flex-start}.t-cap .st::before{margin-top:.4em}
.t-cap .nm{font:700 1.5rem/1 var(--display);letter-spacing:-.02em;transition:color .2s}
.tile:hover .nm{color:var(--violet-text)}
@media(prefers-reduced-motion:no-preference){
h1 .ln>span{animation:rise 1s cubic-bezier(.2,.8,.2,1) both}
h1 .ln:nth-child(2)>span{animation-delay:.09s}h1 .ln:nth-child(3)>span{animation-delay:.18s}
.lede{animation:fade .9s .45s both}.tiles{animation:fade 1s .35s both}}
@keyframes rise{from{transform:translateY(105%)}}
@keyframes fade{from{opacity:0}}

/* status */
.st{display:inline-flex;align-items:center;gap:var(--s2);font-weight:600;font-size:.9375rem}
.st::before{content:"";width:.6rem;height:.6rem;flex:none;border-radius:50%;background:var(--ok)}
.st-maintenance::before{background:var(--warn);border-radius:1px;transform:rotate(45deg)}
.st-offline::before{background:transparent;border:2px solid var(--bad)}
.st-online{color:var(--ok)}.st-maintenance{color:var(--warn)}.st-offline{color:var(--bad)}

/* projects */
.sec-head{padding-block:var(--s9) var(--s7);display:flex;justify-content:space-between;align-items:end;gap:var(--s6);flex-wrap:wrap}
h2{font:800 clamp(2.5rem,6vw,5rem)/.95 var(--display);letter-spacing:-.04em}
.sec-head p{color:var(--muted);max-width:24em}
.project{border-top:1px solid var(--line);padding-block:var(--s8)}
.project-in{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:var(--s7) clamp(2rem,6vw,6rem);align-items:start}
.project.flip .project-in{grid-template-columns:minmax(0,7fr) minmax(0,5fr)}
.project.flip .p-copy{order:2}
.p-copy{position:sticky;top:calc(var(--bar) + var(--s6))}
h3{font:800 clamp(3.25rem,7vw,6.5rem)/.9 var(--display);letter-spacing:-.045em;margin:var(--s4) 0 var(--s5)}
.desc{font-size:1.1875rem;line-height:1.6;color:var(--muted);max-width:28em}
.feats{list-style:none;margin:var(--s6) 0 var(--s6);max-width:34rem}
.feats li{display:grid;grid-template-columns:9.5rem 1fr;gap:var(--s4);border-top:1px solid var(--line);padding:var(--s3) 0;font-weight:600}
.feats li span{font-weight:400;color:var(--muted)}
.cta{display:flex;align-items:center;gap:var(--s5);flex-wrap:wrap}
.host{color:var(--muted);font-size:.9375rem}
.feats li:last-child{border-bottom:1px solid var(--line)}
.btn{display:inline-flex;align-items:center;gap:var(--s3);background:var(--violet);color:var(--void);font:700 1rem/1 var(--body);
padding:1.05rem 1.4rem;border:1px solid var(--violet);border-radius:var(--r);cursor:pointer;transition:background .2s,color .2s,border-color .2s}
.btn svg{width:1rem;height:1rem;transition:transform .25s}
.btn:hover{background:var(--text);border-color:var(--text)}
.btn:hover svg{transform:translate(2px,-2px)}
.btn.ghost{background:transparent;color:var(--text);border-color:var(--line-2)}
.btn.ghost:hover{background:var(--text);color:var(--void);border-color:var(--text)}
.btn.sm{padding:.7rem 1rem}
.p-art{position:relative}
.shot{position:relative;display:block}
.shot::before{content:"";position:absolute;inset:0;border:1px solid rgba(143,107,255,.5);transform:translate(14px,14px);border-radius:var(--r);pointer-events:none;transition:transform .35s cubic-bezier(.2,.8,.2,1)}
.shot:hover::before{transform:translate(20px,20px)}
.shot img{position:relative;display:block;width:100%;aspect-ratio:16/10;object-fit:cover;object-position:top left;background:var(--char);border:1px solid var(--line-2);border-radius:var(--r)}
.flip .shot::before{transform:translate(-14px,14px)}.flip .shot:hover::before{transform:translate(-20px,20px)}
.mark-art{display:flex;justify-content:center;align-items:center;border:1px solid var(--line);aspect-ratio:16/10;background:var(--char);border-radius:var(--r)}
.mark-art svg{width:min(60%,22rem);height:auto}
.px{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;width:min(52%,18rem);aspect-ratio:1}
.px b{border:1px solid var(--line-2)}.px b.on{background:var(--violet);border-color:var(--violet)}
@media(prefers-reduced-motion:no-preference){
.js .shot{opacity:0;transform:translateY(24px);transition:opacity .9s ease,transform 1s cubic-bezier(.2,.8,.2,1)}
.js .shot.in{opacity:1;transform:none}}

/* about */
.about{border-top:1px solid var(--line);background:var(--char)}
.about-in{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:var(--s7) clamp(2rem,6vw,6rem);padding-block:var(--s9)}
.about h2{max-width:8ch}
.statement{font:600 clamp(1.5rem,2.8vw,2.375rem)/1.25 var(--display);letter-spacing:-.02em;max-width:22em}
.focus{list-style:none;margin:var(--s7) 0}
.focus li{display:grid;grid-template-columns:9rem 1fr;gap:var(--s4);border-top:1px solid var(--line-2);padding:var(--s4) 0}
.focus li:last-child{border-bottom:1px solid var(--line-2)}
.focus strong{font:700 1.125rem/1.4 var(--display);color:var(--violet-text)}
.focus span{color:var(--muted)}
.bio h3{font:700 1.25rem/1.2 var(--display);letter-spacing:-.01em;margin:0 0 var(--s3)}
.bio p{color:var(--muted);max-width:36em}.bio p+p{margin-top:var(--s4)}

/* contact */
.contact{border-top:1px solid var(--line)}
.contact-in{padding-block:var(--s9)}
.contact h2{margin-bottom:var(--s4)}
.contact .sub{color:var(--muted);font-size:1.1875rem;max-width:26em;margin-bottom:var(--s7)}
.links a{display:flex;justify-content:space-between;align-items:baseline;gap:var(--s5);border-top:1px solid var(--line-2);padding:var(--s5) 0;
font:700 clamp(1.5rem,5vw,3.75rem)/1.1 var(--display);letter-spacing:-.025em;overflow-wrap:anywhere;transition:color .2s,padding .3s}
.links a:last-child{border-bottom:1px solid var(--line-2)}
.links small{font:600 1rem var(--body);letter-spacing:0;color:var(--muted);flex:none}
.links a:hover{color:var(--violet-text);padding-left:var(--s4)}

.site-foot{border-top:1px solid var(--line);padding-block:var(--s6)}
.foot-row{display:flex;justify-content:space-between;align-items:center;gap:var(--s5);flex-wrap:wrap;color:var(--muted);font-size:.9375rem}
.foot-row nav{display:flex;gap:var(--s5)}.foot-row nav a:hover{color:var(--text)}

/* admin + utility */
.panel{background:var(--char);border:1px solid var(--line-2);border-radius:var(--r);padding:clamp(1.25rem,3vw,2rem)}
.admin-grid{display:grid;grid-template-columns:minmax(0,18rem) minmax(0,1fr);gap:var(--s7);padding-block:var(--s7) var(--s8);align-items:start}
.admin-side{position:sticky;top:calc(var(--bar) + var(--s5))}
.admin-side h1{font-size:clamp(2.25rem,4vw,3.25rem);overflow-wrap:anywhere;line-height:.98;letter-spacing:-.03em;margin-bottom:var(--s5)}
.admin-side p{color:var(--muted);margin-bottom:var(--s4);max-width:22em}
.admin-side ul{list-style:none;margin:var(--s5) 0}
.admin-side li{display:flex;align-items:center;justify-content:space-between;gap:var(--s3);padding:var(--s3) 0;border-top:1px solid var(--line-2);font-weight:600}
.admin-side li:last-child{border-bottom:1px solid var(--line-2)}
fieldset{border:0;margin:0 0 var(--s7)}
legend{font:700 1.5rem/1.1 var(--display);letter-spacing:-.02em;margin-bottom:var(--s4);padding:0}
label{display:block;font-weight:600;margin:var(--s5) 0 var(--s2)}
.hint{color:var(--muted);font-size:.9375rem;font-weight:400}
input,textarea,select{width:100%;background:var(--void);color:var(--text);border:1px solid var(--line-2);border-radius:var(--r);padding:.8rem .9rem;font:inherit;font-size:1rem;transition:border-color .2s}
input:hover,textarea:hover,select:hover{border-color:var(--muted)}
input:focus,textarea:focus,select:focus{outline:2px solid var(--violet-text);outline-offset:2px;border-color:var(--violet)}
select{color-scheme:dark}
textarea{min-height:12rem;resize:vertical;line-height:1.55}
.row{display:grid;grid-template-columns:1fr 1fr;gap:var(--s5)}
.row label:first-child{margin-top:0}
.msg{border-left:4px solid;padding:var(--s3) var(--s4);margin-bottom:var(--s5);font-weight:600;border-radius:var(--r)}
.msg.ok{background:#0f2a1b;color:#86efac;border-color:var(--ok)}
.msg.err{background:#331414;color:#fca5a5;border-color:var(--bad)}
.gate{min-height:100vh;min-height:100svh;display:grid;place-items:center;padding:var(--gutter)}
.gate>div{width:100%;max-width:26rem}
.gate h1{font-size:clamp(2.25rem,8vw,3rem);line-height:1;margin-bottom:var(--s5)}
.gate .mark{margin-bottom:var(--s5)}
.gate .btn{margin-top:var(--s6);width:100%;justify-content:center}
.gate p{margin-bottom:var(--s3);color:var(--muted)}.gate p strong{color:var(--text)}
.lost{flex:1;display:flex;align-items:center}
.lost h1{font-size:clamp(6rem,24vw,18rem);color:var(--violet)}
.lost p{font-size:1.25rem;margin:var(--s5) 0 var(--s6);color:var(--muted)}

/* banner + notes + contact form + inbox */
.banner{background:var(--violet);color:var(--void);font-weight:600;text-align:center;padding:var(--s3) var(--gutter)}
.p-note{margin-top:var(--s2);color:var(--text);font-weight:500;max-width:28em}
.cform{margin-top:var(--s8);display:grid;grid-template-columns:1fr 1fr;gap:var(--s5) var(--s6);max-width:48rem}
.cform h3{grid-column:1/-1;font:700 1.5rem/1.1 var(--display);letter-spacing:-.02em;margin:0}
.cform label{margin:0 0 var(--s2)}
.cform .full{grid-column:1/-1}
.cform .btn{justify-self:start;margin:0}
.hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}
.flash{grid-column:1/-1;margin:0}
.flash.msg{margin-bottom:0}
.mrow{border-top:1px solid var(--line-2);padding:var(--s5) 0;display:grid;grid-template-columns:minmax(0,14rem) minmax(0,1fr) auto;gap:var(--s4) var(--s6);align-items:start}
.mrow:last-child{border-bottom:1px solid var(--line-2)}
.mrow.unread .who{color:var(--text)}.mrow .who{color:var(--muted)}
.mrow .who strong{display:block;color:var(--text);font-weight:700}
.mrow .who a{color:var(--violet-text);overflow-wrap:anywhere}.mrow .who a:hover{text-decoration:underline}
.mrow .body{white-space:pre-wrap;overflow-wrap:anywhere;max-width:44em}
.mrow .acts{display:flex;gap:var(--s2);flex-wrap:wrap}
.dot{display:inline-block;width:.55rem;height:.55rem;border-radius:50%;background:var(--violet);margin-right:var(--s2)}
.badge{background:var(--violet);color:var(--void);border-radius:99px;font-size:.8125rem;padding:.05rem .45rem;margin-left:var(--s2)}
.btn.danger:hover{background:var(--bad);border-color:var(--bad);color:var(--void)}
/* monitor */
.admin-nav{display:flex;gap:var(--s5)}
.admin-nav a{padding:var(--s2) 0;font-weight:600;color:var(--muted);border-bottom:1px solid transparent}
.admin-nav a:hover{color:var(--text)}.admin-nav a[aria-current]{color:var(--text);border-color:var(--violet)}
.ops-top{display:flex;justify-content:space-between;align-items:end;gap:var(--s5);flex-wrap:wrap;padding-block:var(--s7) var(--s6)}
.ops-top h1{font-size:clamp(2.5rem,5vw,4rem);line-height:.95;letter-spacing:-.04em}
.ops-top p{color:var(--muted);max-width:34em;margin-top:var(--s3)}
.ops-top .btn{margin:0}
.svc{border-top:1px solid var(--line-2);padding:var(--s5) 0;display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,.8fr) minmax(0,1.4fr);gap:var(--s4) var(--s6);align-items:center}
.svc:last-of-type{border-bottom:1px solid var(--line-2)}
.svc h2{font:700 1.5rem/1.1 var(--display);letter-spacing:-.02em;max-width:none}
.svc h2 a:hover{color:var(--violet-text)}
.svc .url{color:var(--muted);font-size:.9375rem;overflow-wrap:anywhere}
.stats{display:grid;grid-template-columns:repeat(3,auto);gap:var(--s4);justify-content:start}
.stats dt{color:var(--muted);font-size:.875rem}.stats dd{font:700 1.125rem/1.3 var(--display)}
.strip{display:flex;align-items:flex-end;gap:2px;height:2rem}
.strip i{flex:1;min-width:2px;height:100%;background:var(--ok);border-radius:1px}
.strip i.bad{height:35%;background:var(--bad)}.strip i.none{height:12%;background:var(--line-2)}
.strip-cap{color:var(--muted);font-size:.8125rem;margin-top:var(--s1)}
.ctl{grid-column:1/-1;display:flex;align-items:center;gap:var(--s3);flex-wrap:wrap}
.ctl>span{color:var(--muted);font-size:.9375rem;margin-right:var(--s2)}
.seg{display:inline-flex;border:1px solid var(--line-2);border-radius:var(--r);overflow:hidden}
.seg button{background:transparent;color:var(--text);border:0;border-right:1px solid var(--line-2);padding:.55rem .9rem;font:600 .9375rem var(--body);cursor:pointer;transition:background .2s}
.seg button:last-child{border-right:0}
.seg button:hover{background:var(--raised)}
.seg button[aria-pressed=true]{background:var(--violet);color:var(--void)}
.inc{list-style:none;margin-top:var(--s4)}
.inc li{display:grid;grid-template-columns:11rem 1fr;gap:var(--s4);border-top:1px solid var(--line);padding:var(--s3) 0}
.inc li:last-child{border-bottom:1px solid var(--line)}
.inc time{color:var(--muted)}
.ops-sec{padding-bottom:var(--s8)}
.ops-sec h2{font:700 1.5rem/1.1 var(--display);letter-spacing:-.02em;margin:var(--s7) 0 var(--s2);max-width:none}
.note{color:var(--muted)}
@media(max-width:62rem){
.mrow{grid-template-columns:1fr}.cform{grid-template-columns:1fr}
.svc{grid-template-columns:1fr}.inc li{grid-template-columns:1fr;gap:var(--s1)}

.project-in,.project.flip .project-in,.about-in{grid-template-columns:1fr}
.project.flip .p-copy{order:0}
.p-copy{position:static}
.hero-in{grid-template-columns:1fr;align-items:start}
h1{font-size:clamp(2.6rem,12.5vw,7rem)}
.shot::before,.flip .shot::before{transform:translate(8px,8px)}
.admin-grid{grid-template-columns:1fr}.admin-side{position:static}
}
@media(max-width:40rem){
:root{--bar:4rem}
.site-head nav{gap:var(--s4)}
.t-art{aspect-ratio:4/3}.t-cap{padding:var(--s3)}.t-cap .nm{font-size:1.25rem}
.focus li,.feats li{grid-template-columns:1fr;gap:var(--s1)}
.row{grid-template-columns:1fr}
.links small{display:none}
.foot-row{flex-direction:column;align-items:flex-start}
}
"""

HEAD = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>{meta}
<meta name="theme-color" content="#0b0b0f">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%230b0b0f'/><circle cx='50' cy='50' r='34' fill='none' stroke='%238f6bff' stroke-width='6'/><circle cx='50' cy='16' r='9' fill='%238f6bff'/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&family=Schibsted+Grotesk:wght@600..800&display=swap" rel="stylesheet">
<style>{css}</style><script src="/app.js"></script></head>"""

E = html.escape

LOGO = ('<svg viewBox="0 0 28 28" aria-hidden="true"><circle cx="14" cy="14" r="11" fill="none" stroke="#8f6bff" stroke-width="2"/>'
        '<circle cx="14" cy="3" r="3" fill="#8f6bff"/></svg>')
ARROW = ('<svg viewBox="0 0 20 20" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="square">'
         '<path d="M5 15 15 5M7 5h8v8"/></svg>')

_P = ("1111100", "1000010", "1000010", "1111100", "1000000", "1000000", "1000000")
PIXEL_ART = '<div class="mark-art"><div class="px">' + "".join(
    f'<b class="{"on" if c == "1" else ""}"></b>' for row in _P for c in row) + "</div></div>"
DONUT_ART = ('<div class="mark-art"><svg viewBox="0 0 200 200" aria-hidden="true" fill="none">'
             '<circle cx="100" cy="100" r="92" stroke="#3a3a46"/><circle cx="100" cy="100" r="62" stroke="#8f6bff" stroke-width="34"/>'
             '<circle cx="100" cy="100" r="26" stroke="#3a3a46"/></svg></div>')

TILE_DONUT = DONUT_ART.replace('class="mark-art"', 'style="display:contents"')
TILE_PIXEL = PIXEL_ART.replace('class="mark-art"', 'style="display:contents"')

# Served at /app.js: flags JS early, then reveals project screenshots once as they scroll into view.
SCRIPT = r"""
(function () {
  "use strict";
  document.documentElement.classList.add("js");
  document.addEventListener("DOMContentLoaded", function () {
    var shots = [].slice.call(document.querySelectorAll(".shot"));
    if (!shots.length) return;
    function show(el) { el.classList.add("in"); }
    if (!("IntersectionObserver" in window)) { shots.forEach(show); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { show(e.target); io.unobserve(e.target); } });
    }, { threshold: 0.15 });
    shots.forEach(function (s) { io.observe(s); });
    setTimeout(function () { shots.forEach(show); }, 6000);
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


def tile(key, name, status, fallback):
    f = shot_file(key)
    art = f'<img src="/shots/{f}" alt="" loading="eager" decoding="async">' if f else fallback
    return (f'<a class="tile" href="#{key}" aria-label="{name}, {E(PUBLIC_LABEL[status])}. Jump to project">'
            f'<div class="t-art" aria-hidden="true">{art}</div>'
            f'<div class="t-cap"><span class="nm">{name}</span>{status_html(status)}</div></a>')


def status_html(status):
    return f'<span class="st st-{status}">{E(PUBLIC_LABEL[status])}</span>'


def project(key, flip, url, host, fallback, name, status, desc, chips, note=""):
    feats = "".join(f"<li>{E(n)}<span>{E(d)}</span></li>" for n, d in chips)
    f = shot_file(key)
    if f:
        art = (f'<a class="shot" href="{url}" aria-label="Open the {name} dashboard">'
               f'<img src="/shots/{f}" alt="{name} dashboard" loading="lazy" decoding="async"></a>')
    else:
        art = f'<div class="shot" aria-hidden="true">{fallback}</div>'
    return (
        f'<article class="project{" flip" if flip else ""}" id="{key}"><div class="wrap project-in"><div class="p-copy">'
        f'{status_html(status)}{f"<p class=p-note>{E(note)}</p>" if note else ""}<h3>{name}</h3><p class="desc">{desc}</p><ul class="feats" aria-label="{name} features">{feats}</ul>'
        f'<div class="cta"><a class="btn" href="{url}">Open dashboard{ARROW}</a><span class="host">{host}</span></div></div>'
        f'<div class="p-art">{art}</div></div></article>'
    )


def page_shell(nav, body, foot, banner=""):
    b = f'<div class="banner" role="status">{E(banner)}</div>' if banner else ""
    return (
        '<body><a class="skip" href="#top">Skip to content</a>' + b +
        f'<header class="site-head"><div class="wrap"><a class="mark" href="/" aria-label="Void Labs home">{LOGO}Void Labs</a>'
        f'<nav aria-label="Page sections">{nav}</nav></div></header>'
        f'<main id="top">{body}</main>{foot}</body></html>'
    )


def home_page(s, notice=""):
    s = effective(s)
    paras = "".join(f"<p>{E(p.strip())}</p>" for p in s["about"].split("\n\n") if p.strip())
    has_contact = bool(s["email"] or s["discord"])
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
    nav = '<a href="#projects">Work</a><a href="#about">About</a>' + '<a href="#contact">Contact</a>'
    tiles = (tile("donut", "Donut", s["donut_status"], TILE_DONUT) + tile("pixel", "Pixel", s["pixel_status"], TILE_PIXEL))
    flash = {"sent": '<p class="flash msg ok" role="status">Sent. Thanks, I&rsquo;ll get back to you.</p>',
             "err1": '<p class="flash msg err" role="alert">Add a way to reach you and a message (up to 1000 characters).</p>',
             "err2": '<p class="flash msg err" role="alert">Too many messages for now. Try again in an hour.</p>',
             "err3": '<p class="flash msg err" role="alert">That took too long or too short. Reload the page and try again.</p>'}.get(notice, "")
    tok = str(int(time.time()))
    tok += "." + _sig("f" + tok)
    form = (
        '<form class="cform" method="post" action="/contact"><h3>Or send a message</h3>' + flash +
        f'<input type="hidden" name="t" value="{tok}">'
        '<div class="hp" aria-hidden="true"><label>Leave empty<input name="website" tabindex="-1" autocomplete="off"></label></div>'
        '<div><label for="cn">Name <span class="hint">(optional)</span></label><input id="cn" name="name" maxlength="60" autocomplete="name"></div>'
        '<div><label for="cr">How should I reply?</label><input id="cr" name="reply" maxlength="120" required placeholder="Email or Discord username"></div>'
        '<div class="full"><label for="cm">Message</label><textarea id="cm" name="message" maxlength="1000" required></textarea></div>'
        '<button class="btn" type="submit">Send message</button></form>'
    )
    contact = (
        '<section class="contact" id="contact"><div class="wrap contact-in"><h2>Say hello.</h2><p class="sub">Questions, bug reports or an idea for a bot. Email, Discord or the form below.</p>'
        f'<div class="links">{links}</div>{form}</div></section>'
    )
    foot_nav = '<a href="#projects">Work</a><a href="#about">About</a>' + '<a href="#contact">Contact</a>'
    body = (
        '<section class="hero"><div class="wrap hero-in"><div><h1><span class="ln"><span>Bots and</span></span>'
        '<span class="ln"><span>dashboards,</span></span><span class="ln"><span>built right.</span></span></h1>'
        '<p class="lede"><b>Void Labs is a one-person studio.</b> I design, build and run Discord bots and the dashboards behind them.</p></div>'
        f'<div class="tiles" aria-label="Products and their status">{tiles}</div></div></section>'
        '<section id="projects"><div class="wrap sec-head"><h2>Donut and Pixel.</h2>'
        '<p>Two Discord bots, each with its own web dashboard. Built, hosted and maintained by me.</p></div>'
        f'{donut}{pixel}</section>'
        '<section class="about" id="about"><div class="wrap about-in"><h2>About</h2><div>'
        '<p class="statement">One developer, one studio, and software I&rsquo;m happy to keep running.</p>'
        '<ul class="focus"><li><strong>Software</strong><span>Written, hosted and maintained by me.</span></li>'
        '<li><strong>Discord bots</strong><span>Moderation, tickets, giveaways, applications.</span></li>'
        '<li><strong>Dashboards</strong><span>The web side of every bot: stats, logs, staff controls.</span></li>'
        '<li><strong>Automation</strong><span>The repetitive parts of running a community, handled.</span></li></ul>'
        f'<div class="bio"><h3>Hi, I&rsquo;m {E(s["name"])}.</h3>{paras}</div></div></div></section>'
        f'{contact}'
    )
    foot = (
        '<footer class="site-foot"><div class="wrap foot-row">'
        f'<span>&copy; {time.gmtime().tm_year} Void Labs. Built in the void.</span><nav aria-label="Footer">{foot_nav}</nav></div></footer>'
    )
    return HEAD.format(title="Void Labs | Bots and dashboards", css=CSS, meta='<meta name="description" content="Void Labs is a one-person software studio. Discord bots and web dashboards, including Donut and Pixel.">') + page_shell(nav, body, foot, s["banner"])


def admin_login_page(msg=""):
    m = f'<div class="msg err" role="alert">{E(msg)}</div>' if msg else ""
    body = (
        f'<body><main class="gate"><div><a class="mark" href="/">{LOGO}Void Labs</a><div class="panel"><h1>Admin</h1>'
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
        f'<header class="site-head"><div class="wrap"><a class="mark" href="/">{LOGO}View site</a>'
        f'<nav class="admin-nav" aria-label="Admin"><a href="/admin"{cur("settings")}>Settings</a><a href="/admin/ops"{cur("monitor")}>Monitor</a><a href="/admin/inbox"{cur("inbox")}>Inbox{badge}</a></nav>'
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
    f'<body><main class="gate"><div><a class="mark" href="/">{LOGO}Void Labs</a><div class="panel"><h1>Admin is off</h1>'
    "<p>Set an <strong>ADMIN_PASSWORD</strong> environment variable (at least 10 characters) and restart the server to turn it on.</p>"
    "</div></div></main></body></html>"
)

NOT_FOUND = HEAD.format(title="Not found", css=CSS, meta="") + (
    f'<body><header class="site-head"><div class="wrap"><a class="mark" href="/">{LOGO}Void Labs</a></div></header>'
    '<main class="lost" id="top"><div class="wrap"><h1>404</h1><p>Nothing here.</p>'
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
    if MONITOR_INTERVAL > 0:
        threading.Thread(target=monitor_loop, daemon=True).start()
    else:
        mon_load()
    print(f"Void Labs site running on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
