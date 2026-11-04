"""The site file: writing a starter one (init), reading its Big Bang keys, converting a Big
Bang 3 one to Big Bang 4's, and layering values files the way helm does."""
import json
import os
import sys

import yaml

from bbcommon import ROOT, bigbang_packages, deep_merge
from bbinstall.common import say

def set_key(values, key, value):
    """values[a][b] = value for the Big Bang key "a.b" (e.g. packages.keycloak)."""
    *parents, last = key.split(".")
    for k in parents:
        values = values.setdefault(k, {})
    values[last] = value


def package_values(site, name):
    """The site file's packages.<name> (Big Bang 4), or {}."""
    return ((site.get("packages") or {}).get(name) or {})


def legacy_keys(site, defaults):
    """Big Bang 3.x package keys in a site file (top-level <name> or addons.<name>), which
    Big Bang 4's chart refuses: they moved to packages.<name>."""
    names = {k.split(".")[-1] for k in bigbang_packages(defaults)}
    found = [k for k in site if k in names]
    found += [f"addons.{k}" for k in (site.get("addons") or {})]
    return sorted(found)


def enabled_packages(values):
    """Big Bang keys that are on ('auto' counts: metricsServer), e.g. ['packages.istiod',
    'packages.keycloak']."""
    return sorted(k for k, v in bigbang_packages(values).items() if v.get("enabled") not in (False, None))


def merged_values(defaults, files):
    values = json.loads(json.dumps(defaults))
    for f in files:
        deep_merge(values, yaml.safe_load(open(f, encoding="utf-8")) or {})
    return values


BB_COMMON = ("istio", "networkPolicies", "routes")


def migrate_site(site, defaults):
    """A Big Bang 3 site file in Big Bang 4's keys, and a line per change: each package from
    <name> or addons.<name> to packages.<name>, and a package's values.istio,
    values.networkPolicies and values.routes under values.bb-common (as Big Bang's
    migrate-values-3-to-4.sh --target 4 does). Values already in Big Bang 4's place win."""
    names = {k.split(".")[-1] for k in bigbang_packages(defaults)}
    by_lower = {n.lower(): n for n in names}
    new = {k: v for k, v in site.items() if k not in names and k != "addons"}
    packages = dict(new.get("packages") or {})
    changes = []
    old = [(k, k, v) for k, v in site.items() if k in names]
    old += [(f"addons.{k}", by_lower.get(k.lower(), k), v) for k, v in (site.get("addons") or {}).items()]
    for path, name, pkg in old:
        pkg = json.loads(json.dumps(pkg or {}))
        values = pkg.get("values") if isinstance(pkg.get("values"), dict) else None
        for key in BB_COMMON:
            if values is not None and key in values:
                flat, common = values.pop(key), values.setdefault("bb-common", {})
                nested = common.get(key)
                common[key] = deep_merge(flat, nested) if isinstance(flat, dict) and isinstance(nested, dict) \
                    else (flat if nested is None else nested)
                changes.append(f"{path}.values.{key} -> packages.{name}.values.bb-common.{key}")
        packages[name] = deep_merge(pkg, packages.get(name) or {})
        changes.append(f"{path} -> packages.{name}")
    if packages:
        new["packages"] = packages
    return new, changes


def init(a):
    if os.path.exists(a.site) and not a.force:
        raise SystemExit(f"{a.site} exists (--force overwrites it)")
    discovered = json.load(open(os.path.join(ROOT, "discovery", "images.json"), encoding="utf-8"))
    known = sorted({i["key"].split(".")[-1] for i in discovered["packages"].values() if i.get("key")} - {"flux"})
    domain = a.domain
    enable = [e.strip() for e in (a.enable or "").split(",") if e.strip()]
    interactive = sys.stdin.isatty() and not a.yes
    if interactive and not domain:
        domain = input("Domain the packages are served under (e.g. bigbang.example.com): ").strip()
    if interactive and not a.enable:
        say("Big Bang's default packages are on already. Other packages you can turn on:\n  " + ", ".join(known))
        enable = [e.strip() for e in input("Turn on (comma-separated, Enter for none): ").split(",") if e.strip()]
    if not domain:
        raise SystemExit("--domain is required")
    unknown = [e for e in enable if e.removeprefix("packages.") not in known]
    if unknown:
        raise SystemExit(f"unknown packages {unknown}; known: {', '.join(known)}")
    values = {"domain": domain}
    for e in enable:
        set_key(values, "packages." + e.removeprefix("packages."), {"enabled": True})
    with open(a.site, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Big Bang site file for scripts/bb-install.py. No credentials here: the installer\n"
                "# reads them from the environment or asks, and keeps them in the cluster.\n"
                "# Add your own Big Bang values below (TLS for the Istio gateway, SSO, sizes...).\n")
        yaml.safe_dump(values, f, sort_keys=False)
    say(f"wrote {a.site}. Next: python scripts/bb-install.py install --site {a.site} [--fips]"
        if getattr(a, "cmd", "init") == "init" else f"wrote {a.site}")
    return 0
