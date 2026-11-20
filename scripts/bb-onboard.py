#!/usr/bin/env python3
"""Walk a new Chainguard org and a copy of this repository through setup, step by step.

  bb-onboard.py --org <org domain> --repo OWNER/REPO                # dry run: print every step
  bb-onboard.py --org <org domain> --repo OWNER/REPO --apply        # do it, asking before each change
  bb-onboard.py --org <org domain> --repo OWNER/REPO --apply --yes  # do it without asking
      [--build-registry ghcr.io/<owner>] [--identity-name <name>]

Steps (docs/your-org.md is the same list, for reading):
  1. tools and logins: chainctl logged in (and able to get a cgr.dev token), gh logged in
     with admin on OWNER/REPO; docker is optional (only for building images locally)
  2. entitlements: bb-org-repos.py --quota (plan, per-tier room, what's still needed)
  3. the org repositories this repo needs: bb-org-repos.py --build-bases (--apply creates
     and syncs them)
  4. CI identity: an assumable chainctl identity trusted for GitHub Actions on OWNER/REPO's
     main branch and pull requests, bound to registry.pull and to a custom role
     entitlements-reader (registry.entitlements.list only, for the quota report). Its id is
     the CGR_ACTIONS_ID secret
  5. GitHub repository variables CHAINGUARD_ORG, CHAINGUARD_ORG_ID, BUILD_REGISTRY
     (gh variable set), and the secrets the operator sets (printed; a secret is set only
     when its value is in this process's environment, e.g. RELEASE_PAT=... and --apply)
  6. orgs/<org>.yaml: a commented stub for the org's own exceptions, if there is none
  7. the next command: bb-run.py, which regenerates the overlays for the org

The dry run changes nothing: it runs only read-only queries (logins, permissions, the
quota report, which repositories and identities exist) and prints each command --apply
would run. Read-only queries that fail (not logged in, say) are reported, and the step's
commands are printed with placeholders.
"""
import argparse
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# bb-org-repos.py has a hyphen, so it is loaded by path; its main(argv) does the quota
# report and the create/sync loop, so onboarding stays in step with it.
_spec = importlib.util.spec_from_file_location("bb_org_repos", os.path.join(HERE, "bb-org-repos.py"))
org_repos = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(org_repos)

GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
READER_ROLE = "entitlements-reader"
READER_CAPABILITY = "registry.entitlements.list"

# Every secret and variable .github/workflows/*.yml reads (tests/test_onboard.py checks this
# list against the workflows, so a new one can't be missed here or in docs/your-org.md).
# (name, required | recommended | optional, what it does)
PRIVATE_SETTINGS = [
    ("CGR_ACTIONS_ID", "required",
     "id of the Chainguard identity CI assumes (step 4): every workflow that pulls from the org"),
    ("BOT_APP_PRIVATE_KEY", "optional",
     "private key of a GitHub App (with BOT_APP_CLIENT_ID) the nightly bot pushes and merges as; "
     "or use RELEASE_PAT"),
    ("RELEASE_PAT", "optional",
     "a token the nightly bot uses instead of the App; without either, CI doesn't run on its PRs "
     "and automerge.yml doesn't merge"),
    ("REGISTRY1_USERNAME", "optional",
     "Iron Bank login for the kind/RKE2 tests and the CVE report; without it, tests that need an "
     "Iron Bank image skip"),
    ("REGISTRY1_PASSWORD", "optional", "the password or CLI secret for REGISTRY1_USERNAME"),
]
VARIABLES = [
    ("CHAINGUARD_ORG", "required", "the Chainguard org (unless the CHAINGUARD_ORG file names it)"),
    ("CHAINGUARD_ORG_ID", "recommended",
     "the org's id: the signature check then requires Chainguard's signature under this org"),
    ("BUILD_REGISTRY", "recommended",
     "where builds.yml pushes the images this repo builds (e.g. ghcr.io/<owner>); empty keeps "
     "them on Iron Bank"),
    ("BOT_APP_CLIENT_ID", "optional", "the GitHub App's client id (with the BOT_APP_PRIVATE_KEY secret)"),
    ("SIGN_IMAGES", "optional", "`keyless`: sign and attest the images builds.yml pushes (cosign keyless)"),
    ("BUILD_SIGNER", "optional",
     "owner/repo whose builds.yml signs the built images (default: this repository)"),
    ("AUTOMERGE_BIGBANG_PATCHES", "optional", "`true`: a Big Bang patch release merges itself when green"),
    ("ALERT_MENTION", "optional", "who automerge.yml mentions when a bot PR needs a person (e.g. @login)"),
    ("RKE2_RUNNER", "optional", "a larger runner's label for rke2.yml (16 GB or more)"),
]

ORG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def subject_pattern(owner, repo):
    """The OIDC subject GitHub Actions presents for OWNER/REPO's main branch and its pull
    requests. GitHub may add the owner's and repo's numeric ids (`@123`) to the subject
    (immutable subject claims), so both forms match. A fork's pull request gets no OIDC
    token, so `pull_request` here means this repository's own branches."""
    def lit(s):
        return s.replace(".", r"\.")  # owner/repo names are [A-Za-z0-9._-]; only . is special
    return rf"^repo:{lit(owner)}(@\d+)?/{lit(repo)}(@\d+)?:(ref:refs/heads/main|pull_request)$"


def identity_create(org, owner, repo, name):
    """`chainctl iam identities create` with an issuer and a subject pattern. The `github`
    subcommand takes one --github-ref, so it can't trust main and pull requests at once."""
    return ["chainctl", "iam", "identities", "create", name, "--parent", org,
            "--identity-issuer", GITHUB_ISSUER, "--subject-pattern", subject_pattern(owner, repo),
            "--role", "registry.pull", "--description", f"{owner}/{repo} CI: registry pull and tag listing",
            "--output", "id", "--yes"]


def role_create(org):
    return ["chainctl", "iam", "roles", "create", READER_ROLE, "--parent", org,
            "--capabilities", READER_CAPABILITY,
            "--description", "Read the org's image entitlements and quotas (bb-org-repos.py --quota)", "--yes"]


def role_binding(org, identity, role):
    return ["chainctl", "iam", "role-bindings", "create", "--identity", identity, "--role", role,
            "--parent", org, "--yes"]


def repo_variables(org, org_id, registry):
    """{name: value} for gh variable set. An unknown org id stays a placeholder."""
    return {"CHAINGUARD_ORG": org, "CHAINGUARD_ORG_ID": org_id or "<org id>", "BUILD_REGISTRY": registry}


def variable_set(repo, name, value):
    return ["gh", "variable", "set", name, "--body", value, "--repo", repo]


def org_stub(org):
    """orgs/<org>.yaml for a new org: nothing changed, every option shown commented out.
    bbcommon.load_mapping validates it like mapping.yaml."""
    return f"""# {org}'s own choices. bb-convert.py and bb-verify.py apply this over mapping.yaml when
# CHAINGUARD_ORG is {org}: each entry under `images` replaces mapping.yaml's entry for
# that Iron Bank image. Keep it empty unless the org needs an exception; anything true for
# every org belongs in mapping.yaml.
#
# Examples (the keys are mapping.yaml's image options, docs/how-it-works.md):
#   ironbank/opensource/minio/minio: {{stays: "MinIO not licensed here"}}   # keep on Iron Bank
#   ironbank/neuvector/neuvector/manager: {{cg: neuvector-manager, fips: null}}  # no FIPS twin
images: {{}}
"""


def show(cmd):
    return shlex.join(cmd)


def run(cmd, timeout=120, input=None):
    """subprocess.run, captured; None when the tool isn't installed."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")


def json_of(text):
    """The JSON value in chainctl's output (it can print a notice before it)."""
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    return json.loads(text[min(starts):]) if starts else None


def items(text):
    data = json_of(text) or []
    return data.get("items", []) if isinstance(data, dict) else data


class Onboard:
    def __init__(self, org, repo, registry, identity_name, apply=False, yes=False):
        self.org, self.repo = org, repo
        self.owner, self.name = repo.split("/")
        self.registry = registry
        self.identity_name = identity_name
        self.apply, self.yes = apply, yes
        self.problems = []
        self.org_id = None
        self.identity_id = None

    # --- output and changes ---

    def step(self, n, title):
        print(f"\n== {n}. {title}", flush=True)

    def note(self, text):
        print(f"   {text}")

    def problem(self, text):
        self.problems.append(text)
        print(f"   PROBLEM: {text}")

    def confirm(self, what):
        if self.yes:
            return True
        try:
            return input(f"   {what}? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:  # no terminal: never assume yes
            return False

    def change(self, cmd, what, shown=None, stdin=None):
        """Print an outward change; with --apply (and a yes), run it. The process or None.
        stdin carries a secret's value, so it never appears in argv or the output."""
        print(f"   $ {shown or show(cmd)}")
        if not self.apply:
            return None
        if not self.confirm(what):
            self.note("skipped")
            return None
        p = run(cmd, timeout=600, input=stdin)
        if p is None or p.returncode:
            self.problem(f"{what} failed: {(p.stderr.strip().splitlines() or ['?'])[-1] if p else 'not installed'}")
            return None
        return p

    # --- steps ---

    def tools(self):
        self.step(1, "tools and logins")
        p = run(["chainctl", "auth", "status", "-o", "json"])
        if p is None:
            self.problem("chainctl is not installed (https://edu.chainguard.dev/chainguard/chainctl/)")
        else:
            status = (json_of(p.stdout) if p.returncode == 0 else None) or {}
            if not status.get("valid"):
                self.problem("chainctl is not logged in: chainctl auth login")
            else:
                caps = status.get("capabilities") or {}
                self.note(f"chainctl: logged in (token audience {status.get('audience', '?')})")
                if self.org not in caps:
                    self.problem(f"this chainctl login has no role in {self.org}")
                else:
                    self.note(f"chainctl: role in {self.org}: {caps[self.org]}")
                # Pulls and tag listing use a cgr.dev token; the token itself is discarded.
                t = run(["chainctl", "auth", "token", "--audience", "cgr.dev"], timeout=60)
                if t is None or t.returncode or not t.stdout.strip():
                    self.problem("no cgr.dev token: chainctl auth login --audience cgr.dev")
                else:
                    self.note("chainctl: can get a cgr.dev registry token")
        p = run(["gh", "auth", "status"])
        if p is None:
            self.problem("gh is not installed (https://cli.github.com)")
        elif p.returncode:
            self.problem("gh is not logged in: gh auth login")
        else:
            perm = run(["gh", "api", f"repos/{self.repo}", "--jq", ".permissions.admin"])
            if perm is None or perm.returncode:
                self.problem(f"gh can't read {self.repo} (does it exist? is this login a member?)")
            elif perm.stdout.strip() != "true":
                self.problem(f"this gh login can't administer {self.repo}: variables and secrets need admin")
            else:
                self.note(f"gh: logged in, admin on {self.repo}")
        self.note("docker: " + ("installed" if shutil.which("docker") else
                                "not installed (optional: only to build this repo's images locally)"))

    def entitlements(self):
        self.step(2, "entitlements and quota")
        self.note(f"$ python scripts/bb-org-repos.py --org {self.org} --quota")
        self._org_repos(["--org", self.org, "--quota"], "the quota report")

    def repositories(self):
        self.step(3, "org repositories this repo needs")
        args = ["--org", self.org, "--build-bases"]
        self.note(f"$ python scripts/bb-org-repos.py {' '.join(args)}" + (" --apply" if self.apply else ""))
        if self.apply and not self.confirm(f"create and sync the missing repositories in {self.org}"):
            self.note("skipped; printing the commands instead")
            self._org_repos(args, "the repository listing")
            return
        self._org_repos(args + (["--apply"] if self.apply else []), "creating the repositories")

    def _org_repos(self, args, what):
        try:
            rc = org_repos.main(args)
        except (RuntimeError, SystemExit, OSError) as e:
            self.problem(f"{what} failed: {e}")
            return
        if rc:
            self.problem(f"{what}: something needs Chainguard (see above; docs/your-org.md, entitlements)")

    def identity(self):
        self.step(4, "CI identity (the CGR_ACTIONS_ID secret)")
        name = self.identity_name
        self.note(f"trusts {GITHUB_ISSUER}, subject {subject_pattern(self.owner, self.name)}")
        found = run(["chainctl", "iam", "identities", "list", "--parent", self.org, "--name", name, "-o", "json"])
        existing = [i for i in items(found.stdout) if i.get("name") == name] if found and found.returncode == 0 else []
        if existing:
            self.identity_id = existing[0].get("id")
            pattern = (existing[0].get("claimMatch") or {}).get("subjectPattern")
            self.note(f"identity {name} exists: {self.identity_id}")
            if pattern != subject_pattern(self.owner, self.name):
                self.note(f"its subject pattern is {pattern!r}; check it trusts {self.repo}'s main and PRs")
        else:
            p = self.change(identity_create(self.org, self.owner, self.name, name),
                            f"create identity {name} in {self.org} (bound to registry.pull)")
            if p:
                self.identity_id = (p.stdout.strip().splitlines() or [None])[-1]
        roles = run(["chainctl", "iam", "roles", "list", "--parent", self.org, "--name", READER_ROLE, "-o", "json"])
        if roles and roles.returncode == 0 and any(r.get("name") == READER_ROLE for r in items(roles.stdout)):
            self.note(f"role {READER_ROLE} exists")
        else:
            self.change(role_create(self.org), f"create role {READER_ROLE} ({READER_CAPABILITY} only)")
        # A binding that already exists fails harmlessly; chainctl has no "ensure".
        self.change(role_binding(self.org, self.identity_id or "<identity id>", READER_ROLE),
                    f"bind {name} to {READER_ROLE}")
        if self.identity_id:
            self.note(f"CGR_ACTIONS_ID = {self.identity_id}")

    def github(self):
        self.step(5, f"GitHub settings on {self.repo}")
        p = run(["chainctl", "iam", "organizations", "list", "-o", "json"])
        if p and p.returncode == 0:
            self.org_id = next((o.get("id") for o in items(p.stdout) if o.get("name") == self.org), None)
        if not self.org_id:
            self.note(f"org id unknown: chainctl iam organizations list (the id of {self.org})")
        for name, value in repo_variables(self.org, self.org_id, self.registry).items():
            if value.startswith("<"):
                print(f"   $ {show(variable_set(self.repo, name, value))}   # fill in, then run")
                continue
            self.change(variable_set(self.repo, name, value), f"set repository variable {name}")
        self.note("secrets (set them yourself; never paste a value into a shared terminal or log):")
        for name, need, what in PRIVATE_SETTINGS:
            self.note(f"  {name:20} {need:9} {what}")
        for name, _, _ in PRIVATE_SETTINGS:
            value = os.environ.get(name)
            if value:
                # The value goes in on stdin, never on the command line or in the output.
                self.change(["gh", "secret", "set", name, "--repo", self.repo],
                            f"set secret {name} from the environment",
                            shown=f"gh secret set {name} --repo {self.repo} <<< \"${name}\"", stdin=value)
        if not os.environ.get("CGR_ACTIONS_ID"):
            # The identity's id (printed in step 4), not a credential: it only names the identity.
            self.note("$ " + show(["gh", "secret", "set", "CGR_ACTIONS_ID", "--repo", self.repo,
                                   "--body", self.identity_id or "<identity id from step 4>"]))
        self.note("variables for later (optional; docs/your-org.md):")
        set_here = repo_variables(self.org, self.org_id, self.registry)
        for name, _, what in VARIABLES:
            if name not in set_here:
                self.note(f"  {name:26} {what}")

    def stub(self):
        self.step(6, f"orgs/{self.org}.yaml")
        path = os.path.join(ROOT, "orgs", f"{self.org}.yaml")
        if os.path.exists(path):
            self.note("exists; nothing to do")
            return
        self.note(f"would write orgs/{self.org}.yaml (empty `images`, commented examples)")
        if self.apply and self.confirm(f"write orgs/{self.org}.yaml"):
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(org_stub(self.org))
            self.note("written")

    def next_command(self):
        self.step(7, "next")
        cmd = ["python", "scripts/bb-run.py", "--org", self.org]
        if self.registry:
            cmd += ["--build-registry", self.registry]
        print(f"   $ {show(cmd + ['--save'])}")
        self.note("fetches Big Bang, discovers its images, writes overlays/ for this org and verifies them;")
        self.note("commit the result on a branch and open a pull request: ci.yml checks it with the new identity.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org", required=True, help="Chainguard org domain (e.g. example.com)")
    ap.add_argument("--repo", required=True, help="OWNER/REPO: your copy of this repository on GitHub")
    ap.add_argument("--build-registry", help="BUILD_REGISTRY (default: ghcr.io/<owner, lowercased>)")
    ap.add_argument("--identity-name", help="the CI identity's name (default: <repo>-cicd)")
    ap.add_argument("--apply", action="store_true", help="make the changes (default: print them)")
    ap.add_argument("--yes", action="store_true", help="with --apply: don't ask before each change")
    a = ap.parse_args(argv)
    org = a.org.removeprefix("cgr.dev/").strip("/")
    owner, _, name = a.repo.partition("/")
    if not ORG_RE.match(org):
        ap.error(f"--org {a.org!r}: expected an org domain such as example.com")
    if not (OWNER_RE.match(owner) and REPO_RE.match(name or "")):
        ap.error(f"--repo {a.repo!r}: expected OWNER/REPO")
    registry = (a.build_registry or f"ghcr.io/{owner.lower()}").rstrip("/")
    if registry.split("/")[0] == "cgr.dev":
        ap.error("--build-registry can't be on cgr.dev: Chainguard registries don't accept pushes")
    ob = Onboard(org, a.repo, registry, a.identity_name or f"{name}-cicd", apply=a.apply, yes=a.yes)
    print(f"Onboarding {org} for {a.repo} ({'applying' if a.apply else 'dry run: nothing is changed'})")
    for step in (ob.tools, ob.entitlements, ob.repositories, ob.identity, ob.github, ob.stub, ob.next_command):
        step()
    if ob.problems:
        print(f"\n{len(ob.problems)} problem(s):\n  " + "\n  ".join(ob.problems))
        return 1
    print("\nNo problems found." + ("" if a.apply else " Run again with --apply to make the changes."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
