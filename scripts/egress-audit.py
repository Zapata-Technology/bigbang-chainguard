#!/usr/bin/env python3
"""Hosts a workflow's runs reached that its harden-runner allowed-endpoints list doesn't name.

  python scripts/egress-audit.py                          # every workflow that blocks egress,
                                                          # its latest good run of each event
  python scripts/egress-audit.py builds.yml=42116994913   # one workflow against one run

Reads harden-runner's "domain resolved" and "endpoint called" lines from each run's log
(`gh run view --log`) and prints the hosts the workflow's lists don't cover (wildcards
honoured), with the processes that called them. GitHub's and StepSecurity's own hosts are
skipped: harden-runner allows those itself. Exit 1 when a host is missing.

Why: until 2026-11-11 harden-runner let some unlisted hosts through, then started refusing
them (AGENTS.md, the harden-runner trap). Since then a passing run reaches only listed hosts,
so audit a run from before that date, or a run with `egress-policy: audit` (how to find what
a new tool calls before switching back to block). Audit a run of each event: a pull
request's build never signs or pushes, so it misses what main's does. Needs `gh`.
"""
import argparse
import os
import re
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
# GitHub's and StepSecurity's own hosts, which harden-runner allows by itself.
INFRA = re.compile(r"(\.stepsecurity\.io|\.actions\.githubusercontent\.com|\.githubapp\.com"
                   r"|^productionresultssa\d+\.blob\.core\.windows\.net)$")
RESOLVED = re.compile(r"domain resolved: (?P<host>[A-Za-z0-9.-]+?)\.?, ip address")
CALLED = re.compile(r"endpoint called .*?domain: (?P<host>[A-Za-z0-9.-]+?)\.?, .*?process: (?P<proc>[^,\s]+)")


def gh(*args):
    p = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.stdout if p.returncode == 0 else ""


def allowed(workflow_text):
    """Every host any harden-runner step of the workflow allows (port dropped)."""
    hosts = set()

    def walk(node):
        if isinstance(node, dict):
            if "allowed-endpoints" in node:
                hosts.update(h.rsplit(":", 1)[0] for h in str(node["allowed-endpoints"]).split())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(yaml.safe_load(workflow_text))
    return hosts


def covered(host, hosts):
    return host in hosts or any(h.startswith("*.") and host.endswith(h[1:]) for h in hosts)


def reached(log):
    """{host: set of processes that called it} from a run's harden-runner lines."""
    seen = {}
    for ln in log.splitlines():
        m = CALLED.search(ln) or RESOLVED.search(ln)
        if m:
            procs = seen.setdefault(m["host"].lower(), set())
            if "proc" in m.groupdict():
                procs.add(m["proc"])
    return seen


def unlisted(seen, hosts):
    """{host: processes} for the hosts reached that the lists don't cover."""
    return {h: p for h, p in sorted(seen.items()) if not covered(h, hosts) and not INFRA.search(h)}


def blocking_workflows():
    for name in sorted(os.listdir(WORKFLOWS)):
        with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as f:
            if re.search(r"egress-policy:\s*block", f.read()):
                yield name


def latest_good_runs(workflow):
    """The newest successful run id of each event the workflow ran for."""
    out = gh("run", "list", "--workflow", workflow, "--status", "success", "--limit", "40",
             "--json", "databaseId,event", "--jq", '.[] | "\\(.event) \\(.databaseId)"')
    runs = {}
    for ln in out.splitlines():
        event, run = ln.split()
        runs.setdefault(event, run)
    return runs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pairs", nargs="*", metavar="WORKFLOW=RUN", help="e.g. builds.yml=42116994913")
    a = ap.parse_args()
    pairs = [tuple(p.split("=", 1)) for p in a.pairs] or \
        [(w, r) for w in blocking_workflows() for r in latest_good_runs(w).values()]
    missing = 0
    for workflow, run in pairs:
        with open(os.path.join(WORKFLOWS, workflow), encoding="utf-8") as f:
            hosts = allowed(f.read())
        seen = reached(gh("run", "view", run, "--log"))
        extra = unlisted(seen, hosts)
        missing += len(extra)
        print(f"{workflow} (run {run}): {len(seen)} hosts reached, {len(extra)} not listed")
        for host, procs in extra.items():
            print(f"    {host}  ({', '.join(sorted(procs)) or 'resolved only'})")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
