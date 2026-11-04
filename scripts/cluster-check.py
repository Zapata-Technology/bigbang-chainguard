#!/usr/bin/env python3
"""Fail unless every pod in the cluster is healthy and nothing was refused admission: the
verdict of a whole-cluster test (.github/workflows/rke2.yml), after bb-install.py install.

  cluster-check.py [--events FILE] [--max-restarts 0]

Across every namespace (Big Bang's, Flux's, SeaweedFS's and the cluster's own):
  - each pod Running with every container Ready, or Succeeded (a finished Job)
  - no container restarted more than --max-restarts times
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


def pod_problems(pods, max_restarts=0):
    """PROBLEM lines for pods that aren't Running and Ready (or Succeeded), restarted, or
    wait on an image or config they can't get."""
    problems = []
    for p in pods:
        name = f"{p['metadata']['namespace']}/{p['metadata']['name']}"
        status = p.get("status", {})
        phase = status.get("phase")
        containers = status.get("containerStatuses", [])
        statuses = containers + status.get("initContainerStatuses", [])
        for s in statuses:
            waiting = s.get("state", {}).get("waiting", {}).get("reason")
            if waiting in STUCK_WAITING:
                problems.append(f"{name} container {s['name']}: {waiting} "
                                f"({s.get('state', {}).get('waiting', {}).get('message', '')[:200]})")
            if s.get("restartCount", 0) > max_restarts:
                last = s.get("lastState", {}).get("terminated", {})
                problems.append(f"{name} container {s['name']} restarted {s['restartCount']} times"
                                + (f" (last: {last.get('reason')}, exit {last.get('exitCode')})" if last else ""))
        if phase == "Succeeded":
            continue
        if phase != "Running" or not containers or not all(s.get("ready") for s in containers):
            problems.append(f"{name} is {phase}, not Ready"
                            + (f": {status['reason']}" if status.get("reason") else ""))
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


def event_problems(events):
    """(problems, notes): each admission refusal is a problem; an image pull failure is a
    note (the pod checks above fail one that never recovered). Repeats are reported once."""
    problems, notes, seen = [], [], set()
    for ns, _type, reason, obj, message in events:
        key = (ns, obj, message[:160])
        if key in seen:
            continue
        seen.add(key)
        if REFUSED.search(message):
            problems.append(f"{ns}/{obj} refused admission ({reason}): {message[:400]}")
        elif PULL_FAILED.search(message):
            notes.append(f"{ns}/{obj} pull failed at some point: {message[:300]}")
    return problems, notes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", help="event lines kept during the install (.github/rke2-vm.sh events)")
    ap.add_argument("--max-restarts", type=int, default=0, help="restarts a container may have (default 0)")
    a = ap.parse_args()

    pods = json.loads(kubectl("get", "pod", "-A", "-o", "json"))["items"]
    problems = pod_problems(pods, a.max_restarts)
    events = json_events(json.loads(kubectl("get", "events", "-A", "-o", "json")))
    if a.events and os.path.exists(a.events):
        events += parse_event_lines(open(a.events, encoding="utf-8", errors="replace").read())
    refused, notes = event_problems(events)
    problems += refused
    namespaces = sorted({p["metadata"]["namespace"] for p in pods})
    print(f"{len(pods)} pods in {len(namespaces)} namespaces ({', '.join(namespaces)}); {len(events)} events read")
    for n in notes:
        print("NOTE:", n)
    for p in problems:
        print("PROBLEM:", p)
    print(f"\nRESULT cluster-check: {'PASS' if not problems else f'FAIL ({len(problems)} problems)'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
