#!/usr/bin/env python3
"""Install Big Bang on Chainguard images with one command, and look after it.

  bb-install.py init [--site my-site.yaml] [--domain example.com] [--enable a,b]
                     write a starter site file (no credentials in it)
  bb-install.py install [--site my-site.yaml] [--fips]
                     preflight checks (including a pull of each image built here), pull
                     secrets, Flux, SeaweedFS (random S3 keys), Big Bang with the overlays in
                     the right order, then wait and report; writes the site file first (as
                     init does) when it doesn't exist
  bb-install.py status
                     HelmReleases, pods that aren't healthy (with their last log lines),
                     and running images that aren't the conversion's
  bb-install.py install ... --plan
                     what an install would change (version, FIPS, packages, image tags)
  bb-install.py rollback [--to N] [--yes]
                     the bigbang release back to an earlier revision, then wait and report
  bb-install.py uninstall [--delete-data] [--keep-flux] [--yes]
                     Big Bang, SeaweedFS (its volume and S3 keys stay unless
                     --delete-data), Flux

`install` is safe to re-run: it is also how you upgrade, switch to FIPS (--fips) or
back, and apply a changed site file. Big Bang's chart comes from repo1's public git archive
(no Iron Bank login); --chart takes a local chart or an oci:// ref instead. It deploys what this checkout's overlays were
generated for (the org and object storage are read from their headers), so regenerate
them first (bb-run.py) if you changed mapping.yaml or the org.

Credentials never go in the site file or on a command line, and are never printed. Each
is taken from the first of: the environment, the site file's registryCredentials, the
cluster (a previous run keeps them in the bigbang/bb-install Secret), or a hidden prompt.

  CGR_USERNAME, CGR_PASSWORD     cgr.dev pull token. Without one, and logged in to chainctl,
                                 `install` makes a one-year token itself (asks first unless
                                 --yes); --new-pull-token replaces the stored one
  BUILD_REGISTRY_USERNAME, BUILD_REGISTRY_PASSWORD (or BUILD_REGISTRY_TOKEN alone)
                                 only if the images this repo builds are private
  REGISTRY1_USERNAME, REGISTRY1_PASSWORD
                                 only if an enabled package keeps an Iron Bank image
  S3_ACCESS_KEY, S3_SECRET_KEY   only with an external S3 (OBJECT_STORAGE_ENDPOINT);
                                 for the in-cluster SeaweedFS they are generated
"""
import argparse
import atexit
import getpass
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is missing: pip install pyyaml")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import objstore  # noqa: E402
from bbcluster import (apply_secret, dockerconfig, hr_namespaces, install_flux, kubectl, read_secret,  # noqa: E402
                       report, sh, wait_ready)
from bbcommon import (BUILD_SIGNATURE_ISSUER, ROOT, SIGNATURE_ISSUERS, build_signature_identity,  # noqa: E402
                       build_signer, deep_merge, load_mapping, running_image_problems, signature_identity,
                       split_ref)
from orgtags import REGISTRY  # noqa: E402

NAMESPACE = "bigbang"
STATE_SECRET = "bb-install"            # credentials and install mode, kept in the cluster
TOKEN_USER = "token"                    # sent with a token alone; the pull test shows if the registry takes it
TRIES = 3                               # logins typed at the prompt, each tested by a pull
# registry1's OCI copy of the chart needs an Iron Bank login; repo1's git archive doesn't.
CHART_ARCHIVE = "https://repo1.dso.mil/big-bang/bigbang/-/archive/{v}/bigbang-{v}.tar.gz?path=chart"
CHART_CACHE = os.path.join(ROOT, "work", "charts")
MIN_KUBERNETES = (1, 34)               # Big Bang's chart requires it
PULL_TOKEN_DAYS = 365                  # chainctl's longest pull token (8760h)


def say(msg):
    print(msg, flush=True)


def step(title):
    say(f"\n== {title}")


def confirm(question, default):
    answer = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not answer else answer.startswith("y")


# ---------------------------------------------------------------- what to deploy

class Checkout:
    """The overlays, Flux and SeaweedFS files this checkout (or --overlays DIR) holds."""

    def __init__(self, root):
        self.root = root
        std = os.path.join(root, "overlays", "values-chainguard.yaml")
        if not os.path.exists(std):
            raise SystemExit(f"{std} is missing: generate the overlays first (scripts/bb-run.py)")
        header = open(std, encoding="utf-8").read(800)
        m = re.search(r"Generated for (cgr\.dev/[^\s:]+)", header)
        self.registry = m.group(1) if m else None
        self.org = self.registry.split("/", 1)[1] if self.registry else None
        m = re.search(r"Big Bang (\d+\.\d+\.\d+)", header)
        self.version = m.group(1) if m else open(os.path.join(ROOT, "BIGBANG_VERSION")).read().strip()
        release = os.path.join(root, "RELEASE")    # in release archives (scripts/release.py)
        self.release = open(release, encoding="utf-8").read().strip() if os.path.exists(release) else None
        self.object_storage = os.path.join(root, "overlays", "values-object-storage.yaml")
        self.s3 = None
        if os.path.exists(self.object_storage):
            head = open(self.object_storage, encoding="utf-8").read(600)
            m = re.search(r"S3 object storage at (\S+) instead of MinIO, for: ([^.\n]+)", head)
            if m:
                self.s3 = {"endpoint": m.group(1), "packages": [p.strip() for p in m.group(2).split(",")]}

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def overlays(self, fips):
        files = [self.path("overlays", "values-chainguard.yaml")]
        if fips:
            files.append(self.path("overlays", "values-chainguard-fips.yaml"))
        if self.s3:
            files.append(self.object_storage)
        return files

    def in_cluster_seaweedfs(self):
        return bool(self.s3) and self.s3["endpoint"] == objstore.DEFAULT_ENDPOINT


REPO1 = "https://repo1.dso.mil/"


def chart_ref(a):
    """(chart, extra helm args): --chart as given, else Big Bang's chart from the git
    mirror (--git-mirror) or repo1."""
    if a.chart and a.chart.startswith("oci://"):
        return a.chart, ["--version", a.version]
    mirror = getattr(a, "git_mirror", None)
    return a.chart or (clone_chart(a.version, mirror) if mirror else fetch_chart(a.version)), []


def mirrored(url, mirror):
    """A repo1 URL on the mirror: same path under the mirror's prefix."""
    return mirror.rstrip("/") + "/" + url[len(REPO1):] if mirror and url.startswith(REPO1) else url


def git_mirror_values(defaults, mirror):
    """Every package's git.repo moved from repo1 to the mirror (Big Bang's packages come
    from repo1 git at runtime; a mirror the site controls keeps them coming when repo1
    doesn't answer, and is how a disconnected site gets them at all)."""
    out = {}
    for key, v in defaults.items():
        if isinstance(v, dict) and str((v.get("git") or {}).get("repo", "")).startswith(REPO1):
            out[key] = {"git": {"repo": mirrored(v["git"]["repo"], mirror)}}
    for key, v in (defaults.get("addons") or {}).items():
        if isinstance(v, dict) and str((v.get("git") or {}).get("repo", "")).startswith(REPO1):
            out.setdefault("addons", {})[key] = {"git": {"repo": mirrored(v["git"]["repo"], mirror)}}
    return out


def clone_chart(version, mirror, cache=CHART_CACHE):
    """Big Bang's chart at <version> from the git mirror (git clone of the tag)."""
    dest = os.path.join(cache, f"bigbang-{version}-mirror")
    chart = os.path.join(dest, "chart")
    if os.path.exists(os.path.join(chart, "Chart.yaml")):
        return chart
    shutil.rmtree(dest, ignore_errors=True)
    url = mirrored(BIGBANG_GIT, mirror)
    if subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch", version, url, dest]).returncode:
        raise SystemExit(f"could not clone Big Bang {version} from {url}")
    return chart


def fetch_chart(version, cache=CHART_CACHE):
    """Big Bang's chart at <version> from repo1's public git archive, kept in work/charts."""
    dest = os.path.join(cache, f"bigbang-{version}")
    chart = os.path.join(dest, "chart")
    if os.path.exists(os.path.join(chart, "Chart.yaml")):
        return chart
    say(f"downloading Big Bang {version}'s chart from repo1.dso.mil")
    error = None
    for attempt in range(3):   # repo1 intermittently refuses connections
        try:
            with urllib.request.urlopen(CHART_ARCHIVE.format(v=version), timeout=120) as r:
                data = r.read()
            break
        except OSError as e:
            error = e
            time.sleep(5 * (attempt + 1))
    else:
        raise SystemExit(f"could not download Big Bang {version}'s chart from repo1.dso.mil ({error}); "
                         "pass --chart <path to bigbang/chart> or --chart oci://registry1.dso.mil/bigbang/bigbang")
    part = dest + ".part"
    shutil.rmtree(part, ignore_errors=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        for m in t.getmembers():
            rel = m.name.split("/")[1:]   # drop the archive's top directory
            if not m.isfile() or not rel or ".." in rel or m.name.startswith("/"):
                continue
            out = os.path.join(part, *rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as fh:
                fh.write(t.extractfile(m).read())
    got = (yaml.safe_load(open(os.path.join(part, "chart", "Chart.yaml"), encoding="utf-8")) or {}).get("version")
    if str(got) != version:
        raise SystemExit(f"repo1's archive for {version} holds chart version {got}")
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(part, dest)
    return chart


def mirrored_checkout(co, mirror):
    """A copy of the overlays, Flux and SeaweedFS files with cgr.dev/<org> and the build
    registry replaced by the site's registry prefix (an air-gapped site, bb-airgap.py).
    Headers ("Generated for cgr.dev/<org>:") keep the org, so the rest reads the same."""
    d = tempfile.mkdtemp(prefix="bb-install-mirror-")
    atexit.register(shutil.rmtree, d, True)
    hosts = [f"{REGISTRY}/{co.org}"] + ([_build_registry(co)] if _build_registry(co) else [])
    rx = re.compile("(" + "|".join(re.escape(h) for h in hosts) + r")(?=/|['\"]?\s*$)", re.M)
    for name in ("overlays", "flux", "flux-fips", "seaweedfs", "seaweedfs-fips", "RELEASE"):
        src = co.path(name)
        files = [src] if os.path.isfile(src) else [os.path.join(dp, f) for dp, _, fn in os.walk(src) for f in fn]
        for s in files:
            t = os.path.join(d, os.path.relpath(s, co.root))
            os.makedirs(os.path.dirname(t), exist_ok=True)
            with open(t, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(rx.sub(mirror.rstrip("/"), open(s, encoding="utf-8").read()))
    return Checkout(d)


def chart(a):
    say(fetch_chart(a.version or Checkout(a.overlays).version))
    return 0


def chart_defaults(a):
    ref, version = chart_ref(a)
    p = subprocess.run(["helm", "show", "values", ref, *version], capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"could not read Big Bang's chart ({ref} {' '.join(version)}):\n{p.stderr[-800:]}\n"
                         "Use --chart <path to bigbang/chart> (scripts/bb-fetch.py clones it to work/bigbang).")
    return yaml.safe_load(p.stdout) or {}


def enabled_packages(values):
    """Big Bang keys that are on ('auto' counts: metricsServer), e.g. ['istiod', 'addons.keycloak']."""
    keys = [k for k, v in values.items() if isinstance(v, dict) and v.get("enabled") not in (False, None)
            and k not in ("addons", "networkPolicies")]
    keys += [f"addons.{k}" for k, v in (values.get("addons") or {}).items()
             if isinstance(v, dict) and v.get("enabled") not in (False, None)]
    return sorted(keys)


def merged_values(defaults, files):
    values = json.loads(json.dumps(defaults))
    for f in files:
        deep_merge(values, yaml.safe_load(open(f, encoding="utf-8")) or {})
    return values


def iron_bank_leftovers(enabled, org):
    """Iron Bank images the enabled packages still deploy (mapping `stays`), by package."""
    discovered = json.load(open(os.path.join(ROOT, "discovery", "images.json"), encoding="utf-8"))
    images = load_mapping(org)["images"]
    out = {}
    for info in discovered["packages"].values():
        if info.get("key") not in enabled:
            continue
        for ref in info.get("rendered", []):
            repo, _ = split_ref(ref)
            m = images.get(repo)
            if isinstance(m, dict) and "stays" in m:
                out.setdefault(info["key"], []).append(repo)
    return out


# ---------------------------------------------------------------- credentials

class Credentials:
    """Registry and S3 credentials, from the environment, the site file, the cluster's
    bb-install Secret, or a hidden prompt. Values are never printed."""

    def __init__(self, site, interactive):
        self.site = {r.get("registry"): r for r in site.get("registryCredentials") or [] if isinstance(r, dict)}
        self.stored = read_secret(NAMESPACE, STATE_SECRET) or {}
        self.interactive = interactive
        self.values = dict(self.stored)

    def registry(self, key, registry, env_user, env_pass, why, required=True, make=None, check=None,
                 env_token=None):
        """(username, password) for a registry, or None when it's optional and not given.
        `make` creates credentials when none are found (a chainctl pull token).

        `check(login)` says why a login (or None: no login) can't pull, else None. A login
        from the environment, the site file or the cluster that fails it stops the install,
        unless a prompt can take another; a typed one is asked for again, TRIES times.

        `env_token` names a variable holding a token alone (a registry such as ghcr.io that
        needs no particular username); at the prompt, Enter for the username does the same."""
        user, pw, source = os.environ.get(env_user), os.environ.get(env_pass), "environment"
        if env_token and not pw and os.environ.get(env_token):
            user, pw = user or TOKEN_USER, os.environ[env_token]
        if not (user and pw) and registry in self.site:
            user, pw, source = self.site[registry].get("username"), self.site[registry].get("password"), "site file"
        if user and pw:
            self.values.pop(f"{key}-created", None)   # not a token this installer made
        else:
            user, pw = self.stored.get(f"{key}-username"), self.stored.get(f"{key}-password")
            source = "stored (an earlier run)"
        if not (user and pw) and make:
            user, pw = make() or (None, None)
            source = "new pull token"
            if user and pw:
                self.values[f"{key}-created"] = time.strftime("%Y-%m-%d")
        login = (user, pw) if user and pw else None
        problem = check(login) if check and (login or not required) else None
        if problem:
            replace = f"set {env_user} and {env_pass}" + (f" (or {env_token})" if env_token else "")
            if not self.interactive:
                raise SystemExit(f"{registry}: {'the ' + source + ' login' if login else 'without a login'} "
                                 f"can't pull: {problem}\n{replace} to one that can ({why})")
            say(f"{registry}: {'the ' + source + ' login' if login else 'without a login'} can't pull: {problem}")
            login = None
        # Asked unless an optional registry was just shown to work without a login.
        if not login and self.interactive and (problem or required or not check):
            login = self._ask(registry, why, required, check, token_ok=bool(env_token))
        if not login:
            if required:
                raise SystemExit(f"no {registry} credentials: set {env_user} and {env_pass} ({why})")
            return None
        self.values[f"{key}-username"], self.values[f"{key}-password"] = login
        return login

    def _ask(self, registry, why, required, check, token_ok):
        """A login typed at a hidden prompt and, with `check`, tested; asked again if it fails."""
        say(f"{registry}: {why}")
        for left in range(TRIES - 1, -1, -1):
            pw = getpass.getpass(f"  {registry} {'token or ' if token_ok else ''}password"
                                 f"{'' if required else ' (Enter to skip)'}: ").strip()
            if not pw:
                if required:
                    say("  a password is needed")
                    continue
                problem = check(None) if check else None
                if not problem:
                    return None
                say(f"  without a login: {problem}")
            else:
                user = input(f"  {registry} username{' (Enter for the token alone)' if token_ok else ''}: ").strip()
                user = user or (TOKEN_USER if token_ok else "")
                if not user:
                    say("  a username is needed")
                    continue
                problem = check((user, pw)) if check else None
                if not problem:
                    say("  login works")
                    return user, pw
                say(f"  that login can't pull: {problem}")
            if left:
                say(f"  try again ({left} {'try' if left == 1 else 'tries'} left)")
        raise SystemExit(f"{registry}: no login that can pull after {TRIES} tries")

    def s3(self, generate):
        access = os.environ.get("S3_ACCESS_KEY") or self.stored.get("s3-access-key")
        secret = os.environ.get("S3_SECRET_KEY") or self.stored.get("s3-secret-key")
        if not (access and secret):
            if generate:
                # Kept in the cluster and reused on every run: SeaweedFS's data stays readable.
                access, secret = "bb" + secrets.token_hex(10), secrets.token_urlsafe(32)
            elif self.interactive:
                access = input("  S3 access key: ").strip()
                secret = getpass.getpass("  S3 secret key: ").strip()
            if not (access and secret):
                raise SystemExit("no S3 keys for the external object storage: set S3_ACCESS_KEY and S3_SECRET_KEY")
        self.values["s3-access-key"], self.values["s3-secret-key"] = access, secret
        return access, secret

    def forget(self, key):
        """Drop stored credentials for `key` (to replace them)."""
        for k in [k for k in self.stored if k.startswith(f"{key}-")]:
            self.stored.pop(k)
            self.values.pop(k, None)

    def generated(self, key, make):
        """A value generated once and kept (e.g. Keycloak's admin password)."""
        if not self.values.get(key):
            self.values[key] = make()
        return self.values[key]

    def save(self, mode):
        self.values["mode"] = mode
        with tempfile.TemporaryDirectory(prefix="bb-install-") as d:
            args = []
            for k, v in sorted(self.values.items()):
                f = os.path.join(d, k)
                with open(f, "w", encoding="utf-8") as fh:
                    fh.write(v)
                args.append(f"--from-file={k}={f}")
            apply_secret(NAMESPACE, STATE_SECRET, "generic", *args)


def chainctl_logged_in():
    if not shutil.which("chainctl"):
        return False
    p = subprocess.run(["chainctl", "auth", "status", "-o", "json"], capture_output=True, text=True)
    try:
        return p.returncode == 0 and json.loads(p.stdout).get("valid") is True
    except ValueError:
        return False


def chainctl_pull_token(org):
    """(username, password) of a new one-year cgr.dev pull token for <org>, made with the
    user's chainctl login. The output is captured, never printed."""
    p = subprocess.run(["chainctl", "auth", "pull-token", "create", "--repository=oci", f"--parent={org}",
                        f"--ttl={PULL_TOKEN_DAYS * 24}h", f"--name=bb-install-{time.strftime('%Y%m%d-%H%M%S')}",
                        "--description=Big Bang image pulls (bigbang-chainguard bb-install.py)", "-o", "json"],
                       capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"chainctl couldn't create a pull token for {org} (exit {p.returncode})")
    try:
        d = json.loads(p.stdout)
        return d["identity_id"], d["token"]
    except (ValueError, KeyError):
        raise SystemExit("chainctl's pull-token output wasn't the expected JSON; set CGR_USERNAME and CGR_PASSWORD")


def pull_token_maker(org, interactive, ask):
    """make= for Credentials.registry: a chainctl pull token, if chainctl is logged in and
    (when `ask`) the user agrees."""
    def make():
        if not chainctl_logged_in():
            return None
        if ask and not (interactive and confirm(f"No cgr.dev pull token given. Create a one-year token for {org} "
                                                "with your chainctl login?", default=True)):
            return None
        say("creating a cgr.dev pull token with chainctl (kept in the cluster, not printed)")
        return chainctl_pull_token(org)
    return make


def pull_token_note(state, today=None):
    """A line when the pull token this installer made expires within 30 days."""
    created = state.get("cgr-created")
    if not created:
        return ""
    try:
        start = time.mktime(time.strptime(created, "%Y-%m-%d"))
    except ValueError:
        return ""
    left = int((start + PULL_TOKEN_DAYS * 86400 - (today or time.time())) // 86400)
    if left > 30:
        return ""
    return (f"the cgr.dev pull token made on {created} {'expired' if left < 0 else f'expires in {left} days'}: "
            "re-run bb-install.py install --new-pull-token")


# ---------------------------------------------------------------- preflight

def preflight(a, co, site, defaults):
    """Return (enabled packages, problems, notes). Problems stop the install."""
    problems, notes = [], []
    for tool in ("kubectl", "helm"):
        if not shutil.which(tool):
            problems.append(f"{tool} is not installed")
    if problems:
        return [], problems, notes
    ver = kubectl("version", "-o", "json", check=False)
    server = (json.loads(ver).get("serverVersion") or {}) if ver else {}
    if not server:
        problems.append("can't reach the cluster (kubectl version): check your kubectl context")
    else:
        mm = (int(server.get("major", "0")), int(re.sub(r"\D", "", server.get("minor", "0")) or 0))
        if mm < MIN_KUBERNETES:
            problems.append(f"Kubernetes {mm[0]}.{mm[1]}: Big Bang {co.version} needs "
                            f"{MIN_KUBERNETES[0]}.{MIN_KUBERNETES[1]} or newer")
    if not co.org:
        problems.append("the overlays don't say which org they were generated for: regenerate them")
    if not site.get("domain"):
        problems.append("the site file has no `domain` (bb-install.py init writes one)")
    files = co.overlays(a.fips) + [a.site]
    enabled = enabled_packages(merged_values(defaults, files))
    on = set(enabled)
    for needs in ("prometheusOperatorCRDs", "certManager"):
        # prometheusOperatorCRDs installs the CRDs and creates the monitoring namespace;
        # monitoring's certificates need cert-manager. Both are on by default.
        if on & {"monitoring", "addons.thanos"} and needs not in on:
            problems.append(f"monitoring/Thanos need {needs}: leave it on in the site file")
    if co.in_cluster_seaweedfs() and server:
        scs = json.loads(kubectl("get", "storageclass", "-o", "json", check=False) or '{"items": []}')["items"]
        if not any(s["metadata"].get("annotations", {}).get("storageclass.kubernetes.io/is-default-class") == "true"
                   for s in scs):
            problems.append("no default StorageClass: SeaweedFS needs one for its volume")
    if co.org:
        left = iron_bank_leftovers(set(enabled), co.org)
        if left:
            notes.append("these enabled packages keep Iron Bank images, so registry1 credentials are needed: "
                         + ", ".join(f"{k} ({len(v)})" for k, v in sorted(left.items())))
    return enabled, problems, notes


# ---------------------------------------------------------------- install

def seaweedfs(co, fips, pull, s3):
    """Deploy the generated seaweedfs/ kustomization with its two Secrets; wait for buckets."""
    ns = objstore.NAMESPACE
    with tempfile.TemporaryDirectory(prefix="bb-install-") as d:
        cfg = os.path.join(d, "config.json")
        json.dump(dockerconfig(pull), open(cfg, "w"))
        apply_secret(ns, "private-registry", "generic", "--type=kubernetes.io/dockerconfigjson",
                     f"--from-file=.dockerconfigjson={cfg}")
        s3json = os.path.join(d, "s3.json")
        open(s3json, "w", encoding="utf-8").write(objstore.s3_config(*s3))
        apply_secret(ns, "seaweedfs-s3", "generic", f"--from-file=s3.json={s3json}")
    sh("kubectl", "apply", "-k", co.path("seaweedfs-fips" if fips else "seaweedfs"), capture=True)
    kubectl("-n", ns, "rollout", "status", "deploy/seaweedfs", "--timeout=600s")
    # The bucket Job is immutable once created: a re-run with new buckets replaces it.
    job = kubectl("-n", ns, "wait", "job/seaweedfs-buckets", "--for=condition=complete", "--timeout=600s",
                  check=False)
    if job is None or "condition met" not in job:
        logs = kubectl("-n", ns, "logs", "job/seaweedfs-buckets", "--tail=30", check=False) or ""
        raise SystemExit(f"SeaweedFS's bucket Job didn't complete:\n{logs}")


def object_storage_values(co, enabled, s3):
    """Credential values for the enabled packages that use S3, and only those (Big Bang
    creates Mimir's credential Secret whenever its keys are set, even with Mimir off)."""
    spec = objstore.load_spec()
    extra = {p for p in co.s3["packages"] if spec["packages"].get(p, {}).get("optional")}
    _, paths, _, _ = objstore.build(spec, objstore.tokens(co.s3["endpoint"], "us-east-1", "bigbang"), extra)
    names = {k.split(".")[-1] for k in enabled}
    return objstore.credentials_template(objstore.credentials_for(paths, names), *s3)


def install(a):
    co = Checkout(a.overlays)
    a.version = a.version or co.version
    if not os.path.exists(a.site):
        # A first install needs no separate `init`: ask for (or take --domain/--enable) and
        # write the site file, then carry on.
        say(f"{a.site} doesn't exist: writing it first (bb-install.py init)")
        init(argparse.Namespace(cmd="install", site=a.site, domain=a.domain, enable=a.enable, force=False, yes=a.yes))
    site = yaml.safe_load(open(a.site, encoding="utf-8")) or {}
    stored = read_secret(NAMESPACE, STATE_SECRET) or {}
    if a.git_mirror is None:   # remembered from an earlier run
        a.git_mirror = stored.get("git-mirror") or None
    if a.registry_mirror is None:
        a.registry_mirror = stored.get("registry-mirror") or None
    if a.registry_mirror:
        co = mirrored_checkout(co, a.registry_mirror)
    step("preflight")
    defaults = chart_defaults(a)
    enabled, problems, notes = preflight(a, co, site, defaults)
    say(f"Big Bang {a.version}{f' (release {co.release})' if co.release else ''}, images from {co.registry}, "
        f"{'FIPS' if a.fips else 'standard'}; "
        f"object storage: {'SeaweedFS in the cluster' if co.in_cluster_seaweedfs() else (co.s3 or {}).get('endpoint', 'MinIO (Big Bang)')}")
    say(f"packages: {', '.join(enabled)}")
    for n in notes:
        say(f"note: {n}")
    if problems:
        raise SystemExit("\nnot installing:\n  " + "\n  ".join(problems))
    if a.plan:
        return plan(a, co, site, defaults, enabled)
    interactive = sys.stdin.isatty() and not a.yes
    context = (kubectl("config", "current-context", check=False) or "").strip() or "(no context)"
    say(f"cluster: kubectl context {context}")
    if interactive and read_secret(NAMESPACE, STATE_SECRET) is None and \
            not confirm(f"Install Big Bang on {context}?", default=False):
        raise SystemExit("not installing (--yes skips this question)")

    step("credentials")
    creds = Credentials(site, interactive=interactive)
    # Each login is tested with a pull now: one the cluster can't use otherwise shows up as
    # ImagePullBackOff only when the wait times out.
    checks = not a.no_pull_check
    if a.git_mirror is not None:
        creds.values["git-mirror"] = a.git_mirror      # "" turns the mirror off
    a.git_mirror = a.git_mirror or None
    if a.git_mirror:
        say(f"package git repositories from {a.git_mirror} instead of repo1")
    if a.new_pull_token:
        creds.forget("cgr")
    if a.registry_mirror is not None:
        creds.values["registry-mirror"] = a.registry_mirror or ""
    if a.registry_mirror:
        # Air-gapped: every image comes from the site's registry (bb-airgap.py lists them).
        host = a.registry_mirror.split("/", 1)[0]
        say(f"images from {a.registry_mirror} instead of cgr.dev and the build registry")
        got = creds.registry("mirror", host, "MIRROR_USERNAME", "MIRROR_PASSWORD",
                             "the site registry holding the images; skip if it allows anonymous pulls",
                             required=False, env_token="MIRROR_TOKEN",
                             check=checks and pull_check(deployed_images(co, a.fips, a.registry_mirror)[:1]))
        registries = [(host, *got)] if got else []
        build = None
    else:
        cgr = creds.registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD",
                             "a Chainguard pull token (chainctl auth pull-token create --repository=oci "
                             f"--parent={co.org} --ttl=8760h)",
                             make=pull_token_maker(co.org, interactive, ask=not (a.yes or a.new_pull_token)),
                             check=checks and pull_check(deployed_images(co, a.fips, co.registry)[:1]))
        registries = [("cgr.dev", *cgr)]
        build = _build_registry_host(co)
    if build:
        # Every image: on ghcr.io each package has its own access list.
        got = creds.registry("build", build, "BUILD_REGISTRY_USERNAME", "BUILD_REGISTRY_PASSWORD",
                             "images this repo builds (on ghcr.io: a read-only bot account's read:packages "
                             "token); skip if they are public", required=False, env_token="BUILD_REGISTRY_TOKEN",
                             check=checks and pull_check(build_images(co, a.fips)))
        if got:
            registries.append((build, *got))
    if any("Iron Bank" in n for n in notes):
        got = creds.registry("registry1", "registry1.dso.mil", "REGISTRY1_USERNAME", "REGISTRY1_PASSWORD",
                             "Iron Bank images some enabled packages keep", required=False)
        if got:
            registries.append(("registry1.dso.mil", *got))
        else:
            say("warning: no registry1 credentials; the packages listed above won't pull their Iron Bank images")
    s3 = creds.s3(generate=co.in_cluster_seaweedfs()) if co.s3 else None
    keycloak_password = creds.generated("keycloak-admin-password", lambda: secrets.token_urlsafe(24))
    keycloak_db_password = keycloak_database_password(creds, site, enabled)
    tls = tls_choice(a, creds, site, enabled)
    signatures, org_id = signature_choice(a, creds, enabled)
    built = None if a.registry_mirror or signatures == "off" else built_signer(a, creds, co)
    creds.save("fips" if a.fips else "standard")
    say("stored in the cluster (Secret bigbang/bb-install); nothing printed")

    with tempfile.TemporaryDirectory(prefix="bb-install-") as d:
        cfg = os.path.join(d, "config.json")
        json.dump(dockerconfig(registries), open(cfg, "w"))
        step("Flux")
        apply_secret("flux-system", "private-registry", "generic", "--type=kubernetes.io/dockerconfigjson",
                     f"--from-file=.dockerconfigjson={cfg}")
        if not install_flux(co.path("flux-fips" if a.fips else "flux"), small_cluster=a.small_cluster):
            report(["flux-system"])
            raise SystemExit("Flux didn't become ready (pods above)")

        if co.in_cluster_seaweedfs():
            step("SeaweedFS")
            seaweedfs(co, a.fips, registries[:1] + [r for r in registries[1:] if r[0] != "registry1.dso.mil"], s3)
            say(f"ready at {co.s3['endpoint']}")

        step("Big Bang")
        secret_values = {"registryCredentials": [{"registry": r, "username": u, "password": p}
                                                 for r, u, p in registries]}
        if co.s3:
            deep_merge(secret_values, object_storage_values(co, enabled, s3))
        if "addons.keycloak" in enabled and not _site_sets_keycloak_password(site):
            # Big Bang's default admin password ("password") is too short for Keycloak's
            # FIPS mode (14+ characters); a random one works in both modes. Read it with:
            # kubectl -n bigbang get secret bb-install -o jsonpath='{.data.keycloak-admin-password}'
            deep_merge(secret_values, {"addons": {"keycloak": {"values": {"upstream": {"secrets": {"env": {
                "stringData": {"KEYCLOAK_ADMIN_PASSWORD": keycloak_password}}}}}}}})
        if keycloak_db_password:
            # FIPS also rejects Big Bang's default database password ("keycloak"): the
            # PostgreSQL driver's SCRAM hashing needs 112+ bits. Keycloak's side and the
            # bundled PostgreSQL's (hardcoded by Big Bang) must match.
            deep_merge(secret_values, {"addons": {"keycloak": {"values": {
                "upstream": {"database": {"password": keycloak_db_password}},
                "postgresql": {"global": {"postgresql": {"auth": {"password": keycloak_db_password}}}}}}}})
        if a.git_mirror:
            deep_merge(secret_values, git_mirror_values(defaults, a.git_mirror))
        if tls == "own":
            deep_merge(secret_values, {"istioGateway": {"values": {"gateways": {"public": {"gatewayCerts": [
                {"name": GATEWAY_CERT, "tls": {"cert": creds.values["tls-cert"], "key": creds.values["tls-key"]}}]}}}}})
        sv = os.path.join(d, "credentials.yaml")
        with open(sv, "w", encoding="utf-8") as fh:
            yaml.safe_dump(secret_values, fh)
        if a.gitops:
            # Credentials in a Secret the HelmRelease reads; everything else in files to commit.
            apply_secret(NAMESPACE, GITOPS_SECRET, "generic", f"--from-file=values.yaml={sv}")
            write_gitops(a.gitops, co, a.fips, a.site, a.git_mirror)
            sh("kubectl", "apply", "-k", a.gitops, capture=True)
            say(f"wrote {a.gitops} and applied it; commit it and let a Flux Kustomization keep it in sync "
                f"({a.gitops}/README.md)")
        else:
            ref, version = chart_ref(a)
            files = [x for f in co.overlays(a.fips) + [a.site] for x in ("-f", f)]
            sh("helm", "upgrade", "-i", "bigbang", ref, *version, "-n", NAMESPACE, "--create-namespace",
               *files, "-f", sv, "--timeout", "15m", capture=True)
        say(f"Big Bang applied ({len(enabled)} packages); Flux installs them now")

    if tls:
        step(f"TLS for *.{site['domain']}")
        gateway_tls(site["domain"], tls, creds.values.get("tls-issuer"), wait=900)
    if signatures != "off" or (creds.stored.get("signatures") or "off") != "off":
        step("image signatures")
        cluster_signatures(co.org, org_id, signatures, wait=900, built=built)

    if a.no_wait:
        return 0
    step("waiting for the packages")
    time.sleep(10)
    ok, problems = wait_ready(hr_namespaces(), a.timeout)
    return status_report(co, a.fips, problems) if ok else status_report(co, a.fips, problems, failed=True)


def keycloak_database_password(creds, site, enabled):
    """A generated password for Keycloak's bundled PostgreSQL, or None to leave it alone:
    Keycloak off, the site sets one (or an external database), or Keycloak already runs on
    Big Bang's default (PostgreSQL keeps the password it started with; changing it here
    would lock Keycloak out of its own database)."""
    if "addons.keycloak" not in enabled or _site_sets_keycloak_db_password(site):
        return None
    if creds.values.get("keycloak-db-password") or not _keycloak_installed():
        return creds.generated("keycloak-db-password", lambda: secrets.token_urlsafe(24))
    say("note: Keycloak already runs on Big Bang's default database password; FIPS needs 14+ "
        "characters, so change it in PostgreSQL and the site file before switching to --fips")
    return None


def _keycloak_values(site):
    return (((site.get("addons") or {}).get("keycloak") or {}).get("values") or {})


def _site_sets_keycloak_db_password(site):
    database = ((site.get("addons") or {}).get("keycloak") or {}).get("database") or {}
    return bool((((_keycloak_values(site).get("upstream") or {}).get("database") or {}).get("password"))
                or database.get("host") or database.get("password"))


def _keycloak_installed():
    """True if a Keycloak HelmRelease already exists (its database has a password)."""
    return bool(kubectl("get", "helmrelease", "-n", NAMESPACE, "keycloak", "-o", "name", check=False))


def _site_sets_keycloak_password(site):
    env = (((((site.get("addons") or {}).get("keycloak") or {}).get("values") or {}).get("upstream") or {})
           .get("secrets") or {}).get("env") or {}
    return bool((env.get("stringData") or {}).get("KEYCLOAK_ADMIN_PASSWORD"))


# ---------------------------------------------------------------- plan and rollback

def deployed():
    """(chart version, user-supplied values) of the bigbang release, or (None, {})."""
    out = sh("helm", "list", "-n", NAMESPACE, "-o", "json", check=False, capture=True) or "[]"
    rel = next((r for r in json.loads(out) if r.get("name") == "bigbang"), None)
    if not rel:
        return None, {}
    values = sh("helm", "get", "values", "bigbang", "-n", NAMESPACE, "-o", "json", check=False, capture=True)
    return rel.get("chart", "").rsplit("-", 1)[-1], json.loads(values or "{}") or {}


def plan_lines(old_version, old_values, new_version, new_values, defaults, old_state, new_mode):
    """What an install would change, without values that could be secret: the version,
    FIPS, packages turned on or off, and image repository/tag changes."""
    from release import image_changes   # noqa: E402 (same scripts directory)
    if old_version is None:
        return [f"fresh install of Big Bang {new_version} ({new_mode}): "
                + ", ".join(enabled_packages(deep_merge(json.loads(json.dumps(defaults)), new_values)))]
    lines = []
    if old_version != new_version:
        lines.append(f"Big Bang {old_version} -> {new_version}")
    if old_state.get("mode") and old_state["mode"] != new_mode:
        lines.append(f"images {old_state['mode']} -> {new_mode}")
    before = set(enabled_packages(deep_merge(json.loads(json.dumps(defaults)), old_values)))
    after = set(enabled_packages(deep_merge(json.loads(json.dumps(defaults)), new_values)))
    lines += [f"turn on {p}" for p in sorted(after - before)] + [f"turn off {p}" for p in sorted(before - after)]
    lines += [f"image {c[2:]}" for c in image_changes(yaml.safe_dump(old_values), yaml.safe_dump(new_values))
              if "removed" not in c and "new at" not in c]
    return lines


def plan(a, co, site, defaults, enabled):
    step("plan (nothing is changed)")
    version, values = deployed()
    new_values = {}
    for f in co.overlays(a.fips) + [a.site]:
        deep_merge(new_values, yaml.safe_load(open(f, encoding="utf-8")) or {})
    state = read_secret(NAMESPACE, STATE_SECRET) or {}
    lines = plan_lines(version, values, a.version, new_values, defaults, state, "fips" if a.fips else "standard")
    for line in lines or ["nothing changes"]:
        say(f"  {line}")
    say("\nrun the same command without --plan to apply it")
    return 0


def rollback(a):
    """helm rollback of the bigbang release, then wait and report. Big Bang's HelmReleases
    go back to the earlier chart versions and Flux downgrades the packages; a package whose
    data moved forward (database migrations) may not run on the older version."""
    co = Checkout(a.overlays)
    if kubectl("-n", NAMESPACE, "get", "helmrelease", "bigbang", check=False):
        raise SystemExit("installed with --gitops: revert the commit in your git repository instead")
    history = json.loads(sh("helm", "history", "bigbang", "-n", NAMESPACE, "-o", "json", check=False,
                            capture=True) or "[]")
    if len(history) < 2:
        raise SystemExit("no earlier revision of the bigbang release to roll back to")
    for h in history[-6:]:
        say(f"  revision {h['revision']:>3}  {h.get('updated', '')[:19]}  {h.get('chart', '')}  {h.get('status', '')}")
    current = history[-1]["revision"]
    target = a.to or next(h["revision"] for h in reversed(history) if h["revision"] < current
                          and h.get("status") in ("superseded", "deployed"))
    say(f"roll back revision {current} -> {target}")
    if not a.yes and not (sys.stdin.isatty() and confirm("Roll back?", default=False)):
        raise SystemExit("not confirmed; nothing changed (--yes skips this question)")
    sh("helm", "rollback", "bigbang", str(target), "-n", NAMESPACE, "--wait", "--timeout", "15m", capture=True)
    step("waiting for the packages")
    time.sleep(10)
    state = read_secret(NAMESPACE, STATE_SECRET) or {}
    ok, problems = wait_ready(hr_namespaces(), a.timeout)
    return status_report(co, state.get("mode") == "fips", problems, failed=not ok)


# ---------------------------------------------------------------- GitOps

GITOPS_SECRET = "bb-install-values"    # bigbang/<this>: the credential values (never in git)
BIGBANG_GIT = "https://repo1.dso.mil/big-bang/bigbang.git"

GITOPS_README = """# Big Bang {version} on Chainguard images, deployed by Flux

Written by `bb-install.py install --gitops`. Commit this directory to your own git
repository; Flux then keeps the cluster matching it. Nothing here is secret: the pull
credentials, S3 keys and generated passwords are in the cluster Secret
`bigbang/{secret}`, which this HelmRelease reads last.

- `bigbang.yaml`: Big Bang's chart from repo1 at {version} (no Iron Bank login) and its
  HelmRelease, with the values below in order.
- `values/`: the overlays from bigbang-chainguard{release} and your site file. Change
  the site file here; to upgrade, re-run `install --gitops` from a newer checkout.

Keep it in sync (your repository's URL and this directory's path):

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata: {{name: site, namespace: flux-system}}
spec: {{interval: 5m, url: https://git.example.com/you/site.git, ref: {{branch: main}}}}
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata: {{name: bigbang, namespace: flux-system}}
spec: {{interval: 10m, prune: true, path: ./{path}, sourceRef: {{kind: GitRepository, name: site}}}}
```
"""


def gitops_manifests(version, value_maps, mirror=None):
    """Big Bang's GitRepository (repo1 or the mirror, the tag) and HelmRelease reading
    `value_maps` (ConfigMaps, in order) and then the credentials Secret."""
    repo = {"apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "GitRepository",
            "metadata": {"name": "bigbang", "namespace": NAMESPACE},
            "spec": {"interval": "10m", "url": mirrored(BIGBANG_GIT, mirror), "ref": {"tag": version}}}
    release = {"apiVersion": "helm.toolkit.fluxcd.io/v2", "kind": "HelmRelease",
               "metadata": {"name": "bigbang", "namespace": NAMESPACE},
               "spec": {"interval": "10m", "timeout": "15m",
                        "chart": {"spec": {"chart": "./chart", "reconcileStrategy": "Revision",
                                           "sourceRef": {"kind": "GitRepository", "name": "bigbang"}}},
                        "install": {"remediation": {"retries": 3}},
                        "upgrade": {"remediation": {"retries": 3}},
                        "valuesFrom": [{"kind": "ConfigMap", "name": n} for n in value_maps]
                        + [{"kind": "Secret", "name": GITOPS_SECRET}]}}
    return [repo, release]


def write_gitops(out, co, fips, site_path, mirror=None):
    """A kustomization directory: the values files as ConfigMaps (layered in the same order
    as the helm path), Big Bang's GitRepository and HelmRelease, and a README."""
    os.makedirs(os.path.join(out, "values"), exist_ok=True)
    generators = []
    for i, f in enumerate(co.overlays(fips) + [site_path]):
        name = f"{i:02d}-{os.path.basename(f)}"
        shutil.copyfile(f, os.path.join(out, "values", name))
        generators.append({"name": f"bigbang-values-{i:02d}", "files": [f"values.yaml=values/{name}"]})
    # Kustomize doesn't rewrite valuesFrom names, so no hash suffix; the watch label makes
    # Flux reconcile the HelmRelease when a values ConfigMap changes instead.
    kust = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "namespace": NAMESPACE,
            "resources": ["bigbang.yaml"], "configMapGenerator": generators,
            "generatorOptions": {"disableNameSuffixHash": True,
                                 "labels": {"reconcile.fluxcd.io/watch": "Enabled"}}}
    with open(os.path.join(out, "kustomization.yaml"), "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(kust, f, sort_keys=False)
    with open(os.path.join(out, "bigbang.yaml"), "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump_all(gitops_manifests(co.version, [g["name"] for g in generators], mirror), f,
                           sort_keys=False)
    with open(os.path.join(out, "README.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(GITOPS_README.format(version=co.version, secret=GITOPS_SECRET,
                                     release=f" {co.release}" if co.release else "",
                                     path=os.path.basename(os.path.normpath(out))))


# ---------------------------------------------------------------- TLS

GATEWAY_CERT = "public-cert"           # the Secret Big Bang's public gateway serves
SELF_SIGNED_CA = "bb-install-ca"       # cert-manager/<this>: the self-signed CA to trust


def _site_sets_gateway_certs(site):
    public = ((((site.get("istioGateway") or {}).get("values") or {}).get("gateways") or {}).get("public") or {})
    return bool(public.get("gatewayCerts"))


def tls_choice(a, creds, site, enabled):
    """How the public gateway gets its certificate, or None without an Istio gateway:
    site (the site file sets gatewayCerts), own (--tls-cert/--tls-key), issuer (a
    cert-manager ClusterIssuer the site runs: ACME, Vault, a company CA) or self-signed (a
    CA cert-manager makes; the default). The choice is kept so re-runs don't need the flags."""
    if not {"istiod", "istioGateway"} <= set(enabled):
        return None
    if _site_sets_gateway_certs(site):
        return "site"
    if a.tls_cert or a.tls_key:
        if not (a.tls_cert and a.tls_key):
            raise SystemExit("--tls-cert and --tls-key go together")
        creds.values.update({"tls-mode": "own", "tls-cert": open(a.tls_cert, encoding="utf-8").read(),
                             "tls-key": open(a.tls_key, encoding="utf-8").read()})
    elif a.tls_issuer:
        creds.values.update({"tls-mode": "issuer", "tls-issuer": a.tls_issuer})
    elif a.tls_self_signed:
        creds.values["tls-mode"] = "self-signed"
    mode = creds.values.setdefault("tls-mode", "self-signed")
    if mode != "own":
        creds.values.pop("tls-cert", None)
        creds.values.pop("tls-key", None)
    if mode != "issuer":
        creds.values.pop("tls-issuer", None)
    if mode in ("issuer", "self-signed") and "certManager" not in enabled:
        raise SystemExit(f"TLS {mode} uses cert-manager: leave certManager on, or pass --tls-cert and --tls-key")
    return mode


def tls_manifests(domain, mode, issuer=None):
    """cert-manager objects for the gateway certificate: a self-signed CA and its issuer
    (self-signed), then the wildcard Certificate Big Bang's public gateway serves."""
    docs = []
    if mode == "self-signed":
        issuer = SELF_SIGNED_CA
        docs += [
            {"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer", "metadata": {"name": "bb-install-selfsigned"},
             "spec": {"selfSigned": {}}},
            {"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
             "metadata": {"name": SELF_SIGNED_CA, "namespace": "cert-manager"},
             "spec": {"isCA": True, "commonName": f"Big Bang {domain} CA", "secretName": SELF_SIGNED_CA,
                      "duration": "87600h", "privateKey": {"algorithm": "ECDSA", "size": 256},
                      "issuerRef": {"name": "bb-install-selfsigned", "kind": "ClusterIssuer"}}},
            {"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer", "metadata": {"name": SELF_SIGNED_CA},
             "spec": {"ca": {"secretName": SELF_SIGNED_CA}}},
        ]
    docs.append({"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
                 "metadata": {"name": GATEWAY_CERT, "namespace": "istio-gateway"},
                 "spec": {"secretName": GATEWAY_CERT, "dnsNames": [f"*.{domain}", domain],
                          "duration": "2160h", "renewBefore": "360h",
                          "privateKey": {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
                          "issuerRef": {"name": issuer, "kind": "ClusterIssuer"}}})
    return docs


def gateway_tls(domain, mode, issuer, wait):
    """Apply the cert-manager objects once Big Bang has installed cert-manager and made the
    istio-gateway namespace (never pre-create it: Helm won't adopt it). With a certificate
    from values (own, site), remove a Certificate an earlier run left, so they don't fight."""
    if mode in ("own", "site"):
        kubectl("-n", "istio-gateway", "delete", "certificate", GATEWAY_CERT, "--ignore-not-found", check=False)
        say(f"the public gateway serves {'your certificate' if mode == 'own' else 'the site file certificate'}")
        return True
    if not apply_when_ready(tls_manifests(domain, mode, issuer), "certificates.cert-manager.io", "istio-gateway",
                            ("-n", "istio-gateway", "get", "certificate", GATEWAY_CERT), wait):
        say("warning: cert-manager wasn't ready to issue the gateway certificate; re-run install")
        return False
    say(f"cert-manager issues *.{domain} from {'a self-signed CA' if mode == 'self-signed' else issuer} "
        f"into istio-gateway/{GATEWAY_CERT}")
    return True


def apply_when_ready(docs, crd, namespace, check, wait):
    """Apply `docs` once Big Bang has installed `crd` and made `namespace` (never pre-create
    it: Helm won't adopt it), retrying while the package's admission webhook comes up,
    until `kubectl <check>` finds the result or `wait` seconds pass."""
    manifest = yaml.safe_dump_all(docs)
    end = time.time() + wait
    while True:
        if kubectl("get", "crd", crd, check=False) and kubectl("get", "namespace", namespace, check=False):
            sh("kubectl", "apply", "-f", "-", stdin=manifest, check=False, capture=True)
            if kubectl(*check, check=False):
                return True
        if time.time() > end:
            return False
        time.sleep(15)


# ---------------------------------------------------------------- signatures

SIGNATURE_POLICY = "bb-install-chainguard-signatures"


def signature_choice(a, creds, enabled):
    """off, audit or enforce (remembered), and the org ID signatures must be under. With
    Kyverno on and nothing chosen, enforce: a Pod whose org image Chainguard didn't sign (or
    whose image built here builds.yml didn't) is refused; audit only reports it in Kyverno's
    policy reports. Off by default when Kyverno is off or the site is disconnected
    (--registry-mirror: no Sigstore to check against). A cluster keeps the mode it was
    installed with."""
    if a.verify_signatures:
        creds.values["signatures"] = a.verify_signatures
    if a.org_id:
        creds.values["org-id"] = a.org_id
    default = "enforce" if "kyverno" in enabled and not getattr(a, "registry_mirror", None) else "off"
    mode = creds.values.setdefault("signatures", default)
    if mode != "off" and "kyverno" not in enabled:
        raise SystemExit(f"--verify-signatures {mode} uses Kyverno: leave kyverno on, or pass --verify-signatures off")
    return mode, creds.values.get("org-id") or os.environ.get("CHAINGUARD_ORG_ID")


def built_signer(a, creds, co):
    """(images, subject regexp) for the images this repo builds: <build registry>/bigbang-*
    and builds.yml's signature on main from --build-signer (remembered), $BUILD_SIGNER, or
    any repo of the ghcr.io owner. None when the overlays use none or no signer is known."""
    if a.build_signer is not None:
        creds.values["build-signer"] = a.build_signer
    registry = _build_registry(co)
    subject = registry and build_signature_identity(registry, creds.values.get("build-signer") or build_signer())
    if registry and not subject:
        say(f"note: the images built here ({registry}) aren't signature-checked: pass --build-signer <owner/repo>")
    return (f"{registry}/bigbang-*", subject) if subject else None


def signature_policy(org, org_id, mode, built=None):
    """A Kyverno ClusterPolicy: every Pod image from cgr.dev/<org> needs Chainguard's
    keyless signature, and each image built here (`built`: (pattern, subject)) builds.yml's,
    the same signers bb-signatures.py accepts in CI. Audit records failures in policy
    reports; Enforce refuses the Pod."""
    action = "Enforce" if mode == "enforce" else "Audit"
    rules = [_verify_rule("chainguard-signature", f"{REGISTRY}/{org}/*", action, SIGNATURE_ISSUERS,
                          signature_identity(org_id))]
    if built:
        rules.append(_verify_rule("built-here-signature", built[0], action, BUILD_SIGNATURE_ISSUER, built[1]))
    return {"apiVersion": "kyverno.io/v1", "kind": "ClusterPolicy",
            "metadata": {"name": SIGNATURE_POLICY, "annotations": {
                "policies.kyverno.io/title": "Chainguard image signatures",
                "policies.kyverno.io/description": f"Images from cgr.dev/{org} must carry Chainguard's signature"
                                                   + (f" under org {org_id}" if org_id else "") + "."}},
            "spec": {"background": False, "webhookTimeoutSeconds": 30, "rules": rules}}


def _verify_rule(name, pattern, action, issuer, subject):
    return {"name": name, "match": {"any": [{"resources": {"kinds": ["Pod"]}}]},
            "verifyImages": [{
                "imageReferences": [pattern], "failureAction": action,
                "mutateDigest": False, "verifyDigest": False, "required": True,
                # Big Bang puts its pull secret in every package namespace, Kyverno's too.
                "imageRegistryCredentials": {"secrets": ["private-registry"]},
                "attestors": [{"entries": [{"keyless": {
                    "issuerRegExp": issuer, "subjectRegExp": subject,
                    "rekor": {"url": "https://rekor.sigstore.dev"}}}]}]}]}


def cluster_signatures(org, org_id, mode, wait, built=None):
    if mode == "off":
        kubectl("delete", "clusterpolicy", SIGNATURE_POLICY, "--ignore-not-found", check=False)
        return True
    if not apply_when_ready([signature_policy(org, org_id, mode, built)], "clusterpolicies.kyverno.io", "kyverno",
                            ("get", "clusterpolicy", SIGNATURE_POLICY), wait):
        say("warning: Kyverno wasn't ready for the signature policy; re-run install")
        return False
    say(f"Kyverno checks Chainguard's signature on cgr.dev/{org} images ({mode}"
        + (f", org ID {org_id}" if org_id else ", any Chainguard signer: pass --org-id to require your org") + ")"
        + (f", and builds.yml's on {built[0]}" if built else ""))
    return True


def signature_problem(state):
    """Why the signature policy isn't working, or ''."""
    if state.get("signatures", "off") == "off":
        return ""
    out = kubectl("get", "clusterpolicy", SIGNATURE_POLICY, "-o", "json", check=False)
    if not out:
        return f"no ClusterPolicy {SIGNATURE_POLICY}: re-run install"
    ready = {c["type"]: c for c in json.loads(out).get("status", {}).get("conditions", [])}.get("Ready", {})
    return "" if ready.get("status") == "True" else \
        f"signature policy not ready: {ready.get('message', 'no status')[:200]}"


def signature_note(state):
    """A line when the signature policy only audits: a cluster installed before enforce
    became the default keeps audit, and unsigned images still run there."""
    if state.get("signatures") != "audit":
        return ""
    return ("image signatures are only audited: Pods with unsigned images still run and Kyverno only "
            "reports them; re-run bb-install.py install --verify-signatures enforce to refuse them")


def tls_problem(state):
    """Why the gateway certificate cert-manager issues isn't Ready, or ''."""
    if state.get("tls-mode") not in ("issuer", "self-signed"):
        return ""
    out = kubectl("-n", "istio-gateway", "get", "certificate", GATEWAY_CERT, "-o", "json", check=False)
    if not out:
        return f"no istio-gateway/{GATEWAY_CERT} Certificate: re-run install"
    ready = {c["type"]: c for c in json.loads(out).get("status", {}).get("conditions", [])}.get("Ready", {})
    if ready.get("status") == "True":
        return ""
    return f"gateway certificate not issued yet: {ready.get('message', 'no status')[:200]}"


def _build_registry_host(co):
    """The registry host of the images this repo builds, if the overlays use any."""
    text = open(co.path("overlays", "values-chainguard.yaml"), encoding="utf-8").read()
    hosts = {m.split("/")[0] for m in re.findall(r"\b([a-z0-9.-]+\.[a-z]{2,}/[\w./-]+/bigbang-[\w-]+)", text)}
    hosts.discard("cgr.dev")
    return sorted(hosts)[0] if hosts else None


# ---------------------------------------------------------------- status

def status_report(co, fips, problems=(), failed=False):
    hrs = json.loads(kubectl("get", "helmrelease", "-A", "-o", "json", check=False) or '{"items": []}')["items"]
    step(f"HelmReleases ({len(hrs)})")
    bad = 0
    for hr in sorted(hrs, key=lambda h: h["metadata"]["name"]):
        cond = {c["type"]: c for c in hr.get("status", {}).get("conditions", [])}.get("Ready", {})
        ok = cond.get("status") == "True"
        bad += not ok
        say(f"  {'OK  ' if ok else 'FAIL'} {hr['metadata']['name']:28} {'' if ok else cond.get('message', '')[:160]}")
    namespaces = sorted({hr["spec"].get("targetNamespace") or hr["metadata"]["namespace"] for hr in hrs}
                        | {"flux-system"} | ({objstore.NAMESPACE} if co.in_cluster_seaweedfs() else set()))
    images, unhealthy = set(), []
    for ns in namespaces:
        out = kubectl("get", "pod", "-n", ns, "-o", "json", check=False)
        for p in json.loads(out)["items"] if out else []:
            images |= {c["image"] for c in p["spec"].get("initContainers", []) + p["spec"]["containers"]}
            if p["status"].get("phase") == "Succeeded":
                continue
            if not all(s.get("ready") for s in p["status"].get("containerStatuses", [{}])):
                unhealthy.append(ns)
    if unhealthy:
        step("pods that aren't healthy")
        report(sorted(set(unhealthy)))
    mirror = (read_secret(NAMESPACE, STATE_SECRET) or {}).get("registry-mirror")
    if mirror:   # judge the site registry's copies as the images they were copied from
        build = _build_registry(co)
        images = {f"{build if build and i.split('/')[-1].startswith('bigbang-') else REGISTRY + '/' + co.org}"
                  f"/{i[len(mirror.rstrip('/')) + 1:]}" if i.startswith(mirror.rstrip("/") + "/") else i
                  for i in images}
    policy = running_image_problems(images, co.org, _build_registry(co), load_mapping(co.org),
                                    fips=fips) if co.org else []
    step(f"images: {len(images)} running, {len(policy)} not from the conversion")
    for p in policy:
        say(f"  {p}")
    for p in problems:
        say(f"PROBLEM: {p}")
    state = read_secret(NAMESPACE, STATE_SECRET) or {}
    for note in (deployed_version_note(co), pull_token_note(state), signature_note(state)):
        if note:
            say(note)
    tls = tls_problem(state)
    signature = signature_problem(state)
    for p in (tls, signature):
        if p:
            say(f"PROBLEM: {p}")
    access_report(state)
    healthy = not (failed or bad or unhealthy or policy or tls or signature)
    say(f"\n{'HEALTHY' if healthy else 'NOT HEALTHY'}: {len(hrs) - bad}/{len(hrs)} HelmReleases ready")
    return 0 if healthy else 1


def access_report(state):
    """Where the apps are: the gateways' external addresses (DNS for the domain points
    there) and each VirtualService host."""
    out = kubectl("get", "virtualservice", "-A", "-o", "json", check=False)
    hosts = sorted({h for v in (json.loads(out)["items"] if out else []) for h in v["spec"].get("hosts", [])
                    if "." in h and "*" not in h})
    out = kubectl("get", "service", "-n", "istio-gateway", "-o", "json", check=False)
    gateways = []
    for s in json.loads(out)["items"] if out else []:
        addrs = [i.get("ip") or i.get("hostname") for i in s.get("status", {}).get("loadBalancer", {}).get("ingress", [])]
        if s["spec"].get("type") == "LoadBalancer":
            gateways.append(f"{s['metadata']['name']}: {', '.join(a for a in addrs if a) or 'no external address yet'}")
    if not (hosts or gateways):
        return
    step("where to find it")
    for g in gateways:
        say(f"  gateway {g} (point the domain's DNS here)")
    for h in hosts:
        say(f"  https://{h}")
    if state.get("tls-mode") == "self-signed":
        say("  WARNING: TLS is self-signed (the default, for test sites). For production, re-run install "
            "with --tls-issuer <ClusterIssuer> or --tls-cert/--tls-key.")
        say(f"  Until then, trust its CA: kubectl -n cert-manager get secret {SELF_SIGNED_CA} "
            "-o jsonpath='{.data.ca\\.crt}' | base64 -d > bigbang-ca.crt")
    if state.get("keycloak-admin-password"):
        say(f"  Keycloak admin password: kubectl -n {NAMESPACE} get secret {STATE_SECRET} "
            "-o jsonpath='{.data.keycloak-admin-password}' | base64 -d")


def deployed_version_note(co):
    """A line saying the cluster is behind this checkout (re-run install to upgrade)."""
    out = sh("helm", "list", "-n", NAMESPACE, "-o", "json", check=False, capture=True) or "[]"
    rel = next((r for r in json.loads(out) if r.get("name") == "bigbang"), None)
    if not rel:
        return ""
    deployed = rel.get("chart", "").rsplit("-", 1)[-1]
    if deployed and deployed != co.version:
        return (f"update available: the cluster runs Big Bang {deployed}, this checkout has {co.version}; "
                "re-run bb-install.py install to upgrade")
    return ""


def build_images(co, fips):
    """Every image reference of this repo's builds (<build registry>/bigbang-*) the overlays deploy."""
    prefix = _build_registry(co)
    return [r for r in deployed_images(co, fips, prefix) if r.startswith(prefix + "/bigbang-")] if prefix else []


def pull_check(refs):
    """A Credentials.registry check: the first of `refs` a login can't pull, and why."""
    return lambda login: next((p for r in refs if (p := pull_problem(r, login))), None)


def deployed_images(co, fips, prefix):
    """Every image reference under `prefix` (host[/path]) the merged overlays deploy."""
    refs = set()

    def walk(node):
        if isinstance(node, dict):
            repo, tag = node.get("repository"), node.get("tag")
            if isinstance(repo, str) and tag is not None:
                full = f"{node['registry']}/{repo}" if isinstance(node.get("registry"), str) else repo
                if prefix and full.startswith(prefix + "/"):
                    refs.add(f"{full}:{tag}")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and prefix and re.fullmatch(re.escape(prefix) + r"/[\w./-]+:[\w.-]+", node):
            refs.add(node)
    walk(merged_values({}, co.overlays(fips)))   # what deploys: FIPS replaces the standard refs
    return sorted(refs)


def pull_problem(ref, login=None):
    """None if `ref` (host/repo:tag) can be pulled with `login` ((user, password) or None),
    else why not. The registry's v2 token flow, read-only; an unreachable registry isn't a
    problem here (a proxy may allow the cluster what it denies this machine)."""
    host, rest = ref.split("/", 1)
    repo, tag = rest.rsplit(":", 1)
    url = f"https://{host}/v2/{repo}/manifests/{tag}"
    accept = ", ".join(["application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
                        "application/vnd.docker.distribution.manifest.list.v2+json",
                        "application/vnd.docker.distribution.manifest.v2+json"])
    basic = None
    if login:
        import base64
        basic = "Basic " + base64.b64encode(f"{login[0]}:{login[1]}".encode()).decode()

    def head(headers):
        req = urllib.request.Request(url, method="HEAD", headers={"Accept": accept, **headers})
        try:
            urllib.request.urlopen(req, timeout=20).close()
            return 200, None
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("WWW-Authenticate", "")
    try:
        code, challenge = head({})
        if code == 401 and challenge.lower().startswith("bearer "):
            params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
            q = urllib.parse.urlencode({"service": params.get("service", host),
                                        "scope": f"repository:{repo}:pull"})
            req = urllib.request.Request(f"{params['realm']}?{q}", headers={"Authorization": basic} if basic else {})
            try:
                body = json.load(urllib.request.urlopen(req, timeout=20))
            except urllib.error.HTTPError as e:
                return f"{ref}: {'the registry refused this login' if login else 'needs a login'} (HTTP {e.code})"
            code, _ = head({"Authorization": "Bearer " + (body.get("token") or body.get("access_token", ""))})
        elif code == 401 and basic:
            code, _ = head({"Authorization": basic})
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None
    if code == 200:
        return None
    if code == 404:
        return f"{ref}: not found (not pushed yet, or {'this login' if login else 'no login'} can't see it)"
    return f"{ref}: {'denied with this login' if login else 'needs a login'} (HTTP {code})"


def _build_registry(co):
    text = open(co.path("overlays", "values-chainguard.yaml"), encoding="utf-8").read()
    m = re.search(r"\b([a-z0-9.-]+\.[a-z]{2,}/[\w.-]+)/bigbang-[\w-]+", text)
    return m.group(1) if m else ""


def status(a):
    co = Checkout(a.overlays)
    state = read_secret(NAMESPACE, STATE_SECRET) or {}
    if not state:
        say("note: no bigbang/bb-install Secret: not installed with bb-install.py (checking anyway)")
    return status_report(co, state.get("mode") == "fips")


# ---------------------------------------------------------------- uninstall

def uninstall(a):
    co = Checkout(a.overlays)
    what = ["Big Bang (helm release bigbang and every package it installed)"]
    if co.in_cluster_seaweedfs():
        what.append("SeaweedFS" + (", its volume and the stored S3 keys (DATA IS DELETED)" if a.delete_data
                                   else " (its volume and S3 keys stay; --delete-data removes them)"))
    if not a.keep_flux:
        what.append("Flux")
    state = read_secret(NAMESPACE, STATE_SECRET) or {}
    made_token = a.delete_data and state.get("cgr-created") and state.get("cgr-username")
    if made_token:
        what.append("the cgr.dev pull token install made (its Chainguard identity)")
    say("This removes:\n  " + "\n  ".join(what))
    if not a.yes:
        if not sys.stdin.isatty() or input("Type 'uninstall' to continue: ").strip() != "uninstall":
            raise SystemExit("not confirmed; nothing removed (--yes skips this question)")
    step("Big Bang")
    # Installed with --gitops: remove the HelmRelease first, or Flux puts the release back.
    kubectl("-n", NAMESPACE, "delete", "helmrelease", "bigbang", "--ignore-not-found", "--wait=true", check=False)
    sh("helm", "uninstall", "bigbang", "-n", NAMESPACE, "--wait", "--timeout", "15m", check=False, capture=True)
    end = time.time() + 900
    while time.time() < end:
        out = kubectl("get", "helmrelease", "-A", "-o", "name", check=False)
        if not (out or "").strip():
            break
        say(f"waiting for {len(out.split())} HelmReleases to go")
        time.sleep(15)
    if co.in_cluster_seaweedfs():
        step("SeaweedFS")
        if a.delete_data:
            kubectl("delete", "namespace", objstore.NAMESPACE, "--ignore-not-found", "--wait=true", check=False)
        else:
            rendered = kubectl("kustomize", co.path("seaweedfs"))
            keep = [d for d in yaml.safe_load_all(rendered) if d and d["kind"] not in ("PersistentVolumeClaim",
                                                                                        "Namespace")]
            sh("kubectl", "delete", "--ignore-not-found", "-f", "-", stdin=yaml.safe_dump_all(keep), capture=True)
    if not a.keep_flux:
        step("Flux")
        sh("kubectl", "delete", "--ignore-not-found", "-k", co.path("flux"), check=False, capture=True)
    if made_token:
        step("pull token")
        # Only a token this installer made (cgr-created): one from the environment or the
        # site file belongs to someone else.
        rc = subprocess.run(["chainctl", "iam", "identities", "delete", state["cgr-username"], "--yes"],
                            capture_output=True, text=True).returncode if shutil.which("chainctl") else None
        say("deleted its identity" if rc == 0 else
            "couldn't delete it (needs chainctl, logged in); it expires on its own within a year")
    if a.delete_data:
        kubectl("-n", NAMESPACE, "delete", "secret", STATE_SECRET, "--ignore-not-found", check=False)
    say("\ndone")
    return 0


# ---------------------------------------------------------------- init

def init(a):
    if os.path.exists(a.site) and not a.force:
        raise SystemExit(f"{a.site} exists (--force overwrites it)")
    discovered = json.load(open(os.path.join(ROOT, "discovery", "images.json"), encoding="utf-8"))
    known = sorted({i["key"] for i in discovered["packages"].values() if i.get("key")} - {"flux"})
    domain = a.domain
    enable = [e.strip() for e in (a.enable or "").split(",") if e.strip()]
    interactive = sys.stdin.isatty() and not a.yes
    if interactive and not domain:
        domain = input("Domain the packages are served under (e.g. bigbang.example.com): ").strip()
    if interactive and not a.enable:
        say("Big Bang's default packages are on already. Add-ons and other packages you can turn on:\n  "
            + ", ".join(k for k in known if k.startswith("addons.")))
        enable = [e.strip() for e in input("Turn on (comma-separated, Enter for none): ").split(",") if e.strip()]
    if not domain:
        raise SystemExit("--domain is required")
    unknown = [e for e in enable if e not in known and f"addons.{e}" not in known]
    if unknown:
        raise SystemExit(f"unknown packages {unknown}; known: {', '.join(known)}")
    values = {"domain": domain}
    for e in enable:
        key = e if e in known else f"addons.{e}"
        node = values
        for part in key.split("."):
            node = node.setdefault(part, {})
        node["enabled"] = True
    with open(a.site, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Big Bang site file for scripts/bb-install.py. No credentials here: the installer\n"
                "# reads them from the environment or asks, and keeps them in the cluster.\n"
                "# Add your own Big Bang values below (TLS for the Istio gateway, SSO, sizes...).\n")
        yaml.safe_dump(values, f, sort_keys=False)
    say(f"wrote {a.site}. Next: python scripts/bb-install.py install --site {a.site} [--fips]"
        if getattr(a, "cmd", "init") == "init" else f"wrote {a.site}")
    return 0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--overlays", default=ROOT, help="directory holding overlays/, flux/ and seaweedfs/ "
                                                     "(default: this checkout)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="write a starter site file")
    p.add_argument("--site", default="my-site.yaml")
    p.add_argument("--domain")
    p.add_argument("--enable", help="comma-separated packages to turn on, e.g. keycloak,velero")
    p.add_argument("--force", action="store_true")
    p.add_argument("--yes", action="store_true", help="don't ask anything")
    p = sub.add_parser("install", help="install or upgrade")
    p.add_argument("--site", default="my-site.yaml",
                   help="the site file (default my-site.yaml; written first, as by init, if it doesn't exist)")
    p.add_argument("--domain", help="for a new site file: the domain the packages are served under")
    p.add_argument("--enable", help="for a new site file: comma-separated packages to turn on")
    p.add_argument("--fips", action="store_true", help="FIPS images (re-run without it to switch back)")
    p.add_argument("--chart", help="Big Bang chart: a local path or an oci:// ref (default: downloaded from "
                                   "repo1's git archive, no Iron Bank login needed)")
    p.add_argument("--version", help="Big Bang chart version (default: the overlays')")
    p.add_argument("--timeout", type=int, default=2700, help="seconds to wait for the packages (default 2700)")
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--small-cluster", action="store_true",
                   help="lower Flux's CPU/memory requests (kind or another small test cluster)")
    p.add_argument("--yes", action="store_true", help="never prompt (credentials from the environment, the "
                                                      "cluster, or a pull token made with your chainctl login)")
    p.add_argument("--tls-cert", help="the gateway's certificate (PEM, *.<domain>; with --tls-key)")
    p.add_argument("--tls-key", help="its private key (PEM)")
    p.add_argument("--tls-issuer", help="a cert-manager ClusterIssuer of yours to issue *.<domain> (ACME, Vault, ...)")
    p.add_argument("--tls-self-signed", action="store_true",
                   help="a self-signed CA made by cert-manager (the default when nothing else is set)")
    p.add_argument("--gitops", metavar="DIR",
                   help="instead of helm: write DIR (Big Bang's HelmRelease and the values) to commit to your "
                        "git repo, and apply it; credentials stay in a cluster Secret")
    p.add_argument("--verify-signatures", choices=["off", "audit", "enforce"],
                   help="a Kyverno policy requiring Chainguard's signature on cgr.dev/<org> images and "
                        "builds.yml's on the images built here (remembered; default enforce when Kyverno "
                        "is on, off when disconnected)")
    p.add_argument("--build-signer", metavar="OWNER/REPO",
                   help="the repo whose builds.yml signs the images built here (remembered; default "
                        "$BUILD_SIGNER, else any repo of the ghcr.io owner)")
    p.add_argument("--org-id", help="Chainguard org ID signatures must be under (chainctl iam organizations "
                                    "list; default $CHAINGUARD_ORG_ID, else any Chainguard signer)")
    p.add_argument("--plan", action="store_true",
                   help="show what would change (version, FIPS, packages, image tags) and stop")
    p.add_argument("--registry-mirror", metavar="PREFIX",
                   help="the site registry holding the images (air-gapped; bb-airgap.py lists them): "
                        "cgr.dev/<org> and the build registry become PREFIX; remembered, \"\" turns it off")
    p.add_argument("--git-mirror", metavar="URL",
                   help="a git server mirroring repo1 (same paths under URL) for Big Bang's chart and "
                        "packages; remembered, \"\" turns it off")
    p.add_argument("--no-pull-check", action="store_true",
                   help="don't test each registry login with a pull (when only the nodes can reach a "
                        "registry, with their own identity: ECR, ACR, Artifact Registry)")
    p.add_argument("--new-pull-token", action="store_true",
                   help="replace the stored cgr.dev pull token with a new one from chainctl")
    p = sub.add_parser("chart", help="download Big Bang's chart into work/charts (the installer image bundles it)")
    p.add_argument("--version", help="Big Bang version (default: the overlays')")
    sub.add_parser("status", help="what's running and what isn't")
    p = sub.add_parser("rollback", help="return the bigbang release to an earlier revision")
    p.add_argument("--to", type=int, help="revision (default: the one before the current)")
    p.add_argument("--timeout", type=int, default=2700)
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p = sub.add_parser("uninstall", help="remove Big Bang, SeaweedFS and Flux")
    p.add_argument("--delete-data", action="store_true", help="also SeaweedFS's volume and the stored keys")
    p.add_argument("--keep-flux", action="store_true")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    a = ap.parse_args()
    return {"init": init, "install": install, "chart": chart, "status": status, "rollback": rollback,
            "uninstall": uninstall}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
