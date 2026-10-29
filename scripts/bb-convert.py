#!/usr/bin/env python3
"""Generate the Chainguard overlays for Big Bang from discovery + mapping.yaml.

  bb-convert.py --discovered discovery/images.json [--tags-cache .tags.json]
                [--org <chainguard org>] [--check] [--missing] [--strict-versions]

Writes:
  overlays/values-chainguard.yaml       standard images (FIPS off)
  overlays/values-chainguard-fips.yaml  -fips images; layer it after the first file
  flux/kustomization.yaml, flux-fips/kustomization.yaml
  docs/images.md                        per-package table, including what stays on Iron Bank
  docs/required-images.md               Chainguard repositories an org needs (org-independent)

--check    regenerate in memory; exit 1 on unmapped or untagged images, 3 if a
           file would change only (newer org patches since the last run)
--missing  print the org repositories that discovery needs but the org lacks
--strict-versions
           never move to a newer MINOR than Iron Bank: an image whose MAJOR.MINOR line
           the org lacks is unresolved (exit 1) instead of "MINOR AHEAD"
--org      Chainguard org to point the overlays at (cgr.dev/<org>); default
           $CHAINGUARD_ORG, else the CHAINGUARD_ORG file
--build-registry
           registry holding the images this repo builds (scripts/bb-grafana-plugins.py, bb-base.py);
           default $BUILD_REGISTRY, else the BUILD_REGISTRY file. Empty: they stay on Iron Bank
"""
import argparse
import copy
import json
import os
import re
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bbcommon import build_registry, build_target, ironbank_refs, load_build, load_mapping, split_ref  # noqa: E402
from orgtags import REGISTRY, OrgTags, org_name, parse, resolve  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Unmapped(Exception):
    pass


def set_path(tree, path, value):
    for k in path[:-1]:
        tree = tree.setdefault(k, {})
    tree[path[-1]] = value


class Converter:
    def __init__(self, mapping, tags, registry="", strict=False, ib_tags=None):
        self.cg = f"{REGISTRY}/{tags.org}"
        self.ib_tags = ib_tags or {}   # Iron Bank repo -> tag Big Bang deploys (for `group` checks)
        self._groups = {}
        self.build_registry = registry
        self.strict = strict
        self.images = mapping["images"]
        self.hubs = mapping.get("hubs", {})
        self.defaults = mapping.get("template_defaults", [])
        self.ignore = mapping.get("ignore", [])
        self.tags = tags
        self.rows = []          # (release, ironbank ref, standard, fips, note)
        self.missing = set()    # org repos needed but absent
        self.optional_missing = set()  # absent repos of `fallback: ironbank` images
        self.unmapped = set()
        self.ready = set()      # (Iron Bank repo, org repo): `stays ... until` whose org repo now has tags

    def target(self, ib_repo, ib_tag, release):
        """(standard ref, fips ref, note) for an Iron Bank repo:tag, or None if it stays."""
        m = self.images.get(ib_repo)
        if m is None:
            self.unmapped.add(ib_repo)
            return None
        if isinstance(m, str):
            m = {"cg": m}
        ib = f"{ib_repo}:{ib_tag}" if ib_tag else ib_repo
        if m.get("group") and not self.group_ready(m["group"]):
            self.rows.append((release, ib, "Iron Bank", "Iron Bank",
                              f"falls back to Iron Bank: the {m['group']} images move together; "
                              f"{self._groups[m['group']]}"))
            return None
        if "stays" in m:
            self.rows.append((release, ib, "Iron Bank", "Iron Bank", m["stays"]))
            if m.get("until") and any(parse(t) for t in self.tags.tags(m["until"]) or []):
                self.ready.add((ib_repo, m["until"]))
            return None
        if "build" in m:
            t = build_target(m, self.build_registry)
            if not t:
                self.rows.append((release, ib, "Iron Bank", "Iron Bank",
                                  f"built by scripts/bb-{m['build']}.py; set BUILD_REGISTRY to use it"))
                return None
            self.rows.append((release, ib, *t, f"built FROM the org's images by scripts/bb-{m['build']}.py"))
            return t
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
                    (self.optional_missing if m.get("fallback") else self.missing).add(name)
                    refs.append(None)
                    notes.append(f"org has no {name}")
                    continue
                want = self.version(m, ib_tag)
                ahead = not (self.strict or m.get("strict"))
                tag, note = (resolve(want, tags, m.get("suffix"), allow_ahead=ahead) if want
                             else (None, "no Iron Bank tag"))
            if not tag:
                refs.append(None)
                notes.append(f"{name}: {note}")
                continue
            if note:
                notes.append(f"{name}: {note}")
            elif str(m.get("version")) == "*":
                notes.append(f"{name}: newest org release (Iron Bank {ib_tag})")
            refs.append(f"{self.cg}/{name}:{tag}")
        std, fips = refs
        if fips_name is None:
            fips = std
            notes.append("no FIPS variant; FIPS overlay keeps the standard image")
        if m.get("fallback") == "ironbank" and not (std and fips):
            # Not usable in this org yet (no repo, or nothing on Iron Bank's line):
            # keep Iron Bank's image; a later run converts it once the org has tags.
            self.rows.append((release, ib, "Iron Bank", "Iron Bank",
                              "falls back to Iron Bank until the org has it: " + "; ".join(notes)))
            return None
        self.rows.append((release, ib, std or "UNRESOLVED", fips or "UNRESOLVED", "; ".join(notes)))
        if not std:
            return None
        return std, fips or std

    def version(self, m, ib_tag):
        """The version to match: `version`, the Iron Bank tag of `version_from`, or the image's own."""
        if m.get("version_from"):
            return self.ib_tags.get(m["version_from"])
        return m.get("version", ib_tag)

    def group_ready(self, group):
        """True if every image of a `group` that Big Bang deploys resolves in this org.

        A group (GitLab) moves to Chainguard as one: one member on Iron Bank's
        release and another on a different one would break it.
        """
        if group not in self._groups:
            self._groups[group] = None
            for ib_repo, m in self.images.items():
                if not (isinstance(m, dict) and m.get("group") == group) or ib_repo not in self.ib_tags:
                    continue
                fips = m.get("fips", m["cg"] + "-fips") if "fips" in m else m["cg"] + "-fips"
                for name in (m["cg"], fips):
                    if name is None:
                        continue
                    tags = self.tags.tags(name)
                    if tags is None:
                        (self.optional_missing if m.get("fallback") else self.missing).add(name)
                        self._groups[group] = f"org has no {name}"
                    elif not resolve(self.version(m, self.ib_tags[ib_repo]), tags, m.get("suffix"),
                                     allow_ahead=not (self.strict or m.get("strict")))[0]:
                        self._groups[group] = f"org has no {name} on Iron Bank's line ({self.ib_tags[ib_repo]})"
                    if self._groups[group]:
                        break
                if self._groups[group]:
                    break
        return self._groups[group] is None

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
          @registry               cgr.dev/<org>
        """
        if isinstance(spec, dict):
            return {k: self.overrides(release, v, fips) for k, v in spec.items()}
        if isinstance(spec, list):
            return [self.overrides(release, v, fips) for v in spec]
        if not (isinstance(spec, str) and spec.startswith("@")):
            return spec
        if spec == "@registry":
            return self.cg
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
        tag, note = resolve(version, tags, allow_ahead=not self.strict)
        if not tag:
            self.rows.append((release, f"override {spec}", "UNRESOLVED", "UNRESOLVED", f"{name}: {note}"))
            return spec
        return tag if kind == "tag" else f"{self.cg}/{name}:{tag}"

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


def generate(discovered, mapping, tags, bigbang_version, registry="", strict=False):
    ib_tags = {repo: tag for info in discovered["packages"].values() for repo, tag in ironbank_refs(info) if tag}
    c = Converter(mapping, tags, registry, strict, ib_tags)
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
             ["registry1.dso.mil", "registry.dso.mil"] + allowed_registries(mapping, c.cg, c.build_registry))
    hdr = ("# GENERATED by scripts/bb-convert.py from mapping.yaml - do not edit by hand.\n"
           f"# Generated for {c.cg}: another org regenerates it (README, \"Use it with your org\").\n"
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
    files["docs/images.md"] = report(c.rows, bigbang_version, c.cg)
    return files, c


def allowed_registries(mapping, cg, registry=""):
    """The org, BUILD_REGISTRY, and the owner of every `ref` image (other builds outside the org)."""
    refs = [v[k] for v in mapping["images"].values() if isinstance(v, dict) and "ref" in v
            for k in ("ref", "fips") if v.get(k)]
    return [cg] + sorted({r.rsplit("/", 1)[0] for r in refs} | ({registry} if registry else set()))


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


def required_images(discovered, mapping, version):
    """Markdown: every Chainguard repository the conversion pulls from, for any org.

    Read from mapping.yaml alone (no org file, no org tags), so it is the same for
    every org: the list to check a Chainguard subscription against.
    """
    need = {}  # (standard, fips or None) -> {packages}
    def add(std, fips, release):
        need.setdefault((std, fips), set()).add(release)

    images = mapping["images"]
    for release, info in discovered["packages"].items():
        for ib_repo, _ in ironbank_refs(info):
            m = images.get(ib_repo)
            if m is None:
                continue
            m = {"cg": m} if isinstance(m, str) else m
            if "cg" in m:
                add(m["cg"], m.get("fips", m["cg"] + "-fips") if "fips" in m else m["cg"] + "-fips", release)
            elif "build" in m and (spec := load_build(m["build"])):
                repos = {v: img.get("dev", img.get("base")).split("@")[0].rsplit(":", 1)[0].rsplit("/", 1)[1]
                         for v, img in spec["image"].items()}
                add(repos["standard"], repos.get("fips"), f"{release} (builds/{m['build']})")
    for release, spec in mapping.get("overrides", {}).items():
        for tok in re.findall(r"@(?:repo|tag|ref):([\w.-]+)", json.dumps(spec)):
            add(tok, tok + "-fips", release)
    rows = sorted(need.items())
    fips = sorted({f for (_, f) in need if f})
    lines = [f"# Chainguard images needed for Big Bang {version}", "",
             "Generated by `scripts/bb-convert.py` from `mapping.yaml`. It is the same for every org:",
             "check your Chainguard subscription against it before you start. `bb-convert.py --missing`",
             "lists the ones your org doesn't have yet.", "",
             f"- **{len(rows)} standard images** for a standard (FIPS off) deployment",
             f"- **{len(fips)} FIPS images** for a FIPS deployment (Chainguard FIPS images are usually a separate entitlement)",
             "- Every package is counted. Enable fewer packages and you need fewer images; the last column",
             "  shows which packages use each one.", "",
             "| Standard | FIPS | Used by |", "|---|---|---|"]
    for (std, f), rels in rows:
        lines.append(f"| `{std}` | {f'`{f}`' if f else 'none: FIPS keeps the standard image'} | "
                     f"{', '.join(sorted(rels))} |")
    return "\n".join(lines) + "\n"


def report(rows, version, cg):
    lines = [f"# Image map for Big Bang {version}", "",
             "Generated by `scripts/bb-convert.py`. Every image each package deploys, and what replaces it.",
             f"Standard and FIPS names without a registry are in `{cg}`.",
             "`Iron Bank` means the image stays on registry1 (reason in the last column); "
             "sites that enable that package still need registry1 credentials.", "",
             "| Package | Iron Bank image | Standard | FIPS | Notes |", "|---|---|---|---|---|"]
    seen = set()
    for r in sorted(rows):
        if r in seen:
            continue
        seen.add(r)
        lines.append("| " + " | ".join(x.replace(f"{cg}/", "").replace("|", "\\|") for x in r) + " |")
    stays = sorted({(r[1].split(":")[0], r[4]) for r in rows if r[2] == "Iron Bank"})
    lines += ["", f"## Stays on Iron Bank ({len(stays)})", ""] + [f"- `{i}`: {why}" for i, why in stays]
    ahead = sorted({(r[0], r[1], r[2].replace(f"{cg}/", "")) for r in rows if "MINOR AHEAD" in r[4]})
    older = sorted({(r[0], r[1], r[2].replace(f"{cg}/", "")) for r in rows if "is older than Iron Bank" in r[4]})
    newest = sorted({(r[0], r[1], r[2].replace(f"{cg}/", "")) for r in rows if "newest org release" in r[4]})
    lines += ["", f"## Version differences ({len(ahead) + len(older) + len(newest)})", "",
              "Big Bang tests each package with the Iron Bank version it pins. These images run a",
              "different version because the org has nothing on that MAJOR.MINOR line (the org syncs",
              "Chainguard's current release lines). Test these packages before production, or run",
              "`bb-convert.py --strict-versions` to refuse them.", ""]
    lines += [f"- {rel}: `{ib}` -> `{std}` (newer minor)" for rel, ib, std in ahead]
    lines += [f"- {rel}: `{ib}` -> `{std}` (older patch)" for rel, ib, std in older]
    lines += [f"- {rel}: `{ib}` -> `{std}` (newest release; Iron Bank's tag has no version line)"
              for rel, ib, std in newest]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--discovered", default=os.path.join(ROOT, "discovery", "images.json"))
    ap.add_argument("--mapping", default=os.path.join(ROOT, "mapping.yaml"))
    ap.add_argument("--tags-cache")
    ap.add_argument("--offline", action="store_true", help="use only --tags-cache; never call the registry")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--missing", action="store_true")
    ap.add_argument("--org", help="Chainguard org (default: $CHAINGUARD_ORG, else the CHAINGUARD_ORG file)")
    ap.add_argument("--build-registry", help="registry of the images this repo builds (default: $BUILD_REGISTRY, "
                                             "else the BUILD_REGISTRY file)")
    ap.add_argument("--strict-versions", action="store_true",
                    help="never use a newer MINOR than Iron Bank's (no MINOR AHEAD)")
    ap.add_argument("--mapping-overlay", action="append", default=[], metavar="FILE",
                    help="apply FILE's `images` over the mapping (e.g. compat/<test>.yaml); repeatable")
    ap.add_argument("--out", help="write the generated files under this directory instead of the repo")
    a = ap.parse_args()

    discovered = json.load(open(a.discovered))
    org = org_name(a.org)
    mapping = load_mapping(org, a.mapping, a.mapping_overlay)
    version = open(os.path.join(ROOT, "BIGBANG_VERSION")).read().strip()
    tags = OrgTags(org, a.tags_cache, offline=a.offline)
    files, c = generate(discovered, mapping, tags, version, build_registry(a.build_registry), a.strict_versions)
    files["docs/required-images.md"] = required_images(discovered, yaml.safe_load(open(a.mapping)), version)
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
    if c.ready:
        print("Ready to convert (the org now has tags; change the `stays` entry in mapping.yaml):")
        for ib, repo in sorted(c.ready):
            print(f"  {ib} -> {repo}")
    if a.missing:
        print("Org repositories needed but missing:", *sorted(c.missing) or ["(none)"], sep="\n  ")
        if c.optional_missing:
            print("Optional (their images stay on Iron Bank until added):", *sorted(c.optional_missing),
                  sep="\n  ")
        return rc
    drift = False
    for rel, text in files.items():
        p = os.path.join(a.out or ROOT, rel)
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
