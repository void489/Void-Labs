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
:root{--void:#09070f;--deep:#120d1f;--ink:#f1ecfb;--muted:#9d92b8;--frosting:#c68bff;--cyan:#8f84ff;--accent:#a855f7;--line:#251b3e;
--ok:#4ade80;--warn:#fbbf24;--bad:#f87171}
*{box-sizing:border-box;margin:0}
html{background:var(--void);scroll-behavior:smooth}
body{font-family:"Bricolage Grotesque",system-ui,sans-serif;color:var(--ink);line-height:1.6;font-size:16px;min-height:100vh;
display:flex;flex-direction:column;background:radial-gradient(50rem 32rem at 80% -8%,rgba(124,58,237,.28) 0%,transparent 70%),var(--void)}
a{color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.wrap{width:100%;max-width:960px;margin:0 auto;padding-left:clamp(1.25rem,5vw,2rem);padding-right:clamp(1.25rem,5vw,2rem)}
header{border-bottom:1px solid var(--line)}
header .wrap{display:flex;justify-content:space-between;align-items:center;padding-top:1rem;padding-bottom:1rem;gap:1rem;flex-wrap:wrap}
.mark{display:inline-flex;align-items:center;gap:.6rem;font-weight:600;font-size:1rem;text-decoration:none}
.mark i{width:1.3rem;height:1.3rem;border-radius:50%;background:radial-gradient(circle,var(--void) 0 30%,var(--accent) 32% 100%);animation:breathe 5s ease-in-out infinite}
@keyframes breathe{50%{transform:scale(1.1)}}
nav.top{display:flex;gap:1.5rem;font-size:.95rem}
nav.top a{color:var(--muted);text-decoration:none;font-weight:500;transition:color .2s}
nav.top a:hover{color:var(--ink)}
.hero{padding-top:clamp(2.5rem,7vw,5rem);padding-bottom:clamp(1rem,3vw,2rem)}
h1{font-weight:700;font-size:clamp(2rem,5.5vw,3.5rem);line-height:1.08;letter-spacing:-.03em;max-width:16ch}
.lede{margin-top:1rem;color:var(--muted);font-size:1.05rem;max-width:46ch}
section{padding-top:clamp(2rem,5vw,3.25rem)}
h2{font-weight:700;font-size:1.35rem;letter-spacing:-.02em;line-height:1.2;margin-bottom:1rem}
.projects{display:grid;gap:1rem}
.site{isolation:isolate;display:grid;grid-template-columns:auto 1fr auto;gap:1.25rem;align-items:start;
padding:1.35rem 1.5rem;background:var(--deep);border:1px solid var(--line);border-radius:14px;text-decoration:none;
transition:border-color .2s,transform .2s,box-shadow .2s}
.site:hover,.site:focus-visible{border-color:var(--c);transform:translateY(-2px);box-shadow:0 10px 30px -12px rgba(124,58,237,.45)}
.badge{width:3rem;height:3rem;flex:none;margin-top:.15rem}
.donut{border-radius:50%;background:radial-gradient(circle,var(--deep) 0 24%,var(--frosting) 26% 100%)}
.pixel{display:grid;grid-template-columns:repeat(4,1fr);gap:3px}
.pixel b{background:var(--cyan);opacity:.2;border-radius:1px}.pixel b.on{opacity:1}
.title{display:flex;align-items:center;gap:.7rem;flex-wrap:wrap}
.name{font-weight:700;font-size:1.35rem;letter-spacing:-.02em;line-height:1.2}
.pill{display:inline-flex;align-items:center;gap:.4rem;padding:.1rem .6rem;border:1px solid currentColor;border-radius:999px;font-size:.75rem;font-weight:600}
.pill::before{content:"";width:.45rem;height:.45rem;border-radius:50%;background:currentColor}
.s-online{color:var(--ok)}.s-maintenance{color:var(--warn)}.s-offline{color:var(--bad)}
.desc{color:var(--muted);margin-top:.35rem;font-size:.95rem;max-width:56ch}
.chips{list-style:none;padding:0;display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.9rem}
.chips li{background:rgba(168,85,247,.1);border:1px solid rgba(168,85,247,.25);border-radius:6px;padding:.05rem .55rem;font-size:.8rem;color:#d9c3fa}
.host{font-weight:500;font-size:.85rem;color:var(--c);text-align:right;padding-top:.35rem}
.about p{color:var(--muted);font-size:1rem;max-width:62ch}
.about p strong{color:var(--ink)}
.about p+p{margin-top:.9rem}
.contact p{color:var(--muted);max-width:50ch}
.links{margin-top:1rem;display:flex;flex-direction:column;gap:.5rem;font-size:1rem;font-weight:500}
.links a{text-decoration-color:var(--line);text-underline-offset:5px}
.links a:hover{text-decoration-color:var(--accent)}
.links span{color:var(--muted);font-weight:400;margin-right:.6rem}
main{flex:1;padding-bottom:3rem}
footer{border-top:1px solid var(--line);padding:1.5rem 0;color:var(--muted);font-size:.85rem}
@media(max-width:640px){.site{grid-template-columns:auto 1fr;padding:1.15rem}.host{grid-column:1/-1;text-align:left;padding-top:0}.badge{width:2.5rem;height:2.5rem}}
@media(prefers-reduced-motion:reduce){.mark i{animation:none}.site{transition:none}.site:hover{transform:none}html{scroll-behavior:auto}}
/* admin */
.card{background:var(--deep);border:1px solid var(--line);border-radius:14px;padding:clamp(1.25rem,3vw,1.75rem);max-width:600px}
.card h1{font-size:1.5rem;letter-spacing:-.02em;line-height:1.2;margin-bottom:1rem;max-width:none}
label{display:block;font-weight:600;font-size:.9rem;margin:1rem 0 .35rem}
.hint{color:var(--muted);font-size:.8rem;font-weight:400}
input,textarea,select{width:100%;background:var(--void);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:.6rem .75rem;font:inherit;font-size:.95rem}
input:focus,textarea:focus,select:focus{border-color:var(--accent);outline:none}
textarea{min-height:10rem;resize:vertical}
.row{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
@media(max-width:560px){.row{grid-template-columns:1fr}}
button{margin-top:1.25rem;background:var(--accent);color:#fff;border:0;border-radius:8px;padding:.65rem 1.25rem;font:inherit;font-weight:600;cursor:pointer;transition:background .2s}
button:hover{background:#9333ea}
button.ghost{background:transparent;color:var(--muted);border:1px solid var(--line);margin-top:0}
button.ghost:hover{color:var(--ink);border-color:var(--accent)}
.msg{border-radius:8px;padding:.6rem .85rem;margin-bottom:1rem;font-weight:600;font-size:.9rem}
.msg.ok{background:rgba(74,222,128,.12);color:var(--ok)}.msg.err{background:rgba(248,113,113,.12);color:var(--bad)}
.bar{display:flex;justify-content:space-between;align-items:center;max-width:600px;margin-bottom:1rem;gap:1rem}
.bar form{display:inline}
"""

HEAD = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>{meta}
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='44' fill='%23100d1c'/><circle cx='50' cy='50' r='16' fill='%23f4efff'/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,800&display=swap" rel="stylesheet">
<style>{css}</style></head>"""

E = html.escape

PIXEL_CELLS = "".join('<b class="on"></b>' if i in (1, 3, 4, 6, 9, 11, 12, 14) else "<b></b>" for i in range(16))


def pill(status):
    return f'<span class="pill s-{status}">{E(STATUS_LABEL[status])}</span>'


def project(url, host, color, badge, name, status, desc, chips):
    li = "".join(f"<li>{E(c)}</li>" for c in chips)
    return (
        f'<a class="site" href="{url}" style="--c:var(--{color})">{badge}'
        f'<div><div class="title"><span class="name">{name}</span>{pill(status)}</div>'
        f'<p class="desc">{desc}</p><ul class="chips">{li}</ul></div>'
        f'<span class="host">{host}</span></a>'
    )


def home_page(s):
    paras = "".join(f"<p>{E(p.strip())}</p>" for p in s["about"].split("\n\n") if p.strip())
    has_contact = bool(s["email"] or s["discord"])
    links = ""
    if s["email"]:
        links += f'<a href="mailto:{E(s["email"])}"><span>Email</span>{E(s["email"])}</a>'
    if s["discord"]:
        links += f'<a href="{E(s["discord"])}" rel="noopener" target="_blank"><span>Support Discord</span>{E(s["discord"].replace("https://", ""))}</a>'
    donut = project(
        "https://donut.voidlabs.fun", "donut.voidlabs.fun", "frosting",
        '<div class="badge donut" aria-hidden="true"></div>', "Donut", s["donut_status"],
        "A Discord bot and web dashboard for the Donut network. Sign in with Discord, manage your accounts and keep an eye on everything from one place.",
        ["Player stats", "Payments", "AI chat", "Chat logs and search", "Balance tracking", "Chat alerts", "Admin dashboard"],
    )
    pixel = project(
        "https://pixel.voidlabs.fun", "pixel.voidlabs.fun", "cyan",
        f'<div class="badge pixel" aria-hidden="true">{PIXEL_CELLS}</div>', "Pixel", s["pixel_status"],
        "An all-in-one Discord bot for communities, with a web dashboard to control every system without typing commands.",
        ["Tickets", "Giveaways", "Applications", "Moderation", "Anti-nuke", "Leveling", "Vouches", "Staff tools"],
    )
    nav = '<a href="#projects">Projects</a><a href="#about">About</a>' + ('<a href="#contact">Contact</a>' if has_contact else "")
    contact = ""
    if has_contact:
        contact = (
            '<section class="contact" id="contact"><h2>Get in touch</h2>'
            "<p>Questions, bug reports or ideas? Reach me here.</p>"
            f'<div class="links">{links}</div></section>'
        )
    body = (
        f'<body><header><div class="wrap"><a class="mark" href="/"><i aria-hidden="true"></i>Void Labs</a>'
        f'<nav class="top" aria-label="Page sections">{nav}</nav></div></header>'
        f'<main><div class="wrap"><div class="hero"><h1>Things built in the void.</h1>'
        f'<p class="lede">Void Labs is where I publish the projects I build. Two of them are live right now.</p></div>'
        f'<section id="projects"><h2>Projects</h2><div class="projects">{donut}{pixel}</div></section>'
        f'<section class="about" id="about"><h2>About me</h2><p><strong>Hi, I&rsquo;m {E(s["name"])}.</strong></p>{paras}</section>'
        f"{contact}</div></main>"
        f'<footer><div class="wrap">&copy; {time.gmtime().tm_year} Void Labs</div></footer></body></html>'
    )
    return HEAD.format(title="Void Labs", css=CSS, meta='<meta name="description" content="Void Labs: Donut and Pixel, projects by an independent developer.">') + body


def admin_login_page(msg=""):
    m = f'<div class="msg err">{E(msg)}</div>' if msg else ""
    body = (
        '<body><main><div class="wrap" style="padding-top:3rem"><div class="card"><h1>Admin login</h1>'
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
        '<body><main><div class="wrap" style="padding-top:2rem">'
        '<div class="bar"><a class="mark" href="/"><i aria-hidden="true"></i>View site</a>'
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
