#!/usr/bin/env python3
"""Deploy one Big Bang package on Chainguard images and check that it runs.

Rendering proves the overlays name the right images; this proves the package works
with them. Used for upgrades the mapping doesn't make on its own (a newer major or
minor than Big Bang pins): a compat/<test>.yaml file holds the trial mapping and
which packages to deploy.

  bb-compat.py compat/vault-2.yaml [--fips] [--timeout 1500]

Against the current kubectl context (a throwaway kind cluster; the compat workflow
creates one per test):
  1. bb-convert.py with the compat file's `images` over the mapping, into a temp dir
  2. Flux from the generated flux/ kustomization
  3. Big Bang (work/bigbang/chart) with the overlays and only the test's packages
  4. wait until every HelmRelease is Ready and the test namespaces' pods are Ready
  5. report each pod's images and status; on failure, events and logs

Pull credentials come from the environment and are never printed:
  CGR_TOKEN                   cgr.dev token (chainctl auth token --audience cgr.dev)
  GHCR_USER, GHCR_TOKEN       for BUILD_REGISTRY on ghcr.io (optional)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bbcommon import ROOT, build_registry, deep_merge, load_build, load_mapping, running_image_problems  # noqa: E402
from orgtags import org_name  # noqa: E402
from bbcluster import (dockerconfig, hr_namespaces, images_running, install_flux,  # noqa: E402
                       kubectl, report, sh, wait_ready)


def test_values(chart_values, enable):
    """Turn off every package Big Bang enables by default (true or "auto", like metricsServer),
    then turn on the test's."""
    vals = {"domain": "bigbang.test", "networkPolicies": {"enabled": False}}
    for k, v in chart_values.items():
        if isinstance(v, dict) and v.get("enabled") not in (False, None) and k != "networkPolicies":
            vals[k] = {"enabled": False}
    for k, v in chart_values.get("addons", {}).items():
        if isinstance(v, dict) and v.get("enabled") not in (False, None):
            vals.setdefault("addons", {})[k] = {"enabled": False}
    for dotted in enable:
        node = vals
        for part in dotted.split("."):
            node = node.setdefault(part, {})
        node["enabled"] = True
    return vals


def credentials():
    regs = []
    if os.environ.get("CGR_TOKEN"):
        regs.append(("cgr.dev", "_token", os.environ["CGR_TOKEN"]))
    if os.environ.get("GHCR_TOKEN"):
        regs.append(("ghcr.io", os.environ.get("GHCR_USER", "x"), os.environ["GHCR_TOKEN"]))
    if not regs:
        raise SystemExit("set CGR_TOKEN (and GHCR_TOKEN for BUILD_REGISTRY images)")
    values = {"registryCredentials": [{"registry": r, "username": u, "password": p} for r, u, p in regs]}
    return values, dockerconfig(regs)


def run_installer(a, test, out):
    """Install with bb-install.py from the regenerated overlays in `out`: a site file
    from the test's packages and values, credentials only through the environment, and
    the chart the way a user gets it (no --chart: downloaded from repo1)."""
    chart = os.path.join(a.bigbang, "chart")
    values = test_values(yaml.safe_load(open(os.path.join(chart, "values.yaml"))), test.get("enable", []))
    deep_merge(values, test.get("values", {}) or {})
    site = os.path.join(out, "site.yaml")
    yaml.safe_dump(values, open(site, "w"))
    env = dict(os.environ)
    if os.environ.get("CGR_TOKEN"):
        env.update(CGR_USERNAME="_token", CGR_PASSWORD=os.environ["CGR_TOKEN"])
    if os.environ.get("GHCR_TOKEN"):
        env.update(BUILD_REGISTRY_USERNAME=os.environ.get("GHCR_USER", "x"),
                   BUILD_REGISTRY_PASSWORD=os.environ["GHCR_TOKEN"])
    base = [sys.executable, os.path.join(HERE, "bb-install.py"), "--overlays", out]
    install = base + ["install", "--site", site, "--small-cluster", "--yes",
                      "--timeout", str(a.timeout)] + (["--fips"] if a.fips else []) + test.get("installer_args", [])
    print(f"\n== bb-install.py install ({'FIPS' if a.fips else 'standard'})", flush=True)
    rc = subprocess.run(install, env=env, cwd=ROOT).returncode
    print(f"\n== bb-install.py status (install exited {rc})", flush=True)
    subprocess.run(base + ["status"], env=env, cwd=ROOT)
    if rc:
        print(f"PROBLEM: bb-install.py install exited {rc}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("compat", help="compat/<test>.yaml")
    ap.add_argument("--fips", action="store_true", help="deploy with the FIPS overlay too")
    ap.add_argument("--timeout", type=int, default=1500, help="seconds to wait for the package (default 1500)")
    ap.add_argument("--bigbang", default=os.path.join(ROOT, "work", "bigbang"))
    ap.add_argument("--kind-cluster", default=os.environ.get("KIND_CLUSTER"),
                    help="load images this repo builds into this kind cluster instead of pulling them")
    a = ap.parse_args()

    spec = yaml.safe_load(open(a.compat))
    test = spec.get("test", {})
    out = tempfile.mkdtemp(prefix="bb-compat-")
    print(f"== {os.path.basename(a.compat)}: trial mapping {json.dumps(spec.get('images', {}))}", flush=True)

    # Images the trial mapping builds (`build:`, with an optional `version` line): generate,
    # build and smoke-test them here and load them into kind, so an upgrade is tested
    # before anything is pushed. Before bb-convert, which reads their build.yaml.
    for m in spec.get("images", {}).values():
        if isinstance(m, dict) and m.get("build"):
            name = m["build"]
            print(f"\n== build {name}", flush=True)
            version = ["--version", str(m["version"])] if m.get("version") else []
            rc = subprocess.run([sys.executable, os.path.join(HERE, f"bb-{name}.py"), *version,
                                 "--build", "--test", "--force"]).returncode
            if rc:
                raise SystemExit(f"\nRESULT: building {name} failed (see above); nothing was deployed")
            img = load_build(name)["image"]["fips" if a.fips else "standard"]
            ref = f"{build_registry() or 'bb-local'}/{img['name']}:{img['tag']}"
            if a.kind_cluster:
                sh("kind", "load", "docker-image", ref, "--name", a.kind_cluster, capture=True)
                print(f"loaded {ref} into kind/{a.kind_cluster}")

    rc = subprocess.run([sys.executable, os.path.join(HERE, "bb-convert.py"), "--mapping-overlay", a.compat,
                         "--out", out]).returncode
    if rc not in (0, 3):
        raise SystemExit("bb-convert failed with the trial mapping (see above)")
    for ln in open(os.path.join(out, "docs", "images.md"), encoding="utf-8"):
        if any(k in ln for k in spec.get("images", {})):
            print(ln.rstrip())

    if test.get("installer"):
        # `installer: true`: deploy with scripts/bb-install.py exactly as a user would,
        # then check the result like any other test.
        run_installer(a, test, out)
    else:
        cred_values, dockercfg = credentials()
        secrets = tempfile.mkdtemp(prefix="bb-compat-secrets-")
        try:
            creds = os.path.join(secrets, "creds.yaml")
            yaml.safe_dump(cred_values, open(creds, "w"))
            dcj = os.path.join(secrets, "config.json")
            json.dump(dockercfg, open(dcj, "w"))

            print("\n== flux", flush=True)
            kubectl("create", "namespace", "flux-system", check=False)
            secret = kubectl("-n", "flux-system", "create", "secret", "generic", "private-registry",
                             "--type=kubernetes.io/dockerconfigjson", f"--from-file=.dockerconfigjson={dcj}",
                             "--dry-run=client", "-o", "yaml")
            sh("kubectl", "apply", "-f", "-", stdin=secret, capture=True)
            flux_dir = os.path.join(out, "flux-fips" if a.fips else "flux")
            if not install_flux(flux_dir, small_cluster=True):
                report(["flux-system"])
                print(kubectl("get", "events", "-n", "flux-system", "--sort-by=.lastTimestamp", check=False) or "")
                raise SystemExit("\nRESULT: Flux did not start (pods and events above); Big Bang was not deployed")

            # `setup`: commands run before Big Bang (e.g. deploy SeaweedFS and generate the
            # object-storage overlay). They get COMPAT_OUT (a scratch dir for files they write,
            # which `values_files` can name), COMPAT_DOCKERCFG (registry credentials for their
            # own pull secrets) and COMPAT_FIPS.
            env = dict(os.environ, COMPAT_OUT=out, COMPAT_DOCKERCFG=dcj, COMPAT_FIPS="1" if a.fips else "")
            for cmd in test.get("setup", []):
                print(f"\n== setup: {cmd}", flush=True)
                if subprocess.run(cmd, shell=True, cwd=ROOT, env=env).returncode:
                    raise SystemExit(f"\nRESULT {os.path.basename(a.compat)}: FAIL (setup failed: {cmd})")

            chart = os.path.join(a.bigbang, "chart")
            values = test_values(yaml.safe_load(open(os.path.join(chart, "values.yaml"))), test.get("enable", []))
            deep_merge(values, test.get("values", {}) or {})
            tv = os.path.join(secrets, "test.yaml")
            yaml.safe_dump(values, open(tv, "w"))
            args = ["-f", os.path.join(out, "overlays", "values-chainguard.yaml")]
            if a.fips:
                args += ["-f", os.path.join(out, "overlays", "values-chainguard-fips.yaml")]
            for f in test.get("values_files", []):
                args += ["-f", f.replace("$COMPAT_OUT", out)]
            print(f"\n== bigbang ({'FIPS' if a.fips else 'standard'}): {', '.join(test.get('enable', []))}", flush=True)
            sh("helm", "upgrade", "-i", "bigbang", chart, "-n", "bigbang", "--create-namespace",
               *args, "-f", tv, "-f", creds, "--timeout", "10m", capture=True)
        finally:
            for f in os.listdir(secrets):
                os.remove(os.path.join(secrets, f))
            os.rmdir(secrets)

    ready, problems = wait_ready(test.get("namespaces") or hr_namespaces(), a.timeout)
    namespaces = test.get("namespaces") or hr_namespaces()
    ok = report(namespaces, test.get("no_pods", False)) and ready
    for p in problems:
        print("PROBLEM:", p)
    if not ok:
        print("\n== recent events")
        for ns in namespaces + ["bigbang"]:
            print(kubectl("get", "events", "-n", ns, "--sort-by=.lastTimestamp", check=False) or "")
    # `debug`: shell commands whose output helps explain a result (e.g. the sidecar
    # injector's config); run whatever the outcome.
    for cmd in test.get("debug", []):
        print(f"\n== debug: {cmd}")
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=ROOT,
                           env=dict(os.environ, COMPAT_OUT=out))
        print((p.stdout + p.stderr)[-4000:])
    # `expect`: images (name:tag fragments) the test is about. A pass only covers the
    # ones that actually ran; the rest need the feature that uses them switched on.
    # Every image the test's namespaces and Flux run must be the conversion's: the org,
    # BUILD_REGISTRY or a documented Iron Bank leftover, and -fips in FIPS mode. This is
    # what catches images injected at runtime (Istio's istio-init sidecar).
    spec_images = set()
    for ns in namespaces + ["flux-system"]:
        out_ns = kubectl("get", "pod", "-n", ns, "-o", "json", check=False)
        for p in json.loads(out_ns)["items"] if out_ns else []:
            spec_images |= {c["image"] for c in p["spec"].get("initContainers", []) + p["spec"]["containers"]}
    org = org_name()
    policy = running_image_problems(spec_images, org, build_registry(),
                                    load_mapping(org, overlays=[a.compat]), fips=a.fips)
    for p in policy:
        print("IMAGE POLICY:", p)
    print(f"image policy: {len(spec_images)} images, {len(policy)} problems")
    ok = ok and not policy
    # `forbid_log`: a regex no pod in the test namespaces may log (e.g. S3 errors).
    if test.get("forbid_log"):
        rx = re.compile(test["forbid_log"])
        for ns in namespaces:
            out_ns = kubectl("get", "pod", "-n", ns, "-o", "jsonpath={.items[*].metadata.name}", check=False) or ""
            for pod in out_ns.split():
                logs = kubectl("logs", "-n", ns, pod, "--all-containers", "--tail=500", check=False) or ""
                hits = [ln for ln in logs.splitlines() if rx.search(ln)]
                if hits:
                    ok = False
                    print(f"FORBIDDEN LOG in {ns}/{pod}: {hits[0][:300]}")
    # `check`: shell commands that must succeed for a pass (e.g. an HTTPS request through
    # the gateway with the CA the installer made).
    for cmd in test.get("check", []):
        print(f"\n== check: {cmd}", flush=True)
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=ROOT,
                           env=dict(os.environ, COMPAT_OUT=out, COMPAT_FIPS="1" if a.fips else ""))
        print((p.stdout + p.stderr)[-2000:])
        if p.returncode:
            ok = False
            print(f"CHECK FAILED ({p.returncode}): {cmd}")
    running = images_running(namespaces)
    expect = test.get("expect", [])
    seen = [e for e in expect if any(e in i for i in running)]
    for e in expect:
        print(f"{'EXERCISED' if e in seen else 'NOT EXERCISED'}: {e}")
    mode = "FIPS" if a.fips else "standard"
    print(f"\nRESULT {os.path.basename(a.compat)} ({mode}): {'PASS' if ok else 'FAIL'}"
          + (f"; {len(seen)} of {len(expect)} expected images ran" if expect else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
