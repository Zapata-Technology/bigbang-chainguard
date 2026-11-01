"""Cluster helpers shared by bb-compat.py (kind tests) and bb-install.py (installs):
run kubectl/helm, create Secrets without printing them, wait for Flux and Big Bang's
HelmReleases, and report pods. Not a command; imported by those scripts.
"""
import base64
import json
import os
import re
import subprocess
import threading
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


def wait_ready(namespaces, timeout, abort=None):
    """Wait for every HelmRelease and the namespaces' pods; return (ok, summary lines).
    `abort`: a callable returning reasons to stop early (a failure that waiting won't fix,
    PodLogKeeper.abort_reasons); the wait then ends at once with those reasons."""
    end = time.time() + timeout
    last, polls = "", 0

    def stop():
        reasons = abort() if abort else []
        return (False, [f"stopped early: {r}" for r in reasons]) if reasons else None

    while time.time() < end:
        polls += 1
        if stopped := stop():
            return stopped
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
        if stopped := stop():
            return stopped
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


def started_images(pod):
    """Images of the pod's containers that are running or finished cleanly (an init
    container, a Job). A Pending pod's spec names images that never started."""
    statuses = {s["name"]: s for s in pod["status"].get("containerStatuses", [])
                + pod["status"].get("initContainerStatuses", [])}
    imgs = set()
    for c in pod["spec"].get("initContainers", []) + pod["spec"]["containers"]:
        state = statuses.get(c["name"], {}).get("state", {})
        if "running" in state or state.get("terminated", {}).get("exitCode") == 0:
            imgs |= {c["image"], statuses[c["name"]].get("image", "")}
    return imgs - {""}


def terminations(pod):
    """(container, reason, exit code) for each container of the pod that ended badly, now
    or before its last restart: OOMKilled, or a non-zero exit (Completed with 0 is a Job
    or an init container doing its work)."""
    found = []
    for s in pod["status"].get("containerStatuses", []) + pod["status"].get("initContainerStatuses", []):
        for t in (s.get("state", {}).get("terminated"), s.get("lastState", {}).get("terminated")):
            if t and (t.get("reason") == "OOMKilled" or t.get("exitCode")):
                found.append((s["name"], t.get("reason") or "Error", t.get("exitCode")))
    return found


# Log lines that mean something went wrong, whatever the app: shown live (LIVE), and a
# container that keeps crashing with one of them in its log stops the test early.
FATAL_LOG = re.compile(r"rake aborted!|panic: |Traceback \(most recent call last\)|\bFATAL\b|"
                       r"out of shared memory|exec format error|x509: |no space left on device|"
                       r"Exception in thread \"main\"|segmentation fault", re.I)
# Waiting reasons that mean the container can't even start (the image or its config).
STUCK_WAITING = ("ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError")


class PodLogKeeper:
    """Watches the test namespaces' pods while a test runs, every `interval` seconds:

    - saves each container's current log and, after a restart, the log of the run that
      ended, to `dest`. Flux uninstalls a release whose install fails, so without this a
      crash-looping pod's output is gone by the time the report runs;
    - prints the first FATAL_LOG line each container logs as it happens (`LIVE ...`);
    - remembers every bad termination (`exits`), so an OOM kill counts even if the pod was
      replaced;
    - collects reasons to stop the test early (`abort_reasons`): a failure that waiting
      won't fix, so a broken test ends in minutes with the cause named, not at its timeout.
    """

    CRASHES = 3            # restarts with a FATAL_LOG line before the test stops
    STUCK_SECONDS = 300    # how long a container may wait on its image or config

    def __init__(self, namespaces, dest, interval=30, clock=time.time):
        self.namespaces, self.dest, self.interval, self.clock = namespaces, dest, interval, clock
        self.exits, self._seen, self._stuck, self._abort = {}, set(), {}, {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        os.makedirs(self.dest, exist_ok=True)
        self._thread.start()
        return self

    def abort_reasons(self):
        return list(self._abort.values())

    def _keep(self, path, text):
        # Keep the last non-empty output: a deleted pod's logs come back empty.
        if text and text.strip():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

    def _fatal_line(self, where, text):
        """The first FATAL_LOG line of `text`, printed once per container as LIVE."""
        hit = next((ln.strip() for ln in (text or "").splitlines() if FATAL_LOG.search(ln)), None)
        if hit and where not in self._seen:
            self._seen.add(where)
            print(f"{time.strftime('%H:%M:%S')} LIVE {where}: {hit[:300]}", flush=True)
        return hit

    def _check_jobs(self, ns):
        out = kubectl("get", "job", "-n", ns, "-o", "json", check=False)
        for j in json.loads(out)["items"] if out else []:
            for c in j.get("status", {}).get("conditions", []):
                if c.get("type") == "Failed" and c.get("status") == "True":
                    name = j["metadata"]["name"]
                    self._abort[("job", ns, name)] = f"Job {ns}/{name} failed: {c.get('reason')} {c.get('message', '')}"

    def snapshot(self):
        now = self.clock()
        for ns in self.namespaces():
            self._check_jobs(ns)
            out = kubectl("get", "pod", "-n", ns, "-o", "json", check=False)
            for p in json.loads(out)["items"] if out else []:
                pod = p["metadata"]["name"]
                for c, reason, code in terminations(p):
                    self.exits[(ns, pod, c)] = (reason, code)
                    if reason == "OOMKilled":
                        self._abort[("oom", ns, pod, c)] = f"{ns}/{pod} container {c} was OOMKilled"
                statuses = {s["name"]: s for s in p["status"].get("containerStatuses", [])
                            + p["status"].get("initContainerStatuses", [])}
                for c in p["spec"].get("initContainers", []) + p["spec"]["containers"]:
                    name, s = c["name"], statuses.get(c["name"], {})
                    base = os.path.join(self.dest, "logs", ns, f"{pod}.{name}")
                    current = kubectl("logs", "-n", ns, pod, "-c", name, "--tail=2000", check=False)
                    self._keep(base + ".log", current)
                    self._fatal_line(f"{ns}/{pod} {name}", current)
                    if s.get("restartCount"):
                        previous = kubectl("logs", "-n", ns, pod, "-c", name, "--previous", "--tail=2000",
                                           check=False)
                        self._keep(base + ".previous.log", previous)
                        hit = self._fatal_line(f"{ns}/{pod} {name} (before a restart)", previous)
                        if hit and s["restartCount"] >= self.CRASHES:
                            self._abort[("crash", ns, pod, name)] = (
                                f"{ns}/{pod} container {name} restarted {s['restartCount']} times; its log: {hit[:200]}")
                    waiting = s.get("state", {}).get("waiting", {})
                    key = (ns, pod, name)
                    if waiting.get("reason") in STUCK_WAITING:
                        first = self._stuck.setdefault(key, now)
                        if now - first >= self.STUCK_SECONDS:
                            self._abort[("stuck",) + key] = (f"{ns}/{pod} container {name}: {waiting['reason']} for "
                                                             f"{int(now - first)}s: {waiting.get('message', '')[:200]}")
                    else:
                        self._stuck.pop(key, None)
        lines = [f"{ns}/{pod} {c}: {r} (exit {code})" for (ns, pod, c), (r, code) in sorted(self.exits.items())]
        self._keep(os.path.join(self.dest, "exits.txt"), "\n".join(lines) + "\n" if lines else "")

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.snapshot()
            except Exception as e:  # a snapshot must never break the test it watches
                print(f"(log keeper: {e})", flush=True)
            self._stop.wait(self.interval)

    def finish(self):
        """Stop, take a last snapshot, and save the cluster's state for the report:
        events, pod descriptions, node allocations and Flux's view. Runs once (the test
        calls it before its verdict; atexit covers a test that stops early)."""
        if self._stop.is_set():
            return
        self._stop.set()
        self._thread.join(timeout=120)
        try:
            self.snapshot()
        except Exception as e:
            print(f"(log keeper: {e})", flush=True)
        namespaces = self.namespaces()
        dumps = {"nodes.txt": [("describe", "nodes")],
                 "flux.txt": [("get", "helmrelease,gitrepository,helmrepository,helmchart,kustomization", "-A",
                               "-o", "wide"), ("describe", "helmrelease", "-n", "bigbang")]}
        for ns in namespaces:
            dumps[f"{ns}.txt"] = [("get", "pod,deploy,statefulset,job,pvc,svc", "-n", ns, "-o", "wide"),
                                  ("get", "events", "-n", ns, "--sort-by=.lastTimestamp"),
                                  ("describe", "pod", "-n", ns)]
        for name, cmds in dumps.items():
            text = "".join(f"$ kubectl {' '.join(c)}\n{kubectl(*c, check=False) or ''}\n" for c in cmds)
            self._keep(os.path.join(self.dest, "cluster", name), text)


def images_running(namespaces):
    imgs = set()
    for ns in namespaces:
        for p in json.loads(kubectl("get", "pod", "-n", ns, "-o", "json"))["items"]:
            imgs |= started_images(p)
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
