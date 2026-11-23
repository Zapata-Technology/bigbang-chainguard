"""scripts/nv-report.py: NeuVector's scans of a deployed Big Bang, from canned API answers."""
import io
import json
import urllib.error

import pytest

from conftest import load_script

nv = load_script("nv-report")

ORG = "example.com"
D1, D2, D3, D4, D5 = ("sha256:" + c * 64 for c in "abcde")


def workload(cid, image, digest, status="finished", base_os="wolfi", children=None, **kw):
    return dict(id=cid, image=image, image_id=digest, running=True, children=children or [],
                scan_summary={"status": status, "base_os": base_os}, **kw)


WORKLOADS = [
    # A pod-level entry whose containers are its children (NeuVector nests them).
    workload("pod1", "", "", children=[
        workload("c1", f"cgr.dev/{ORG}/kyverno@{D1}", D1),
        workload("c2", "registry.k8s.io/pause:3.10", ""),
    ]),
    workload("c3", f"cgr.dev/{ORG}/kyverno@{D1}", D1),        # a second replica: counted, not re-fetched
    workload("c4", f"ghcr.io/owner/bigbang-base@{D2}", D2),
    workload("c5", f"docker.io/rancher/local-path-provisioner@{D3}", D3, base_os="alpine"),
    dict(workload("c6", f"cgr.dev/{ORG}/old@{D3}", D3), running=False),
]

REPORTS = {
    "c1": {"vulnerabilities": [
        {"name": "CVE-2026-1", "severity": "High", "score": 7.5, "package_name": "openssl",
         "package_version": "3.5.0-r0", "fixed_version": "3.5.1-r0", "link": "https://x/1"},
        {"name": "CVE-2026-2", "severity": "Critical", "score": 9.8, "package_name": "glibc",
         "package_version": "2.41-r0", "fixed_version": "", "link": "https://x/2"},
    ], "modules": [{"name": "openssl"}, {"name": "glibc"}]},
    "c4": {"vulnerabilities": [], "modules": []},              # nothing read: flagged, not "clean"
    "c5": {"vulnerabilities": [{"name": "CVE-2026-3", "severity": "Medium", "fixed_version": "1.2"}],
           "modules": [{"name": "busybox"}]},
}


def test_containers_flattens_children_and_skips_pause_and_stopped():
    assert [c["id"] for c in nv.containers(WORKLOADS)] == ["c1", "c3", "c4", "c5"]


def test_summarize_counts_fixable_origin_and_dedupes_by_digest():
    fetched = []
    out = nv.summarize(nv.containers(WORKLOADS), lambda cid: fetched.append(cid) or REPORTS[cid],
                       org=ORG, build_registry="ghcr.io/owner", tags={D1: "1.19.1"})
    assert fetched == ["c1", "c4", "c5"]
    by = {i["image"]: i for i in out}
    k = by[f"cgr.dev/{ORG}/kyverno"]
    assert (k["origin"], k["tag"], k["containers"], k["fixable"], k["packages"]) == ("chainguard", "1.19.1", 2, 1, 2)
    assert k["counts"] == {"Critical": 1, "High": 1, "Medium": 0, "Low": 0}
    assert [v["name"] for v in k["vulnerabilities"]] == ["CVE-2026-2", "CVE-2026-1"]   # worst first
    assert by["ghcr.io/owner/bigbang-base"]["origin"] == "built-here"
    assert by["docker.io/rancher/local-path-provisioner"]["origin"] == "other"
    assert [i["origin"] for i in out] == ["chainguard", "built-here", "other"]


def test_markdown_flags_images_with_no_packages_and_totals_by_origin():
    images = nv.summarize(nv.containers(WORKLOADS), REPORTS.__getitem__, org=ORG, build_registry="ghcr.io/owner")
    md = nv.markdown({"bigbang": "4.1.1", "mode": "fips", "run": "", "cvedb": {"version": "3.9", "created": "2026-11-20"},
                      "images": images})
    assert "Big Bang 4.1.1 (fips)" in md and "database 3.9, built 2026-11-20" in md
    assert "0 (none found: not read)" in md
    assert "chainguard: 1 Critical, 1 High, 0 Medium, 0 Low" in md


def test_overlay_tags_reads_digest_and_new_tag_pairs(tmp_path):
    p = tmp_path / "o.yaml"
    p.write_text(f"""      images:
        - digest: {D1}
          name: registry1.dso.mil/ironbank/x
          newName: cgr.dev/{ORG}/x
          newTag: 1.2.3
        - digest: {D2}
          name: cgr.dev/{ORG}/y
""")
    assert nv.overlay_tags(str(p)) == {D1: "1.2.3"}


class FakeApi(nv.Api):
    """Answers like the controller: first login asks for a new password."""

    def __init__(self, scans_after=0):
        super().__init__("https://127.0.0.1:10443")
        self.calls, self.polls, self.scans_after = [], 0, scans_after

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/v1/auth" and method == "POST":
            p = body["password"]
            if p["password"] == "boot" and "new_password" not in p:
                return {"need_to_reset_password": True, "token": {"token": "limited"}}
            if p["password"] in ("boot", "new") :
                return {"token": {"token": "tok"}}
            raise urllib.error.HTTPError(path, 401, "no", {}, io.BytesIO())
        if path == "/v1/workload":
            self.polls += 1
            status = "finished" if self.polls > self.scans_after else ""
            return {"workloads": [workload("c1", f"cgr.dev/{ORG}/k@{D1}", D1, status=status)]}
        return {}


def test_login_sets_the_new_password_on_first_login():
    api = FakeApi()
    api.login("boot", "new")
    assert api.token == "tok"
    assert api.calls[-1][2] == {"password": {"username": "admin", "password": "boot", "new_password": "new"}}


def test_login_on_a_cluster_whose_password_was_already_changed():
    api = FakeApi()
    api.login("stale-bootstrap", "new")
    assert api.token == "tok"


def test_wait_for_scans_turns_auto_scan_on_and_waits_until_every_container_finished():
    api, t = FakeApi(scans_after=2), [0]
    cs = nv.wait_for_scans(api, 600, log=lambda *_: None, clock=lambda: t[0],
                           sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert [c["id"] for c in cs] == ["c1"] and api.polls == 3
    assert ("PATCH", "/v1/scan/config", {"config": {"auto_scan": True}}) in api.calls


def test_wait_for_scans_gives_up_naming_what_is_left():
    api, t = FakeApi(scans_after=10**6), [0]
    with pytest.raises(SystemExit, match=r"unscanned \(no scanner registered.*\): cgr.dev/example.com/k@"):
        nv.wait_for_scans(api, 100, log=lambda *_: None, clock=lambda: t[0],
                          sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert any(m == "POST" and p == "/v1/scan/workload/c1" for m, p, _ in api.calls)   # nudged once


def test_no_password_reaches_the_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("NV_BOOTSTRAP", "boot")
    monkeypatch.setenv("NV_PASSWORD", "new")
    monkeypatch.setattr(nv, "Api", lambda base: FakeApi())
    monkeypatch.setattr("sys.argv", ["nv-report.py", "--api", "https://x", "--out", str(tmp_path), "--timeout", "5"])
    monkeypatch.setattr(nv.time, "sleep", lambda s: None)
    nv.main()
    for name in ("nv-report.json", "nv-report.md"):
        text = (tmp_path / name).read_text(encoding="utf-8")
        assert "boot" not in text and '"new"' not in text
    assert json.loads((tmp_path / "nv-report.json").read_text())["images"][0]["image"] == f"cgr.dev/{ORG}/k"


def test_login_retries_while_the_api_comes_up():
    api, waits = FakeApi(), []
    real = api.login
    fails = [urllib.error.URLError(ConnectionRefusedError(10061, "refused"))] * 2

    def flaky(b, p):
        if fails:
            raise fails.pop()
        return real(b, p)
    api.login = flaky
    nv.login(api, "boot", "new", sleep=waits.append)
    assert api.token == "tok" and len(waits) == 2


def test_login_does_not_retry_a_refused_password():
    api = FakeApi()
    api.login = lambda b, p: (_ for _ in ()).throw(urllib.error.HTTPError("/v1/auth", 401, "no", {}, io.BytesIO()))
    with pytest.raises(urllib.error.HTTPError):
        nv.login(api, "boot", "new", sleep=lambda s: pytest.fail("retried"))


def test_a_dropped_connection_is_retried_but_an_http_error_is_not(monkeypatch):
    api, answers, waits = nv.Api("https://127.0.0.1:10443"), [], []
    api.sleep = waits.append

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, context=None, timeout=None):
        answers.append(req.full_url)
        if len(answers) < 3:
            raise urllib.error.URLError(ConnectionResetError(10054, "reset"))
        return Resp(b'{"workloads": []}')
    monkeypatch.setattr(nv.urllib.request, "urlopen", urlopen)
    assert api.call("GET", "/v1/workload") == {"workloads": []} and len(waits) == 2

    def refused(req, context=None, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "no", {}, io.BytesIO())
    monkeypatch.setattr(nv.urllib.request, "urlopen", refused)
    with pytest.raises(urllib.error.HTTPError):
        api.call("GET", "/v1/workload")
    assert len(waits) == 2


def test_login_explains_a_password_changed_by_an_earlier_run():
    api = FakeApi()
    with pytest.raises(SystemExit, match="changed by an earlier run"):
        api.login("stale-bootstrap", "also-wrong")


@pytest.mark.parametrize("ref, want", [
    (f"cgr.dev/{ORG}/neuvector-controller@{D1}", ("cgr.dev", f"{ORG}/neuvector-controller", D1)),
    ("registry.k8s.io/etcd:3.7.0-0", ("registry.k8s.io", "etcd", "3.7.0-0")),
    ("rancher/local-path-provisioner:v0.0.31", ("docker.io", "rancher/local-path-provisioner", "v0.0.31")),
    ("busybox", ("docker.io", "library/busybox", "latest")),
    ("localhost:5000/x/y:1", ("localhost:5000", "x/y", "1")),
])
def test_split_ref(ref, want):
    assert nv.split_ref(ref) == want


class RepoApi:
    def __init__(self, fail=()):
        self.requests, self.fail = [], fail

    def call(self, method, path, body=None, **kw):
        req = body["request"]
        self.requests.append(req)
        if req["repository"] in self.fail:
            raise urllib.error.HTTPError(path, 500, "no", {}, io.BytesIO())
        return {"report": {"base_os": "chainguard:20230214", "modules": [{"name": "glibc"}],
                           "vulnerabilities": [{"name": "CVE-2026-9", "severity": "Low", "fixed_version": ""}]}}


def test_repository_scans_cover_what_the_workload_scan_left_out():
    covered = nv.summarize(nv.containers(WORKLOADS), REPORTS.__getitem__, org=ORG)
    pods = [f"cgr.dev/{ORG}/kyverno@{D1}",                         # already scanned as a workload
            f"cgr.dev/{ORG}/neuvector-controller@{D4}"] * 1 + [f"cgr.dev/{ORG}/neuvector-scanner@{D5}"] * 3
    api = RepoApi(fail=(f"{ORG}/neuvector-scanner",))
    rows = nv.repository_scans(api, pods, covered, {"cgr.dev": ("_token", "tok")}, org=ORG,
                               tags={D4: "5.6.4"}, log=lambda *_: None,
                               scan=lambda a, req: a.call("POST", "/v1/scan/repository", {"request": req}).get("report"))
    assert [r["repository"] for r in api.requests] == [f"{ORG}/neuvector-controller", f"{ORG}/neuvector-scanner"]
    assert api.requests[0] == {"registry": "https://cgr.dev", "repository": f"{ORG}/neuvector-controller",
                               "tag": D4, "username": "_token", "password": "tok"}
    by = {r["image"]: r for r in rows}
    ctrl, scanner = by[f"cgr.dev/{ORG}/neuvector-controller"], by[f"cgr.dev/{ORG}/neuvector-scanner"]
    assert (ctrl["origin"], ctrl["tag"], ctrl["scan_status"], ctrl["counts"]["Low"]) == ("chainguard", "5.6.4", "repository", 1)
    assert (scanner["scan_status"], scanner["containers"], scanner["packages"]) == ("failed", 3, 0)
    md = nv.markdown({"bigbang": "4.1.1", "mode": "standard", "run": "", "cvedb": {}, "images": rows})
    assert "(scanned by reference)" in md and "(scan failed)" in md
    assert "tok" not in json.dumps(rows)


def test_registry_creds_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("CGR_PASSWORD", "c")
    monkeypatch.setenv("BUILD_REGISTRY", "ghcr.io/owner")
    monkeypatch.setenv("BUILD_REGISTRY_TOKEN", "g")
    monkeypatch.setenv("BUILD_REGISTRY_USERNAME", "bot")
    assert nv.registry_creds() == {"cgr.dev": ("_token", "c"), "ghcr.io": ("bot", "g")}


def test_repository_scans_skip_images_from_other_registries():
    api = RepoApi()
    rows = nv.repository_scans(api, [f"docker.io/rancher/hardened-coredns@{D4}"], [], {}, org=ORG, log=lambda *_: None)
    assert api.requests == [] and rows == []


def test_scan_repository_repeats_while_neuvector_answers_304():
    answers = [304, 304, None]

    class Api:
        def call(self, method, path, body=None, **kw):
            code = answers.pop(0)
            if code:
                raise urllib.error.HTTPError(path, code, "scanning", {}, io.BytesIO())
            return {"report": {"base_os": "wolfi"}}
    waits = []
    assert nv.scan_repository(Api(), {}, clock=lambda: 0, sleep=waits.append) == {"base_os": "wolfi"}
    assert len(waits) == 2


def test_scan_repository_gives_up_after_its_timeout():
    t = [0]

    class Api:
        def call(self, method, path, body=None, **kw):
            raise urllib.error.HTTPError(path, 304, "scanning", {}, io.BytesIO())
    with pytest.raises(urllib.error.HTTPError):
        nv.scan_repository(Api(), {}, timeout=30, clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))


def test_a_failed_digest_scan_logs_neuvector_s_reason_and_retries_by_tag():
    """RKE2 run 45063275137: every by-digest scan of NeuVector's own images got HTTP 500, with
    no reason in the log. Log the body (without the password) and try the overlay's tag."""
    calls, logged = [], []

    def scan(api, req):
        calls.append(req["tag"])
        if req["tag"].startswith("sha256:"):
            raise urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(b'{"message": "bad tag for tok"}'))
        return {"digest": D4, "base_os": "wolfi", "modules": [{"name": "x"}], "vulnerabilities": []}
    rows = nv.repository_scans(None, [f"cgr.dev/{ORG}/neuvector-controller@{D4}"], [], {"cgr.dev": ("_token", "tok")},
                               org=ORG, tags={D4: "5.6.4"}, log=logged.append, scan=scan)
    assert calls == [D4, "5.6.4"]
    assert any("HTTP 500" in m and "bad tag for ***" in m for m in logged)
    assert not any("tok" in m.replace("***", "") for m in logged if "HTTP" in m)
    assert rows[0]["scan_status"] == "repository (by tag)" and rows[0]["digest"] == D4
    # The tag moved after the deployment: the row says the scan wasn't of the deployed image.
    rows = nv.repository_scans(None, [f"cgr.dev/{ORG}/neuvector-controller@{D5}"], [], {}, org=ORG,
                               tags={D5: "5.6.4"}, log=lambda *_: None, scan=scan)
    assert rows[0]["scan_status"].startswith("repository (by tag; scanned sha256:")


def test_an_image_whose_pin_has_no_tag_is_retried_at_its_images_md_tag(tmp_path):
    """NeuVector's own images: one chart-wide tag, so their digest pins carry no newTag and the
    by-tag retry never ran (RKE2 run 45097553250: "Image not found" for every digest)."""
    md = tmp_path / "images.md"
    md.write_text("| Package | Iron Bank image | Standard | FIPS | Notes |\n|---|---|---|---|---|\n"
                  "| neuvector | ironbank/neuvector/neuvector/controller:5.6.4 | neuvector-controller:5.6.4 "
                  "| neuvector-controller-fips:5.6.4 | set by mapping overrides |\n")
    repo_tags = nv.images_md_tags(str(md), ORG)
    assert repo_tags == {f"cgr.dev/{ORG}/neuvector-controller": "5.6.4",
                         f"cgr.dev/{ORG}/neuvector-controller-fips": "5.6.4"}
    calls = []

    def scan(api, req):
        calls.append(req["tag"])
        if req["tag"].startswith("sha256:"):
            raise urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(b'{"message": "Image not found"}'))
        return {"digest": D4, "modules": [{"name": "x"}], "vulnerabilities": []}
    rows = nv.repository_scans(None, [f"cgr.dev/{ORG}/neuvector-controller-fips@{D4}"], [], {}, org=ORG,
                               log=lambda *_: None, scan=scan, repo_tags=repo_tags)
    assert calls == [D4, "5.6.4"] and rows[0]["scan_status"] == "repository (by tag)"
    assert nv.images_md_tags(str(tmp_path / "missing.md"), ORG) == {}
