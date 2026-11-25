"""release.py and release.yml: tag numbering, the notes, the archive, and how a release is
cut and signed (main only, for the files a site deploys or runs)."""
import io
import os
import re
import subprocess
import sys
import tarfile

import pytest
import yaml

from conftest import ROOT, SCRIPTS, load_script

release = load_script("release")


def workflow(name):
    return yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8"))


def test_next_tag_counts_per_big_bang_version():
    assert release.next_tag("3.35.1", []) == "3.35.1-cg.1"
    assert release.next_tag("3.35.1", ["3.35.1-cg.1", "3.35.1-cg.2", "3.34.0-cg.7"]) == "3.35.1-cg.3"
    assert release.next_tag("3.36.0", ["3.35.1-cg.9"]) == "3.36.0-cg.1"


def test_previous_tag_orders_by_version_not_text():
    assert release.previous_tag(["3.35.1-cg.9", "3.35.1-cg.10", "3.9.0-cg.20", "other"]) == "3.35.1-cg.10"
    assert release.previous_tag(["v1"]) is None


def test_image_changes_from_the_overlay():
    old = "a:\n  image:\n    repository: cgr.dev/o/x\n    tag: 1.0.0\nb:\n  image: cgr.dev/o/y:2.0\n"
    new = "a:\n  image:\n    repository: cgr.dev/o/x\n    tag: 1.0.1\nc:\n  hub: cgr.dev/o\n  tag: 3.0\n"
    assert release.image_changes(old, new) == ["- `cgr.dev/o/x`: 1.0.0 → 1.0.1", "- `cgr.dev/o`: new at 3.0",
                                               "- `cgr.dev/o/y`: removed"]


def test_release_identity_is_main_only():
    rx = release.release_identity("my-org/bigbang-chainguard")
    base = "https://github.com/my-org/bigbang-chainguard/.github/workflows/release.yml@refs/heads/"
    assert re.match(rx, base + "main")
    for other in (base + "feature", base + "main2", base.replace("my-org", "my-orgX") + "main",
                  base.replace("release.yml", "releaseXyml") + "main"):
        assert not re.match(rx, other), other


def test_notes_go_download_verify_extract_check_install(tmp_path):
    body = release.notes("4.1.1-cg.2", None, "4.1.1", "example.com", "o/r", root=str(tmp_path))
    steps = ["/bigbang-chainguard-4.1.1-cg.2.tar.gz\n", ".tar.gz.sigstore.json\n", "cosign verify-blob",
             "tar xzf", "bb-install.py check", "bb-install.py install"]
    at = [body.index(s) for s in steps]
    assert at == sorted(at), body
    assert "--certificate-identity-regexp '" + release.release_identity("o/r") + "'" in body
    assert "https://github.com/o/r/releases/download/4.1.1-cg.2/" in body
    assert "Zapata" not in body and "docs/upgrades" not in body       # no upgrade notes for 4.1.1 here
    (tmp_path / "docs" / "upgrades").mkdir(parents=True)
    (tmp_path / "docs" / "upgrades" / "4.1.1.md").write_text("x")
    body = release.notes("4.1.1-cg.2", None, "4.1.1", "example.com", "o/r", root=str(tmp_path))
    assert "(https://github.com/o/r/blob/4.1.1-cg.2/docs/upgrades/4.1.1.md)" in body
    assert body.index("docs/upgrades") < body.index("curl ")


def test_notes_link_the_security_report_when_the_repo_has_one(tmp_path):
    body = release.notes("4.1.1-cg.2", None, "4.1.1", "example.com", "o/r", root=str(tmp_path))
    assert "security-report.pdf" not in body
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "security-report.pdf").write_bytes(b"%PDF")
    body = release.notes("4.1.1-cg.2", None, "4.1.1", "example.com", "o/r", root=str(tmp_path))
    assert "(https://github.com/o/r/releases/download/4.1.1-cg.2/security-report.pdf)" in body


def test_archive_names_its_repository(monkeypatch, tmp_path):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as t:
        info = tarfile.TarInfo("bigbang-chainguard-4.1.1-cg.2/BIGBANG_VERSION")
        info.size = 6
        t.addfile(info, io.BytesIO(b"4.1.1\n"))
    monkeypatch.setattr(release.subprocess, "run",
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, raw.getvalue(), b""))
    monkeypatch.setattr(release.shutil, "which", lambda t: None)
    assets = release.archive("4.1.1-cg.2", str(tmp_path), "o/r")
    with tarfile.open(assets[0]) as t:
        files = {m.name: t.extractfile(m).read().decode() for m in t.getmembers()}
    # RELEASE stays the bare tag: installers already deployed compare it to the tag exactly.
    assert files["bigbang-chainguard-4.1.1-cg.2/RELEASE"] == "4.1.1-cg.2\n"
    assert files["bigbang-chainguard-4.1.1-cg.2/RELEASE_REPO"] == "repo: o/r\n"


def test_no_repository_no_release(monkeypatch):
    """Without --repo, $GITHUB_REPOSITORY or a GitHub remote, release.py asks for --repo
    rather than naming a repository it wasn't run from."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(release, "github_remote", lambda root=None: None)
    monkeypatch.setattr(sys, "argv", ["release.py", "--dry-run"])
    with pytest.raises(SystemExit, match="--repo"):
        release.main()


# --- release.yml ---

def test_release_runs_only_from_main_with_a_timeout():
    job = workflow("release.yml")["jobs"]["release"]
    assert job["if"].startswith("github.ref == 'refs/heads/main' &&")
    assert job["timeout-minutes"] <= 30


def test_release_waits_for_ci_on_main_pushes_only():
    """A release is cut after CI finished on a push to this repository's main, never on the
    push itself (CI's verify might still fail) and never for a pull request's CI run."""
    wf = workflow("release.yml")
    on = wf.get(True) or wf["on"]
    assert set(on) == {"workflow_run", "workflow_dispatch"}
    assert on["workflow_run"] == {"workflows": ["CI"], "types": ["completed"], "branches": ["main"]}
    assert workflow("ci.yml")["name"] == "CI" and release.VERIFY in workflow("ci.yml")["jobs"]
    job = wf["jobs"]["release"]
    for cond in ("github.event.workflow_run.event == 'push'", "github.event.workflow_run.head_branch == 'main'",
                 "github.event.workflow_run.head_repository.full_name == github.repository"):
        assert cond in job["if"], cond
    assert job["permissions"]["checks"] == "read"
    checkout = next(s for s in job["steps"] if "actions/checkout@" in s.get("uses", ""))
    assert checkout["with"]["ref"] == "${{ github.event.workflow_run.head_sha || github.sha }}"
    run = next(s["run"] for s in job["steps"] if s.get("name") == "Release")
    assert "release.py --after-ci" in run and "release.py --require-verify" in run


def gate_args(**kw):
    return type("A", (), {"require_verify": False, "after_ci": False, **kw})()


@pytest.mark.parametrize("changed, verify, want", [
    (["docs/install.md", "tests/test_x.py", "README.md"], "success", "no deployable file changed"),
    (["overlays/values-chainguard.yaml"], "success", None),
    (["docs/x.md", "scripts/bbinstall/install.py"], "success", None),
    (["overlays/values-chainguard.yaml"], "failure", "is failure, not success"),
    (["overlays/values-chainguard.yaml"], None, "is missing"),
])
def test_after_ci_releases_deployable_changes_once_verify_passed(monkeypatch, changed, verify, want):
    monkeypatch.setattr(release, "behind", lambda prev: False)
    monkeypatch.setattr(release, "changed_since", lambda prev: changed)
    monkeypatch.setattr(release, "verify_result", lambda repo, sha: verify)
    monkeypatch.setattr(release, "git", lambda *a: "abc123")
    stop = release.gate(gate_args(after_ci=True), "o/r", "4.1.1-cg.1")
    assert (stop is None) if want is None else (want in stop), stop


def test_after_ci_skips_a_commit_older_than_the_latest_release(monkeypatch):
    monkeypatch.setattr(release, "behind", lambda prev: True)
    monkeypatch.setattr(release, "verify_result", lambda repo, sha: pytest.fail("no API call needed"))
    assert "older than the latest release" in release.gate(gate_args(after_ci=True), "o/r", "4.1.1-cg.3")


def test_manual_release_still_needs_verify(monkeypatch):
    """workflow_dispatch releases whatever changed, but only a verified commit."""
    monkeypatch.setattr(release, "changed_since", lambda prev: pytest.fail("a manual release ignores paths"))
    monkeypatch.setattr(release, "git", lambda *a: "abc123")
    monkeypatch.setattr(release, "verify_result", lambda repo, sha: "pending")
    assert "is pending" in release.gate(gate_args(require_verify=True), "o/r", None)
    monkeypatch.setattr(release, "verify_result", lambda repo, sha: "success")
    assert release.gate(gate_args(require_verify=True), "o/r", None) is None
    assert release.gate(gate_args(), "o/r", None) is None     # a local --dry-run asks nothing


def test_deployable_matches_files_and_directories():
    assert all(p.endswith("/**") or "*" not in p for p in release.DEPLOYABLE)
    assert release.deployable("overlays/values-chainguard.yaml")
    assert release.deployable("scripts/bbinstall/x.py") and release.deployable("BIGBANG_VERSION")
    for no in ("overlays", "overlaysX/a", "scripts/bb-convert.py", "docs/install.md", "tests/test_release.py",
               "BIGBANG_VERSION.bak", ".github/workflows/ci.yml"):
        assert not release.deployable(no), no


def local_imports(path, seen):
    """scripts/ modules `path` imports, transitively (a package counts as its directory)."""
    for name in re.findall(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)", open(path, encoding="utf-8").read(), re.M):
        if name in seen:
            continue
        if os.path.isdir(os.path.join(SCRIPTS, name)):
            seen.add(name)
            for f in sorted(os.listdir(os.path.join(SCRIPTS, name))):
                if f.endswith(".py"):
                    local_imports(os.path.join(SCRIPTS, name, f), seen)
        elif os.path.exists(os.path.join(SCRIPTS, name + ".py")):
            seen.add(name)
            local_imports(os.path.join(SCRIPTS, name + ".py"), seen)
    return seen


def test_release_paths_cover_what_the_installer_runs_and_no_other_script():
    paths = release.DEPLOYABLE
    assert "scripts/**" not in paths
    want = {"scripts/bb-install.py"}
    for name in local_imports(os.path.join(SCRIPTS, "bb-install.py"), set()):
        want.add(f"scripts/{name}/**" if os.path.isdir(os.path.join(SCRIPTS, name)) else f"scripts/{name}.py")
    assert want <= set(paths), f"release.yml doesn't cut a release for: {sorted(want - set(paths))}"
    scripts = {p for p in paths if p.startswith("scripts/")}
    assert scripts == want, f"not run by the installer: {sorted(scripts - want)}"
    for p in ("overlays/**", "flux/**", "flux-fips/**", "seaweedfs/**", "seaweedfs-fips/**", "BIGBANG_VERSION",
              ".github/workflows/release.yml"):
        assert p in paths, p


# --- builds.yml: only main and manual runs can push or sign ---

def test_pull_request_builds_cannot_push():
    jobs = workflow("builds.yml")["jobs"]
    build, publish = jobs["build"], jobs["publish"]
    assert build["if"] == "github.event_name == 'pull_request'"
    assert build["permissions"]["packages"] == "read"
    assert publish["if"] == "github.event_name != 'pull_request'"
    assert publish["permissions"]["packages"] == "write"
    assert build["steps"] == publish["steps"] and build["strategy"] == publish["strategy"]
    assert "github.event_name != 'pull_request'" in build["env"]["PUSH"]
    for job in jobs.values():
        assert job.get("timeout-minutes"), "every build job needs a timeout"
    # The installer image has no .git: it names its repository as a release archive does.
    run = next(s["run"] for s in build["steps"] if s.get("name") == "Build and smoke-test (standard and FIPS)")
    assert 'echo "repo: $GITHUB_REPOSITORY" > ' + release.REPO_FILE in run


@pytest.mark.parametrize("name", ["builds.yml", "ci.yml", "compat.yml", "rke2.yml", "release.yml"])
def test_checkouts_keep_no_credentials(name):
    """None of these jobs pushes to git, so the token isn't left in .git/config for a later
    step (or a build's code) to read."""
    for job in workflow(name)["jobs"].values():
        for step in job.get("steps", []):
            if "actions/checkout@" in step.get("uses", ""):
                assert (step.get("with") or {}).get("persist-credentials") is False, name
