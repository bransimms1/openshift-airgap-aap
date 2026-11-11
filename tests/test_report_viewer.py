"""Readiness report viewer: report-viewer/viewer.py and its OpenShift manifests.

The viewer runs in-process against a fake Controller API. No cluster needed.

    python3 -m unittest -v tests/test_report_viewer.py
"""

import gzip
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "report-viewer"))
from fake_controller import FakeController, html_artifact  # noqa: E402
import viewer  # noqa: E402

# Quiet by default; assertLogs still captures it where a test asks.
viewer.log.addHandler(logging.NullHandler())
viewer.log.propagate = False

TOKEN = "viewer-token-3d9a71c4"
MARKER = "REPORT-BODY-MARKER-5b2e"
PAGE = f"<!DOCTYPE html><html><body><h1>READY</h1><p>{MARKER}</p><script>alert(1)</script></body></html>"
FAIL_PAGE = f"<!DOCTYPE html><html><body><h1>NOT_READY</h1><p>{MARKER}</p></body></html>"
MANIFESTS = REPO / "report-viewer" / "openshift"


def job(jid, artifacts, name="20-readiness-report", status="successful", org="Platform Engineering"):
    return {"id": jid, "name": name, "status": status, "artifacts": artifacts,
            "summary_fields": {"organization": {"id": 1, "name": org}}}


class Viewer:
    """A viewer on an ephemeral port, configured like the deployment."""

    def __init__(self, aap_url, token=TOKEN, **env):
        self.dir = Path(tempfile.mkdtemp(prefix="viewer-"))
        (self.dir / "token").write_text(token + "\n")
        cfg = viewer.Config({"AAP_URL": aap_url, "AAP_TOKEN_FILE": str(self.dir / "token"),
                             "AAP_CA_FILE": str(self.dir / "absent.crt"), "AAP_TIMEOUT": "1", "PORT": "0", **env})
        self.server = viewer.make_server(cfg)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def get(self, path, user="alice", method="GET"):
        headers = {"X-Forwarded-User": user} if user is not None else {}
        req = urllib.request.Request(self.url + path, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)


class ViewerCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.aap = FakeController(TOKEN)
        report = {"overall": "READY_WITH_LIMITATIONS"}
        cls.aap.jobs = {
            82: job(82, {"readiness_report": report, "readiness_report_html": html_artifact(PAGE)}),
            83: job(83, {"readiness_report": {"overall": "NOT_READY"}, "readiness_report_html": html_artifact(FAIL_PAGE)},
                    status="failed"),
            84: job(84, {}, name="30-bmc-prepare-hosts"),
            85: job(85, {"readiness_report": report}),
            86: job(86, {"readiness_report_html": {"encoding": "gzip+base64", "content": "not base64!"}}),
            87: job(87, {"readiness_report_html": {"encoding": "gzip+base64", "content": "A" * (1024 * 1024 + 4)}}),
            88: job(88, {"readiness_report_html": html_artifact(b"\0" * (9 * 1024 * 1024))}),
            89: job(89, {}, status="running"),
            90: job(90, {"readiness_report_html": html_artifact(PAGE)}, org="Elsewhere"),
            91: job(91, {"readiness_report_html": html_artifact(PAGE)}),
            93: job(93, {"readiness_report_html": {"omitted": True, "reason": "<img src=x onerror=alert(1)> too big"}}),
            94: job(94, "$hidden due to Ansible no_log flag$"),
        }
        cls.aap.slow = {91: 3}
        cls.aap.errors = {92: 500}
        cls.v = Viewer(cls.aap.url)

    @classmethod
    def tearDownClass(cls):
        cls.v.stop()
        cls.aap.stop()

    def assertSafeHeaders(self, headers):
        csp = headers["Content-Security-Policy"]
        for directive in ("default-src 'none'", "frame-ancestors 'none'", "form-action 'none'", "sandbox"):
            self.assertIn(directive, csp)
        self.assertNotIn("script-src", csp)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def assertErrorPage(self, path, status, text):
        code, headers, body = self.v.get(path)
        self.assertEqual(code, status, body[:300])
        self.assertIn(text, body)
        self.assertSafeHeaders(headers)
        self.assertNotIn(self.aap.url, body)
        self.assertNotIn(TOKEN, body)
        return body


class Reports(ViewerCase):

    def test_successful_report(self):
        code, headers, body = self.v.get("/report/82")
        self.assertEqual(code, 200)
        self.assertEqual(body, PAGE)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertSafeHeaders(headers)

    def test_failed_not_ready_job(self):
        code, _, body = self.v.get("/report/83/")
        self.assertEqual(code, 200)
        self.assertEqual(body, FAIL_PAGE)

    def test_query_string_is_ignored(self):
        self.assertEqual(self.v.get("/report/82?x=<script>")[0], 200)

    def test_head(self):
        code, headers, body = self.v.get("/report/82", method="HEAD")
        self.assertEqual((code, body), (200, ""))
        self.assertSafeHeaders(headers)

    def test_not_a_report_job(self):
        self.assertErrorPage("/report/84", 404, "is not a 20-readiness-report job")

    def test_missing_job(self):
        self.assertErrorPage("/report/999", 404, "no job 999 visible to the viewer")

    def test_job_without_dashboard(self):
        self.assertErrorPage("/report/85", 404, "older version of the role")

    def test_malformed_artifact(self):
        self.assertErrorPage("/report/86", 404, "could not be decoded")

    def test_oversized_compressed_artifact(self):
        self.assertErrorPage("/report/87", 404, "over the 1048576-byte limit")

    def test_oversized_decompressed_artifact(self):
        self.assertErrorPage("/report/88", 404, "expands past")

    def test_running_job(self):
        self.assertErrorPage("/report/89", 409, "Reload this page")

    def test_hidden_artifacts(self):
        self.assertErrorPage("/report/94", 404, "no_log")

    def test_omitted_reason_is_escaped(self):
        body = self.assertErrorPage("/report/93", 404, "left the dashboard out")
        self.assertNotIn("<img", body)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", body)

    def test_aap_error(self):
        self.assertErrorPage("/report/92", 502, "AAP answered HTTP 500")

    def test_aap_timeout(self):
        started = time.monotonic()
        self.assertErrorPage("/report/91", 504, "could not get an answer from AAP")
        self.assertLess(time.monotonic() - started, 2.5)


class Boundaries(ViewerCase):

    def test_invalid_job_ids(self):
        for path in ("/report/0", "/report/abc", "/report/-1", "/report/12345678901", "/report/82/extra",
                     "/report/../etc/passwd", "/report/%38%32", "/reports/82", "/report/ 82",
                     "/report/", "/api/controller/v2/jobs/82/", "/"):
            with self.subTest(path):
                code, headers, body = self.v.get(path.replace(" ", "%20"))
                self.assertEqual(code, 404)
                self.assertNotIn(MARKER, body)
                self.assertSafeHeaders(headers)

    def test_request_without_proxy_user_is_refused(self):
        for user in (None, ""):
            with self.subTest(user=user):
                code, headers, body = self.v.get("/report/82", user=user)
                self.assertEqual(code, 403)
                self.assertNotIn(MARKER, body)

    def test_healthz_needs_no_user_and_no_aap(self):
        code, _, body = self.v.get("/healthz", user=None)
        self.assertEqual((code, body), (200, "ok\n"))

    def test_only_get_and_head(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method):
                code, headers, _ = self.v.get("/report/82", method=method)
                self.assertEqual(code, 405)
                self.assertEqual(headers["Allow"], "GET, HEAD")

    def test_organization_restriction(self):
        v = Viewer(self.aap.url, REPORT_ORGANIZATION="Platform Engineering")
        try:
            self.assertEqual(v.get("/report/82")[0], 200)
            self.assertEqual(v.get("/report/90")[0], 404)
        finally:
            v.stop()

    def test_rejected_token(self):
        v = Viewer(self.aap.url, token="wrong-token")
        try:
            code, _, body = v.get("/report/82")
            self.assertEqual(code, 502)
            self.assertIn("AAP rejected the viewer", body)
            self.assertNotIn("wrong-token", body)
        finally:
            v.stop()

    def test_missing_token(self):
        v = Viewer(self.aap.url, token="")
        try:
            code, _, body = v.get("/report/82")
            self.assertEqual(code, 503)
            self.assertIn("has no AAP token", body)
        finally:
            v.stop()

    def test_aap_unreachable(self):
        v = Viewer("http://127.0.0.1:9")
        try:
            code, _, body = v.get("/report/82")
            self.assertEqual(code, 504)
            self.assertNotIn("127.0.0.1:9", body)
        finally:
            v.stop()

    def test_logs_carry_no_token_or_report_content(self):
        with self.assertLogs("report-viewer", level="INFO") as logs:
            for path in ("/report/82", "/report/83", "/report/84", "/report/999", "/report/93"):
                self.v.get(path)
        text = "\n".join(logs.output)
        self.assertIn("GET /report/82 200 user=alice", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(MARKER, text)
        self.assertNotIn("<img", text.split("job 93")[0])


class Configuration(unittest.TestCase):

    def test_aap_url_is_required(self):
        for value in ("", "aap.example.com"):
            with self.subTest(value):
                with self.assertRaises(SystemExit):
                    viewer.Config({"AAP_URL": value})

    def test_defaults_keep_the_viewer_private_and_verifying(self):
        cfg = viewer.Config({"AAP_URL": "https://aap.example.com"})
        self.assertEqual(cfg.address, "127.0.0.1")
        self.assertTrue(cfg.verify_tls)
        self.assertTrue(cfg.require_proxy_user)
        self.assertEqual(cfg.template, "20-readiness-report")

    def test_tls_verification_and_custom_ca(self):
        logging.disable(logging.CRITICAL)
        try:
            cfg = viewer.Config({"AAP_URL": "https://aap.example.com", "AAP_CA_FILE": "/nonexistent"})
            self.assertTrue(cfg.tls_context().check_hostname)
            ca = Path(tempfile.mkdtemp()) / "ca.crt"
            ca.write_text(_SELF_SIGNED)
            ctx = viewer.Config({"AAP_URL": "https://aap.example.com", "AAP_CA_FILE": str(ca)}).tls_context()
            self.assertEqual(len(ctx.get_ca_certs()), 1)
            off = viewer.Config({"AAP_URL": "https://aap.example.com", "AAP_VERIFY_TLS": "false"}).tls_context()
            self.assertFalse(off.check_hostname)
        finally:
            logging.disable(logging.NOTSET)


class Manifests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        files = sorted(MANIFESTS.glob("*.yaml"))
        assert len(files) >= 7, f"expected the viewer manifests, found {len(files)}"
        cls.docs = [d for f in files for d in yaml.safe_load_all(f.read_text()) if d]
        cls.by_kind = {}
        for d in cls.docs:
            cls.by_kind.setdefault(d["kind"], []).append(d)

    def test_no_secrets_committed(self):
        self.assertNotIn("Secret", self.by_kind)

    def test_pod_runs_restricted(self):
        (dep,) = self.by_kind["Deployment"]
        self.assertEqual(dep["spec"]["replicas"], 1)
        pod = dep["spec"]["template"]["spec"]
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        for key in ("hostNetwork", "hostPID", "hostIPC"):
            self.assertNotIn(key, pod)
        self.assertEqual({c["name"] for c in pod["containers"]}, {"oauth-proxy", "viewer"})
        for c in pod["containers"]:
            with self.subTest(c["name"]):
                sc = c["securityContext"]
                self.assertFalse(sc["allowPrivilegeEscalation"])
                self.assertTrue(sc["readOnlyRootFilesystem"])
                self.assertEqual(sc["capabilities"]["drop"], ["ALL"])
                self.assertNotIn("privileged", sc)
                self.assertNotIn("runAsUser", sc)       # restricted-v2 assigns one
                self.assertIn("livenessProbe", c)
                self.assertIn("readinessProbe", c)
                self.assertTrue(c["resources"]["requests"] and c["resources"]["limits"])

    def test_only_the_proxy_is_exposed(self):
        pod = self.by_kind["Deployment"][0]["spec"]["template"]["spec"]
        proxy = next(c for c in pod["containers"] if c["name"] == "oauth-proxy")
        view = next(c for c in pod["containers"] if c["name"] == "viewer")
        self.assertNotIn("ports", view)
        args = proxy["args"]
        self.assertIn("--provider=openshift", args)
        self.assertIn("--upstream=http://127.0.0.1:8080", args)
        self.assertIn("--pass-basic-auth=false", args)
        self.assertIn("--pass-user-headers=true", args)
        self.assertFalse([a for a in args if a.startswith(("--skip-auth", "--bypass-auth"))])
        sar = json.loads(next(a for a in args if a.startswith("--openshift-sar=")).split("=", 1)[1])
        self.assertEqual(sar, {"namespace": "readiness-viewer", "resource": "services",
                               "resourceName": "report-viewer", "verb": "get"})
        (svc,) = self.by_kind["Service"]
        self.assertEqual([p["targetPort"] for p in svc["spec"]["ports"]], ["https"])
        (route,) = self.by_kind["Route"]
        self.assertEqual(route["spec"]["tls"]["termination"], "reencrypt")

    def test_access_is_one_group_and_one_verb(self):
        (role,) = self.by_kind["Role"]
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["services"],
                                          "resourceNames": ["report-viewer"], "verbs": ["get"]}])
        (binding,) = self.by_kind["RoleBinding"]
        self.assertEqual([s["kind"] for s in binding["subjects"]], ["Group"])
        self.assertNotIn("ClusterRoleBinding", self.by_kind)
        self.assertNotIn("ClusterRole", self.by_kind)

    def test_install_script_renders(self):
        tool = shutil.which("oc") or shutil.which("kubectl")
        self.assertIsNotNone(tool, "oc or kubectl is needed to render the manifests; this test does not skip")
        env = dict(os.environ, AAP_URL="https://aap.apps.example.com",
                   OAUTH_PROXY_IMAGE="quay.io/example/oauth-proxy@sha256:" + "0" * 64,
                   VIEWER_IMAGE="registry.example.com:5000/ns/report-viewer:1.2", VIEWER_HOST="reports.apps.example.com")
        r = subprocess.run([str(REPO / "report-viewer/install.sh"), "--render"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        docs = [d for d in yaml.safe_load_all(r.stdout) if d]
        dep = next(d for d in docs if d["kind"] == "Deployment")
        images = {c["name"]: c["image"] for c in dep["spec"]["template"]["spec"]["containers"]}
        self.assertEqual(images, {"oauth-proxy": "quay.io/example/oauth-proxy@sha256:" + "0" * 64,
                                  "viewer": "registry.example.com:5000/ns/report-viewer:1.2"})
        cm = next(d for d in docs if d["kind"] == "ConfigMap")
        self.assertEqual(cm["data"]["AAP_URL"], "https://aap.apps.example.com")
        self.assertEqual(next(d for d in docs if d["kind"] == "Route")["spec"]["host"], "reports.apps.example.com")
        self.assertTrue(all(d["metadata"].get("namespace", "readiness-viewer") == "readiness-viewer" for d in docs))

    def test_install_script_rejects_bad_input(self):
        for env in ({"AAP_URL": ""}, {"AAP_URL": "http://aap.example.com"},
                    {"AAP_URL": "https://aap.example.com", "VIEWER_HOST": 'x"; rm -rf /'}):
            with self.subTest(env):
                r = subprocess.run([str(REPO / "report-viewer/install.sh"), "--render"],
                                   env={**os.environ, **env}, capture_output=True, text=True)
                self.assertEqual(r.returncode, 1)
                self.assertIn("error:", r.stderr)


# A throwaway self-signed certificate, for loading as a custom CA. No key.
_SELF_SIGNED = """-----BEGIN CERTIFICATE-----
MIIBlTCCATugAwIBAgIUH8c99ryu42+3o5TLF0/DOv13lF0wCgYIKoZIzj0EAwIw
IDEeMBwGA1UEAwwVcmVwb3J0LXZpZXdlciB0ZXN0IENBMB4XDTI2MTExMTIyNTg1
NloXDTM2MTEwODIyNTg1NlowIDEeMBwGA1UEAwwVcmVwb3J0LXZpZXdlciB0ZXN0
IENBMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEIyNqljZclPzlWCUvBPljx9Xd
EbAAo4x3hnjY4Tr/A2sK8hSQTjhtEk93xEubQ5IJR+rEknGnOzKYA3/eLKtSy6NT
MFEwHQYDVR0OBBYEFDthedxHJE7KlVVeGOyk36sblM/sMB8GA1UdIwQYMBaAFDth
edxHJE7KlVVeGOyk36sblM/sMA8GA1UdEwEB/wQFMAMBAf8wCgYIKoZIzj0EAwID
SAAwRQIgOVXXB10QwrOg6opWkjXBCpzshJb+bEagR2e13onZe9YCIQCiXSpU7A6d
3RtEtmZg+7tr3tLq1Jn7T1WSAMq9hXxe1w==
-----END CERTIFICATE-----
"""


if __name__ == "__main__":
    unittest.main()
