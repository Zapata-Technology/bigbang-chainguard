"""The checks check, install and upgrade start with, before anything changes: tools, the
cluster, the site file rendered by Big Bang's chart, and the upgrade path."""
import argparse
import json
import os
import re
import shutil
import subprocess

import yaml

from bbcluster import kubectl, read_secret
from bbcommon import ROOT, is_bigbang4, load_mapping, split_ref
from bbinstall.chart import chart_defaults, chart_ref
from bbinstall.checkout import Checkout, mirrored_checkout
from bbinstall.common import NAMESPACE, STATE_SECRET, say, step
from bbinstall.sitefile import enabled_packages, init, legacy_keys, merged_values
from bbinstall.status import deployed

MIN_KUBERNETES = (1, 35)               # Big Bang 4's chart requires it


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
    storage = co.object_storage_problem() if hasattr(co, "object_storage_problem") else None
    if storage:
        problems.append(storage)
    if not site.get("domain"):
        problems.append("the site file has no `domain` (bb-install.py init writes one)")
    legacy = legacy_keys(site, defaults) if is_bigbang4(defaults) else []
    if legacy:
        problems.append(f"the site file uses Big Bang 3 keys ({', '.join(legacy)}); Big Bang {co.version} reads "
                        "packages.<name>. `bb-install.py upgrade` converts it for you (keeping a backup)")
    files = co.overlays(a.fips) + [a.site]
    enabled = enabled_packages(merged_values(defaults, files))
    if not legacy and not problems:
        kube = f"{server.get('major')}.{re.sub(r'[^0-9]', '', server.get('minor', ''))}.0" if server else \
            f"{MIN_KUBERNETES[0]}.{MIN_KUBERNETES[1]}.0"
        problems += render_problems(a, files, kube)
    on = set(enabled)
    for needs in ("prometheusOperatorCRDs", "certManager"):
        # prometheusOperatorCRDs installs the CRDs and creates the monitoring namespace;
        # monitoring's certificates need cert-manager. Both are on by default.
        if on & {"packages.monitoring", "packages.thanos"} and f"packages.{needs}" not in on:
            problems.append(f"monitoring/Thanos need {needs}: leave it on in the site file")
    if "packages.gitlab" in on:
        notes.append("GitLab's migrations need PostgreSQL max_locks_per_transaction=256 "
                     "(the chart default 64 fails with out of shared memory). Set it on the "
                     "database GitLab uses; the chart does not configure that server")
    if server:
        nodes = json.loads(kubectl("get", "nodes", "-o", "json", check=False) or "{}").get("items", [])
        notes += [n for n in (memory_note(nodes, on), loadbalancer_note(nodes)) if n]
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


# Memory for Big Bang's default packages: on a 12 GB node they scheduled but starved the
# control plane, and on 16 GB they ran (docs/testing.md, the RKE2 test). The extras are
# rough allowances for packages much larger than the defaults, so a site sees the warning
# before pods sit Pending; they are not measured limits.
MIN_MEMORY_GIB = 16
EXTRA_MEMORY_GIB = {"packages.gitlab": 8, "packages.gitlabRunner": 2, "packages.mattermost": 2,
                    "packages.sonarqube": 4, "packages.elasticsearchKibana": 8, "packages.eckOperator": 1,
                    "packages.anchoreEnterprise": 8, "packages.harbor": 4, "packages.mimir": 4,
                    "packages.loki": 2, "packages.thanos": 2}
UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}
# Cloud providers whose controller gives LoadBalancer Services an address (node providerID).
LB_CLOUDS = ("aws://", "azure://", "gce://", "oci://", "digitalocean://", "linode://", "ibm://", "openstack://")
# In-cluster LoadBalancer implementations, by namespace or pod name prefix (k3s's ServiceLB).
LB_NAMESPACES = ("metallb-system", "kube-vip", "cilium-lb")
LB_PODS = ("svclb-", "kube-vip", "cloud-provider-kind", "metallb-")


def quantity_bytes(q):
    """Bytes of a Kubernetes memory quantity ("16Gi", "16000000k", "17179869184")."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]i|[kMGT])?", str(q or "0"))
    return int(float(m.group(1)) * UNITS.get(m.group(2) or "", 1)) if m else 0


def memory_note(nodes, enabled):
    """A warning when the nodes' allocatable memory is under what the enabled packages
    need (MIN_MEMORY_GIB plus EXTRA_MEMORY_GIB), else None."""
    total = sum(quantity_bytes((n.get("status") or {}).get("allocatable", {}).get("memory")) for n in nodes)
    if not total:
        return None
    need = MIN_MEMORY_GIB + sum(EXTRA_MEMORY_GIB.get(k, 0) for k in enabled)
    have = total / 2**30
    if have >= need:
        return None
    return (f"the nodes have {have:.1f} GiB of allocatable memory; these packages need about {need} GiB "
            "(pods stay Pending, or the control plane starves): add nodes or memory, or turn packages off")


def loadbalancer_note(nodes):
    """A warning when nothing seems to give a LoadBalancer Service an address, else None.
    Big Bang's gateways are LoadBalancer Services: without an address the apps aren't
    reachable even though every pod is Ready. Read-only: nothing is created to test it."""
    out = kubectl("get", "service", "-A", "-o", "json", check=False)
    lbs = [s for s in (json.loads(out) if out else {}).get("items", []) if s["spec"].get("type") == "LoadBalancer"]
    if any((s.get("status") or {}).get("loadBalancer", {}).get("ingress") for s in lbs):
        return None
    if lbs:
        names = ", ".join(f"{s['metadata']['namespace']}/{s['metadata']['name']}" for s in lbs[:3])
        return (f"LoadBalancer Services here have no address ({names}): the gateways will get none either. "
                "Install a load balancer (MetalLB, kube-vip, your cloud's controller) or front them yourself")
    if any(str((n.get("spec") or {}).get("providerID", "")).startswith(LB_CLOUDS) for n in nodes):
        return None
    out = kubectl("get", "namespace", "-o", "name", check=False) or ""
    if any(f"namespace/{ns}" in out.split() for ns in LB_NAMESPACES):
        return None
    out = kubectl("get", "pod", "-A", "-o", "name", check=False) or ""
    if any(p.split("/", 1)[-1].startswith(LB_PODS) for p in out.split()):
        return None
    return ("no load balancer found (no cloud provider, MetalLB, kube-vip or ServiceLB): Big Bang's gateways "
            "are LoadBalancer Services and may never get an address; status shows it")


def render_problems(a, files, kube_version):
    """Big Bang rendered with the overlays and the site file (helm template, nothing
    applied): the chart's schema and templates check every setting, so a misspelled key, a
    Big Bang 3 key or a bad value stops the install here instead of in a HelmRelease."""
    ref, version = chart_ref(a)
    p = subprocess.run(["helm", "template", "bigbang", ref, *version, "-n", NAMESPACE,
                        *[x for f in files for x in ("-f", f)], "--kube-version", kube_version],
                       capture_output=True, text=True)
    if p.returncode == 0:
        return []
    lines = [ln.rstrip() for ln in p.stderr.splitlines() if ln.strip() and not ln.startswith("Use --debug")]
    return ["Big Bang doesn't accept these settings (helm template):\n    " + "\n    ".join(lines[:20])]


def upgrade_path_problem(running, target):
    """Why Big Bang can't go from the `running` chart version to `target`, or None. Big Bang
    4 supports upgrades from 3.35.1 only, and a chart never goes back a major version."""
    if not running:
        return None
    major = lambda v: int(str(v).split(".")[0])   # noqa: E731
    if major(target) < major(running):
        return f"the cluster runs Big Bang {running}; {target} is an older major version (use rollback instead)"
    if major(running) == 3 and major(target) >= 4 and running != "3.35.1":
        return (f"the cluster runs Big Bang {running}; Big Bang {target} upgrades from 3.35.1 only. Install a "
                "3.35.1-cg.<n> release first (its bb-install.py install), then upgrade to this one")
    return None


def prepare(a):
    """What check, install and upgrade all start with: the checkout, the site file (written
    first when it doesn't exist), Big Bang's defaults and the preflight, printed."""
    co = Checkout(a.overlays)
    a.version = a.version or co.version
    if not os.path.exists(a.site):
        # A first install needs no separate `init`: ask for (or take --domain/--enable) and
        # write the site file, then carry on.
        say(f"{a.site} doesn't exist: writing it first (bb-install.py init)")
        init(argparse.Namespace(cmd="install", site=a.site, domain=a.domain, enable=a.enable,
                                disable=getattr(a, "disable", None), force=False, yes=a.yes))
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
    problem = upgrade_path_problem(deployed()[0], a.version) if shutil.which("helm") else None
    if problem:
        problems.append(problem)
    say(f"Big Bang {a.version}{f' (release {co.release})' if co.release else ''}, images from {co.registry}, "
        f"{'FIPS' if a.fips else 'standard'}; "
        f"object storage: {'SeaweedFS in the cluster' if co.in_cluster_seaweedfs() else (co.s3 or {}).get('endpoint', 'MinIO (Big Bang)')}")
    say(f"packages: {', '.join(enabled)}")
    for n in notes:
        say(f"note: {n}")
    return co, site, defaults, enabled, problems, notes
