#!/usr/bin/env python3
"""Generate the Chainguard overlays for Big Bang from discovery + mapping.yaml.

  bb-convert.py --discovered discovery/images.json [--tags-cache .tags.json]
                [--check] [--missing]

Writes:
  overlays/values-chainguard.yaml       standard images (FIPS off)
  overlays/values-chainguard-fips.yaml  -fips images; layer it after the first file
  flux/kustomization.yaml, flux-fips/kustomization.yaml
  docs/images.md                        per-package table, including what stays on Iron Bank

--check    regenerate in memory; exit 1 on unmapped or untagged images, 3 if a
           file would change only (newer org patches since the last run)
--missing  print the org repositories that discovery needs but the org lacks
"""
import argparse
import copy
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bbcommon import IRONBANK_HOSTS  # noqa: E402
from orgtags import ORG, REGISTRY, OrgTags, resolve  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CG = f"{REGISTRY}/{ORG}"
ALLOWED_REGISTRIES = ["cgr.dev/zapatatechnology.com", "ghcr.io/zapata-technology"]


class Unmapped(Exception):
    pass


def split_ref(ref):
    """'registry1.dso.mil/ironbank/x/y:1.2' -> ('ironbank/x/y', '1.2'); tag may be None."""
    for h in IRONBANK_HOSTS:
        if ref.startswith(h + "/"):
            ref = ref[len(h) + 1:]
    ref = ref.split("@")[0]
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = ref.rsplit(":", 1)
        return repo, tag
    return ref, None


def set_path(tree, path, value):
    for k in path[:-1]:
        tree = tree.setdefault(k, {})
    tree[path[-1]] = value


class Converter:
    def __init__(self, mapping, tags):
        self.images = mapping["images"]
        self.hubs = mapping.get("hubs", {})
        self.defaults = mapping.get("template_defaults", [])
        self.ignore = mapping.get("ignore", [])
        self.tags = tags
        self.rows = []          # (release, ironbank ref, standard, fips, note)
        self.missing = set()    # org repos needed but absent
        self.unmapped = set()

    def target(self, ib_repo, ib_tag, release):
        """(standard ref, fips ref, note) for an Iron Bank repo:tag, or None if it stays."""
        m = self.images.get(ib_repo)
        if m is None:
            self.unmapped.add(ib_repo)
            return None
        if isinstance(m, str):
            m = {"cg": m}
        ib = f"{ib_repo}:{ib_tag}" if ib_tag else ib_repo
        if "stays" in m:
            self.rows.append((release, ib, "Iron Bank", "Iron Bank", m["stays"]))
            return None
        if "ref" in m:
            std = f'{m["ref"]}:{m["tag"]}'
            fips = f'{m["fips"]}:{m["tag"]}' if m.get("fips") else std
            self.rows.append((release, ib, std, fips, m.get("note", "")))
            return std, fips
        notes = []
        refs = []
        fips_name = m.get("fips", m["cg"] + "-fips") if "fips" in m else m["cg"] + "-fips"
        for name in (m["cg"], fips_name):
            if name is None:
                refs.append(None)
                continue
            if m.get("tag"):
                tag, note = str(m["tag"]), None
            else:
                tags = self.tags.tags(name)
                if tags is None:
                    self.missing.add(name)
                    refs.append(None)
                    notes.append(f"org has no {name}")
                    continue
                want = m.get("version", ib_tag)
                tag, note = resolve(want, tags, m.get("suffix")) if want else (None, "no Iron Bank tag")
            if not tag:
                refs.append(None)
                notes.append(f"{name}: {note}")
                continue
            if note:
                notes.append(f"{name}: {note}")
            refs.append(f"{CG}/{name}:{tag}")
        std, fips = refs
        if fips_name is None:
            fips = std
            notes.append("no FIPS variant; FIPS overlay keeps the standard image")
        self.rows.append((release, ib, std or "UNRESOLVED", fips or "UNRESOLVED", "; ".join(notes)))
        if not std:
            return None
        return std, fips or std

    def ignored(self, release, path):
        dotted = ".".join(map(str, path))
        return any(i["release"] == release and (dotted == i["path"] or dotted.startswith(i["path"] + "."))
                   for i in self.ignore)

    def package(self, release, info, std_vals, fips_vals):
        """Write overrides for one release into std_vals / fips_vals (package values trees)."""
        covered = set()
        lists = {}
        for n in info["values"]:
            path = n["path"]
            if self.ignored(release, path):
                continue
            shape, v = n["shape"], n["value"]
            if shape == "registry":
                # Blank Big Bang's chart-wide registry1 setting so each image's own
                # registry (set below) applies.
                set_path(std_vals, path, "")
                set_path(fips_vals, path, "")
                continue
            if shape == "hub":
                for dotted, ib_repo in self.hubs.get(release, {}).items():
                    t = self.target(ib_repo, v.get("tag"), release)
                    if t:
                        set_path(std_vals, dotted.split("."), t[0])
                        set_path(fips_vals, dotted.split("."), t[1])
                    covered.add(ib_repo)
                continue
            if shape == "string":
                ib_repo, ib_tag = split_ref(v)
                ib_tag = ib_tag or n.get("tag")
            elif shape == "repo":
                reg = v.get("registry", "")
                full = f'{reg}/{v["repository"]}' if reg else v["repository"]
                ib_repo, _ = split_ref(full)
                ib_tag = v.get("tag", v.get("release"))
            else:  # "name": {registry?, image, tag}
                reg = v.get("registry", "")
                full = f'{reg}/{v["image"]}' if reg else v["image"]
                ib_repo, _ = split_ref(full)
                ib_tag = v.get("tag")
            covered.add(ib_repo)
            t = self.target(ib_repo, ib_tag, release)
            if not t:
                continue
            for vals, ref in ((std_vals, t[0]), (fips_vals, t[1])):
                repo, tag = ref.rsplit(":", 1)
                if "list_path" in n:
                    lst = lists.setdefault((id(vals), tuple(n["list_path"])),
                                           (vals, n["list_path"], copy.deepcopy(n["list_value"])))[2]
                    node = lst
                    for k in path[len(n["list_path"]):-1]:
                        node = node[k]
                    node[path[-1]] = ref if shape == "string" else node[path[-1]]
                    continue
                if shape == "string":
                    if n.get("tag_key") and ":" not in v.rsplit("/", 1)[-1]:
                        set_path(vals, path, repo)
                        set_path(vals, path[:-1] + [n["tag_key"]], tag)
                    else:
                        set_path(vals, path, ref)
                elif shape == "repo":
                    if "registry" in v:
                        # registry carries the org path: charts such as Loki drop the
                        # registry when the repository's first segment has a dot.
                        set_path(vals, path + ["registry"], repo.rsplit("/", 1)[0])
                        set_path(vals, path + ["repository"], repo.rsplit("/", 1)[1])
                    else:
                        set_path(vals, path + ["repository"], repo)
                    set_path(vals, path + ["release" if "release" in v and "tag" not in v else "tag"], tag)
                    if v.get("digest"):
                        set_path(vals, path + ["digest"], "")
                else:
                    if "registry" in v:
                        set_path(vals, path + ["registry"], repo.rsplit("/", 1)[0])
                        set_path(vals, path + ["image"], repo.rsplit("/", 1)[1])
                    else:
                        set_path(vals, path + ["image"], repo)
                    set_path(vals, path + ["tag"], tag)
        for vals, list_path, lst in lists.values():
            set_path(vals, list_path, lst)
        # hardcoded template defaults that no value node covered
        for ref in info["rendered"]:
            ib_repo, ib_tag = split_ref(ref)
            for d in self.defaults:
                if d["image"] == ib_repo:
                    t = self.target(ib_repo, ib_tag, release)
                    if t:
                        set_path(std_vals, d["path"].split("."), t[0])
                        set_path(fips_vals, d["path"].split("."), t[1])
                    covered.add(ib_repo)

    def overrides(self, release, spec, fips):
        """Expand a mapping.yaml `overrides` block. Tokens:
          @repo:<name>            <name>, or <name>-fips in the FIPS overlay
          @tag:<name>:<version>   org tag of <name>(-fips) for <version> (same rules as images)
          @ref:<name>:<version>   cgr.dev/<org>/<name>(-fips):<tag>
        """
        if isinstance(spec, dict):
            return {k: self.overrides(release, v, fips) for k, v in spec.items()}
        if isinstance(spec, list):
            return [self.overrides(release, v, fips) for v in spec]
        if not (isinstance(spec, str) and spec.startswith("@")):
            return spec
        kind, _, rest = spec[1:].partition(":")
        name, _, version = rest.partition(":")
        name = f"{name}-fips" if fips else name
        if kind == "repo":
            return name
        tags = self.tags.tags(name)
        if tags is None:
            self.missing.add(name)
            self.rows.append((release, f"override {spec}", "UNRESOLVED", "UNRESOLVED", f"org has no {name}"))
            return spec
        tag, note = resolve(version, tags)
        if not tag:
            self.rows.append((release, f"override {spec}", "UNRESOLVED", "UNRESOLVED", f"{name}: {note}"))
            return spec
        return tag if kind == "tag" else f"{CG}/{name}:{tag}"

    def flux(self, info):
        std, fips = [], []
        for ref in info["rendered"]:
            ib_repo, ib_tag = split_ref(ref)
            t = self.target(ib_repo, ib_tag, "flux")
            if not t:
                continue
            for out, r in ((std, t[0]), (fips, t[1])):
                repo, tag = r.rsplit(":", 1)
                out.append({"name": f"registry1.dso.mil/{ib_repo}", "newName": repo, "newTag": tag})
        return std, fips


def dump(data, header):
    return header + yaml.safe_dump(data, sort_keys=True, default_flow_style=False, width=120)


def generate(discovered, mapping, tags, bigbang_version):
    c = Converter(mapping, tags)
    std, fips = {}, {}
    flux_std = flux_fips = []
    for release, info in sorted(discovered["packages"].items()):
        if release == "flux":
            flux_std, flux_fips = c.flux(info)
            continue
        key = info["key"].split(".")
        # Where the package's chart values live under the Big Bang key; istioGateway
        # takes shared settings under values.shared (merged into every gateway).
        root = ["values"] + mapping.get("value_roots", {}).get(info["key"], "").split(".")             if mapping.get("value_roots", {}).get(info["key"]) else ["values"]
        s, f = {}, {}
        c.package(release, info, s, f)
        # Images hardcoded in a template (no value to set): Flux post-renderer rewrite.
        for ib_repo in mapping.get("post_renderers", {}).get(info["key"], []):
            for ref in info["rendered"]:
                r_repo, r_tag = split_ref(ref)
                if r_repo != ib_repo:
                    continue
                t = c.target(ib_repo, r_tag, release)
                if not t:
                    continue
                for tree, tgt in ((std, t[0]), (fips, t[1])):
                    pr = _get(tree, key + ["postRenderers"]) or [{"kustomize": {"images": []}}]
                    pr[0]["kustomize"]["images"].append({"name": f"registry1.dso.mil/{ib_repo}",
                                                         "newName": tgt.rsplit(":", 1)[0],
                                                         "newTag": tgt.rsplit(":", 1)[1]})
                    set_path(tree, key + ["postRenderers"], pr)
        if release in mapping.get("overrides", {}):
            spec = mapping["overrides"][release]
            _merge_into(s, c.overrides(release, spec, False))
            _merge_into(f, c.overrides(release, spec, True))
        if s:
            set_path(std, key + root, _merge(_get(std, key + root), s))
        if f:
            set_path(fips, key + root, _merge(_get(fips, key + root), f))
    # Big Bang's Kyverno policy only admits Iron Bank registries by default.
    set_path(std, ["kyvernoPolicies", "values", "policies", "restrict-image-registries", "parameters", "allow"],
             ["registry1.dso.mil", "registry.dso.mil"] + ALLOWED_REGISTRIES)
    hdr = ("# GENERATED by scripts/bb-convert.py from mapping.yaml - do not edit by hand.\n"
           f"# Big Bang {bigbang_version}. ")
    files = {
        "overlays/values-chainguard.yaml": dump(std, hdr + "Standard (non-FIPS) Chainguard images.\n"
                                            "# helm upgrade -i bigbang <chart> -f overlays/values-chainguard.yaml -f <site>.yaml\n"),
        "overlays/values-chainguard-fips.yaml": dump(fips, hdr + "FIPS: layer after values-chainguard.yaml.\n"
                                                 "# ... -f overlays/values-chainguard.yaml -f overlays/values-chainguard-fips.yaml -f <site>.yaml\n"),
    }
    for d, imgs in (("flux", flux_std), ("flux-fips", flux_fips)):
        files[f"{d}/kustomization.yaml"] = dump(
            {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
             "resources": [f"https://repo1.dso.mil/big-bang/bigbang.git//base/flux?ref={bigbang_version}"],
             "images": imgs},
            hdr + ("FIPS " if d.endswith("fips") else "") + "Flux on Chainguard images.\n"
            f"# kubectl apply -k {d}/   (needs the private-registry pull secret in flux-system)\n")
    files["docs/images.md"] = report(c.rows, bigbang_version)
    return files, c


def _get(tree, path):
    for k in path:
        if not isinstance(tree, dict) or k not in tree:
            return {}
        tree = tree[k]
    return tree


def _merge_into(a, b):
    from bbcommon import deep_merge
    deep_merge(a, b)


def _merge(a, b):
    from bbcommon import deep_merge
    return deep_merge(copy.deepcopy(a), b)


def report(rows, version):
    lines = [f"# Image map for Big Bang {version}", "",
             "Generated by `scripts/bb-convert.py`. Every image each package deploys, and what replaces it.",
             "`Iron Bank` means the image stays on registry1 (reason in the last column); "
             "sites that enable that package still need registry1 credentials.", "",
             "| Package | Iron Bank image | Standard | FIPS | Notes |", "|---|---|---|---|---|"]
    seen = set()
    for r in sorted(rows):
        if r in seen:
            continue
        seen.add(r)
        lines.append("| " + " | ".join(x.replace(f"{CG}/", "") .replace("|", "\\|") for x in r) + " |")
    stays = sorted({(r[1].split(":")[0], r[4]) for r in rows if r[2] == "Iron Bank"})
    lines += ["", f"## Stays on Iron Bank ({len(stays)})", ""] + [f"- `{i}`: {why}" for i, why in stays]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--discovered", default=os.path.join(ROOT, "discovery", "images.json"))
    ap.add_argument("--mapping", default=os.path.join(ROOT, "mapping.yaml"))
    ap.add_argument("--tags-cache")
    ap.add_argument("--offline", action="store_true", help="use only --tags-cache; never call the registry")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--missing", action="store_true")
    a = ap.parse_args()

    discovered = json.load(open(a.discovered))
    mapping = yaml.safe_load(open(a.mapping))
    version = open(os.path.join(ROOT, "BIGBANG_VERSION")).read().strip()
    tags = OrgTags(a.tags_cache, offline=a.offline)
    files, c = generate(discovered, mapping, tags, version)
    if not a.offline:
        tags.save()

    rc = 0
    if c.unmapped:
        print("Images not in mapping.yaml (add them):", *sorted(c.unmapped), sep="\n  ")
        rc = 1
    unresolved = sorted({r for r in c.rows if "UNRESOLVED" in (r[2], r[3])})
    if unresolved:
        print("Images with no usable org tag:")
        for r in unresolved:
            print(f"  {r[0]}: {r[1]} -> {r[4]}")
        rc = 1
    if a.missing:
        print("Org repositories needed but missing:", *sorted(c.missing) or ["(none)"], sep="\n  ")
        return rc
    drift = False
    for rel, text in files.items():
        p = os.path.join(ROOT, rel)
        old = open(p, encoding="utf-8").read() if os.path.exists(p) else None
        if a.check:
            if old != text:
                print(f"{rel} is out of date; run scripts/bb-convert.py")
                drift = True
        elif old != text:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w", encoding="utf-8", newline="\n").write(text)
            print(f"wrote {rel}")
    # 3 = only drift (e.g. newer org patches since the last run): CI warns, the
    # nightly update workflow regenerates
    return rc or (3 if drift else 0)


if __name__ == "__main__":
    sys.exit(main())
