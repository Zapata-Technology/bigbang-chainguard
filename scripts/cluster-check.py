#!/usr/bin/env python3
"""Fail unless every pod in the cluster is healthy and nothing was refused admission: the
verdict of a whole-cluster test (.github/workflows/rke2.yml), after bb-install.py install.

  cluster-check.py [--events FILE] [--max-restarts 0]

Every HelmRelease Ready (Flux uninstalls a release whose install failed, so its pods are
gone by now), and across every namespace (Big Bang's, Flux's, SeaweedFS's and the cluster's own):
  - each pod Running with every container Ready, or Succeeded (a finished Job)
  - no container restarted more than --max-restarts times (a finished Job's retries aside:
    RKE2's own helm-install Jobs retry while the API settles). A container that restarted
    at most STARTUP_RESTARTS times while what it needs came up, and has been Ready and
    running for SETTLED seconds since, is a note: NeuVector's exporter exits until the
    controller answers, then runs.
  - no container waiting on an image it can't pull (ErrImagePull, ImagePullBackOff,
    InvalidImageName) or on its config (CreateContainerConfigError)
  - no pod refused by Pod Security Admission or an admission webhook (Kyverno), in the
    cluster's events and FILE: `kubectl get events --watch` lines kept for the whole install
    (.github/rke2-vm.sh events), since the API server forgets events after an hour.
    A controller whose pod was refused retries, so the pod may never show up as failing.
An image pull that failed and later succeeded is listed, not failed (a registry hiccup).
Prints PROBLEM lines and a RESULT line; exit 1 on any problem.
"""
import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bbcluster import STUCK_WAITING, kubectl  # noqa: E402

# Admission refusals, as the controllers that create pods report them (FailedCreate).
REFUSED = re.compile(r"violates PodSecurity|admission webhook \S+ denied the request")
PULL_FAILED = re.compile(r"Failed to pull image|ErrImagePull|ImagePullBackOff")
STARTUP_RESTARTS = 5
SETTLED = 600


def settled(s, now):
    """Whether a container's restarts were startup only: few, and Ready and running for
    SETTLED seconds since the last one."""
    started = s.get("state", {}).get("running", {}).get("startedAt")
    if not (started and s.get("ready") and s.get("restartCount", 0) <= STARTUP_RESTARTS):
        return False
    since = now - datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
    return since.total_seconds() >= SETTLED


def pod_problems(pods, max_restarts=0, now=None, notes=None):
    """PROBLEM lines for pods that aren't Running and Ready (or Succeeded), restarted, or
    wait on an image or config they can't get. Startup restarts go to `notes`."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    problems = []
    for p in pods:
        name = f"{p['metadata']['namespace']}/{p['metadata']['name']}"
        status = p.get("status", {})
        phase = status.get("phase")
        containers = status.get("containerStatuses", [])
        statuses = containers + status.get("initContainerStatuses", [])
        for s in statuses:
            if phase == "Succeeded":
                continue
            waiting = s.get("state", {}).get("waiting", {}).get("reason")
            if waiting in STUCK_WAITING:
                problems.append(f"{name} container {s['name']}: {waiting} "
                                f"({s.get('state', {}).get('waiting', {}).get('message', '')[:200]})")
            if s.get("restartCount", 0) > max_restarts:
                last = s.get("lastState", {}).get("terminated", {})
                line = (f"{name} container {s['name']} restarted {s['restartCount']} times"
                        + (f" (last: {last.get('reason')}, exit {last.get('exitCode')})" if last else ""))
                if settled(s, now) and notes is not None:
                    notes.append(line + f", while starting; Ready since {s['state']['running']['startedAt']}")
                else:
                    problems.append(line)
        if phase == "Succeeded":
            continue
        if phase != "Running" or not containers or not all(s.get("ready") for s in containers):
            problems.append(f"{name} is {phase}, not Ready"
                            + (f": {status['reason']}" if status.get("reason") else ""))
    return problems


def helmrelease_problems(hrs):
    """PROBLEM lines for HelmReleases that aren't Ready, with Flux's reason."""
    problems = []
    for hr in hrs:
        ready = {c["type"]: c for c in hr.get("status", {}).get("conditions", [])}.get("Ready", {})
        if ready.get("status") != "True":
            problems.append(f"HelmRelease {hr['metadata']['namespace']}/{hr['metadata']['name']} not Ready: "
                            f"{ready.get('message', 'no status yet')[:300]}")
    return problems


def parse_event_lines(text):
    """(namespace, type, reason, object, message) from `.github/rke2-vm.sh events` lines."""
    out = []
    for line in text.splitlines():
        parts = line.split("\t", 4)
        if len(parts) == 5:
            out.append(tuple(parts))
    return out


def json_events(doc):
    """The same tuples from `kubectl get events -A -o json`."""
    return [(e["metadata"].get("namespace", ""), e.get("type", ""), e.get("reason", ""),
             f"{e.get('involvedObject', {}).get('kind')}/{e.get('involvedObject', {}).get('name')}",
             e.get("message", "")) for e in doc.get("items", [])]


def _same(obj, message):
    """An event's object and message without what changes between retries: the pod's
    random name, a ReplicaSet's template hash, the "combined from similar events" prefix."""
    obj = re.sub(r"^(ReplicaSet/.+)-[0-9a-f]{6,10}$", r"\1", obj)
    message = re.sub(r'pods "[^"]+"', "pods", message.replace("(combined from similar events): ", ""))
    return obj, message[:200]


def event_problems(events):
    """(problems, notes): each admission refusal is a problem; an image pull failure is a
    note (the pod checks above fail one that never recovered). A controller retries a refused
    pod every few seconds: each distinct refusal is reported once, with how often it was seen."""
    counts, order = {}, []
    for ns, _type, reason, obj, message in events:
        if not (REFUSED.search(message) or PULL_FAILED.search(message)):
            continue
        key = (ns, reason, *_same(obj, message))
        if key not in counts:
            order.append(key)
        counts[key] = counts.get(key, 0) + 1
    problems, notes = [], []
    for ns, reason, obj, message in order:
        n = counts[(ns, reason, obj, message)]
        seen = f" [seen {n} times]" if n > 1 else ""
        if REFUSED.search(message):
            problems.append(f"{ns}/{obj} refused admission ({reason}){seen}: {message}")
        else:
            notes.append(f"{ns}/{obj} pull failed at some point{seen}: {message}")
    return problems, notes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", help="event lines kept during the install (.github/rke2-vm.sh events)")
    ap.add_argument("--max-restarts", type=int, default=0, help="restarts a container may have (default 0)")
    a = ap.parse_args()

    hrs = json.loads(kubectl("get", "helmrelease", "-A", "-o", "json", check=False) or '{"items": []}')["items"]
    pods = json.loads(kubectl("get", "pod", "-A", "-o", "json"))["items"]
    startup = []
    problems = helmrelease_problems(hrs) + pod_problems(pods, a.max_restarts, notes=startup)
    events = json_events(json.loads(kubectl("get", "events", "-A", "-o", "json")))
    if a.events and os.path.exists(a.events):
        events += parse_event_lines(open(a.events, encoding="utf-8", errors="replace").read())
    refused, notes = event_problems(events)
    notes = startup + notes
    problems += refused
    namespaces = sorted({p["metadata"]["namespace"] for p in pods})
    print(f"{len(hrs)} HelmReleases; {len(pods)} pods in {len(namespaces)} namespaces ({', '.join(namespaces)}); {len(events)} events read")
    for n in notes:
        print("NOTE:", n)
    for p in problems:
        print("PROBLEM:", p)
    print(f"\nRESULT cluster-check: {'PASS' if not problems else f'FAIL ({len(problems)} problems)'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
