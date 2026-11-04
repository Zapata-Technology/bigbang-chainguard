"""Big Bang's chart: from repo1's public git archive (no Iron Bank login), a git mirror, a
local path or an oci:// ref; its default values; and package git repositories moved to a mirror."""
import io
import os
import shutil
import subprocess
import tarfile
import time
import urllib.request

import yaml

from bbcommon import ROOT, bigbang_packages
from bbinstall.common import say
from bbinstall.sitefile import set_key

# registry1's OCI copy of the chart needs an Iron Bank login; repo1's git archive doesn't.
CHART_ARCHIVE = "https://repo1.dso.mil/big-bang/bigbang/-/archive/{v}/bigbang-{v}.tar.gz?path=chart"
CHART_CACHE = os.path.join(ROOT, "work", "charts")
REPO1 = "https://repo1.dso.mil/"
BIGBANG_GIT = "https://repo1.dso.mil/big-bang/bigbang.git"


def chart_ref(a):
    """(chart, extra helm args): --chart as given, else Big Bang's chart from the git
    mirror (--git-mirror) or repo1."""
    if a.chart and a.chart.startswith("oci://"):
        return a.chart, ["--version", a.version]
    mirror = getattr(a, "git_mirror", None)
    return a.chart or (clone_chart(a.version, mirror) if mirror else fetch_chart(a.version)), []


def mirrored(url, mirror):
    """A repo1 URL on the mirror: same path under the mirror's prefix."""
    return mirror.rstrip("/") + "/" + url[len(REPO1):] if mirror and url.startswith(REPO1) else url


def git_mirror_values(defaults, mirror):
    """Every package's git.repo moved from repo1 to the mirror (Big Bang's packages come
    from repo1 git at runtime; a mirror the site controls keeps them coming when repo1
    doesn't answer, and is how a disconnected site gets them at all)."""
    out = {}
    for key, v in bigbang_packages(defaults).items():
        if str((v.get("git") or {}).get("repo", "")).startswith(REPO1):
            set_key(out, key, {"git": {"repo": mirrored(v["git"]["repo"], mirror)}})
    return out


def clone_chart(version, mirror, cache=CHART_CACHE):
    """Big Bang's chart at <version> from the git mirror (git clone of the tag)."""
    dest = os.path.join(cache, f"bigbang-{version}-mirror")
    chart = os.path.join(dest, "chart")
    if os.path.exists(os.path.join(chart, "Chart.yaml")):
        return chart
    shutil.rmtree(dest, ignore_errors=True)
    url = mirrored(BIGBANG_GIT, mirror)
    if subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch", version, url, dest]).returncode:
        raise SystemExit(f"could not clone Big Bang {version} from {url}")
    return chart


def fetch_chart(version, cache=CHART_CACHE):
    """Big Bang's chart at <version> from repo1's public git archive, kept in work/charts."""
    dest = os.path.join(cache, f"bigbang-{version}")
    chart = os.path.join(dest, "chart")
    if os.path.exists(os.path.join(chart, "Chart.yaml")):
        return chart
    say(f"downloading Big Bang {version}'s chart from repo1.dso.mil")
    error = None
    for attempt in range(3):   # repo1 intermittently refuses connections
        try:
            with urllib.request.urlopen(CHART_ARCHIVE.format(v=version), timeout=120) as r:
                data = r.read()
            break
        except OSError as e:
            error = e
            time.sleep(5 * (attempt + 1))
    else:
        raise SystemExit(f"could not download Big Bang {version}'s chart from repo1.dso.mil ({error}); "
                         "pass --chart <path to bigbang/chart> or --chart oci://registry1.dso.mil/bigbang/bigbang")
    part = dest + ".part"
    shutil.rmtree(part, ignore_errors=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        for m in t.getmembers():
            rel = m.name.split("/")[1:]   # drop the archive's top directory
            if not m.isfile() or not rel or ".." in rel or m.name.startswith("/"):
                continue
            out = os.path.join(part, *rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as fh:
                fh.write(t.extractfile(m).read())
    got = (yaml.safe_load(open(os.path.join(part, "chart", "Chart.yaml"), encoding="utf-8")) or {}).get("version")
    if str(got) != version:
        raise SystemExit(f"repo1's archive for {version} holds chart version {got}")
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(part, dest)
    return chart


def chart_defaults(a):
    ref, version = chart_ref(a)
    p = subprocess.run(["helm", "show", "values", ref, *version], capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"could not read Big Bang's chart ({ref} {' '.join(version)}):\n{p.stderr[-800:]}\n"
                         "Use --chart <path to bigbang/chart> (scripts/bb-fetch.py clones it to work/bigbang).")
    return yaml.safe_load(p.stdout) or {}
