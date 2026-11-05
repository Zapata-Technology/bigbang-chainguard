#!/usr/bin/env python3
"""Add the Chainguard repositories Big Bang needs to your org, and say which ones your
entitlement doesn't cover.

  bb-org-repos.py [--org <org>] [--optional] [--build-bases] [--apply]
  bb-org-repos.py --org <org> --apply go go-fips      # just these
  bb-org-repos.py --quota                             # the plan's per-tier room, and what uses it

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

`--quota` reads that summary and says, per tier, how many images the plan counts and how
many it allows; which counted images Big Bang doesn't use (another project may: a slot is
freed only by asking Chainguard to swap one, and only if nothing else needs it); and which
repositories this repo still needs, each with its tier and whether that tier has room.
Exit 1 when something needed has no room: a person has to talk to Chainguard. The nightly
update prints it in its pull request.
"""
import argparse
import json
import os
import re
import glob
import subprocess
import sys

import yaml

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


def used_by_bigbang(mapping, texts):
    """Org repository names this repo uses: every mapped image and its FIPS twin, `until`
    names, override tokens, SeaweedFS, the build bases, and any cgr.dev/<org>/<name> in
    the given texts (builds' Dockerfiles and build.yaml)."""
    names = {"seaweedfs", "seaweedfs-fips", *BUILD_BASES}
    for m in mapping.get("images", {}).values():
        m = {"cg": m} if isinstance(m, str) else (m or {})
        if m.get("cg"):
            names.add(m["cg"])
            fips = m.get("fips", m["cg"] + "-fips")
            if fips:
                names.add(fips)
        if m.get("until"):
            names.add(m["until"])

    def tokens(spec):
        if isinstance(spec, dict):
            for v in spec.values():
                tokens(v)
        elif isinstance(spec, list):
            for v in spec:
                tokens(v)
        elif isinstance(spec, str) and spec.startswith(("@repo:", "@tag:", "@ref:")):
            base = spec.split(":")[1]
            names.update({base, base + "-fips"})
    tokens(mapping.get("overrides", {}))
    for text in texts:
        names.update(re.findall(r"cgr\.dev/[^/\s\"']+/([a-z0-9][a-z0-9._-]*)", text))
    return names


def quota_report(summary, used, needed):
    """(lines, blocked) from `chainctl images entitlements summary -o json`, the names this
    repo uses, and {needed name: catalog tier}. blocked: needed names whose tier is full.

    A CATALOG plan entitles every image in its activeTiers: Chainguard still returns the
    per-tier counts from the old per-image plan, but they no longer limit anything, so
    those tiers are unlimited and a tier outside activeTiers is not entitled. A tier with
    an empty quota ({}) or a negative max has no limit either."""
    quota = summary.get("quota") or {}
    active = summary.get("activeImages") or []
    catalog = summary.get("plan") == "CATALOG"
    tiers = set(summary.get("activeTiers") or [])
    lines = [f"Chainguard plan: {summary.get('plan', '?')}"
             + (f" (swap quota {summary['swapQuota']})"
                if summary.get("swapQuota") is not None and summary["swapQuota"] >= 0 else "")]
    if catalog:
        lines.append("  every image in these tiers is entitled: " + (", ".join(sorted(tiers)) or "(none)"))
    free = {}
    for tier in sorted(set(quota) | (tiers if catalog else set())):
        q = quota.get(tier) or {}
        cur, top = q.get("current", 0), q.get("max")
        unlimited = catalog or top is None or top < 0
        if catalog and tier not in tiers:
            free[tier] = 0
            lines.append(f"  {tier:12} not entitled")
            continue
        free[tier] = None if unlimited else top - cur
        state = "unlimited" if unlimited else ("FULL" if cur >= top else f"{top - cur} free")
        lines.append(f"  {tier:12} {cur}/{'-' if unlimited else top}  {state}")
    unused = {}
    for img in active:
        if img.get("name") not in used:
            unused.setdefault(img.get("catalogTier", "?"), []).append(img.get("name"))
    if unused and not catalog:
        lines.append("Counted, but not used by Big Bang (another project may use them):")
        for tier in sorted(unused):
            lines.append(f"  {tier}: {', '.join(sorted(unused[tier]))}")
    blocked = []
    if needed:
        lines.append("Needed by this repo and not in the org:")
        want = {}
        for name, tier in sorted(needed.items()):
            want[tier] = want.get(tier, 0) + 1
            if catalog and tier not in tiers:
                blocked.append(name)
                note = f"{tier or '?'} not in the plan"
            elif tier in free and free[tier] is not None and want[tier] > free[tier]:
                blocked.append(name)
                note = f"no room in {tier}"
            else:
                note = "room" if tier in free else "tier not counted"
            lines.append(f"  {name:28} {tier or '?':12} {note}")
    else:
        lines.append("Needed by this repo and not in the org: (none)")
    if blocked:
        lines.append(f"{len(blocked)} needed image(s) don't fit the plan: ask Chainguard to raise the quota "
                     "or add the tier" + ("." if catalog else ", or to swap out a counted image nothing uses."))
    return lines, blocked


def quota(org, optional):
    summary = chainctl_json_obj("images", "entitlements", "summary", "--parent", org)
    root = os.path.dirname(HERE)
    with open(os.path.join(root, "mapping.yaml"), encoding="utf-8") as f:
        mapping = yaml.safe_load(f)
    texts = []
    for path in glob.glob(os.path.join(root, "builds", "*", "*")):
        if os.path.basename(path) in ("Dockerfile", "build.yaml"):
            with open(path, encoding="utf-8") as f:
                texts.append(f.read())
    used = used_by_bigbang(mapping, texts)
    p = subprocess.run([sys.executable, os.path.join(HERE, "bb-convert.py"), "--org", org, "--missing"],
                       capture_output=True, text=True)
    have = {r.get("name"): bool(r.get("sync_config")) for r in chainctl_json("images", "repos", "list", "--parent", org)}
    names = [n for n in missing_from(p.stdout, optional) + BUILD_BASES if not have.get(n)]
    needed = {}
    for name in dict.fromkeys(names):
        entry = catalog_entry(name)
        if entry:
            needed[name] = entry[1]
    lines, blocked = quota_report(summary, used, needed)
    print("\n".join(lines))
    return 1 if blocked else 0


def chainctl_json_obj(*args):
    """A chainctl command's JSON object (chainctl_json unwraps lists)."""
    p = subprocess.run(["chainctl", *args, "-o", "json"], capture_output=True, text=True, timeout=300)
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[-300:])
    out = p.stdout
    return json.loads(out[out.find("{"):]) if "{" in out else {}


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
    ap.add_argument("--quota", action="store_true", help="the plan's per-tier quotas and what this repo needs")
    a = ap.parse_args()
    org = org_name(a.org)
    if a.quota:
        return quota(org, a.optional)
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
