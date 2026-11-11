"""Readiness dashboard: rendering, the HTML artifact, and retrieval from AAP.

Renders the report through playbooks/20_readiness_report.yml - the real role,
grading included - with fixture branch results passed as extra variables, the
way the workflow passes artifacts. Then reads the published artifacts back
through tools/fetch_readiness_report.py, both from a file and from a fake
Controller API.

    python3 -m unittest -v tests/test_readiness_report.py

Needs ansible-playbook and the collections in execution-environment/
requirements.yml. Set REPORT_PREVIEW_DIR to keep the rendered pages.
"""

import base64
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_controller import FakeController  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "report"
TEMPLATE = REPO / "roles" / "readiness_report" / "templates" / "readiness_report.html.j2"
HELPER = REPO / "tools" / "fetch_readiness_report.py"
CHECKS = ["dns", "ntp", "registry", "vips", "bastion", "bmc"]
TOKEN = "test-token-6f1c2a9e"

# scenario -> (expected verdict, expected approval state)
SCENARIOS = {
    "not_ready_dns": ("NOT_READY", "blocked"),
    "ready_with_limitations": ("READY_WITH_LIMITATIONS", "eligible"),
    "ready_live": ("READY", "eligible"),
    "sparse": ("READY_WITH_LIMITATIONS", "eligible"),
    "hostile": ("NOT_READY", "blocked"),
}

_rendered = {}
_output = {}


def render(scenario, **extra):
    """Run the report node for a fixture; return (rc, html, artifacts, rendered file)."""
    key = (scenario, tuple(sorted(extra.items())))
    if key in _rendered:
        return _rendered[key]
    if shutil.which("ansible-playbook") is None:
        raise AssertionError("ansible-playbook is not on PATH; these tests do not skip")
    work = Path(tempfile.mkdtemp(prefix=f"report-{scenario}-"))
    cfg = work / "ansible.cfg"
    cfg.write_text(
        "[defaults]\n"
        f"roles_path = {REPO}/roles\n"
        f"collections_path = {os.environ.get('ANSIBLE_COLLECTIONS_PATH', '')}:{REPO}/collections:"
        f"{Path.home()}/.ansible/collections\n"
        "interpreter_python = auto_silent\n"
        "stdout_callback = ansible.posix.json\n")
    env = dict(os.environ, ANSIBLE_CONFIG=str(cfg), ANSIBLE_SHOW_CUSTOM_STATS="1")
    cmd = ["ansible-playbook", "-i", str(REPO / "inventories/demo/hosts.yml"),
           str(REPO / "playbooks/20_readiness_report.yml"),
           "-e", f"@{FIXTURES / (scenario + '.json')}",
           "-e", json.dumps({"report_workdir": str(work / "out"), **extra})]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f"ansible-playbook output was not JSON:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    artifacts = result.get("global_custom_stats") or {}
    pages = sorted((work / "out").glob("*.html"))
    if len(pages) != 1:
        raise AssertionError(f"expected one rendered page for {scenario}, found {len(pages)}\n{proc.stderr[-2000:]}")
    html = pages[0].read_text()
    preview = os.environ.get("REPORT_PREVIEW_DIR")
    if preview and not extra:
        Path(preview).mkdir(parents=True, exist_ok=True)
        shutil.copy(pages[0], Path(preview) / f"{scenario}.html")
    _rendered[key] = (proc.returncode, html, artifacts, pages[0])
    _output[key] = proc.stdout
    return _rendered[key]


def job_output(scenario, **extra):
    """The report job's output (JSON callback), for checking what it prints."""
    render(scenario, **extra)
    return _output[(scenario, tuple(sorted(extra.items())))]


def fixture(scenario):
    return json.loads((FIXTURES / f"{scenario}.json").read_text())


class Case(unittest.TestCase):
    """Short failure messages: the default ones print the whole page."""

    def assertIn(self, member, container, msg=None):
        if member not in container:
            self.fail(msg or f"{member!r} not found")

    def assertNotIn(self, member, container, msg=None):
        if member in container:
            i = container.index(member)
            self.fail(msg or f"{member!r} unexpectedly present: ...{container[max(0, i - 80):i + 80]!r}...")


def run_helper(*args, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith("AAP_")}
    e.update(env or {})
    return subprocess.run([sys.executable, str(HELPER), *args], env=e, capture_output=True, text=True)


class FixtureSet(Case):
    def test_every_scenario_has_a_fixture(self):
        found = sorted(p.stem for p in FIXTURES.glob("*.json"))
        self.assertGreaterEqual(len(found), 5, "fixture glob matched too few files")
        self.assertEqual(found, sorted(SCENARIOS))


class TemplateSource(Case):
    """Ansible does not apply Jinja autoescape, so escaping is checked statically."""

    MACROS = ("st(", "tn(", "pill(", "source(", "items(", "value(", "date(", "title(")

    def test_every_output_expression_is_escaped(self):
        src = TEMPLATE.read_text()
        exprs = re.findall(r"\{\{(.*?)\}\}", src, flags=re.S)
        self.assertGreater(len(exprs), 50)
        bad = []
        for raw in exprs:
            e = " ".join(raw.split())
            if re.search(r"\|\s*e$", e) or e.startswith(self.MACROS):
                continue
            # a conditional whose every branch is a string literal
            if re.match(r"^'[^']*' if ", e) and all(x == "'" for x in re.findall(r"\belse\s*\(?\s*(\S)", e)):
                continue
            bad.append(e)
        self.assertEqual(bad, [], "unescaped output expressions in the template")

    def test_no_external_resources_or_script(self):
        src = TEMPLATE.read_text()
        for needle in ("<script", "<link", "@import", "url(", "http://", "https://", " src="):
            self.assertNotIn(needle, src)


class Rendering(Case):

    def cards(self, html):
        return dict(re.findall(r'<article class="card t-\w+" data-check="([^"]+)" data-status="([^"]+)"', html))

    def test_verdict_and_tone(self):
        tones = {"NOT_READY": "bad", "READY_WITH_LIMITATIONS": "warn", "READY": "ok"}
        for scenario, (verdict, _) in SCENARIOS.items():
            with self.subTest(scenario):
                _, html, artifacts, _ = render(scenario)
                self.assertEqual(artifacts["readiness_overall"], verdict)
                self.assertIn(f'<section class="verdict t-{tones[verdict]}" data-verdict="{verdict}"', html)
                self.assertIn(f'<h1 id="verdict">{verdict}</h1>', html)

    def test_card_status_is_exactly_the_published_status(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario):
                _, html, artifacts, _ = render(scenario)
                published = artifacts["readiness_report"]["checks"]
                cards = self.cards(html)
                self.assertEqual(sorted(cards), sorted(CHECKS))
                for name in CHECKS:
                    self.assertEqual(cards[name], published[name]["status"], name)

    def test_counts_never_promote_simulated_or_skipped(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario):
                _, html, artifacts, _ = render(scenario)
                statuses = [c["status"] for c in artifacts["readiness_report"]["checks"].values()]
                for key, status in [("pass", "PASS"), ("fail", "FAIL"), ("unconfirmed", "UNCONFIRMED"),
                                    ("simulated", "SIMULATED"), ("skipped", "SKIPPED")]:
                    m = re.search(rf'data-count="{key}"><b>(\d+)</b>', html)
                    self.assertIsNotNone(m, key)
                    self.assertEqual(int(m.group(1)), statuses.count(status), key)

    def test_simulated_checks_are_labelled_as_such(self):
        _, html, _, _ = render("ready_with_limitations")
        self.assertEqual(html.count("Simulated &middot; nothing contacted"), 2 * 4)   # card + evidence row, 4 checks
        self.assertIn("Graded against fixture data", html)                           # DNS in demo mode
        self.assertNotIn("Executed against live environment", html)
        self.assertIn('data-environment="demo"', html)

    def test_live_run_is_labelled_live(self):
        _, html, _, _ = render("ready_live")
        self.assertIn('data-environment="live"', html)
        self.assertNotIn("Demo mode", html)
        self.assertNotIn("nothing contacted", html)
        self.assertIn("6 of 6 checks passed against a live service", html)
        self.assertIn("2027-12-31 23:59 UTC", html)                     # ASN.1 time formatted
        self.assertIn("4812", html)
        self.assertIn("platform-approvers", html)

    def test_approval_is_never_claimed(self):
        for scenario, (verdict, approval) in SCENARIOS.items():
            with self.subTest(scenario):
                _, html, _, _ = render(scenario)
                self.assertIn(f'data-approval="{approval}"', html)
                text = re.sub(r"<[^>]+>", " ", html).lower()
                for claim in ("approval granted", "was approved", "has been approved", "approved by",
                              "execution authorised", "execution was authorised"):
                    self.assertNotIn(claim, text)
                if approval == "blocked":
                    self.assertIn("Execution blocked", html)
                    self.assertIn("Not reachable", html)
                else:
                    self.assertIn("Eligible for human approval", html)
                    self.assertIn("does not record a decision", html)

    def test_limitations_are_listed(self):
        _, html, artifacts, _ = render("ready_with_limitations")
        block = html.split("Approving accepts these limitations", 1)[1].split("</ul>", 1)[0]
        for name in artifacts["readiness_report"]["not_executed"]:
            title = {"ntp": "NTP", "registry": "Mirror registry", "vips": "VIPs",
                     "bastion": "Bastion", "bmc": "BMC / Redfish"}[name]
            self.assertIn(f"<b>{title}</b>", block)

    def test_failed_dns_names_the_record(self):
        _, html, _, _ = render("not_ready_dns")
        self.assertIn('<tr class="row-bad">', html)
        self.assertIn("api-int.ocp-demo.lab.example.com", html)
        self.assertIn("NXDOMAIN", html)
        self.assertIn("fd772dd5eea60420accd57b68f9f89bad968921be7924762860842656e607144", html)

    def test_skipped_bastion_is_not_reported_as_measured(self):
        _, html, _, _ = render("ready_with_limitations")
        card = html.split('data-check="bastion"', 1)[1].split("</article>", 1)[0]
        self.assertIn("not measured", card)
        self.assertNotIn("0.0 GB", card)

    def test_missing_fields_render(self):
        rc, html, artifacts, _ = render("sparse")
        self.assertEqual(rc, 0)
        self.assertEqual(self.cards(html)["ntp"], "SKIPPED")
        self.assertIn("<code>unknown</code>", html)                    # cluster
        self.assertIn('data-status="UNCONFIRMED"', html)
        self.assertIn("Branch did not report.", html)

    def test_hostile_values_are_escaped(self):
        _, html, _, _ = render("hostile")
        for raw in ("<script", "<img", "<iframe", "<svg", "<style>body", "<b>not-a-digest"):
            self.assertNotIn(raw, html)
        self.assertIn("&lt;script&gt;alert(&#39;cluster&#39;)&lt;/script&gt;", html)
        self.assertIn("&#34;&gt;&lt;img src=x onerror=alert(1)&gt;", html)
        self.assertIn("ntp&#39; onmouseover=&#39;alert(1)", html)
        self.assertIn("Draft export", html)

    def test_self_contained_and_responsive(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario):
                _, html, _, _ = render(scenario)
                self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', html)
                self.assertIn("@media (max-width:600px)", html)
                self.assertNotIn("<script", html)
                self.assertNotIn("<link", html)
                self.assertNotIn("url(", html)
                self.assertEqual([h for h in re.findall(r'href="([^"]*)"', html) if not h.startswith("#")], [])
                self.assertIsNone(re.search(r"<[a-z][^>]*\ssrc=", html), "a tag loads a resource")
                for anchor in re.findall(r'href="#([^"]+)"', html):
                    self.assertIn(f'id="{anchor}"', html)


class Artifact(Case):

    def test_failed_report_job_still_publishes_the_dashboard(self):
        rc, html, artifacts, _ = render("not_ready_dns")
        self.assertNotEqual(rc, 0, "NOT_READY must fail the report job")
        art = artifacts["readiness_report_html"]
        self.assertEqual(art["encoding"], "gzip+base64")
        self.assertEqual(gzip.decompress(base64.b64decode(art["content"])).decode(), html)

    def test_structured_report_is_unchanged(self):
        _, _, artifacts, _ = render("ready_with_limitations")
        report = artifacts["readiness_report"]
        self.assertEqual(sorted(report), sorted([
            "approval_requested_from", "bundle_is_draft", "bundle_sha256", "checks", "cluster", "demo_mode",
            "demo_scenario", "execution_environment", "failed_checks", "generated", "job_id", "launched_by",
            "not_executed", "overall", "release", "workflow_job_id"]))

    def test_artifact_is_small(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario):
                _, _, artifacts, _ = render(scenario)
                self.assertLess(len(artifacts["readiness_report_html"]["content"]), 32 * 1024)

    def test_oversized_dashboard_is_omitted_not_truncated(self):
        rc, _, artifacts, _ = render("ready_with_limitations", report_html_artifact_max_bytes=100)
        self.assertEqual(rc, 0)
        art = artifacts["readiness_report_html"]
        self.assertTrue(art["omitted"])
        self.assertNotIn("content", art)
        self.assertIn("readiness_report", artifacts)


class ViewerLink(Case):
    """readiness_viewer_url adds a link to the job output and changes nothing else."""

    VIEWER = "https://viewer.example.test"

    def test_link_when_configured(self):
        out = job_output("ready_live", readiness_viewer_url=self.VIEWER + "/")
        self.assertIn(f"VIEW READINESS DASHBOARD: {self.VIEWER}/report/4812", out)
        self.assertIn(f"Open it in a browser: {self.VIEWER}/report/4812", out)

    def test_no_link_without_viewer(self):
        out = job_output("ready_live")
        self.assertNotIn("VIEW READINESS DASHBOARD", out)
        self.assertNotIn("readiness_viewer_url", out)
        self.assertIn("tools/fetch_readiness_report.py 4812 --open", out)

    def test_not_ready_still_fails_with_a_link(self):
        rc, _, artifacts, _ = render("not_ready_dns", readiness_viewer_url=self.VIEWER, awx_job_id=77)
        out = job_output("not_ready_dns", readiness_viewer_url=self.VIEWER, awx_job_id=77)
        self.assertNotEqual(rc, 0)
        self.assertIn(f"VIEW READINESS DASHBOARD: {self.VIEWER}/report/77", out)
        self.assertEqual(artifacts["readiness_overall"], "NOT_READY")
        self.assertIn("readiness_report_html", artifacts)

    def test_artifacts_unchanged_by_the_link(self):
        _, html, with_link, _ = render("ready_live", readiness_viewer_url=self.VIEWER)
        _, _, without, _ = render("ready_live")
        self.assertEqual(sorted(with_link), sorted(without))
        self.assertEqual(with_link["readiness_report"]["checks"], without["readiness_report"]["checks"])
        self.assertNotIn(self.VIEWER, html)

    def test_no_job_id_means_no_link(self):
        out = job_output("ready_with_limitations", readiness_viewer_url=self.VIEWER)
        self.assertNotIn("VIEW READINESS DASHBOARD", out)
        self.assertIn("this run has no AAP job ID to link to", out)

    def test_malformed_url_is_reported_not_linked(self):
        out = job_output("ready_live", readiness_viewer_url="viewer.example.test")
        self.assertNotIn("VIEW READINESS DASHBOARD", out)
        self.assertIn("it is not an http(s) URL", out)


class HelperFromFile(Case):

    def save(self, data):
        d = Path(tempfile.mkdtemp(prefix="fetch-"))
        (d / "in.json").write_text(json.dumps(data))
        return d

    def test_round_trip_for_passing_and_failing(self):
        for scenario in ("not_ready_dns", "ready_with_limitations"):
            with self.subTest(scenario):
                _, html, artifacts, _ = render(scenario)
                d = self.save(artifacts)
                r = run_helper("--from-file", str(d / "in.json"), "-o", str(d / "out.html"), "--json")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual((d / "out.html").read_text(), html)
                self.assertEqual(json.loads((d / "out.json").read_text()), artifacts["readiness_report"])
                self.assertEqual((d / "out.html").stat().st_mode & 0o777, 0o600)

    def test_omitted_dashboard_explains_itself(self):
        _, _, artifacts, _ = render("ready_with_limitations", report_html_artifact_max_bytes=100)
        d = self.save(artifacts)
        r = run_helper("--from-file", str(d / "in.json"), "-o", str(d / "out.html"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("report_html_artifact_max_bytes", r.stderr)
        r = run_helper("--from-file", str(d / "in.json"), "-o", str(d / "out.html"), "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((d / "out.json").exists())
        self.assertFalse((d / "out.html").exists())

    def test_truncated_content_is_reported(self):
        _, _, artifacts, _ = render("ready_with_limitations")
        broken = json.loads(json.dumps(artifacts))
        broken["readiness_report_html"]["content"] = broken["readiness_report_html"]["content"][:400]
        d = self.save(broken)
        r = run_helper("--from-file", str(d / "in.json"), "-o", str(d / "out.html"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not be decoded", r.stderr)


class HelperAgainstController(Case):

    @classmethod
    def setUpClass(cls):
        _, cls.fail_html, fail_art, _ = render("not_ready_dns")
        _, cls.pass_html, pass_art, _ = render("ready_with_limitations")
        old = {k: v for k, v in pass_art.items() if k != "readiness_report_html"}
        cls.aap = FakeController(TOKEN, basic="viewer:s3cret")
        cls.aap.jobs = {
            102: {"id": 102, "name": "20-readiness-report", "status": "failed", "artifacts": fail_art},
            103: {"id": 103, "name": "20-readiness-report", "status": "running", "artifacts": {}},
            104: {"id": 104, "name": "20-readiness-report", "status": "successful",
                  "artifacts": "$hidden due to Ansible no_log flag$"},
            105: {"id": 105, "name": "20-readiness-report", "status": "successful", "artifacts": old},
            106: {"id": 106, "name": "30-bmc-prepare-hosts", "status": "successful", "artifacts": {}},
            110: {"id": 110, "name": "20-readiness-report", "status": "successful", "artifacts": pass_art},
        }
        cls.aap.nodes = {
            200: [{"identifier": "intake", "summary_fields": {"job": {"id": 90}}},
                  {"identifier": "readiness-report", "summary_fields": {"job": {"id": 102}}}],
            201: [{"identifier": "intake", "summary_fields": {"job": {"id": 91}}},
                  {"identifier": "readiness-report", "summary_fields": {}}],
        }
        cls.env = {"AAP_HOST": cls.aap.url, "AAP_TOKEN": TOKEN}

    @classmethod
    def tearDownClass(cls):
        cls.aap.stop()

    def fetch(self, *args, env=None):
        out = Path(tempfile.mkdtemp(prefix="fetch-"))
        r = run_helper(*args, "-o", str(out), env=env if env is not None else self.env)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)
        self.assertNotIn("s3cret", r.stdout + r.stderr)
        return r, out

    def test_failed_not_ready_job(self):
        r, out = self.fetch("102")
        self.assertEqual(r.returncode, 0, r.stderr)
        saved = list(out.glob("job102-readiness-report-*.html"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_text(), self.fail_html)
        self.assertIn("verdict NOT_READY", r.stderr)
        self.assertIn("job status failed", r.stderr)

    def test_workflow_job(self):
        r, out = self.fetch("--workflow", "200")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(list(out.glob("job102-*.html"))), 1)

    def test_workflow_that_never_reached_the_report(self):
        r, _ = self.fetch("--workflow", "201")
        self.assertEqual(r.returncode, 1)
        self.assertIn("has not run its readiness report", r.stderr)

    def test_latest(self):
        r, out = self.fetch("--latest")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(list(out.glob("job110-*.html"))[0].read_text(), self.pass_html)
        self.assertIn("name=20-readiness-report", self.aap.last_query)
        self.assertIn("order_by=-id", self.aap.last_query)

    def test_basic_auth(self):
        r, _ = self.fetch("110", env={"AAP_HOST": self.env["AAP_HOST"], "AAP_USERNAME": "viewer",
                                      "AAP_PASSWORD": "s3cret"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_failures_name_the_remedy(self):
        cases = [
            ("999", {}, "Cleanup Job Details"),
            ("103", {}, "is running"),
            ("104", {}, "no_log"),
            ("105", {}, "older version of the role"),
            ("106", {}, "not '20-readiness-report'"),
            ("110", {"AAP_TOKEN": "wrong"}, "Authentication failed"),
        ]
        for job, env, message in cases:
            with self.subTest(job):
                r, out = self.fetch(job, env={**self.env, **env})
                self.assertEqual(r.returncode, 1)
                self.assertIn(message, r.stderr)
                self.assertEqual(list(out.iterdir()), [])

    def test_no_credentials(self):
        r, _ = self.fetch("110", env={"AAP_HOST": self.env["AAP_HOST"]})
        self.assertEqual(r.returncode, 1)
        self.assertIn("No credentials", r.stderr)


if __name__ == "__main__":
    unittest.main()
