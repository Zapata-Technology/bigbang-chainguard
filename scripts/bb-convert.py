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
  docs/version-differences.md           each image's version: Iron Bank's vs the one deployed

--check    regenerate in memory; exit 1 on unmapped or untagged images, 3 if a
           file would change only (newer org patches since the last run)
--missing  print the org repositories that discovery needs but the org lacks
--strict-versions
           never move to a newer MINOR than Iron Bank: an image whose MAJOR.MINOR line
           the org lacks is unresolved (exit 1) instead of "MINOR AHEAD"
--org      Chainguard org to point the overlays at (cgr.dev/<org>); default
           $CHAINGUARD_ORG, else the CHAINGUARD_ORG file
--build-registry
           registry holding the images this repo builds (scripts/bb-build.py, builds/<name>/);
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
from bbcommon import IRONBANK_HOSTS, build_registry, build_target, ironbank_refs, load_build, load_mapping, org_note, split_ref  # noqa: E402
from orgtags import _IB_SUFFIXES, REGISTRY, OrgTags, org_name, parse, resolve  # noqa: E402
import fluxrelease  # noqa: E402
import objstore  # noqa: E402

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
        self._group_patch = {}
        self.build_registry = registry
        self.strict = strict
        self.images = mapping["images"]
        self.hubs = mapping.get("hubs", {})
        self.defaults = mapping.get("template_defaults", [])
        self.ignore = mapping.get("ignore", [])
        self.unpinned = mapping.get("unpinned", {})   # release -> {org repo: why it stays on its tag}
        self.tags = tags
        self.rows = []          # (release, ironbank ref, standard, fips, note)
        self.missing = set()    # org repos needed but absent
        self.optional_missing = set()  # absent repos of `fallback: ironbank` images
        self.unmapped = set()
        self.ready = set()      # (Iron Bank repo, org repo): `stays ... until` whose org repo now has tags
        self.used = {}          # release -> {"standard"|"fips": {org ref}}: what digest_pins() pins

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
                                  f"built by scripts/bb-build.py {m['build']}; set BUILD_REGISTRY to use it"))
                return None
            note = f"built FROM the org's images by scripts/bb-build.py {m['build']}"
            tag = t[0].rsplit(":", 1)[1]
            if ib_tag and parse(tag) and parse(ib_tag) and parse(tag)[0][:2] != parse(ib_tag)[0][:2]:
                note += f"; VERSION DIFFERS from Big Bang's {ib_tag}"
            self.rows.append((release, ib, *t, note))
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
                if below_floor(want, m.get("min_version")):
                    # An end-of-life line (vault's kubectl 1.30): the org stops rebuilding it,
                    # so its CVEs pile up, and it is outside kubectl's skew for the clusters
                    # Big Bang supports. Take the newest release instead.
                    notes.append(f"{name}: Iron Bank {ib_tag} is below min_version {m['min_version']}; newest release")
                    want = "*"
                ahead = not (self.strict or m.get("strict"))
                pinned = self.group_patch(m)
                if pinned:
                    # A group's members all run the same patch: the newest every member has.
                    tag = exact_tag(tags, pinned, m.get("suffix"))
                    note = None if tag else f"no {'.'.join(map(str, pinned))} tag"
                else:
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
        used = self.used.setdefault(release, {"standard": set(), "fips": set()})
        used["standard"].add(std)
        used["fips"].add(fips or std)
        return std, fips or std

    def digest_pins(self, release, variant):
        """Kustomize image entries pinning each org image the release deploys to the digest
        its tag has now. Chainguard rebuilds images under the same tag (keycloak 26.8.1 on
        2026-10-30 changed file modes and broke Big Bang's uid); pinned, a rebuild reaches a
        cluster only through the nightly pull request, after its tests. A post-renderer, not
        values: charts reuse image tags in labels and version checks, where a digest breaks
        them. A repository the release uses at two tags is left unpinned (kustomize matches
        by name only), as are images set in custom resources rather than pod specs."""
        by_repo = {}
        for ref in self.used.get(release, {}).get(variant, ()):
            repo, tag = ref.rsplit(":", 1)
            by_repo.setdefault(repo, set()).add(tag)
        pins = []
        skip = {f"{self.cg}/{r}" for r in self.unpinned.get(release, {})}
        skip |= {f"{r}-fips" for r in skip}
        for repo, tags in sorted(by_repo.items()):
            if len(tags) != 1 or repo in skip:
                continue
            digest = self.tags.pinned(repo[len(self.cg) + 1:], next(iter(tags)))
            if digest:
                pins.append({"name": repo, "digest": digest})
        return pins

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
            common = {}   # (same_patch set, version line) -> patches every member of it has
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
                    elif m.get("same_patch"):
                        line = parse(self.version(m, self.ib_tags[ib_repo]))[0][:2]
                        key = (m["same_patch"], line)
                        have = {p[0] for t in tags if (p := parse(t)) and p[1] == m.get("suffix")
                                and len(p[0]) >= 3 and p[0][:2] == line}
                        common[key] = have if key not in common else common[key] & have
                    if self._groups[group]:
                        break
                if self._groups[group]:
                    break
            if self._groups[group] is None:
                empty = sorted(f"{ln[0]} {'.'.join(map(str, ln[1]))}" for ln, have in common.items() if not have)
                if empty:
                    self._groups[group] = f"no {', '.join(empty)} patch that every member has"
                else:
                    self._group_patch[group] = {ln: max(have) for ln, have in common.items()}
        return self._groups[group] is None

    def group_patch(self, m):
        """The patch a `same_patch` member of a group must use (None otherwise)."""
        if not (m.get("group") and m.get("same_patch")) or not self.group_ready(m["group"]):
            return None
        ib_repo = next((r for r, v in self.images.items() if v is m), None)
        want = self.version(m, self.ib_tags.get(ib_repo, "")) if ib_repo else None
        line = (m["same_patch"], parse(want)[0][:2]) if want and parse(want) else None
        return self._group_patch.get(m["group"], {}).get(line)

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
                        if info.get("key") in d.get("patch_keys", []):
                            continue   # generate() adds d["patch"] as a post-renderer instead
                        for k, v in (d.get("beside") or {}).items():   # e.g. the wait job's seccompProfile
                            for vals in (std_vals, fips_vals):
                                set_path(vals, d["path"].split(".")[:-1] + [k], copy.deepcopy(v))
                    covered.add(ib_repo)

    def overrides(self, release, spec, fips, tag=None):
        """Expand a mapping.yaml `overrides` block. Tokens:
          @repo:<name>            <name>, or <name>-fips in the FIPS overlay
          @tag:<name>:<version>   org tag of <name>(-fips) for <version> (same rules as images);
                                  <version> "ib=<Iron Bank repo>" means that image's deployed tag
          @ref:<name>:<version>   cgr.dev/<org>/<name>(-fips):<tag>
          @registry               cgr.dev/<org>
        """
        if isinstance(spec, dict):
            # A chart-wide tag (NeuVector's) belongs to every @repo below it: carry it down
            # so those images are recorded for digest_pins() too.
            if isinstance(spec.get("tag"), str) and spec["tag"].startswith("@tag:"):
                tag = self.overrides(release, spec["tag"], fips)
            return {k: self.overrides(release, v, fips, tag) for k, v in spec.items()}
        if isinstance(spec, list):
            return [self.overrides(release, v, fips, tag) for v in spec]
        if not (isinstance(spec, str) and spec.startswith("@")):
            return spec
        if spec == "@registry":
            return self.cg
        kind, _, rest = spec[1:].partition(":")
        base, _, version = rest.partition(":")
        name = f"{base}-fips" if fips else base
        if fips and kind in ("repo", "ref") and not any(parse(t) for t in self.tags.tags(name) or []):
            # The org has no usable -fips image (e.g. neuvector-manager-fips exists without
            # tags): the FIPS overlay keeps the standard image rather than name a missing one.
            self.rows.append((release, f"override {spec}", base, base,
                              f"no {name} tags in the org; FIPS overlay keeps the standard image"))
            name = base
        if kind == "repo":
            if tag and not tag.startswith("@") and tag in (self.tags.tags(name) or []):
                self.used.setdefault(release, {"standard": set(), "fips": set()})[
                    "fips" if fips else "standard"].add(f"{self.cg}/{name}:{tag}")
            return name
        tags = self.tags.tags(name)
        if tags is None:
            self.missing.add(name)
            self.rows.append((release, f"override {spec}", "UNRESOLVED", "UNRESOLVED", f"org has no {name}"))
            return spec
        if version.startswith("ib="):
            version = self.ib_tags.get(version[3:], "")
        tag, note = resolve(version, tags, allow_ahead=not self.strict)
        if not tag:
            self.rows.append((release, f"override {spec}", "UNRESOLVED", "UNRESOLVED", f"{name}: {note}"))
            return spec
        # The image an override names is deployed like any other: digest_pins() pins it.
        used = self.used.setdefault(release, {"standard": set(), "fips": set()})
        used["fips" if fips else "standard"].add(f"{self.cg}/{name}:{tag}")
        return tag if kind == "tag" else f"{self.cg}/{name}:{tag}"

    def flux(self, info):
        """{"standard"|"fips": (Iron Bank repo, Iron Bank tag, chosen ref)} for Flux's controllers."""
        out = {"standard": [], "fips": []}
        for ref in info["rendered"]:
            ib_repo, ib_tag = split_ref(ref)
            t = self.target(ib_repo, ib_tag, "flux")
            if t:
                out["standard"].append((ib_repo, ib_tag, t[0]))
                out["fips"].append((ib_repo, ib_tag, t[1]))
        return out


def chart_registry_builds(mapping, c, bb_key, variant):
    """Kustomize image rewrites for images this repo builds in a chart that prefixes every
    image with one chart-wide registry (mapping chart_registry_builds): the overlay sets that
    registry to the org's, so the chart renders <org registry>/<built image name>; this
    moves it to BUILD_REGISTRY. With no BUILD_REGISTRY the image stays on Iron Bank, and the
    chart renders <org registry>/<Iron Bank repository> (cgr.dev/<org>/ironbank/...), which
    does not exist: this moves it back to registry1, at the chart's own tag."""
    out = []
    for ib_repo in mapping.get("chart_registry_builds", {}).get(bb_key, []):
        m = c.images.get(ib_repo)
        t = build_target(m, c.build_registry) if isinstance(m, dict) and "build" in m else None
        if t:
            name, tag = t[variant == "fips"].rsplit(":", 1)
            out.append({"name": f"{c.cg}/{name.rsplit('/', 1)[1]}", "newName": name, "newTag": tag})
        else:
            out.append({"name": f"{c.cg}/{ib_repo}", "newName": f"{IRONBANK_HOSTS[0]}/{ib_repo}"})
    return out


def dump(data, header):
    return header + yaml.safe_dump(data, sort_keys=True, default_flow_style=False, width=120)


def generate(discovered, mapping, tags, bigbang_version, registry="", strict=False):
    ib_tags = {repo: tag for info in discovered["packages"].values() for repo, tag in ironbank_refs(info) if tag}
    c = Converter(mapping, tags, registry, strict, ib_tags)
    std, fips = {}, {}
    flux = {"standard": [], "fips": []}
    for release, info in sorted(discovered["packages"].items()):
        if release == "flux":
            flux = c.flux(info)
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
                    name, tag = tgt.rsplit(":", 1)
                    entry = {"name": f"registry1.dso.mil/{ib_repo}", "newName": name, "newTag": tag}
                    # kustomize applies one entry per image name, so the digest pin for the
                    # new name never reaches a renamed image: the rename carries it.
                    digest = c.tags.pinned(name[len(c.cg) + 1:], tag) if name.startswith(c.cg + "/") else None
                    if digest:
                        entry["digest"] = digest
                    pr[0]["kustomize"]["images"].append(entry)
                    set_path(tree, key + ["postRenderers"], pr)
        # Version labels that must name the image actually deployed (a newer patch than
        # the chart's appVersion): Flux post-renderer JSON patch on the workload.
        for kind_name, ib_repo in mapping.get("version_labels", {}).get(info["key"], {}).items():
            t = c.target(ib_repo, c.ib_tags.get(ib_repo), release)
            if not t:
                continue
            kind, name = kind_name.split("/", 1)
            for tree, tgt in ((std, t[0]), (fips, t[1])):
                pr = _get(tree, key + ["postRenderers"]) or [{"kustomize": {}}]
                pr[0]["kustomize"].setdefault("patches", []).append({
                    "target": {"kind": kind, "name": name},
                    "patch": json.dumps([{"op": "add", "path": "/metadata/labels/app.kubernetes.io~1version",
                                          "value": tgt.rsplit(":", 1)[1]}])})
                set_path(tree, key + ["postRenderers"], pr)
        # Wait jobs whose scripts say #!/bin/sh but need bash: run each script with bash.
        for job in mapping.get("bash_wait_jobs", {}).get(info["key"], []):
            for tree in (std, fips):
                pr = _get(tree, key + ["postRenderers"]) or [{"kustomize": {}}]
                pr[0]["kustomize"].setdefault("patches", []).append({
                    "target": {"kind": "Job", "name": job},
                    "patch": json.dumps([{"op": "replace", "path": "/spec/template/spec/containers/0/command",
                                          "value": ["/bin/bash", "-c", BASH_WAIT]}])})
                set_path(tree, key + ["postRenderers"], pr)
        # template_defaults whose `beside` values this package's chart schema refuses: the
        # same change as a post-renderer patch.
        rendered = {split_ref(ref)[0] for ref in info["rendered"]}
        for d in mapping.get("template_defaults", []):
            if info["key"] in d.get("patch_keys", []) and d["image"] in rendered and d.get("patch"):
                for tree in (std, fips):
                    pr = _get(tree, key + ["postRenderers"]) or [{"kustomize": {}}]
                    patches = pr[0]["kustomize"].setdefault("patches", [])
                    if d["patch"] not in patches:   # one per Big Bang key (istioGateway renders two gateways)
                        patches.append(copy.deepcopy(d["patch"]))
                    set_path(tree, key + ["postRenderers"], pr)
        # Containers whose command is /bin/sh -c <bash script>: run it with /bin/bash.
        for ref in mapping.get("bash_commands", {}).get(info["key"], []):
            kind, name, container = ref["kind"], ref["name"], ref["container"]
            for tree in (std, fips):
                pr = _get(tree, key + ["postRenderers"]) or [{"kustomize": {}}]
                pr[0]["kustomize"].setdefault("patches", []).append({
                    "target": {"kind": kind, "name": name},
                    "patch": json.dumps([{"op": "replace", "path": f"/spec/template/spec/{container}/command/0",
                                          "value": "/bin/bash"}])})
                set_path(tree, key + ["postRenderers"], pr)
        if release in mapping.get("overrides", {}):
            spec = mapping["overrides"][release]
            _merge_into(s, c.overrides(release, spec, False))
            _merge_into(f, c.overrides(release, spec, True))
        if s:
            set_path(std, key + root, _merge(_get(std, key + root), s))
        if f:
            set_path(fips, key + root, _merge(_get(fips, key + root), f))
        # Org images pinned to today's digests (Converter.digest_pins); after values, so
        # the images the values just set are covered. Not for a package whose Big Bang
        # template can't take post-renderers (mapping no_post_renderers).
        for tree, variant in (() if info["key"] in mapping.get("no_post_renderers", {})
                              else ((std, "standard"), (fips, "fips"))):
            pins = c.digest_pins(release, variant) + chart_registry_builds(mapping, c, info["key"], variant)
            if pins:
                pr = _get(tree, key + ["postRenderers"]) or [{"kustomize": {}}]
                pr[0]["kustomize"].setdefault("images", []).extend(pins)
                set_path(tree, key + ["postRenderers"], pr)
    for path, allow in registry_policies(mapping, c.cg, c.build_registry).items():
        set_path(std, path, allow)
    hdr = ("# GENERATED by scripts/bb-convert.py from mapping.yaml - do not edit by hand.\n"
           f"{org_note(c.cg)}\n"
           f"# Big Bang {bigbang_version}. ")
    files = {
        "overlays/values-chainguard.yaml": dump(std, hdr + "Standard (non-FIPS) Chainguard images.\n"
                                            "# helm upgrade -i bigbang <chart> -f overlays/values-chainguard.yaml -f <site>.yaml\n"),
        "overlays/values-chainguard-fips.yaml": dump(fips, hdr + "FIPS: layer after values-chainguard.yaml.\n"
                                                 "# ... -f overlays/values-chainguard.yaml -f overlays/values-chainguard-fips.yaml -f <site>.yaml\n"),
    }
    for d, variant in (("flux", "standard"), ("flux-fips", "fips")):
        files[f"{d}/kustomization.yaml"] = flux_kustomization(flux[variant], bigbang_version, c, hdr, d)
    files["docs/images.md"] = report(c.rows, bigbang_version, c.cg)
    files["docs/version-differences.md"] = version_report(c.rows, bigbang_version, c.cg, mapping["images"])
    return files, c


def wanted_repos(mapping):
    """Every org repository the conversion may ask tags for: each image's name and its FIPS
    twin, `until` names, override tokens' names (and -fips), SeaweedFS's."""
    names = {"seaweedfs", "seaweedfs-fips"}
    for m in mapping["images"].values():
        m = {"cg": m} if isinstance(m, str) else m
        if m.get("cg"):
            names.add(m["cg"])
            if m.get("fips", m["cg"] + "-fips"):
                names.add(m.get("fips", m["cg"] + "-fips"))
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
    return names


def prefetch(tags, mapping, dry_run):
    """Ask the registry for everything the conversion will need, in parallel, so the real
    run answers from the cache: one request at a time, that took ~3 minutes. Tags come
    first (the repositories the mapping names); then a dry run of the conversion collects
    the digests it pins (each found only once its tag is chosen), and those are fetched.
    Anything this misses, the real run still asks for: the result is the same either way."""
    if tags.offline:
        return
    tags.prefetch(repos=wanted_repos(mapping))
    tags.record_digests = set()
    try:
        dry_run()
        refs = tags.record_digests
    finally:
        tags.record_digests = None
    tags.prefetch(refs=refs)


def run_on_copies(run, *inputs):
    """run() with each of `inputs` (dicts it reads) restored afterwards: the dry run must
    leave nothing behind for the real one."""
    saved = [copy.deepcopy(x) for x in inputs]
    try:
        return run()
    finally:
        for x, s in zip(inputs, saved):
            x.clear()
            x.update(s)


def flux_kustomization(controllers, bigbang_version, c, hdr, d):
    """Big Bang's base/flux with Chainguard images. When the org's controllers are a newer
    minor than Big Bang's, Big Bang's base has CRDs too old for them: install the upstream
    Flux release they come from instead, with Big Bang's patches."""
    def name(ib_repo):
        return ib_repo.rsplit("/", 1)[1]

    def pin(image):
        """The controller pinned to its tag's digest (Converter.digest_pins says why)."""
        if image["newName"].startswith(c.cg + "/"):
            digest = c.tags.pinned(image["newName"][len(c.cg) + 1:], image["newTag"])
            if digest:
                image["digest"] = digest
        return image
    ahead = [n for ib_repo, ib_tag, ref in controllers
             if (n := name(ib_repo)) and fluxrelease._line(ref.rsplit(":", 1)[1]) != fluxrelease._line(ib_tag)]
    note = ""
    if not ahead:
        resources = [f"https://repo1.dso.mil/big-bang/bigbang.git//base/flux?ref={bigbang_version}"]
        images = [pin({"name": f"registry1.dso.mil/{ib_repo}", "newName": ref.rsplit(":", 1)[0],
                       "newTag": ref.rsplit(":", 1)[1]}) for ib_repo, _, ref in controllers]
        patches = [fluxrelease.seccomp_patch()]
    else:
        versions = {name(ib_repo): ref.rsplit(":", 1)[1] for ib_repo, _, ref in controllers}
        found = fluxrelease.release_for(versions)
        if not found:
            c.rows.append(("flux", "flux controllers", "UNRESOLVED", "UNRESOLVED",
                           f"no Flux release has controllers on the lines of {versions}"))
            return "# UNRESOLVED: no Flux release matches the org's controllers; see bb-convert output\n"
        tag, _ = found
        resources = [fluxrelease.INSTALL.format(tag=tag)]
        images = [pin({"name": f"ghcr.io/fluxcd/{name(ib_repo)}", "newName": ref.rsplit(":", 1)[0],
                       "newTag": ref.rsplit(":", 1)[1]}) for ib_repo, _, ref in controllers]
        patches = fluxrelease.remove_extras() + fluxrelease.bigbang_patches(bigbang_version, os.path.join(ROOT, "work")) \
            + [fluxrelease.seccomp_patch()]
        note = (f"# The org's Flux controllers ({', '.join(sorted(ahead))}) are newer than Big Bang {bigbang_version}'s,\n"
                f"# whose CRDs are too old for them: this installs Flux {tag} (their release) with Big Bang's patches.\n")
    k = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
         "resources": resources, "images": images}
    if patches:
        k["patches"] = patches
    return dump(k, hdr + ("FIPS " if d.endswith("fips") else "") + "Flux on Chainguard images.\n" + note
                + f"# kubectl apply --server-side -k {d}/   (needs the private-registry pull secret in flux-system)\n")


STORAGE_FILES = ("overlays/values-object-storage.yaml", "seaweedfs/kustomization.yaml", "seaweedfs/seaweedfs.yaml",
                 "seaweedfs-fips/kustomization.yaml", "seaweedfs-fips/seaweedfs.yaml")


def object_storage_files(kind, endpoint, c, tags, version, files):
    """OBJECT_STORAGE=seaweedfs: the object-storage overlay and seaweedfs(-fips)/ (and a
    section in docs/images.md and docs/required-images.md). minio: none of them (None
    removes a file left from before)."""
    if kind == "minio":
        files["docs/images.md"] += "\n".join([
            "", "## Object storage", "",
            "OBJECT_STORAGE=minio: Big Bang deploys MinIO itself, on the images listed above.", ""])
        return {rel: None for rel in STORAGE_FILES}
    spec = objstore.load_spec()
    t = objstore.tokens(endpoint, "us-east-1", "bigbang")
    overlay, _, buckets, chosen = objstore.build(spec, t, set())
    out = {"overlays/values-object-storage.yaml": objstore.overlay_text(
        overlay, t, chosen, "scripts/bb-convert.py (OBJECT_STORAGE=seaweedfs)")}
    refs = {}
    for d, repo in (("seaweedfs", "seaweedfs"), ("seaweedfs-fips", "seaweedfs-fips")):
        tag = objstore_tag(tags.tags(repo))
        if not tag:
            c.missing.add(repo)
            c.rows.append(("object-storage", f"SeaweedFS ({d}/)", "UNRESOLVED", "UNRESOLVED",
                           f"org has no {repo} release with a -dev twin (OBJECT_STORAGE=seaweedfs)"))
            continue
        refs[d] = f"{c.cg}/{repo}:{tag}"
        header = "\n".join([
            "# GENERATED by scripts/bb-convert.py (OBJECT_STORAGE=seaweedfs) - do not edit by hand.",
            f"# SeaweedFS for Big Bang {version}'s object storage{' (FIPS)' if d.endswith('fips') else ''}.",
            org_note(c.cg),
            "# Before applying, create the private-registry and seaweedfs-s3 Secrets in the",
            '# seaweedfs namespace (README, "Object storage").', ""])
        pinned = [f"{r}@{dg}" if (dg := tags.pinned(r.rsplit("/", 1)[1].split(":")[0], r.rsplit(":", 1)[1])) else r
                  for r in (refs[d], f"{refs[d]}-dev")]
        for rel, text in objstore.seaweedfs_files(*pinned, buckets, header).items():
            out[f"{d}/{rel}"] = text
    files["docs/images.md"] += "\n".join(
        ["", "## Object storage", "",
         "OBJECT_STORAGE=seaweedfs: Big Bang's MinIO is off, and packages use the S3 endpoint "
         f"{t['endpoint']} (overlays/values-object-storage.yaml).", ""]
        + [f"- `{d}/`: `{ref}`" for d, ref in refs.items()] + [""])
    files["docs/required-images.md"] += "\n".join([
        "", "## Object storage", "",
        "With OBJECT_STORAGE=seaweedfs (the default): `seaweedfs` and `seaweedfs-fips`, each with its `-dev`",
        "twin (the bucket Job). With OBJECT_STORAGE=minio: `minio` and `minio-client` (and their `-fips`),",
        "already listed above.", ""])
    return out


# Big Bang's wait-job loop (gluon chart), running each script with bash instead of by its
# #!/bin/sh line, and failing on the first script that fails.
BASH_WAIT = ('if [ -d /src ]; then cp /src/* /wait/ 2>/dev/null || true; fi\n'
             'for script in *; do\n'
             '  [ -f "$script" ] || continue\n'
             '  echo "---"; echo "Running $script..."; echo "---"\n'
             '  bash "./$script" || exit 1\n'
             'done\n')


def below_floor(version, floor):
    """True when `version` (an Iron Bank tag or mapping version) is on a MAJOR.MINOR line
    older than `floor` ("1.34")."""
    if not floor or str(version) == "*":
        return False
    want, low = parse(re.sub(r"-ubi.*$", "", str(version))), parse(str(floor))
    return bool(want and low) and want[0][:2] < low[0][:2]


def exact_tag(tags, nums, suffix=None):
    """The org's spelling of version `nums` (plain over v-prefixed), or None."""
    hits = [t for t in tags or [] if (p := parse(t)) and p[0] == nums and p[1] == suffix]
    return sorted(hits, key=lambda t: t.startswith("v"))[0] if hits else None


def objstore_tag(available):
    """The org's newest SeaweedFS release that also has a -dev tag (the bucket Job needs a shell)."""
    releases = sorted(((p[0], t) for t in available or [] if (p := parse(t)) and p[1] is None), reverse=True)
    return next((t for _, t in releases if f"{t}-dev" in available), None)


IRON_BANK_REGISTRIES = ["registry1.dso.mil", "registry.dso.mil"]


def registry_policies(mapping, cg, registry=""):
    """{values path: allow list} for Big Bang's two registry admission policies, which
    admit only Iron Bank's registries by default: Kyverno's restrict-image-registries and
    Gatekeeper's allowed-docker-registries (enforcementAction deny; with Gatekeeper on,
    every org image was refused, compat/gatekeeper.yaml). Helm replaces lists, so each
    keeps Iron Bank's registries for the documented leftovers."""
    allow = IRON_BANK_REGISTRIES + allowed_registries(mapping, cg, registry)
    return {
        ("packages", "kyvernoPolicies", "values", "policies", "restrict-image-registries", "parameters", "allow"): allow,
        ("packages", "gatekeeper", "values", "violations", "allowedDockerRegistries", "parameters", "repos"):
            list(allow),
    }


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
             org_note(f"`{cg}`", ""),
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
    built = sorted({(r[0], r[1], r[2]) for r in rows if "VERSION DIFFERS" in r[4]})
    lines += ["", f"## Version differences ({len(ahead) + len(older) + len(newest) + len(built)})", "",
              "Big Bang tests each package with the Iron Bank version it pins. These images run a",
              "different version because the org has nothing on that MAJOR.MINOR line (the org syncs",
              "Chainguard's current release lines). Test these packages before production, or run",
              "`bb-convert.py --strict-versions` to refuse them.", ""]
    lines += [f"- {rel}: `{ib}` -> `{std}` (newer minor)" for rel, ib, std in ahead]
    lines += [f"- {rel}: `{ib}` -> `{std}` (older patch)" for rel, ib, std in older]
    lines += [f"- {rel}: `{ib}` -> `{std}` (newest release; Iron Bank's tag has no version line)"
              for rel, ib, std in newest]
    lines += [f"- {rel}: `{ib}` -> `{std}` (built by this repo on a different version line)"
              for rel, ib, std in built]
    return "\n".join(lines) + "\n"


# version_report's comparisons, most different first.
DIFFERENCES = ["newer major", "older major", "newer minor", "older minor", "older patch", "newer patch",
               "same", "no version to compare", "numbered differently", "stays on Iron Bank"]


def version_difference(ib_tag, tag):
    """How the deployed tag's version compares with Iron Bank's: one of DIFFERENCES."""
    if tag is None:
        return "stays on Iron Bank"
    def version(t):
        # A version after a prefix counts too: GitLab Runner's "ubi-fips-v19.2.2".
        m = re.search(r"v?\d+\.\d+(\.\d+)*", t)
        return parse(t) or (parse(m.group(0)) if m else None)
    a, b = version(_IB_SUFFIXES.sub("", ib_tag)), version(tag)
    if not a or not b:
        return "no version to compare"
    a, b = (a[0] + (0, 0, 0))[:3], (b[0] + (0, 0, 0))[:3]
    for i, part in enumerate(("major", "minor", "patch")):
        if a[i] != b[i]:
            return f"{'newer' if b[i] > a[i] else 'older'} {part}"
    return "same"


def numbering(m):
    """Why a mapping entry's tag isn't compared with Iron Bank's, or None: `version_from`
    (another image's version, e.g. the GitLab release) or a pinned `version` (an image
    Chainguard numbers by another release, e.g. alloy-operator's)."""
    if not isinstance(m, dict):
        return None
    if m.get("version_from"):
        return f"follows `{m['version_from']}`'s version"
    if m.get("version") == "*":
        return "newest release (Iron Bank's tag isn't this image's version)"
    if m.get("version") is not None:
        return f"pinned to the {m['version']} line (Iron Bank's tag isn't this image's version)"
    return None


def version_report(rows, version, cg, images_map=None):
    """docs/version-differences.md: for each Iron Bank image Big Bang pins, the version the
    overlays deploy instead (standard and FIPS), and how far it is from Iron Bank's."""
    images = {}
    for rel, ib, std, fips, _ in rows:
        # Image rows only: not override notes ("override @repo:..."), nothing unresolved.
        if " " in ib or ":" not in ib or std == "UNRESOLVED" or (std != "Iron Bank" and ":" not in std):
            continue
        entry = images.setdefault((ib, std, fips), set())
        entry.add(rel)

    def short(ref):
        return ref.replace(f"{cg}/", "") if ref != "Iron Bank" else "Iron Bank"

    out = []
    for (ib, std, fips), rels in images.items():
        ib_tag = ib.rsplit(":", 1)[1]
        tag = None if std == "Iron Bank" else std.rsplit(":", 1)[1]
        source = "Iron Bank" if tag is None else ("Chainguard" if std.startswith(cg + "/") or "/" not in std
                                                  else "built here")
        why = numbering((images_map or {}).get(split_ref(ib)[0])) if tag is not None else None
        difference = "numbered differently" if why else version_difference(ib_tag, tag)
        out.append((DIFFERENCES.index(difference), ib, short(std), short(fips), source,
                    ", ".join(sorted(rels)) + (f" ({why})" if why else "")))
    out.sort()
    counts = {d: sum(1 for o in out if o[0] == i) for i, d in enumerate(DIFFERENCES)}
    lines = [f"# Image versions: Iron Bank vs Chainguard (Big Bang {version})", "",
             "Generated by `scripts/bb-convert.py`. For each image Big Bang "
             f"{version} pins from Iron Bank, the image the overlays deploy instead and how its "
             "version compares. Chainguard names are in "
             f"`{cg}`; \"built here\" images are built by this repo FROM Chainguard images.",
             org_note(f"`{cg}`", ""), "",
             "Tags are picked as the newest Chainguard patch on Iron Bank's MAJOR.MINOR line. An image",
             "lands on a different minor or major only when the org has nothing on Iron Bank's line",
             "(Chainguard keeps current release lines); `bb-convert.py --strict-versions` refuses those.",
             "", "Counts are per Iron Bank image and tag: an image Big Bang pins at two tags counts twice.",
             "", "## Summary", "", "| Compared with Iron Bank | Images |", "|---|---|"]
    lines += [f"| {d} | {n} |" for d, n in counts.items() if n]
    lines += [f"| **total** | **{len(out)}** |", ""]
    for i, title in ((range(0, 5), "Different version line"), (range(5, 6), "Newer patch, same line"),
                     (range(6, 7), "Same version"),
                     (range(7, 9), "Not compared: numbered differently, or no version in the tag"),
                     (range(9, 10), "Stays on Iron Bank")):
        group = [o for o in out if o[0] in i]
        if not group:
            continue
        lines += [f"## {title} ({len(group)})", "",
                  "| Iron Bank image | Standard | FIPS | Source | Compared | Packages |",
                  "|---|---|---|---|---|---|"]
        lines += [f"| `{ib}` | `{std}` | `{fips}` | {src} | {DIFFERENCES[d]} | {rels} |"
                  for d, ib, std, fips, src, rels in group]
        lines.append("")
    return "\n".join(lines)


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
    ap.add_argument("--object-storage", help="seaweedfs (default) or minio (default: $OBJECT_STORAGE, else the "
                                              "OBJECT_STORAGE file)")
    ap.add_argument("--object-storage-endpoint", help="S3 endpoint for OBJECT_STORAGE=seaweedfs (default: the "
                                                       "in-cluster SeaweedFS in seaweedfs/)")
    a = ap.parse_args()

    discovered = json.load(open(a.discovered))
    org = org_name(a.org)
    mapping = load_mapping(org, a.mapping, a.mapping_overlay)
    version = open(os.path.join(ROOT, "BIGBANG_VERSION")).read().strip()
    tags = OrgTags(org, a.tags_cache, offline=a.offline)

    def run():
        files, c = generate(discovered, mapping, tags, version, build_registry(a.build_registry), a.strict_versions)
        files["docs/required-images.md"] = required_images(discovered, yaml.safe_load(open(a.mapping)), version)
        files.update(object_storage_files(objstore.kind(a.object_storage),
                                          objstore.endpoint(a.object_storage_endpoint), c, tags, version, files))
        return files, c
    prefetch(tags, mapping, lambda: run_on_copies(run, discovered, mapping))
    files, c = run()
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
        if text is None:   # a file the current settings don't produce (OBJECT_STORAGE=minio)
            if old is not None:
                if a.check:
                    print(f"{rel} should not exist with these settings; run scripts/bb-convert.py")
                    drift = True
                else:
                    os.remove(p)
                    print(f"removed {rel}")
            continue
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
