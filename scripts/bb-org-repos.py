#!/usr/bin/env python3
"""Add the Chainguard repositories Big Bang needs to your org, and say which ones your
entitlement doesn't cover.

  bb-org-repos.py [--org <org>] [--optional] [--build-bases] [--apply]
  bb-org-repos.py --org <org> --apply go go-fips      # just these

Without names it asks bb-convert.py which repositories discovery needs and the org lacks
(`--missing`; `--optional` adds the ones whose images otherwise stay on Iron Bank).
`--build-bases` adds the bases two of this repo's builds use when the org has them: go and
go-fips (the Mattermost Operator), glibc-openssl and glibc-openssl-fips (NeuVector's
registry adapter).

For each name it looks up the catalog entry and, with --apply, runs what AGENTS.md
("Add a repository to the org") describes:

  chainctl images repos create <name> --parent <org> --tier <catalog tier>
  chainctl images repos update <name> --parent <org> --source <catalog id>   # starts the sync

Without --apply it only prints those commands. A sync Chainguard refuses
("organization does not have an active entitlement") is reported, not retried: the image
is outside your plan. An "unlimited" plan can still count base images (toolchains and
runtimes such as go, glibc-openssl, jdk, node) against a per-tier quota;
`chainctl images entitlements summary --parent <org>` shows the quotas, and Chainguard
(your account team, or "Contact us for access" in the console) raises them.
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orgtags import org_name  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_BASES = ["go", "go-fips", "glibc-openssl", "glibc-openssl-fips"]
NOT_ENTITLED = re.compile(r"does not have an active entitlement|PermissionDenied", re.I)


def missing_from(text, optional=False):
    """Repository names from `bb-convert.py --missing` output."""
    names, section = [], None
    for line in text.splitlines():
        if line.startswith("Org repositories needed but missing:"):
            section = "needed"
        elif line.startswith("Optional (their images stay on Iron Bank"):
            section = "optional"
        elif line.startswith("  ") and section and line.strip() != "(none)":
            if section == "needed" or optional:
                names.append(line.strip())
        elif line.strip():
            section = None
    return names


def chainctl_json(*args):
    p = subprocess.run(["chainctl", *args, "-o", "json"], capture_output=True, text=True, timeout=300)
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[-300:])
    out = p.stdout
    start = [i for i in (out.find("["), out.find("{")) if i >= 0]
    data = json.loads(out[min(start):]) if start else []
    return data.get("items", []) if isinstance(data, dict) else data


def catalog_entry(name):
    """(catalog id, tier) for an image name, or None."""
    for e in chainctl_json("images", "catalog", "--name", name):
        if isinstance(e, dict) and e.get("name") == name:
            return e.get("id"), e.get("catalog_tier")
    return None


def outcome(returncode, stderr):
    """'ok', 'not entitled' or the error's last line."""
    if returncode == 0:
        return "ok"
    if NOT_ENTITLED.search(stderr):
        return "not entitled"
    return (stderr.strip().splitlines() or ["failed"])[-1][:200]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="repositories to add (default: what bb-convert.py --missing lists)")
    ap.add_argument("--org", help="Chainguard org (default: CHAINGUARD_ORG)")
    ap.add_argument("--optional", action="store_true", help="also the optional repositories --missing lists")
    ap.add_argument("--build-bases", action="store_true", help="also " + ", ".join(BUILD_BASES))
    ap.add_argument("--apply", action="store_true", help="create and sync them (default: print the commands)")
    a = ap.parse_args()
    org = org_name(a.org)
    names = list(a.names)
    if not names:
        p = subprocess.run([sys.executable, os.path.join(HERE, "bb-convert.py"), "--org", org, "--missing"],
                           capture_output=True, text=True)
        names = missing_from(p.stdout, a.optional)
    if a.build_bases:
        names += [n for n in BUILD_BASES if n not in names]
    have = {r.get("name"): bool(r.get("sync_config")) for r in chainctl_json("images", "repos", "list", "--parent", org)}
    results = {}
    for name in names:
        if have.get(name):
            results[name] = "already in the org"
            continue
        entry = catalog_entry(name)
        if not entry:
            results[name] = "not in Chainguard's catalog"
            continue
        cid, tier = entry
        create = ["chainctl", "images", "repos", "create", name, "--parent", org, "--tier", tier]
        update = ["chainctl", "images", "repos", "update", name, "--parent", org, "--source", cid]
        if not a.apply:
            print(" ".join(create) if name not in have else f"# {name}: repository exists, not syncing")
            print(" ".join(update))
            results[name] = "would add"
            continue
        if name not in have:
            p = subprocess.run(create, capture_output=True, text=True)
            if p.returncode:
                results[name] = outcome(p.returncode, p.stderr)
                continue
        p = subprocess.run(update, capture_output=True, text=True)
        results[name] = "added (syncing)" if p.returncode == 0 else outcome(p.returncode, p.stderr)
        print(f"{name}: {results[name]}", flush=True)
    print(f"\n{len(names)} repositories for {org}:")
    for state in sorted(set(results.values())):
        group = sorted(n for n, s in results.items() if s == state)
        print(f"  {state} ({len(group)}): {', '.join(group)}")
    if "not entitled" in results.values():
        print(f"\nChainguard refused to sync the ones marked 'not entitled': your plan doesn't cover them.\n"
              f"  chainctl images entitlements summary --parent {org}   # plan, per-tier quotas, entitled images\n"
              "Ask your Chainguard account team (or 'Contact us for access' on the image in the console) to add\n"
              "them, then run this again: the repositories already exist and only need the sync.")
    return 1 if any(s not in ("ok", "already in the org", "added (syncing)", "would add") for s in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
