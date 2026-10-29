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
import base64
import json
import os
import subprocess
import sys
import tempfile
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bbcommon import ROOT, build_registry, deep_merge, load_build  # noqa: E402


def sh(*cmd, check=True, capture=False, stdin=None):
    p = subprocess.run(cmd, input=stdin, text=True, capture_output=capture)
    if check and p.returncode:
        raise SystemExit(f"failed ({p.returncode}): {' '.join(cmd[:4])} ...\n{(p.stderr or '')[-2000:]}")
    return p.stdout if capture else p.returncode


def kubectl(*args, capture=True, check=True):
    return sh("kubectl", *args, capture=capture, check=check)


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
    dockercfg = {"auths": {r: {"username": u, "password": p, "auth": base64.b64encode(f"{u}:{p}".encode()).decode()}
                           for r, u, p in regs}}
    return values, dockercfg


def flux_ready():
    deps = json.loads(kubectl("get", "deploy", "-n", "flux-system", "-o", "json"))["items"]
    return bool(deps) and all(d.get("status", {}).get("readyReplicas", 0) >= d["spec"].get("replicas", 1)
                              for d in deps)


def wait_ready(namespaces, timeout):
    """Wait for every HelmRelease and the namespaces' pods; return (ok, summary lines)."""
    end = time.time() + timeout
    last = ""
    while time.time() < end:
        hrs = json.loads(kubectl("get", "helmrelease", "-A", "-o", "json"))["items"]
        status = {}
        for hr in hrs:
            cond = {c["type"]: c for c in hr.get("status", {}).get("conditions", [])}
            ready = cond.get("Ready", {})
            status[hr["metadata"]["name"]] = (ready.get("status"), ready.get("reason", ""), ready.get("message", ""))
        line = ", ".join(f"{n}={s[0]}" for n, s in sorted(status.items()))
        detail = "; ".join(f"{n}: {s[2][:200]}" for n, s in sorted(status.items()) if s[0] == "False")
        if line != last:
            print(f"{time.strftime('%H:%M:%S')} helmreleases: {line or '(none yet)'}"
                  + (f"  [{detail}]" if detail else ""), flush=True)
            last = line
        failed = [n for n, s in status.items() if s[0] == "False" and s[1] in ("InstallFailed", "UpgradeFailed")]
        if failed:
            return False, [f"HelmRelease {n}: {status[n][1]} {status[n][2]}" for n in failed]
        if status and all(s[0] == "True" for s in status.values()):
            break
        time.sleep(20)
    else:
        return False, [f"timed out; HelmReleases: {last}"]
    for ns in namespaces:
        # report() judges each pod; this only gives slow starters time.
        kubectl("wait", "pod", "-n", ns, "--all", "--for=condition=Ready",
                f"--timeout={max(60, int(end - time.time()))}s",
                "--field-selector=status.phase!=Succeeded", check=False)
    return True, []


def hr_namespaces():
    """Namespaces the HelmReleases install into (when a test doesn't list them)."""
    hrs = json.loads(kubectl("get", "helmrelease", "-A", "-o", "json"))["items"]
    return sorted({hr["spec"].get("targetNamespace") or hr["metadata"]["namespace"] for hr in hrs})


def images_running(namespaces):
    imgs = set()
    for ns in namespaces:
        for p in json.loads(kubectl("get", "pod", "-n", ns, "-o", "json"))["items"]:
            for s in p["status"].get("containerStatuses", []) + p["status"].get("initContainerStatuses", []):
                imgs.add(s.get("image", ""))
            for c in p["spec"].get("initContainers", []) + p["spec"]["containers"]:
                imgs.add(c["image"])
    return imgs


def report(namespaces, no_pods=False):
    ok = True
    for ns in namespaces:
        pods = json.loads(kubectl("get", "pod", "-n", ns, "-o", "json"))["items"]
        print(f"\n== {ns}: {len(pods)} pods")
        for p in pods:
            phase = p["status"].get("phase")
            statuses = p["status"].get("containerStatuses", []) + p["status"].get("initContainerStatuses", [])
            ready = all(s.get("ready") for s in p["status"].get("containerStatuses", []))
            restarts = sum(s.get("restartCount", 0) for s in statuses)
            good = phase == "Succeeded" or (phase == "Running" and ready)
            ok &= good
            print(f"  {'OK  ' if good else 'FAIL'} {p['metadata']['name']} {phase} ready={ready} restarts={restarts}")
            for c in p["spec"].get("initContainers", []) + p["spec"]["containers"]:
                print(f"         {c['name']}: {c['image']}")
            if not good or restarts:
                for s in statuses:
                    st = s.get("state", {})
                    why = st.get("waiting") or st.get("terminated") or {}
                    if why:
                        print(f"         {s['name']} {why.get('reason')}: {str(why.get('message', ''))[:300]}")
                logs = kubectl("logs", "-n", ns, p["metadata"]["name"], "--all-containers", "--tail=40",
                               check=False) or ""
                print("         logs:\n" + "\n".join("           " + ln for ln in logs.splitlines()[-40:]))
        if not pods and not no_pods:
            ok = False
    return ok


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
        sh("kubectl", "apply", "--server-side", "--force-conflicts", "-k", flux_dir, capture=True)
        # Big Bang asks for up to 1.8 CPUs per Flux controller; a CI runner's one kind
        # node can't schedule all four. Test clusters only: requests down, limits kept.
        kubectl("-n", "flux-system", "set", "resources", "deploy", "--all", "--requests=cpu=50m,memory=64Mi",
                check=False)
        kubectl("-n", "flux-system", "rollout", "status", "deploy", "--timeout=600s", check=False)
        if not flux_ready():
            report(["flux-system"])
            print(kubectl("get", "events", "-n", "flux-system", "--sort-by=.lastTimestamp", check=False) or "")
            raise SystemExit("\nRESULT: Flux did not start (pods and events above); Big Bang was not deployed")

        chart = os.path.join(a.bigbang, "chart")
        values = test_values(yaml.safe_load(open(os.path.join(chart, "values.yaml"))), test.get("enable", []))
        deep_merge(values, test.get("values", {}) or {})
        tv = os.path.join(secrets, "test.yaml")
        yaml.safe_dump(values, open(tv, "w"))
        args = ["-f", os.path.join(out, "overlays", "values-chainguard.yaml")]
        if a.fips:
            args += ["-f", os.path.join(out, "overlays", "values-chainguard-fips.yaml")]
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
        print(subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout[-4000:])
    # `expect`: images (name:tag fragments) the test is about. A pass only covers the
    # ones that actually ran; the rest need the feature that uses them switched on.
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
