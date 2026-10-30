#!/usr/bin/env python3
"""Scan Big Bang's package charts for what breaks when Iron Bank's UBI images become
Chainguard's, beyond image names:

  shell   shell code run by sh that needs bash (arrays, here-strings, ...). UBI's sh is
          bash; a Chainguard -dev image's sh is busybox, which stops at the first line.
  renderers  a Big Bang HelmRelease template that writes a package's postRenderers
          without the `postRenderers:` key (Keycloak in 3.35.1): any post-renderer the
          overlays give that package (digest pins, bash fixes) breaks its HelmRelease
  nulls   chart defaults with no value inside a map the chart writes out with toYaml
          (Tempo's `opencensus:` receiver). Helm 3 drops them; Flux's render kept it,
          and the application refused the config.

  bb-scan.py --pkgs <dir> [--bigbang <checkout>] [--mapping mapping.yaml]

<dir> is bb-fetch.py's work/pkgs. Every finding must be listed in mapping.yaml's
`scan_reviewed` with how it is handled (bash_wait_jobs, bash_commands, overrides, or
an image this repo builds); exit 1 on any finding not listed, so a new Big Bang release
gets the same review.
"""
import argparse
import os
import re
import sys
import tarfile

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASHISMS = [
    (r"^\s*\w+=\(", "array assignment"),
    (r"\$\{#?\w+\[", "array index"),
    (r"<<<", "here-string"),
    (r"(?<![<>$])[<>]\((?!\))", "process substitution"),
    (r"\b(declare|typeset|mapfile|readarray)\s", "declare/mapfile"),
    (r"\blocal\s+-[aA]\b", "local array"),
    (r"\{\d+\.\.\d+\}", "brace range"),
]
SH_SHEBANG = re.compile(r"#!\s*(/bin/sh|/usr/bin/env\s+sh)\s*$")
SH_COMMAND = re.compile(r"""(["'\[\s-])(/bin/)?sh["']?\s*(,|\n|\])""")


def shell_findings(name, text):
    lines = text.replace("\r", "").split("\n")
    if not (SH_SHEBANG.match(lines[0]) or (not name.endswith(".sh") and SH_COMMAND.search(text))):
        return []
    hits = []
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        for rx, what in BASHISMS:
            if re.search(rx, line):
                hits.append(what)
    return [f"{name}: sh runs bash ({', '.join(sorted(set(hits)))})"] if hits else []


def renderer_findings(name, text):
    """A HelmRelease template that renders .postRenderers but never writes the key."""
    if name.endswith("helmrelease.yaml") and ".postRenderers" in text and "postRenderers:" not in text:
        return [f"{name}: postRenderers written without their key (no_post_renderers)"]
    return []


def nulls(d, path=()):
    if isinstance(d, dict):
        for k, v in d.items():
            p = path + (str(k),)
            if v is None:
                yield p
            else:
                yield from nulls(v, p)


def null_findings(chart, values_text, templates):
    try:
        values = yaml.safe_load(values_text) or {}
    except yaml.YAMLError:
        return []
    text = "\n".join(templates) + "\n" + values_text   # some charts template from a values string
    out = []
    for p in nulls(values):
        parent = re.escape(".".join(p[:-1]))
        if not parent:
            continue
        if (re.search(r"toYaml\s+\(?\s*\$?\.Values\." + parent + r"\s*[\)|}]", text)
                or re.search(r"with\s+\$?\.Values\." + parent + r"\s*-?}}[\s\S]{0,200}?toYaml\s+\.\s*[\)|}]", text)):
            out.append(f"{chart}: empty default {'.'.join(p)} written with toYaml")
    return out


def scan(root, prefix=""):
    found = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d != ".git"]
        rel = os.path.relpath(dp, root).replace(os.sep, "/")
        rel = prefix + ("" if rel == "." else rel + "/")
        if "Chart.yaml" in fn and "values.yaml" in fn:
            templates = []
            for tp, _, tf in os.walk(os.path.join(dp, "templates")):
                templates += [open(os.path.join(tp, f), encoding="utf-8", errors="replace").read() for f in tf]
            found += null_findings(rel.rstrip("/"), open(os.path.join(dp, "values.yaml"), encoding="utf-8",
                                                         errors="replace").read(), templates)
        for f in fn:
            p = os.path.join(dp, f)
            if f.endswith(".tgz"):
                found += scan_archive(p, rel + f)
            elif re.search(r"\.(sh|yaml|tpl)$", f):
                text = open(p, encoding="utf-8", errors="replace").read()
                found += shell_findings(rel + f, text) + renderer_findings(rel + f, text)
    return found


def scan_archive(path, name):
    """A packaged subchart: its own values and templates (not its nested subcharts)."""
    found, charts = [], {}
    try:
        t = tarfile.open(path)
    except (tarfile.TarError, OSError):
        return found
    with t:
        for m in t.getmembers():
            parts = m.name.split("/")
            if not m.isfile() or len(parts) < 2 or "charts" in parts[1:-1]:
                continue
            text = t.extractfile(m).read().decode("utf-8", "replace")
            c = charts.setdefault(parts[0], {"values": None, "templates": []})
            if parts[1:] == ["values.yaml"]:
                c["values"] = text
            elif parts[1] == "templates":
                c["templates"].append(text)
            if re.search(r"\.(sh|yaml|tpl)$", m.name):
                found += shell_findings(f"{name}!{m.name}", text)
    for chart, c in charts.items():
        if c["values"]:
            found += null_findings(f"{name}!{chart}", c["values"], c["templates"])
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pkgs", required=True)
    ap.add_argument("--bigbang")
    ap.add_argument("--mapping", default=os.path.join(ROOT, "mapping.yaml"))
    a = ap.parse_args()
    found = scan(a.pkgs)
    if a.bigbang:
        found += scan(os.path.join(a.bigbang, "chart"), "bigbang/")
    reviewed = (yaml.safe_load(open(a.mapping, encoding="utf-8")) or {}).get("scan_reviewed", {}) or {}
    new = sorted(f for f in set(found) if f.split(": ", 1)[0] not in reviewed)
    stale = sorted(k for k in reviewed if not any(f.split(": ", 1)[0] == k for f in found))
    for f in sorted(set(found)):
        key = f.split(": ", 1)[0]
        print(("ok    " if key in reviewed else "NEW   ") + f + (f"  -> {reviewed[key]}" if key in reviewed else ""))
    for k in stale:
        print(f"gone  {k} (no longer found; remove it from scan_reviewed)")
    if new:
        print(f"\n{len(new)} finding(s) not in mapping.yaml scan_reviewed: handle each (bash_wait_jobs, "
              "bash_commands, overrides) and list it there.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
