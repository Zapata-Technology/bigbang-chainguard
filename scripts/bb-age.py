#!/usr/bin/env python3
"""Fail when an org image the overlays use hasn't been rebuilt recently.

Chainguard rebuilds the release lines it maintains every few days. An image whose build
is weeks old is on a line it has stopped maintaining, and its CVEs pile up: Vault's kubectl
1.30 (end of life) had 19 Critical CVEs in the FIPS image before `min_version` moved it.
This catches the next one when it goes stale, not when a scan finds the CVEs.

A line can also be left behind sooner: when Chainguard publishes a new minor it usually stops
rebuilding the old one. Alertmanager 0.34.2 (Iron Bank's line) was last built 2026-11-09,
the day before 0.35.0; on 2026-11-22 it still had glibc CVE-2026-84243 (High), fixed in
0.35.0, and the 30-day rule would not have said so until December. So an image is also
superseded when the org has a newer minor of the same major (same suffix) built after it:
a warning after --warn-superseded days without a rebuild, a failure after --superseded-days
(a maintained line is rebuilt far more often than that). Raise the line with `min_version`.

  bb-age.py [--max-days 30] [--warn-superseded 7] [--superseded-days 14] [--images docs/images.md] [--org <org>]

Reads every converted image (standard and FIPS) from docs/images.md, asks the registry for
each one's build time (its config's `created`), and lists those older than --max-days.
Images outside the org (built here, `ref:` entries) are skipped: their own builds scan them.
The lookups (four registry requests per image) run in parallel.
"""
import argparse
import datetime
import importlib.util
import os
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from orgtags import REGISTRY, OrgTags, org_name, parse  # noqa: E402

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


def newer_minor(tag, tags):
    """The org's newest release of a newer MINOR in the same MAJOR as `tag`, with the same
    suffix (-dev, ...), or None. Stream tags (1.37) count only when no full version does."""
    p = parse(tag)
    if not p or len(p[0]) < 2:
        return None
    nums, suffix = p
    best = None
    for t in tags or []:
        q = parse(t)
        if q and q[1] == suffix and q[0][0] == nums[0] and q[0][:2] > nums[:2]:
            if best is None or (len(q[0]), q[0]) > (len(best[0]), best[0]):
                best = (q[0], t)
    return best[1] if best else None


def superseded(refs, ages, tags, now, days):
    """[(repo, tag, age, newer tag, its age)] for org images not rebuilt in `days` days
    whose repository has a newer minor built after them: a line Chainguard left behind."""
    out = []
    for repo, tag in refs:
        age = ages.get((repo, tag))
        if age is None or age <= days:
            continue
        newer = newer_minor(tag, tags.tags(repo))
        if not newer:
            continue
        newer_age = age_days(tags.created(repo, newer), now)
        if newer_age is not None and newer_age < age:
            out.append((repo, tag, age, newer, newer_age))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-days", type=int, default=30)
    ap.add_argument("--warn-superseded", type=int, default=7)
    ap.add_argument("--superseded-days", type=int, default=14)
    ap.add_argument("--images", default=os.path.join(ROOT, "docs", "images.md"))
    ap.add_argument("--org")
    a = ap.parse_args()

    org = org_name(a.org)
    tags = OrgTags(org)
    now = datetime.datetime.now(datetime.timezone.utc)
    refs = org_refs(open(a.images, encoding="utf-8").read(), org)
    with ThreadPoolExecutor(8) as pool:
        created = list(pool.map(lambda ref: tags.created(*ref), refs))
    ages = {ref: age_days(c, now) for ref, c in zip(refs, created)}
    old = stale(ages, a.max_days)
    newest = sorted(d for d in ages.values() if d is not None)
    print(f"{len(ages)} org images; median build age {newest[len(newest) // 2] if newest else '?'} days")
    for repo, tag, days in old:
        print(f"  STALE {repo}:{tag}: {'no build time' if days is None else f'built {days} days ago'}")
    if old:
        print(f"{len(old)} images older than {a.max_days} days: the org may have stopped maintaining their "
              "line. Raise the line (`min_version`, `version`) or map another image.")
    stale_refs = {(r, t) for r, t, _ in old}
    with ThreadPoolExecutor(8) as pool:   # warms the tag lists in parallel
        list(pool.map(tags.tags, sorted({r for r, _ in refs})))
    left = [x for x in superseded(refs, ages, tags, now, a.warn_superseded) if x[:2] not in stale_refs]
    failed = [x for x in left if x[2] > a.superseded_days]
    for repo, tag, age, newer, newer_age in left:
        line = f"{repo}:{tag}: built {age} days ago; {newer} was built {newer_age} days ago"
        if age > a.superseded_days:
            print(f"  SUPERSEDED {line}")
        else:
            # a GitHub annotation: the PR shows it before it fails
            print(f"  WARNING superseded soon: {line}")
            if os.environ.get("GITHUB_ACTIONS"):
                print(f"::warning title=Superseded image line::{line}; fails after {a.superseded_days} days")
    if left:
        print(f"{len(left)} images on a line the org may have stopped rebuilding when a newer minor came out "
              f"({len(failed)} past {a.superseded_days} days): raise the line with `min_version` in "
              "mapping.yaml (after checking the chart takes it).")
    return 1 if old or failed else 0


if __name__ == "__main__":
    sys.exit(main())
