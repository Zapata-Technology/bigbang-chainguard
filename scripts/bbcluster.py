"""Cluster helpers shared by bb-compat.py (kind tests) and bb-install.py (installs):
run kubectl/helm, create Secrets without printing them, wait for Flux and Big Bang's
HelmReleases, and report pods. Not a command; imported by those scripts.
"""
import base64
import json
import subprocess
import time


def sh(*cmd, check=True, capture=False, stdin=None):
    p = subprocess.run(cmd, input=stdin, text=True, capture_output=capture)
    if check and p.returncode:
        raise SystemExit(f"failed ({p.returncode}): {' '.join(cmd[:4])} ...\n{(p.stderr or '')[-2000:]}")
    return p.stdout if capture else p.returncode


def kubectl(*args, capture=True, check=True):
    return sh("kubectl", *args, capture=capture, check=check)


def apply_secret(namespace, name, *args):
    """Create or update a Secret (kubectl create secret <args> --dry-run | apply).
    Values pass through stdin/files only, never the command line of a printed command."""
    kubectl("create", "namespace", namespace, check=False)
    manifest = kubectl("-n", namespace, "create", "secret", *args, name, "--dry-run=client", "-o", "yaml")
    sh("kubectl", "apply", "-f", "-", stdin=manifest, capture=True)


def dockerconfig(registries):
    """A .dockerconfigjson for [(registry, username, password), ...]."""
    return {"auths": {r: {"username": u, "password": p, "auth": base64.b64encode(f"{u}:{p}".encode()).decode()}
                      for r, u, p in registries}}


def read_secret(namespace, name):
    """{key: value} of a Secret, or None if it doesn't exist."""
    out = kubectl("get", "secret", "-n", namespace, name, "-o", "json", check=False)
    if not out:
        return None
    return {k: base64.b64decode(v).decode() for k, v in (json.loads(out).get("data") or {}).items()}


def install_flux(flux_dir, small_cluster=False, attempts=3):
    """Apply a flux/ kustomization (server-side; it pulls Flux's release manifest from
    github.com, so retry) and wait for the controllers. Returns True when ready."""
    for i in range(attempts):
        p = subprocess.run(["kubectl", "apply", "--server-side", "--force-conflicts", "-k", flux_dir],
                           capture_output=True, text=True)
        if not p.returncode:
            break
        if i == attempts - 1:
            raise SystemExit(f"installing Flux failed:\n{p.stderr[-2000:]}")
        time.sleep(30)
    if small_cluster:
        # Big Bang asks for up to 1.8 CPUs per Flux controller; a small test cluster
        # (kind on a CI runner) can't schedule all four. Requests down, limits kept.
        kubectl("-n", "flux-system", "set", "resources", "deploy", "--all", "--requests=cpu=50m,memory=64Mi",
                check=False)
    kubectl("-n", "flux-system", "rollout", "status", "deploy", "--timeout=600s", check=False)
    return flux_ready()


def flux_ready():
    deps = json.loads(kubectl("get", "deploy", "-n", "flux-system", "-o", "json"))["items"]
    return bool(deps) and all(d.get("status", {}).get("readyReplicas", 0) >= d["spec"].get("replicas", 1)
                              for d in deps)


SOURCE_KINDS = "gitrepositories.source.toolkit.fluxcd.io,helmrepositories.source.toolkit.fluxcd.io," \
               "ocirepositories.source.toolkit.fluxcd.io"


def source_problems(nudge=False):
    """Flux sources (package git repos, chart repos) that can't fetch, as lines naming the
    host: repo1.dso.mil refuses connections now and then, and a HelmRelease only says its
    chart "is not ready". With nudge, ask Flux to retry them now instead of at their interval."""
    out = kubectl("get", SOURCE_KINDS, "-A", "-o", "json", check=False)
    lines = []
    for s in json.loads(out)["items"] if out else []:
        ready = {c["type"]: c for c in s.get("status", {}).get("conditions", [])}.get("Ready", {})
        if ready.get("status") != "False":
            continue
        url = s["spec"].get("url", "")
        host = url.split("://", 1)[-1].split("/", 1)[0]
        ns, name = s["metadata"]["namespace"], s["metadata"]["name"]
        lines.append(f"{host or s['kind']}: {ns}/{name} can't fetch ({ready.get('message', '')[:160]})")
        if nudge:
            kubectl("-n", ns, "annotate", "--overwrite", s["kind"].lower(), name,
                    f"reconcile.fluxcd.io/requestedAt={time.time():.0f}", check=False)
    return lines


def wait_ready(namespaces, timeout):
    """Wait for every HelmRelease and the namespaces' pods; return (ok, summary lines)."""
    end = time.time() + timeout
    last, polls = "", 0
    while time.time() < end:
        polls += 1
        if polls % 6 == 0:   # every ~2 minutes: name sources that can't fetch, retry them
            for line in source_problems(nudge=True):
                print(f"{time.strftime('%H:%M:%S')} source: {line}; retrying", flush=True)
        out = kubectl("get", "helmrelease", "-A", "-o", "json", check=False)
        if not out:   # API server busy on a loaded runner: try again
            time.sleep(20)
            continue
        hrs = json.loads(out)["items"]
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
        return False, [f"timed out; HelmReleases: {last}"] + [
            f"{line} (Flux keeps retrying; re-run status later, or use --git-mirror)" for line in source_problems()]
    # Give slow starters time (NeuVector's enforcers restart until they join the
    # controllers): poll until every pod that isn't a finished Job pod is Ready with no
    # new restarts for three polls in a row (a crash-looping pod is Ready between
    # crashes), or the time runs out. report() judges the result.
    steady, last_restarts = 0, None
    while time.time() < end and steady < 3:
        pending, restarts = [], 0
        for ns in namespaces:
            out = kubectl("get", "pod", "-n", ns, "-o", "json", check=False)
            for p in json.loads(out)["items"] if out else []:
                if p["status"].get("phase") == "Succeeded":
                    continue
                statuses = p["status"].get("containerStatuses", [{}])
                restarts += sum(s.get("restartCount", 0) for s in statuses)
                if not all(s.get("ready") for s in statuses):
                    pending.append(f"{ns}/{p['metadata']['name']}")
        steady = steady + 1 if not pending and restarts == last_restarts else 0
        last_restarts = restarts
        if pending:
            print(f"{time.strftime('%H:%M:%S')} waiting for {len(pending)} pods: {', '.join(pending[:6])}", flush=True)
        time.sleep(20)
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
    ok, total = True, 0
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
        total += len(pods)
    # A namespace may hold only CRDs or config (e.g. prometheus-operator-crds); the test
    # still needs some pods unless it says it has none (a CronJob-only package).
    return ok and (total > 0 or no_pods)
