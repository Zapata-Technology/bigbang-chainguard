"""The other commands: chart, check, upgrade, status, rollback and uninstall (init is in
sitefile, install in install)."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

import yaml

import objstore
from bbcluster import hr_namespaces, kubectl, read_secret, sh, wait_ready
from bbcommon import is_bigbang4
from bbinstall.chart import chart_defaults, fetch_chart
from bbinstall.checkout import Checkout
from bbinstall.common import NAMESPACE, STATE_SECRET, confirm, say, step
from bbinstall.credentials import check_logins
from bbinstall.images import image_problems
from bbinstall.install import install
from bbinstall.nodes import INOTIFY, inotify_problems, node_inotify, probe_image
from bbinstall.preflight import prepare
from bbinstall.sitefile import legacy_keys, migrate_site
from bbinstall.status import status_report

def chart(a):
    say(fetch_chart(a.version or Checkout(a.overlays).version))
    return 0


def check(a):
    """Every setting tested, nothing changed (the node check's pods are deleted); exit 1 if
    anything would stop the install."""
    co, site, defaults, enabled, problems, notes = prepare(a)
    logins = {}
    if not problems and not a.registry_mirror and not a.no_pull_check:
        step("images (a pull test of each, with your logins)")
        logins = check_logins(co, site)
        if not logins["cgr.dev"]:
            problems.append("no cgr.dev login: run `chainctl auth login`, or set CGR_USERNAME and CGR_PASSWORD")
        else:
            count, failed = image_problems(co, a.fips, enabled, logins)
            say(f"{count - len(failed)} of {count} images can be pulled")
            problems += [f"{p} (an image your Chainguard plan doesn't cover is refused the same way; "
                         "scripts/bb-org-repos.py names them)" if p.startswith("cgr.dev/") else p for p in failed]
    if not problems and "packages.neuvector" in enabled and not a.no_node_check:
        problems += node_problems(co, a.fips, logins.get("cgr.dev"))
    if problems:
        raise SystemExit("\nnot ready:\n  " + "\n  ".join(problems))
    say("\nready: every check passed. Run the same command with `install` (or `upgrade`) to apply it")
    return 0


def node_problems(co, fips, login):
    """The nodes' inotify limits, which NeuVector's enforcer needs raised (bbinstall.nodes).
    A node that can't be read is a note: the limits stay the site's to set."""
    image = probe_image(co, fips)
    if not image:
        return []
    step("nodes (inotify limits for NeuVector, a short-lived pod on each)")
    # With a registry mirror the nodes pull with their own identity; otherwise the cgr.dev login.
    readings, unread = node_inotify(image, login if image.startswith("cgr.dev/") else None)
    if readings:
        say(f"{len(readings)} node(s) read")
    if unread:
        say(f"note: couldn't read the inotify limits on {', '.join(unread)} (the probe pod didn't run): "
            "make sure each node has " + " and ".join(f"fs.inotify.{k}={v}" for k, v in INOTIFY.items()))
    return inotify_problems(readings)


def upgrade(a):
    """install for a cluster that runs Big Bang already: a Big Bang 3 site file converted
    first (backup kept), then the plan, a question, and the install."""
    a.version = a.version or Checkout(a.overlays).version
    if os.path.exists(a.site):
        site = yaml.safe_load(open(a.site, encoding="utf-8")) or {}
        defaults = chart_defaults(a)
        if is_bigbang4(defaults) and legacy_keys(site, defaults):
            new, changes = migrate_site(site, defaults)
            backup = a.site + ".bigbang3"
            if not os.path.exists(backup):
                shutil.copyfile(a.site, backup)
            with open(a.site, "w", encoding="utf-8", newline="\n") as f:
                f.write("# Converted to Big Bang 4's keys by bb-install.py upgrade; the original is "
                        f"{os.path.basename(backup)}.\n")
                yaml.safe_dump(new, f, sort_keys=False)
            step(f"site file converted to Big Bang 4 (original kept as {backup})")
            for c in changes:
                say(f"  {c}")
            say("Big Bang 4 also turns on Istio ambient mode (istio.ambient.enabled: false keeps sidecars) and "
                "uses CEL Kyverno policies: custom policy settings follow Big Bang's "
                "docs/migration/migrating-kyverno-policies-for-bb4.0.md")
    if not a.plan:
        install(argparse.Namespace(**{**vars(a), "plan": True}))
        if sys.stdin.isatty() and not a.yes and not confirm("Apply this?", default=False):
            raise SystemExit("not upgrading (--yes skips this question)")
    return install(a)


def status(a):
    co = Checkout(a.overlays)
    state = read_secret(NAMESPACE, STATE_SECRET) or {}
    if not state:
        say("note: no bigbang/bb-install Secret: not installed with bb-install.py (checking anyway)")
    return status_report(co, state.get("mode") == "fips")


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
