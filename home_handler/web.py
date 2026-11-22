"""The agent's page and its command endpoint, behind Cloudflare Access when reached from outside."""

import hmac
import json
import os
import re
import sys
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import commands, loose_ends

PAGE = os.path.join(os.path.dirname(__file__), "web", "page.html")
ROUTE = re.compile(r"^/api/incident/(\d+)/(done|reopen)$")


TEAM = os.environ.get("HOME_HANDLER_ACCESS_TEAM", "")   # e.g. https://yourteam.cloudflareaccess.com
ALLOWED = {e.strip().lower() for e in os.environ.get("HOME_HANDLER_ALLOWED_EMAILS", "").split(",") if e.strip()}
_jwks = None


def _claims(token, aud):
    """Verified claims of a Cloudflare Access JWT, or None."""
    global _jwks
    if not token or not aud or not TEAM:
        return None
    try:
        import jwt
        if _jwks is None:
            _jwks = jwt.PyJWKClient(TEAM + "/cdn-cgi/access/certs", cache_keys=True, lifespan=3600)
        key = _jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256"], audience=aud, issuer=TEAM)
    except Exception:
        return None


def access_email(token, aud):
    """The signed-in email Access vouched for, if it's on HOME_HANDLER_ALLOWED_EMAILS."""
    c = _claims(token, aud) or {}
    email = (c.get("email") or "").lower()
    return email if email and email in ALLOWED else None


def allowed(ip, token, allow, aud, access_fn):
    if ip in ("127.0.0.1", "::1"):
        return True
    if ip not in allow:
        return False
    return access_fn(token, aud) is not None


def cmd_allowed(ip, auth, token, tunnel):
    """The phone widget's terminal line: a bearer token, from the home LAN only, never via the tunnel."""
    if ip in ("127.0.0.1", "::1"):
        return True
    if not token or not auth or not auth.startswith("Bearer "):
        return False
    if ip in tunnel or not ip.startswith(("10.", "192.168.", "172.")):
        return False
    return hmac.compare_digest(auth[7:].strip(), token)


def service_token_ok(jwt_token, aud, client_id):
    """True when the Access JWT is valid for this app and was issued to the given service token."""
    if not client_id:
        return False
    return (_claims(jwt_token, aud) or {}).get("common_name") == client_id


def state(journal):
    inc = lambda i: {k: v for k, v in asdict(i).items()}
    active = journal.incidents(status=("open", "waiting"))
    waiting = [i for i in active if i.status == "waiting" or i.sort_label == "needs_bae"]
    spotted = sorted([i for i in active if i.source == "watcher"], key=lambda i: (-i.severity, -i.last_seen))
    return {
        "waiting": [inc(i) for i in waiting],
        "open": [inc(i) for i in spotted],
        "today": journal.latest_summary(),
        "watchers": journal.watcher_stats(),
        "replay": journal.replay_findings(),
        "history": [inc(i) for i in journal.incidents(limit=200)],
        "counts": {"waiting": len(waiting), "open": len(spotted),
                   "loose_open": sum(1 for i in active if i.source == "loose_ends")},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "HouseAgent"

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    def _send(self, code, body=b"", ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str).encode()
        self.send_response(code)
        for k, v in (("Content-Type", ctype), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                     ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                     ("X-Frame-Options", "DENY")):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _ok(self):
        s = self.server
        return allowed(self.client_address[0], self.headers.get("Cf-Access-Jwt-Assertion"), s.allow, s.aud,
                       s.access_fn)

    def do_GET(self):
        if not self._ok():
            return self._send(403, {"error": "forbidden"})
        if self.path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(200, state(self.server.journal))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/command":
            return self._command()
        if not self._ok():
            return self._send(403, {"error": "forbidden"})
        m = ROUTE.match(self.path)
        if not m:
            return self._send(404, {"error": "not found"})
        if self.headers.get("X-Requested-With") != "home_handler":
            return self._send(400, {"error": "missing header"})  # blocks cross-site form posts
        iid, action = int(m.group(1)), m.group(2)
        j = self.server.journal
        try:
            j.get(iid)
        except TypeError:
            return self._send(404, {"error": "no such incident"})
        if action == "done":
            loose_ends.mark_done(self.server.cfg, j, iid, by="you", store=self.server.store)
        else:
            loose_ends.reopen(self.server.cfg, j, iid, store=self.server.store)
        return self._send(200, asdict(j.get(iid)))


    def _command(self):
        s = self.server
        ip, auth = self.client_address[0], self.headers.get("Authorization")
        bearer = bool(s.cmd_token) and auth == "Bearer " + s.cmd_token
        # Away from home the widget comes through the NAS tunnel: Access must have passed its service token,
        # and it must still carry the command token.
        via_tunnel = ip in s.allow and bearer and service_token_ok(
            self.headers.get("Cf-Access-Jwt-Assertion"), s.aud, s.cf_client_id)
        if not (self._ok() or via_tunnel or cmd_allowed(ip, auth, s.cmd_token, s.allow)):
            return self._send(403, {"error": "forbidden"})
        if self.headers.get("X-Requested-With") != "home_handler":
            return self._send(400, {"error": "missing header"})
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            text = json.loads(self.rfile.read(n) or b"{}").get("text", "")
        except (ValueError, AttributeError):
            return self._send(400, {"error": "bad json"})
        return self._send(200, commands.run(str(text), s.journal, s.cfg, store=s.store, state_fn=state))


def make_server(journal, cfg, port, allow, aud, bind="0.0.0.0", store=None, access_fn=None):
    s = ThreadingHTTPServer((bind, port), Handler)
    s.journal, s.cfg, s.allow, s.aud = journal, cfg, set(allow), aud
    s.store = store
    s.access_fn = access_fn or access_email
    s.cmd_token = os.environ.get("HOME_HANDLER_CMD_TOKEN", "")
    s.cf_client_id = os.environ.get("HOME_HANDLER_CF_CLIENT_ID", "")
    return s


def serve(journal, cfg, port, allow, aud):
    if not aud:
        print("warning: no --aud, so only localhost can use the page", file=sys.stderr)
    s = make_server(journal, cfg, port, allow, aud)
    print(f"house agent page on :{port}, tunnel from {sorted(s.allow)}", file=sys.stderr)
    s.serve_forever()
