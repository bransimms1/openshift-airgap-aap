"""A stand-in for the parts of the Controller API the report tools read.

Shapes follow the AWX source: job detail at /api/controller/v2/jobs/<id>/ with
"artifacts", workflow nodes at workflow_jobs/<id>/workflow_nodes/, and job
lists filtered with ?name=&order_by=.
"""

import base64
import gzip
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/api/controller/v2/"


def html_artifact(page, **extra):
    """A readiness_report_html artifact as the role publishes it."""
    data = base64.b64encode(gzip.compress(page.encode() if isinstance(page, str) else page)).decode()
    return {"filename": "readiness-report-test.html", "encoding": "gzip+base64", "content": data, **extra}


class FakeController:
    """jobs: {id: job detail}. nodes: {workflow job id: [node]}. slow: {id: seconds}.
    errors: {id: HTTP status}. Accepts Bearer <token> or Basic <basic>."""

    def __init__(self, token, basic=None):
        self.jobs, self.nodes, self.slow, self.errors = {}, {}, {}, {}
        self.token, self.basic = token, basic
        self.last_query = ""
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                allowed = {f"Bearer {fake.token}"}
                if fake.basic:
                    allowed.add("Basic " + base64.b64encode(fake.basic.encode()).decode())
                if self.headers.get("Authorization", "") not in allowed:
                    return self.reply(401, {"detail": "Authentication credentials were not provided."})
                path, _, query = self.path.partition("?")
                rest = path[len(PREFIX):] if path.startswith(PREFIX) else None
                if rest is None:
                    return self.reply(404, {"detail": "Not found."})
                if m := re.fullmatch(r"jobs/(\d+)/", rest):
                    jid = int(m.group(1))
                    time.sleep(fake.slow.get(jid, 0))
                    if jid in fake.errors:
                        return self.reply(fake.errors[jid], {"detail": "Server error."})
                    job = fake.jobs.get(jid)
                    return self.reply(200, job) if job else self.reply(404, {"detail": "Not found."})
                if rest == "jobs/":
                    fake.last_query = query
                    return self.reply(200, {"results": [{"id": max(fake.jobs)}]})
                if m := re.fullmatch(r"workflow_jobs/(\d+)/workflow_nodes/", rest):
                    nodes = fake.nodes.get(int(m.group(1)))
                    return self.reply(200, {"results": nodes}) if nodes is not None else self.reply(404, {"detail": "Not found."})
                return self.reply(404, {"detail": "Not found."})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
