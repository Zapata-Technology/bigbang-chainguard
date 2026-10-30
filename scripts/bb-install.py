#!/usr/bin/env python3
"""Install Big Bang on Chainguard images with one command, and look after it.

  bb-install.py init [--site my-site.yaml] [--domain example.com] [--enable a,b]
                     write a starter site file (no credentials in it)
  bb-install.py install --site my-site.yaml [--fips]
                     preflight checks, pull secrets, Flux, SeaweedFS (random S3 keys),
                     Big Bang with the overlays in the right order, then wait and report
  bb-install.py status
                     HelmReleases, pods that aren't healthy (with their last log lines),
                     and running images that aren't the conversion's
  bb-install.py uninstall [--delete-data] [--keep-flux] [--yes]
                     Big Bang, SeaweedFS (its volume and S3 keys stay unless
                     --delete-data), Flux

`install` is safe to re-run: it is also how you upgrade, switch to FIPS (--fips) or
back, and apply a changed site file. It deploys what this checkout's overlays were
generated for (the org and object storage are read from their headers), so regenerate
them first (bb-run.py) if you changed mapping.yaml or the org.

Credentials never go in the site file or on a command line, and are never printed. Each
is taken from the first of: the environment, the site file's registryCredentials, the
cluster (a previous run keeps them in the bigbang/bb-install Secret), or a hidden prompt.

  CGR_USERNAME, CGR_PASSWORD     cgr.dev pull token. Create one (valid up to a year) with
                                 chainctl auth pull-token create --repository=oci
                                   --parent=<org> --ttl=8760h
  BUILD_REGISTRY_USERNAME, BUILD_REGISTRY_PASSWORD
                                 only if the images this repo builds are private
  REGISTRY1_USERNAME, REGISTRY1_PASSWORD
                                 only if an enabled package keeps an Iron Bank image
  S3_ACCESS_KEY, S3_SECRET_KEY   only with an external S3 (OBJECT_STORAGE_ENDPOINT);
                                 for the in-cluster SeaweedFS they are generated
"""
import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import objstore  # noqa: E402
from bbcluster import (apply_secret, dockerconfig, hr_namespaces, install_flux, kubectl, read_secret,  # noqa: E402
                       report, sh, wait_ready)
from bbcommon import ROOT, deep_merge, load_mapping, running_image_problems, split_ref  # noqa: E402

NAMESPACE = "bigbang"
STATE_SECRET = "bb-install"            # credentials and install mode, kept in the cluster
CHART = "oci://registry1.dso.mil/bigbang/bigbang"
MIN_KUBERNETES = (1, 34)               # Big Bang's chart requires it


def say(msg):
    print(msg, flush=True)


def step(title):
    say(f"\n== {title}")


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


def chart_ref(a):
    return (a.chart, []) if a.chart and not a.chart.startswith("oci://") else \
        (a.chart or CHART, ["--version", a.version])


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

    def registry(self, key, registry, env_user, env_pass, why, required=True):
        """(username, password) for a registry, or None when it's optional and not given."""
        user, pw = os.environ.get(env_user), os.environ.get(env_pass)
        if not (user and pw) and registry in self.site:
            user, pw = self.site[registry].get("username"), self.site[registry].get("password")
        if not (user and pw):
            user, pw = self.stored.get(f"{key}-username"), self.stored.get(f"{key}-password")
        if not (user and pw) and self.interactive:
            say(f"{registry}: {why}")
            user = input(f"  {registry} username{'' if required else ' (Enter to skip)'}: ").strip()
            pw = getpass.getpass(f"  {registry} password: ").strip() if user else ""
        if not (user and pw):
            if required:
                raise SystemExit(f"no {registry} credentials: set {env_user} and {env_pass} ({why})")
            return None
        self.values[f"{key}-username"], self.values[f"{key}-password"] = user, pw
        return user, pw

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
    if on & {"monitoring", "addons.thanos"} and "prometheusOperatorCRDs" not in on:
        problems.append("monitoring/Thanos need prometheusOperatorCRDs (it installs the CRDs and creates the "
                        "monitoring namespace): leave it on in the site file")
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
    site = yaml.safe_load(open(a.site, encoding="utf-8")) or {}
    step("preflight")
    defaults = chart_defaults(a)
    enabled, problems, notes = preflight(a, co, site, defaults)
    say(f"Big Bang {a.version}, images from {co.registry}, {'FIPS' if a.fips else 'standard'}; "
        f"object storage: {'SeaweedFS in the cluster' if co.in_cluster_seaweedfs() else (co.s3 or {}).get('endpoint', 'MinIO (Big Bang)')}")
    say(f"packages: {', '.join(enabled)}")
    for n in notes:
        say(f"note: {n}")
    if problems:
        raise SystemExit("\nnot installing:\n  " + "\n  ".join(problems))

    step("credentials")
    creds = Credentials(site, interactive=sys.stdin.isatty() and not a.yes)
    cgr = creds.registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD",
                         "a Chainguard pull token (chainctl auth pull-token create --repository=oci "
                         f"--parent={co.org} --ttl=8760h)")
    registries = [("cgr.dev", *cgr)]
    build = _build_registry_host(co)
    if build:
        got = creds.registry("build", build, "BUILD_REGISTRY_USERNAME", "BUILD_REGISTRY_PASSWORD",
                             "images this repo builds; skip if they are public", required=False)
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
        sv = os.path.join(d, "credentials.yaml")
        with open(sv, "w", encoding="utf-8") as fh:
            yaml.safe_dump(secret_values, fh)
        ref, version = chart_ref(a)
        files = [x for f in co.overlays(a.fips) + [a.site] for x in ("-f", f)]
        sh("helm", "upgrade", "-i", "bigbang", ref, *version, "-n", NAMESPACE, "--create-namespace",
           *files, "-f", sv, "--timeout", "15m", capture=True)
        say(f"helm release bigbang applied ({len(enabled)} packages); Flux installs them now")

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
    policy = running_image_problems(images, co.org, _build_registry(co), load_mapping(co.org),
                                    fips=fips) if co.org else []
    step(f"images: {len(images)} running, {len(policy)} not from the conversion")
    for p in policy:
        say(f"  {p}")
    for p in problems:
        say(f"PROBLEM: {p}")
    behind = deployed_version_note(co)
    if behind:
        say(behind)
    healthy = not (failed or bad or unhealthy or policy)
    say(f"\n{'HEALTHY' if healthy else 'NOT HEALTHY'}: {len(hrs) - bad}/{len(hrs)} HelmReleases ready")
    return 0 if healthy else 1


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
    say("This removes:\n  " + "\n  ".join(what))
    if not a.yes:
        if not sys.stdin.isatty() or input("Type 'uninstall' to continue: ").strip() != "uninstall":
            raise SystemExit("not confirmed; nothing removed (--yes skips this question)")
    step("Big Bang")
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
    say(f"wrote {a.site}. Next: python scripts/bb-install.py install --site {a.site} [--fips]")
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
    p.add_argument("--site", required=True)
    p.add_argument("--fips", action="store_true", help="FIPS images (re-run without it to switch back)")
    p.add_argument("--chart", help=f"Big Bang chart: a local path, or an OCI ref (default {CHART})")
    p.add_argument("--version", help="Big Bang chart version (default: the overlays')")
    p.add_argument("--timeout", type=int, default=2700, help="seconds to wait for the packages (default 2700)")
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--small-cluster", action="store_true",
                   help="lower Flux's CPU/memory requests (kind or another small test cluster)")
    p.add_argument("--yes", action="store_true", help="never prompt (credentials must be in the environment)")
    sub.add_parser("status", help="what's running and what isn't")
    p = sub.add_parser("uninstall", help="remove Big Bang, SeaweedFS and Flux")
    p.add_argument("--delete-data", action="store_true", help="also SeaweedFS's volume and the stored keys")
    p.add_argument("--keep-flux", action="store_true")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    a = ap.parse_args()
    return {"init": init, "install": install, "status": status, "uninstall": uninstall}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
