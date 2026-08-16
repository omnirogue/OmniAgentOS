import http.server
import json
import os
import re
import socketserver
import time
import urllib.parse
import urllib.request

PORT = int(os.environ.get("OAUTH_PORT", "8765"))
REDIRECT = f"http://localhost:{PORT}/"
CLIENT_ID = os.environ["OAUTH_CLIENT_ID"]
CLIENT_SECRET = os.environ["OAUTH_CLIENT_SECRET"]
SCOPES = os.environ.get(
    "OAUTH_SCOPES",
    "openid email https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/documents https://www.googleapis.com/auth/gmail.readonly",
)
# Optional: OAUTH_KEY_SUFFIX=ADS stores GOOGLE_OAUTH_REFRESH_TOKEN_ADS (extra accounts, same client).
KEY_SUFFIX = os.environ.get("OAUTH_KEY_SUFFIX", "").strip().upper()
RT_KEY = "GOOGLE_OAUTH_REFRESH_TOKEN" + (f"_{KEY_SUFFIX}" if KEY_SUFFIX else "")
VAULT = os.path.expanduser("~/.config/omni/connections.env")

auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
    {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
)
print("AUTH_URL=" + auth_url, flush=True)

result = {}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" not in q:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"no code")
            return
        data = urllib.parse.urlencode(
            {
                "code": q["code"][0],
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": REDIRECT,
                "grant_type": "authorization_code",
            }
        ).encode()
        try:
            with urllib.request.urlopen(
                urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
            ) as r:
                tok = json.load(r)
        except Exception as e:
            body = e.read().decode() if hasattr(e, "read") else str(e)
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"token exchange failed: {body}".encode())
            print("EXCHANGE_ERROR=" + body, flush=True)
            result["err"] = body
            return
        rt = tok.get("refresh_token")
        if not rt:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"no refresh_token returned")
            print("NO_REFRESH_TOKEN=" + json.dumps({k: ("<" + k + ">") for k in tok}), flush=True)
            result["err"] = "no_refresh"
            return
        # write to vault (names only in logs; value only to the file)
        lines = []
        if os.path.exists(VAULT):
            lines = open(VAULT).read().splitlines()

        def setkv(lines, k, v):
            out, done = [], False
            for ln in lines:
                if re.match(rf"^{re.escape(k)}=", ln):
                    out.append(f"{k}={v}")
                    done = True
                else:
                    out.append(ln)
            if not done:
                out.append(f"{k}={v}")
            return out

        lines = setkv(lines, "GOOGLE_OAUTH_CLIENT_ID", CLIENT_ID)
        lines = setkv(lines, "GOOGLE_OAUTH_CLIENT_SECRET", CLIENT_SECRET)
        lines = setkv(lines, RT_KEY, rt)
        open(VAULT, "w").write("\n".join(lines) + "\n")
        os.chmod(VAULT, 0o600)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>Authorized. You can close this tab and return to Claude.</h2>")
        print(f"SUCCESS=stored {RT_KEY} + CLIENT_ID + CLIENT_SECRET in vault", flush=True)
        result["ok"] = True


with socketserver.TCPServer(("127.0.0.1", PORT), H) as httpd:
    httpd.timeout = 3600
    end = time.time() + 3600
    while "ok" not in result and "err" not in result and time.time() < end:
        httpd.handle_request()
    if "ok" not in result and "err" not in result:
        print("TIMEOUT=no response within 3600s", flush=True)
