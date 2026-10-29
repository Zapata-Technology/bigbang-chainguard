#!/usr/bin/env python3
"""Report which Iron Bank leftovers Chainguard now publishes.

mapping.yaml marks an image with no Chainguard equivalent `{stays: ..., until: <name>}`.
bb-convert converts it once the org's <name> repository has tags; this looks one step
earlier, in Chainguard's catalog, so the nightly report says when to add the repository:

  bb-leftovers.py [--mapping mapping.yaml] [--org <org>]

For each `until` name (and its -fips variant): in the org, in the catalog only (add it:
AGENTS.md, "Add a repository to the org"), or not published yet. Exit 0 always; it
reports, it doesn't gate.
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bbcommon import load_mapping, setting  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def chainctl_json(*args):
    p = subprocess.run(["chainctl", *args, "-o", "json"], capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[-200:])
    text = p.stdout[min(i for i in (p.stdout.find("["), p.stdout.find("{")) if i >= 0):] if p.stdout.strip() else "[]"
    return json.loads(text)


def in_catalog(name):
    items = chainctl_json("images", "catalog", "--name", name, "--limit", "20")
    items = items.get("items", []) if isinstance(items, dict) else items
    return any(isinstance(i, dict) and i.get("name") == name for i in items)


def in_org(org, name):
    items = chainctl_json("images", "repos", "list", "--parent", org)
    items = items.get("items", []) if isinstance(items, dict) else items
    return any(isinstance(i, dict) and i.get("name") == name for i in items)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mapping", default=os.path.join(ROOT, "mapping.yaml"))
    ap.add_argument("--org")
    a = ap.parse_args()
    org = setting("CHAINGUARD_ORG", a.org)
    mapping = load_mapping(org, a.mapping)
    waiting = sorted({(m["until"], ib) for ib, m in mapping["images"].items()
                      if isinstance(m, dict) and m.get("until")})
    print("Iron Bank leftovers waiting on Chainguard:")
    for name, ib in waiting:
        for n in (name, f"{name}-fips"):
            try:
                if org and in_org(org, n):
                    state = "in the org (bb-convert converts it once it has tags)"
                elif in_catalog(n):
                    state = "PUBLISHED: add it to the org (AGENTS.md, \"Add a repository to the org\")"
                else:
                    state = "not published yet"
            except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as e:
                state = f"unknown ({e})"
            print(f"  {n:40} {state}   [{ib}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
