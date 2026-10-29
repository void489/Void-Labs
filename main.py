import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# bot-hosting.net sets SERVER_PORT; fall back to 8080 for local testing
PORT = int(os.getenv("SERVER_PORT") or 8080)
os.chdir(os.path.dirname(os.path.abspath(__file__)))

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)

if __name__ == "__main__":
    print(f"Void Labs site running on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
