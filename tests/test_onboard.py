"""bb-onboard.py: the dry run changes nothing and prints every step, the CI identity trusts
only main and pull requests, and its lists match what the workflows read."""
import glob
import json
import os
import re
import subprocess

import yaml
from conftest import ROOT, load_script

import bbcommon

onboard = load_script("bb-onboard")


def test_subject_pattern_trusts_main_and_pull_requests_only():
    pat = re.compile(onboard.subject_pattern("Example-Org", "big.bang"))
    assert pat.pattern == r"^repo:Example-Org(@\d+)?/big\.bang(@\d+)?:(ref:refs/heads/main|pull_request)$"
    for ok in ("repo:Example-Org/big.bang:ref:refs/heads/main",
               "repo:Example-Org/big.bang:pull_request",
               "repo:Example-Org@123/big.bang@456:ref:refs/heads/main"):
        assert pat.match(ok), ok
    for bad in ("repo:Example-Org/big.bang:ref:refs/heads/feature",
                "repo:Example-Org/big.bang:ref:refs/heads/main-evil",
                "repo:Example-Org/bigXbang:pull_request",
                "repo:Other/big.bang:pull_request",
                "repo:Example-Org/big.bang:environment:prod"):
        assert not pat.match(bad), bad


def test_identity_command_uses_the_github_issuer_and_registry_pull():
    cmd = onboard.identity_create("example.com", "o", "r", "r-cicd")
    assert cmd[:5] == ["chainctl", "iam", "identities", "create", "r-cicd"]
    assert cmd[cmd.index("--identity-issuer") + 1] == "https://token.actions.githubusercontent.com"
    assert cmd[cmd.index("--subject-pattern") + 1] == onboard.subject_pattern("o", "r")
    assert cmd[cmd.index("--role") + 1] == "registry.pull"
    role = onboard.role_create("example.com")
    assert role[role.index("--capabilities") + 1] == "registry.entitlements.list"


def workflow_refs():
    refs = {"secrets": set(), "vars": set()}
    for path in glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")):
        for kind, name in re.findall(r"\b(secrets|vars)\.([A-Za-z0-9_]+)", open(path, encoding="utf-8").read()):
            refs[kind].add(name)
    return refs


def test_every_workflow_secret_and_variable_is_listed_and_documented():
    refs = workflow_refs()
    assert {n for n, _, _ in onboard.SECRETS} == refs["secrets"]
    assert {n for n, _, _ in onboard.VARIABLES} == refs["vars"]
    doc = open(os.path.join(ROOT, "docs", "your-org.md"), encoding="utf-8").read()
    for name in refs["secrets"] | refs["vars"]:
        assert f"`{name}`" in doc, f"docs/your-org.md doesn't list {name}"
    assert "scripts/bb-onboard.py" in doc


def test_repo_variables():
    assert onboard.repo_variables("example.com", "abc123", "ghcr.io/o") == {
        "CHAINGUARD_ORG": "example.com", "CHAINGUARD_ORG_ID": "abc123", "BUILD_REGISTRY": "ghcr.io/o"}
    assert onboard.repo_variables("example.com", None, "")["CHAINGUARD_ORG_ID"] == "<org id>"
    assert onboard.variable_set("o/r", "X", "v") == ["gh", "variable", "set", "X", "--body", "v", "--repo", "o/r"]


def test_org_stub_validates_and_changes_nothing():
    stub = yaml.safe_load(onboard.org_stub("example.com"))
    bbcommon.validate_mapping(stub, "stub")
    assert stub == {"images": {}}


class FakeRun:
    """subprocess.run for chainctl and gh: logged in, admin, nothing in the org yet."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        out = ""
        if cmd[:3] == ["chainctl", "auth", "status"]:
            out = json.dumps({"valid": True, "audience": "https://console-api.enforce.dev",
                              "capabilities": {"example.com": "owner"}})
        elif cmd[:3] == ["chainctl", "auth", "token"]:
            out = "not-a-real-token\n"
        elif cmd[:4] == ["chainctl", "iam", "organizations", "list"]:
            out = json.dumps({"items": [{"name": "example.com", "id": "0123abcd"}]})
        elif cmd[:3] == ["chainctl", "iam", "identities"] or cmd[:3] == ["chainctl", "iam", "roles"]:
            out = json.dumps({"items": []})
        elif cmd[:2] == ["gh", "api"]:
            out = "true\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")


def test_dry_run_prints_every_step_and_changes_nothing(monkeypatch, capsys, tmp_path):
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    org_repos_calls = []
    monkeypatch.setattr(onboard.org_repos, "main", lambda argv: org_repos_calls.append(argv) or 0)
    monkeypatch.setattr(onboard, "ROOT", str(tmp_path))
    monkeypatch.setenv("RELEASE_PAT", "s3cret-value")
    monkeypatch.delenv("CGR_ACTIONS_ID", raising=False)
    assert onboard.main(["--org", "example.com", "--repo", "Example-Org/big-bang"]) == 0
    out = capsys.readouterr().out
    for n in range(1, 8):
        assert f"== {n}. " in out
    assert "chainctl iam identities create big-bang-cicd --parent example.com" in out
    assert "chainctl iam roles create entitlements-reader" in out
    assert "chainctl iam role-bindings create --identity '<identity id>' --role entitlements-reader" in out
    assert "gh variable set CHAINGUARD_ORG --body example.com --repo Example-Org/big-bang" in out
    assert "gh variable set CHAINGUARD_ORG_ID --body 0123abcd" in out
    assert "gh variable set BUILD_REGISTRY --body ghcr.io/example-org" in out
    assert "gh secret set RELEASE_PAT" in out
    assert "python scripts/bb-run.py --org example.com --build-registry ghcr.io/example-org --save" in out
    assert "s3cret-value" not in out and "not-a-real-token" not in out
    # Only read-only commands ran: no create, set or binding.
    for cmd in fake.calls:
        assert not ({"create", "set"} & set(cmd)), cmd
    assert org_repos_calls == [["--org", "example.com", "--quota"], ["--org", "example.com", "--build-bases"]]
    assert not os.path.exists(tmp_path / "orgs" / "example.com.yaml")


def test_apply_yes_runs_the_changes_and_passes_secrets_on_stdin(monkeypatch, capsys, tmp_path):
    fake = FakeRun()
    stdin = {}

    def run(cmd, **kw):
        if kw.get("input"):
            stdin[cmd[3]] = kw["input"]
        p = fake(cmd, **kw)
        if cmd[:4] == ["chainctl", "iam", "identities", "create"]:
            p.stdout = "id-of-new-identity\n"
        return p
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(onboard.org_repos, "main", lambda argv: 0)
    monkeypatch.setattr(onboard, "ROOT", str(tmp_path))
    os.makedirs(tmp_path / "orgs")
    monkeypatch.setenv("RELEASE_PAT", "s3cret-value")
    monkeypatch.delenv("CGR_ACTIONS_ID", raising=False)
    assert onboard.main(["--org", "example.com", "--repo", "o/r", "--apply", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "s3cret-value" not in out and "s3cret-value" not in str(fake.calls)
    assert stdin == {"RELEASE_PAT": "s3cret-value"}
    ran = [c[:4] for c in fake.calls]
    assert ["chainctl", "iam", "identities", "create"] in ran and ["chainctl", "iam", "roles", "create"] in ran
    assert ["chainctl", "iam", "role-bindings", "create", "--identity", "id-of-new-identity",
            "--role", "entitlements-reader", "--parent", "example.com", "--yes"] in fake.calls
    assert "CGR_ACTIONS_ID = id-of-new-identity" in out
    bbcommon.validate_mapping(yaml.safe_load(open(tmp_path / "orgs" / "example.com.yaml", encoding="utf-8")))


def test_bad_arguments_are_refused(capsys):
    for argv in (["--org", "example.com", "--repo", "noslash"],
                 ["--org", "../x", "--repo", "o/r"],
                 ["--org", "example.com", "--repo", "o/r", "--build-registry", "cgr.dev/x"]):
        try:
            onboard.main(argv)
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError(argv)
