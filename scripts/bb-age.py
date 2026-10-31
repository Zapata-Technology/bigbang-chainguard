#!/usr/bin/env python3
"""Fail when an org image the overlays use hasn't been rebuilt recently.

Chainguard rebuilds the release lines it maintains every few days. An image whose build
is weeks old is on a line it has stopped maintaining, and its CVEs pile up: Vault's kubectl
1.30 (end of life) had 19 Critical CVEs in the FIPS image before `min_version` moved it.
This catches the next one when it goes stale, not when a scan finds the CVEs.

  bb-age.py [--max-days 30] [--images docs/images.md] [--org <org>]

Reads every converted image (standard and FIPS) from docs/images.md, asks the registry for
each one's build time (its config's `created`), and lists those older than --max-days.
Images outside the org (built here, `ref:` entries) are skipped: their own builds scan them.
"""
import argparse
import datetime
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from orgtags import REGISTRY, OrgTags, org_name  # noqa: E402

_spec = importlib.util.spec_from_file_location("bb_cve", os.path.join(HERE, "bb-cve.py"))
bb_cve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bb_cve)


def org_refs(text, org):
    """(repo, tag) for every org image in docs/images.md, standard and FIPS, each once."""
    prefix = f"{REGISTRY}/{org}/"
    refs = set()
    for fips in (False, True):
        for _, new in bb_cve.image_pairs(text, fips):
            if new and new.startswith(prefix):
                repo, _, tag = new[len(prefix):].rpartition(":")
                refs.add((repo, tag))
    return sorted(refs)


def age_days(created, now):
    """Whole days between an ISO 8601 `created` and now; None when it can't be read."""
    if not created:
        return None
    try:
        t = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return (now - t).days


def stale(ages, max_days):
    """[(repo, tag, days)] older than max_days, oldest first. Unreadable ages count as stale:
    an image with no build time (1970, or none) is not one we can vouch for."""
    out = [(r, t, d) for (r, t), d in ages.items() if d is None or d > max_days]
    return sorted(out, key=lambda x: -(x[2] if x[2] is not None else 10 ** 6))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-days", type=int, default=30)
    ap.add_argument("--images", default=os.path.join(ROOT, "docs", "images.md"))
    ap.add_argument("--org")
    a = ap.parse_args()

    org = org_name(a.org)
    tags = OrgTags(org)
    now = datetime.datetime.now(datetime.timezone.utc)
    ages = {}
    for repo, tag in org_refs(open(a.images, encoding="utf-8").read(), org):
        ages[(repo, tag)] = age_days(tags.created(repo, tag), now)
    old = stale(ages, a.max_days)
    newest = sorted(d for d in ages.values() if d is not None)
    print(f"{len(ages)} org images; median build age {newest[len(newest) // 2] if newest else '?'} days")
    for repo, tag, days in old:
        print(f"  STALE {repo}:{tag}: {'no build time' if days is None else f'built {days} days ago'}")
    if old:
        print(f"{len(old)} images older than {a.max_days} days: the org may have stopped maintaining their "
              "line. Raise the line (`min_version`, `version`) or map another image.")
    return 1 if old else 0


if __name__ == "__main__":
    sys.exit(main())
