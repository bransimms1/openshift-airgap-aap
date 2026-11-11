#!/usr/bin/env python3
"""Save the readiness dashboard from a finished AAP job as a local HTML file.

The 20-readiness-report job publishes the rendered dashboard as a job artifact
(readiness_report_html: gzip, then base64). Controller keeps job artifacts in
its database after the execution pod is gone, so they can be read back from

    GET {host}/api/controller/v2/jobs/{id}/   ->   "artifacts"

This works for failed NOT_READY jobs too: the report is published before the
job fails on purpose.

Python 3.8+ standard library only.

    export AAP_HOST=https://aap.apps.example.com
    export AAP_TOKEN=...                  # or AAP_USERNAME, and a password prompt

    tools/fetch_readiness_report.py 1234 --open          # report job ID
    tools/fetch_readiness_report.py --workflow 1230      # workflow job ID
    tools/fetch_readiness_report.py --latest --open      # most recent report job

Exit status: 0 saved, 1 could not retrieve, 2 usage error.
"""

import argparse
import base64
import binascii
import getpass
import gzip
import io
import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ARTIFACT = "readiness_report_html"
REPORT_TEMPLATE = "20-readiness-report"
REPORT_NODE = "readiness-report"          # identifier in controller/workflow_templates.yml
MAX_HTML_BYTES = 20 * 1024 * 1024        # a dashboard is tens of KB; refuse anything absurd
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024     # encoded; the role caps it at 256 KB
MAX_RESPONSE_BYTES = 16 * 1024 * 1024    # one job's JSON, artifacts and extra_vars included
FINISHED = {"successful", "failed", "error", "canceled"}


class Failure(Exception):
    """A retrieval problem with a message that says what to do about it."""


# --- AAP API ------------------------------------------------------------------

class Controller:
    def __init__(self, host, api_prefix, auth_header, context, timeout=30):
        self.base = host.rstrip("/") + "/" + api_prefix.strip("/") + "/"
        self._auth = auth_header      # never printed
        self._context = context
        self.timeout = timeout

    def get(self, path, query=None):
        url = urllib.parse.urljoin(self.base, path.lstrip("/"))
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, headers={"Authorization": self._auth, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, context=self._context, timeout=self.timeout) as resp:
                body = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise Failure(f"{url} returned more than {MAX_RESPONSE_BYTES} bytes; refusing to parse it.")
            return json.loads(body)
        except urllib.error.HTTPError as e:
            raise HttpError(e.code, url, _detail(e)) from None
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise Failure(
                    f"TLS verification failed for {self.base}: {reason.verify_message}.\n"
                    "Pass the CA that signed the AAP route with --ca-file (or AAP_CA_FILE). "
                    "--insecure skips verification; use it only on a lab cluster.") from None
            raise Failure(f"Could not reach {self.base}: {reason}") from None
        except socket.timeout:          # TimeoutError from 3.10; a separate class on 3.9
            raise Failure(f"{self.base} did not answer within {self.timeout} seconds.") from None
        except OSError as e:
            raise Failure(f"Connection to {self.base} failed: {e}") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise Failure(f"{url} did not return JSON. Is --host the AAP platform URL?") from None


class HttpError(Failure):
    def __init__(self, status, url, detail):
        self.status = status
        hints = {
            401: "Authentication failed. Check AAP_TOKEN, or AAP_USERNAME and the password.",
            403: "Authenticated, but this user cannot read that job. It needs at least read access "
                 "to the job template or workflow.",
            404: "Not found. The ID may be wrong, the job may have been removed by AAP's "
                 "'Cleanup Job Details' schedule, or --api-prefix does not match this AAP.",
        }
        msg = f"HTTP {status} from {url}"
        if detail:
            msg += f": {detail}"
        if status in hints:
            msg += "\n" + hints[status]
        super().__init__(msg)


def _detail(err):
    try:
        body = json.loads(err.read().decode("utf-8", "replace"))
        if isinstance(body, dict) and isinstance(body.get("detail"), str):
            return body["detail"][:300]
    except Exception:  # noqa: BLE001 - error bodies are best-effort
        pass
    return ""


def _auth_header(args):
    token = os.environ.get("AAP_TOKEN")
    if token:
        return "Bearer " + token
    user = args.username or os.environ.get("AAP_USERNAME")
    if not user:
        raise Failure("No credentials. Set AAP_TOKEN, or AAP_USERNAME (or --username) for a password prompt.")
    password = os.environ.get("AAP_PASSWORD")
    if password is None:
        if not sys.stdin.isatty():
            raise Failure("AAP_PASSWORD is not set and there is no terminal to prompt on.")
        password = getpass.getpass(f"AAP password for {user}: ")
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _tls_context(args):
    if args.insecure:
        print("warning: TLS verification disabled (--insecure)", file=sys.stderr)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context(cafile=args.ca_file or os.environ.get("AAP_CA_FILE") or None)


def find_job(api, args):
    """Return the job detail of the readiness-report job the user asked for."""
    if args.job_id is not None:
        try:
            return api.get(f"jobs/{args.job_id}/")
        except HttpError as e:
            if e.status == 404:
                raise Failure(f"{e}\nIf {args.job_id} is a workflow job ID, use --workflow {args.job_id}.") from None
            raise

    if args.workflow is not None:
        nodes = api.get(f"workflow_jobs/{args.workflow}/workflow_nodes/", {"page_size": 200}).get("results", [])
        for node in nodes:
            if node.get("identifier") == REPORT_NODE:
                job = (node.get("summary_fields") or {}).get("job")
                if not job:
                    raise Failure(f"Workflow job {args.workflow} has not run its readiness report yet "
                                  "(an earlier node may have failed before the validation branches).")
                return api.get(f"jobs/{job['id']}/")
        seen = ", ".join(sorted(str(n.get("identifier")) for n in nodes)) or "none"
        raise Failure(f"Workflow job {args.workflow} has no node with identifier '{REPORT_NODE}' "
                      f"(found: {seen}). Pass the report job ID directly instead.")

    found = api.get("jobs/", {"name": args.template, "order_by": "-id", "page_size": 1}).get("results", [])
    if not found:
        raise Failure(f"No jobs named '{args.template}' are visible to this user.")
    return api.get(f"jobs/{found[0]['id']}/")


# --- artifact -> HTML -------------------------------------------------------------

def extract_html(job, max_artifact_bytes=MAX_ARTIFACT_BYTES, max_html_bytes=MAX_HTML_BYTES):
    """Return (html_bytes, filename) from a job detail or a bare artifacts dict."""
    artifacts = job.get("artifacts", job) if isinstance(job, dict) else None
    ident = f"job {job.get('id')}" if isinstance(job, dict) and "id" in job else "these artifacts"
    status = job.get("status") if isinstance(job, dict) else None

    if isinstance(artifacts, str):
        raise Failure(f"AAP is hiding the artifacts of {ident} ({artifacts}). "
                      "The set_stats task that published them must not use no_log.")
    if not isinstance(artifacts, dict):
        raise Failure(f"Unexpected artifacts value for {ident}.")
    if status and status not in FINISHED:
        raise Failure(f"{ident} is {status}. Retry once it has finished.")

    art = artifacts.get(ARTIFACT)
    if art is None:
        if "readiness_report" in artifacts:
            raise Failure(f"{ident} published the structured readiness_report but no dashboard. "
                          "It ran an older version of the role: sync the project and relaunch the workflow.\n"
                          "--json saves the structured report from this job meanwhile.")
        if job.get("name") and job.get("name") != REPORT_TEMPLATE:
            raise Failure(f"{ident} is '{job.get('name')}', not '{REPORT_TEMPLATE}'. "
                          "Use the report job's ID, or --workflow with the workflow job ID.")
        raise Failure(f"{ident} has no {ARTIFACT} artifact. It may have failed before the report "
                      "was rendered; check its output in AAP.")
    if not isinstance(art, dict):
        raise Failure(f"{ARTIFACT} in {ident} is not the expected mapping.")
    if art.get("omitted"):
        raise Failure(f"{ident} left the dashboard out of its artifacts: {art.get('reason', 'no reason given')}\n"
                      "--json saves the structured report instead.")
    if art.get("encoding") != "gzip+base64" or not isinstance(art.get("content"), str):
        raise Failure(f"{ARTIFACT} in {ident} has unsupported encoding {art.get('encoding')!r}.")
    if len(art["content"]) > max_artifact_bytes:
        raise Failure(f"{ARTIFACT} in {ident} is {len(art['content'])} bytes encoded, over the "
                      f"{max_artifact_bytes}-byte limit; refusing to decode it.")

    try:
        compressed = base64.b64decode(art["content"], validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as gz:
            html = gz.read(max_html_bytes + 1)
    except (binascii.Error, OSError, EOFError) as e:
        raise Failure(f"{ARTIFACT} in {ident} could not be decoded ({e}). It may have been truncated.") from None
    if len(html) > max_html_bytes:
        raise Failure(f"{ARTIFACT} in {ident} expands past {max_html_bytes} bytes; refusing to use it.")
    return html, art.get("filename") or "readiness-report.html"


def _safe_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(name)).strip(".-")
    return name or "readiness-report.html"


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


# --- CLI ----------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        description="Save the readiness dashboard from an AAP job as a local HTML file.",
        epilog="Credentials come from AAP_TOKEN, or AAP_USERNAME with AAP_PASSWORD or a prompt. "
               "They are sent only to --host and never printed.")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("job_id", nargs="?", type=int, help="ID of a 20-readiness-report job")
    which.add_argument("--workflow", type=int, metavar="ID", help="ID of an openshift-site-readiness workflow job")
    which.add_argument("--latest", action="store_true", help="the most recent readiness-report job")
    which.add_argument("--from-file", type=Path, metavar="JSON",
                       help="a saved job detail or artifacts JSON file, instead of contacting AAP")
    p.add_argument("--host", default=os.environ.get("AAP_HOST"), help="AAP platform URL (default: $AAP_HOST)")
    p.add_argument("--username", help="AAP user, if not using AAP_TOKEN (default: $AAP_USERNAME)")
    p.add_argument("--ca-file", help="CA bundle for the AAP route (default: $AAP_CA_FILE, else system trust)")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification (lab clusters only)")
    p.add_argument("--api-prefix", default="/api/controller/v2",
                   help="Controller API path (default: %(default)s; /api/v2 for AAP 2.4 and earlier)")
    p.add_argument("--template", default=REPORT_TEMPLATE, help="job template name for --latest (default: %(default)s)")
    p.add_argument("-o", "--output", type=Path, help="file or directory to write (default: current directory)")
    p.add_argument("--json", action="store_true", help="also save the structured readiness_report as JSON")
    p.add_argument("--open", action="store_true", help="open the saved report in the default browser")
    args = p.parse_args(argv)

    try:
        if args.from_file:
            try:
                job = json.loads(args.from_file.read_text())
            except (OSError, json.JSONDecodeError) as e:
                raise Failure(f"Could not read {args.from_file}: {e}") from None
        else:
            if not args.host:
                p.error("--host or AAP_HOST is required")
            if not args.host.startswith(("https://", "http://")):
                args.host = "https://" + args.host
            api = Controller(args.host, args.api_prefix, _auth_header(args), _tls_context(args))
            job = find_job(api, args)

        artifacts = job.get("artifacts", job) if isinstance(job, dict) else {}
        report = artifacts.get("readiness_report") if isinstance(artifacts, dict) else None
        if args.json and not isinstance(report, dict):
            raise Failure("No structured readiness_report artifact to save.")

        try:
            html, filename = extract_html(job)
        except Failure:
            if not (args.json and report):
                raise
            html, filename = None, None
            print("warning: no dashboard in this job; saving the structured report only", file=sys.stderr)

        stem = _safe_name(filename or "readiness-report.html")
        if "id" in job and isinstance(job.get("id"), int) and not args.from_file:
            stem = f"job{job['id']}-{stem}"
        out = args.output or Path.cwd()
        target = out / stem if out.is_dir() or str(out).endswith(os.sep) else out
        if html is not None:
            _write(target, html)
            print(target)
        if args.json:
            jtarget = target.with_suffix(".json")
            _write(jtarget, (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
            print(jtarget)

        if isinstance(report, dict):
            print(f"verdict {report.get('overall', 'unknown')} | cluster {report.get('cluster', 'unknown')} "
                  f"| job status {job.get('status', 'n/a')}", file=sys.stderr)
        if args.open and html is not None:
            webbrowser.open(target.resolve().as_uri())
        return 0
    except Failure as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main())
