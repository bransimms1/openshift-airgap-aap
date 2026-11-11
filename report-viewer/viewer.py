#!/usr/bin/env python3
"""Serve readiness dashboards from AAP job artifacts.

    GET /report/<job id>   the readiness_report_html artifact of that job
    GET /healthz           liveness; does not contact AAP

Runs behind OpenShift's oauth-proxy in the same pod and listens on 127.0.0.1
only, so every request has been authenticated and authorised by the proxy
before it arrives. The proxy sets X-Forwarded-User, replacing anything the
client sent, and the viewer refuses report requests without it.

Fetching and decoding are tools/fetch_readiness_report.py, imported as is.
The viewer adds the HTTP front end, a check that the job really is a
readiness-report job, and response headers that sandbox the page.

Configuration, all from the environment:

    AAP_URL               AAP platform URL (required)
    AAP_TOKEN_FILE        file holding a read-only AAP token (/etc/aap-token/token)
    AAP_CA_FILE           CA bundle for AAP's route, used if the file exists
                          (/etc/aap-ca/ca.crt); otherwise the system trust store
    AAP_VERIFY_TLS        "false" disables certificate verification (default true)
    AAP_API_PREFIX        /api/controller/v2
    AAP_TIMEOUT           seconds per AAP request (10)
    REPORT_TEMPLATE       job name a report job must have (20-readiness-report)
    REPORT_ORGANIZATION   if set, the job must belong to this organization
    REQUIRE_PROXY_USER    "false" serves requests without X-Forwarded-User (default true)
    LISTEN_ADDRESS, PORT  127.0.0.1, 8080
"""

import html
import logging
import os
import re
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))   # repository layout
import fetch_readiness_report as fetch   # noqa: E402  (beside this file in the image)

log = logging.getLogger("report-viewer")

REPORT_PATH = re.compile(r"^/report/([1-9][0-9]{0,9})/?$")

# The page is self-contained: inline CSS, no script, no images, no fonts. It is
# served sandboxed regardless, so even a forged artifact cannot run script,
# load anything, submit a form or be framed. Authentication says who may look
# at a page; it does not make the page's content trustworthy.
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class Config:
    def __init__(self, env=os.environ):
        self.aap_url = env.get("AAP_URL", "").strip()
        if not self.aap_url.startswith("https://") and not self.aap_url.startswith("http://"):
            raise SystemExit("AAP_URL must be set to the AAP platform URL, for example https://aap.apps.example.com")
        self.token_file = Path(env.get("AAP_TOKEN_FILE", "/etc/aap-token/token"))
        self.ca_file = Path(env.get("AAP_CA_FILE", "/etc/aap-ca/ca.crt"))
        self.verify_tls = env.get("AAP_VERIFY_TLS", "true").lower() != "false"
        self.api_prefix = env.get("AAP_API_PREFIX", "/api/controller/v2")
        self.timeout = float(env.get("AAP_TIMEOUT", "10"))
        self.template = env.get("REPORT_TEMPLATE", fetch.REPORT_TEMPLATE)
        self.organization = env.get("REPORT_ORGANIZATION", "").strip()
        self.require_proxy_user = env.get("REQUIRE_PROXY_USER", "true").lower() != "false"
        self.address = env.get("LISTEN_ADDRESS", "127.0.0.1")
        self.port = int(env.get("PORT", "8080"))

    def tls_context(self):
        if not self.verify_tls:
            log.warning("AAP_VERIFY_TLS=false: AAP's certificate is not verified")
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if self.ca_file.is_file():
            log.info("trusting the CA bundle in %s for AAP", self.ca_file)
            return ssl.create_default_context(cafile=str(self.ca_file))
        log.info("trusting the system CA store for AAP")
        return ssl.create_default_context()

    def controller(self, context):
        # Read on every request, so a rotated Secret takes effect without a restart.
        try:
            token = self.token_file.read_text().strip()
        except OSError:
            token = ""
        if not token:
            raise NoToken()
        return fetch.Controller(self.aap_url, self.api_prefix, "Bearer " + token, context, self.timeout)


class NoToken(Exception):
    pass


class Page(Exception):
    """An error page: status, title, plain-text explanation (escaped when rendered).
    Never carries AAP's URL or the token."""

    def __init__(self, status, title, text):
        super().__init__(title)
        self.status, self.title, self.text = status, title, text


def load_report(cfg, context, job_id):
    """Return the dashboard HTML for a readiness-report job, or raise Page."""
    try:
        job = cfg.controller(context).get(f"jobs/{job_id}/")
    except NoToken:
        log.error("no AAP token in %s", cfg.token_file)
        raise Page(503, "Viewer not configured", "The viewer has no AAP token. Ask the administrator "
                   "to create the report-viewer-aap-token Secret.")
    except fetch.HttpError as e:
        log.warning("job %s: %s", job_id, str(e).splitlines()[0])
        if e.status == 404:
            raise Page(404, "Report not found", f"There is no job {job_id} visible to the viewer. The ID may "
                       "be wrong, or AAP may have removed the job when its job history was cleaned up.")
        if e.status in (401, 403):
            raise Page(502, "Viewer cannot sign in to AAP", "AAP rejected the viewer's token. Ask the "
                       "administrator to check or rotate the report-viewer-aap-token Secret.")
        raise Page(502, "AAP returned an error", f"AAP answered HTTP {e.status}. Try again shortly.")
    except fetch.Failure as e:
        log.warning("job %s: %s", job_id, str(e).splitlines()[0])
        raise Page(504, "AAP is unavailable", "The viewer could not get an answer from AAP. Try again "
                   "shortly; if it persists, ask the administrator to check the viewer's log.")

    if not isinstance(job, dict) or job.get("name") != cfg.template:
        raise Page(404, "Not a readiness report", f"Job {job_id} is not a {cfg.template} job. The "
                   "link in that job's output points at the right one.")
    if cfg.organization:
        org = ((job.get("summary_fields") or {}).get("organization") or {}).get("name")
        if org != cfg.organization:
            log.warning("job %s: organization %r, expected %r", job_id, org, cfg.organization)
            raise Page(404, "Not a readiness report", f"Job {job_id} is not a {cfg.template} job "
                       "this viewer serves.")
    if job.get("status") not in fetch.FINISHED:
        raise Page(409, "Report not ready yet", f"Job {job_id} is {job.get('status', 'not finished')}. "
                   "Reload this page when it has finished.")
    try:
        page, _ = fetch.extract_html(job, max_artifact_bytes=1024 * 1024, max_html_bytes=8 * 1024 * 1024)
    except fetch.Failure as e:
        reason = str(e).splitlines()[0]
        log.warning("job %s: %s", job_id, reason)
        raise Page(404, "No dashboard in this job", reason)
    return page


ERROR_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>body{{margin:0;background:#0b111c;color:#e6ecf4;font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
main{{max-width:40rem;margin:12vh auto;padding:0 24px}}div{{border-top:3px solid #ee0000;background:#131d2d;border-radius:12px;padding:28px}}
h1{{font-size:1.4rem;margin:0 0 10px}}p{{color:#9aa8bb;margin:0 0 10px}}code{{color:#e6ecf4}}</style>
</head><body><main><div><h1>{title}</h1><p>{text}</p><p><code>HTTP {status}</code></p></div></main></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "readiness-report-viewer"
    sys_version = ""
    cfg = None
    tls = None

    def do_GET(self):
        started = time.monotonic()
        path = self.path.split("?", 1)[0]
        user = self.headers.get("X-Forwarded-User", "")
        status = 500
        try:
            if path == "/healthz":
                status = self.send(200, b"ok\n", "text/plain; charset=utf-8")
                return
            if self.cfg.require_proxy_user and not user:
                raise Page(403, "Sign in required", "This request did not come through the viewer's "
                           "authenticating proxy.")
            match = REPORT_PATH.match(path)
            if path in ("/", ""):
                raise Page(404, "Readiness report viewer", "Open a report from the link in a "
                           f"{self.cfg.template} job's output. Report addresses look like /report/1234.")
            if not match:
                raise Page(404, "Not found", "Report addresses look like /report/1234.")
            page = load_report(self.cfg, self.tls, int(match.group(1)))
            status = self.send(200, page, "text/html; charset=utf-8")
        except Page as p:
            body = ERROR_PAGE.format(title=html.escape(p.title), text=html.escape(p.text), status=p.status)
            status = self.send(p.status, body.encode(), "text/html; charset=utf-8")
        except Exception:  # noqa: BLE001 - one bad request must not take the server down
            log.exception("unhandled error for %s", path)
            body = ERROR_PAGE.format(title="Viewer error", text="Unexpected error; see the viewer's log.", status=500)
            status = self.send(500, body.encode(), "text/html; charset=utf-8")
        finally:
            if path != "/healthz":
                log.info("%s %s %s user=%s %.0fms", self.command, path[:80], status, user or "-",
                         (time.monotonic() - started) * 1000)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        self.send(405, b"Method not allowed\n", "text/plain; charset=utf-8", allow="GET, HEAD")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST

    def send(self, status, body, content_type, **extra):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        for name, value in extra.items():
            self.send_header(name.title(), value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return status

    def log_message(self, *args):
        pass   # do_GET logs one line per request, without query strings


def make_server(cfg):
    handler = type("ConfiguredHandler", (Handler,), {"cfg": cfg, "tls": cfg.tls_context()})
    server = ThreadingHTTPServer((cfg.address, cfg.port), handler)
    server.daemon_threads = True
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    cfg = Config()
    server = make_server(cfg)
    log.info("serving on %s:%s for %s (job template %s)", cfg.address, cfg.port, cfg.aap_url, cfg.template)
    server.serve_forever()


if __name__ == "__main__":
    main()
