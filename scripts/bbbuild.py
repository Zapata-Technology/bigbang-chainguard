"""Shared code for the images this repo builds FROM the Chainguard org's images.

scripts/bb-build.py <name> drives every build from builds/<name>/recipe.yaml (hand-written:
what the build is) and the recipe's hook module, scripts/buildhooks/<hook>.py (the Python a
build needs: reading Iron Bank's recipe, pinning org images, its smoke test). The hook's
generate() returns the spec bb-build.py writes as builds/<name>/build.yaml; this module has
the helpers the hooks share: fetching with retries, Iron Bank's recipe pinned to a commit,
org images pinned by digest, and the go_* helpers of the Go builds (the source tarball, the
org's build and runtime images, the binary's build info). Recipes fill in values with
`{{path}}` templates (fill()); recipe_build_args() and recipe_smoke_test() are the
declarative build arguments and smoke test a recipe can give instead of hook code.
"""
import glob
import hashlib
import importlib
import io
import json
import os
import re
import subprocess
import tarfile
import time
import urllib.parse
import urllib.request

import yaml

from bbcommon import ROOT, ironbank_refs, org_note
from orgtags import REGISTRY, OrgTags, org_name, resolve

REPO1 = "https://repo1.dso.mil"
GO_PROXY = "https://proxy.golang.org"


def fetch(url, attempts=4):
    """GET with retries: repo1.dso.mil intermittently refuses connections."""
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except OSError:
            if i == attempts - 1:
                raise
            time.sleep(10 * (i + 1))


def go_latest(module):
    """`module@<newest release>` from the Go module proxy (the builders raise modules past
    what upstream's go.mod pins when the pinned ones have fixable CVEs)."""
    return f"{module}@{json.loads(fetch(f'{GO_PROXY}/{module}/@latest'))['Version']}"


# --- Go programs built from upstream's source (the mattermost-operator, tempo-query, minio and
# gitaly-init-cgroups hooks) ---
#
# Org image pairs to build in and run on, in order of preference: the first the org has.
# The Go images carry the toolchain (go-fips: Chainguard's FIPS Go); kubectl -dev gets
# Wolfi's Go from apk. glibc-openssl(-fips) is a minimal runtime with the FIPS OpenSSL a
# cgo FIPS binary needs; kubectl(-fips), which every Big Bang org has, has the same libraries.
GO_BUILD_IMAGES = [{"standard": "go", "fips": "go-fips"}, {"standard": "kubectl", "fips": "kubectl-fips"}]
GO_RUNTIME_IMAGES = [{"standard": "glibc-openssl", "fips": "glibc-openssl-fips"},
                     {"standard": "kubectl", "fips": "kubectl-fips"}]
# Wolfi's Go, for a build image without it (the FIPS build then uses Go's own FIPS module).
GO_PACKAGE = "go"
# What each FIPS build records in the binary's build info: any one of the mode's sets.
# Chainguard's go-fips 1.26 uses an OpenSSL backend (GOEXPERIMENT=systemcrypto); go-fips 1.27
# builds against Go's own validated FIPS 140-3 module instead (GOFIPS140=v1.0.0-..., the
# fips140v1.0 tag, fips140=on), still with cgo. Both are FIPS builds. A native build also
# records CGO_ENABLED: 0 for a static program, 1 for one that needs cgo (fips_build_missing's
# cgo argument; the registry adapter).
GO_FIPS_BUILDS = {
    "toolchain": [[r"^build\tCGO_ENABLED=1$",
                   r"^build\tGOEXPERIMENT=\S*(systemcrypto|opensslcrypto|boringcrypto|strictfipsruntime)"],
                  [r"^build\tCGO_ENABLED=1$", r"^build\tGOFIPS140=v\d\S*",
                   r"^build\tDefaultGODEBUG=\S*fips140=(on|only)"]],
    "native": [[r"^build\tCGO_ENABLED=0$", r"^build\tGOFIPS140=\S+", r"^build\tDefaultGODEBUG=\S*fips140=(on|only)"]],
}


def fips_build_missing(mode, settings, cgo=False):
    """[] if the build info matches one of the mode's sets, else what the closest set lacks.
    cgo=True: a native build must be a cgo build (CGO_ENABLED=1) instead of a static one."""
    sets = [[r"^build\tCGO_ENABLED=1$" if cgo and rx == r"^build\tCGO_ENABLED=0$" else rx for rx in rules]
            for rules in GO_FIPS_BUILDS[mode]]
    gaps = [[rx for rx in rules if not re.search(rx, settings, re.M)] for rules in sets]
    return [] if any(not g for g in gaps) else min(gaps, key=len)


def go_line(gomod):
    m = re.search(r"^go\s+(\d+\.\d+(?:\.\d+)?)\s*$", gomod, re.M)
    if not m:
        raise SystemExit("upstream's go.mod has no `go` line; update go_line()")
    return m.group(1)


def go_source(url, gomod="go.mod"):
    """A GitHub source tarball: {source, sha256, go (the line of its go.mod, or of `gomod`
    for a module in a subdirectory), commit, time}.

    GitHub's archives are `git archive`: the commit id is the pax comment, and every
    entry carries the commit time (a reproducible stand-in for a build date)."""
    data = fetch(url)
    want = os.path.normpath(gomod).replace(os.sep, "/")
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        members = t.getmembers()
        gomod = next((t.extractfile(m).read().decode() for m in members
                      if m.name.split("/", 1)[-1] == want and "/" in m.name), None)
        commit, when = t.pax_headers.get("comment", ""), members[0].mtime
    if gomod is None or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SystemExit(f"{url} has no {want} or commit id")
    return {"source": url, "sha256": hashlib.sha256(data).hexdigest(), "go": go_line(gomod),
            "commit": commit, "time": when}


def first_pair(tags, pairs):
    """The first {variant: repo} pair whose repositories the org has (both variants)."""
    for pair in pairs:
        if all(tags.tags(r) for r in pair.values()):
            return pair
    raise SystemExit(f"the org has none of {[list(p.values()) for p in pairs]}: add one pair "
                     "(AGENTS.md, \"Add a repository to the org\")")


def go_images(org_arg, go_version):
    """Pin the org images a Go build uses: {variant: {dev, base, go_package[, fips]}} and the
    repositories chosen ({"build": pair, "run": pair}). With the org's go/go-fips images the
    build uses their toolchain on go.mod's line and FIPS is "toolchain"; otherwise Wolfi's Go
    in kubectl(-fips) -dev and FIPS is "native" (Go's own FIPS 140-3 module)."""
    tags = OrgTags(org_name(org_arg))
    build_repos, run_repos = first_pair(tags, GO_BUILD_IMAGES), first_pair(tags, GO_RUNTIME_IMAGES)
    has_go = build_repos["standard"] == "go"
    build = org_images(org_arg, build_repos, go_version if has_go else "*")
    run = org_images(org_arg, run_repos, "*", dev=False)
    images = {v: {"dev": build[v]["dev"], "base": run[v]["base"], "go_package": "" if has_go else GO_PACKAGE,
                  **({"fips": "toolchain" if has_go else "native"} if v == "fips" else {})}
              for v in ("standard", "fips")}
    return images, {"build": build_repos, "run": run_repos}


def file_from(image, path):
    """The bytes of `path` in `image` (no shell needed: docker create + cp)."""
    cid = subprocess.run(["docker", "create", image], capture_output=True, text=True, check=True).stdout.strip()
    try:
        out = subprocess.run(["docker", "cp", "-L", f"{cid}:{path}", "-"], capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(out)) as t:
            return t.extractfile(t.getmembers()[0]).read()
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


def build_info(binary):
    """The Go build info lines (path, dep, build) embedded in a binary."""
    return sorted({m.decode(errors="replace") for m in
                   re.findall(rb"(?:^|\n)((?:path|mod|dep|build)\t[^\n]+)", binary)})


def go_build_problems(image, binary, variant, image_spec, go_get, cgo=False):
    """What is wrong with a Go image's binary: a raised module not at its version, a FIPS
    build that isn't one (image_spec["fips"]: toolchain or native), a standard build that
    isn't static (with cgo=True: that isn't a cgo build), or a -fips runtime whose OpenSSL
    doesn't default to fips=yes."""
    info = build_info(file_from(image, binary))
    settings = "\n".join(info)
    print("  build info: " + "; ".join(i.replace("\t", " ") for i in info
                                        if i.startswith("build") or any(m.split("@")[0] + "\t" in i for m in go_get)))
    failed = [f"{m.split('@')[0]} is not {m.split('@')[1]}" for m in go_get
              if "dep\t{}\t{}".format(*m.split("@")) not in settings]
    if variant == "fips":
        mode = image_spec["fips"]
        missing = fips_build_missing(mode, settings, cgo)
        if missing:
            failed.append(f"FIPS Go build ({mode}): no {', '.join(missing)}")
        cnf = file_from(image, "/etc/ssl/openssl.cnf").decode(errors="replace")
        if not re.search(r"^\s*(default_)?properties\s*=\s*fips=yes", cnf, re.M):
            failed.append("openssl fips=yes")
        else:
            print("  openssl: fips=yes")
    elif f"build\tCGO_ENABLED={int(cgo)}" not in settings:
        failed.append("cgo build (CGO_ENABLED=1)" if cgo else "static build (CGO_ENABLED=0)")
    return failed


def bigbang_tag(ib_image):
    """The tag of an Iron Bank image Big Bang deploys (the newest, if packages differ)."""
    discovered = json.load(open(os.path.join(ROOT, "discovery", "images.json")))
    tags = {tag for info in discovered["packages"].values() for repo, tag in ironbank_refs(info)
            if repo == ib_image and tag}
    if not tags:
        raise SystemExit(f"Big Bang does not deploy {ib_image} (discovery/images.json)")
    return max(tags, key=lambda t: [int(x) for x in re.findall(r"\d+", t)])


def ironbank_recipe(project, ref, group="dsop"):
    """(commit, hardening_manifest, Dockerfile text) of <group>/<project> at <ref>. Iron Bank
    moves recipes it no longer maintains to iron-bank-archives/dsop-archives (MinIO's operator)."""
    quoted = urllib.parse.quote(f"{group}/{project}", safe="")
    commit = json.loads(fetch(f"{REPO1}/api/v4/projects/{quoted}/repository/commits/{ref}"))["id"]
    raw = f"{REPO1}/{group}/{project}/-/raw/{commit}"
    return (commit, yaml.safe_load(fetch(f"{raw}/hardening_manifest.yaml")),
            fetch(f"{raw}/Dockerfile").decode())


def org_images(org_arg, repos, version, dev=True):
    """Pin the org's runtime and -dev images for `version`.

    repos: {variant: org repo}. Returns {variant: {"tag", "base", "dev"}} ("dev" only
    with dev=True); each variant resolves on its own (standard and -fips repos can be
    a patch apart).
    """
    org = org_name(org_arg)
    tags = OrgTags(org)
    out = {}
    for variant, repo in repos.items():
        available = tags.tags(repo) or []
        t, note = resolve(version, available)
        if not t:
            raise SystemExit(f"{REGISTRY}/{org}/{repo}: {note}")
        if note:
            print(f"{repo}: {note}")
        out[variant] = {"tag": t, "base": f"{REGISTRY}/{org}/{repo}:{t}@{tags.digest(repo, t)}"}
        if dev:
            if f"{t}-dev" not in available:
                raise SystemExit(f"{REGISTRY}/{org}/{repo} has no {t}-dev tag to build in")
            out[variant]["dev"] = f"{REGISTRY}/{org}/{repo}:{t}-dev@{tags.digest(repo, t + '-dev')}"
    return out


def write_spec(name, spec, header):
    out = os.path.join(ROOT, "builds", name)
    os.makedirs(out, exist_ok=True)
    body = yaml.safe_dump(spec, sort_keys=False, width=200)
    org = re.search(r"cgr\.dev/[\w.-]+", body)   # the org whose images it builds FROM
    note = org_note(org.group(0)) + "\n" if org else ""
    with open(os.path.join(out, "build.yaml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# GENERATED by scripts/bb-build.py {name} - do not edit by hand.\n" + note + header + body)
    print(f"wrote builds/{name}/build.yaml")


def run_checks(image, script, checks, shell="bash", docker_args=()):
    """Run a script in the image (with `shell`, after `docker run` options `docker_args`) and
    require each regex in `checks` ({label: regex}) to match."""
    p = subprocess.run(["docker", "run", "--rm", "--entrypoint", shell, *docker_args, image, "-c", "set -e; " + script],
                       capture_output=True, text=True)
    out = p.stdout + p.stderr
    print("  " + out.strip().replace("\n", "\n  "))
    failed = [label for label, rx in checks.items() if not re.search(rx, out, re.M)]
    if p.returncode:
        failed.insert(0, f"script exited {p.returncode}")
    if failed:
        raise SystemExit(f"{image}: failed {', '.join(failed)}")


def require_openssl_fips(container, image):
    """A running container's OpenSSL must default to fips=yes. The runtime image may have no
    shell: copy the file out (docker cp - writes a tar stream)."""
    cnf = subprocess.run(["docker", "cp", "-L", f"{container}:/etc/ssl/openssl.cnf", "-"],
                         capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(cnf.stdout)) as t:
        text = t.extractfile(t.getmembers()[0]).read().decode(errors="replace")
    if not re.search(r"^\s*(default_)?properties\s*=\s*fips=yes", text, re.M):
        raise SystemExit(f"{image}: OpenSSL does not default to fips=yes")
    print("  openssl: fips=yes")


def logs(container):
    return subprocess.run(["docker", "logs", "--tail", "50", container], capture_output=True, text=True).stdout


# --- recipes: builds/<name>/recipe.yaml ---

# `{{path}}` in a recipe string: a dotted path into the data it is filled from (the spec,
# or the recipe itself), `$variant` standing for the variant being built (standard or fips),
# a trailing `?` for "empty if missing", and `|filter`s. A list fills in space-separated.
TEMPLATE = re.compile(r"\{\{\s*([\w.$-]+)(\?)?((?:\|\w+)*)\s*\}\}")
FILTERS = {"line": lambda v: ".".join(str(v).split(".")[:2]),   # MAJOR.MINOR of a version
           "rx": re.escape}                                     # literal inside a regex
# Every recipe has these; the rest belong to its hook (or to `build_args` / `smoke`).
RECIPE_REQUIRED = ["description", "hook", "image", "header"]
# Appended to every declarative smoke test: for -fips, OpenSSL must default to fips=yes.
FIPS_OPENSSL = "grep -hE '^ *(default_)?properties *= *fips=yes' /etc/ssl/openssl.cnf || true"


def recipe_path(name):
    return os.path.join(ROOT, "builds", name, "recipe.yaml")


def load_recipe(name):
    """builds/<name>/recipe.yaml with its `name`; fails on a missing build or required key."""
    if not os.path.exists(recipe_path(name)):
        raise SystemExit(f"no builds/{name}/recipe.yaml (bb-build.py --list names the builds)")
    r = yaml.safe_load(open(recipe_path(name), encoding="utf-8")) or {}
    missing = [k for k in RECIPE_REQUIRED if k not in r]
    if missing:
        raise SystemExit(f"builds/{name}/recipe.yaml has no {', '.join(missing)}")
    return {"name": name, **r}


def recipe_names():
    """Every build with a recipe, in the order to generate them: a recipe's `after` first
    (the installer reads base's build.yaml)."""
    names = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(recipe_path("*")))
    ordered = []

    def visit(name, path=()):
        if name in path:
            raise SystemExit(f"builds/{name}/recipe.yaml: `after` loops through {' -> '.join(path)}")
        if name not in ordered:
            for first in load_recipe(name).get("after", []):
                visit(first, path + (name,))
            ordered.append(name)
    for name in names:
        visit(name)
    return ordered


def load_hook(r):
    """The recipe's hook module, scripts/buildhooks/<hook>.py."""
    return importlib.import_module(f"buildhooks.{r['hook']}")


def lookup(data, path, variant=None, optional=False):
    try:
        for key in path.split("."):
            data = data[variant if key == "$variant" else key]
        return data
    except (KeyError, TypeError):
        if optional:
            return ""
        raise SystemExit(f"recipe template: no {path!r}")


def fill(text, data, variant=None):
    """`text` with each {{path}} replaced from `data` (see TEMPLATE)."""
    def value(m):
        v = lookup(data, m.group(1), variant, optional=bool(m.group(2)))
        for f in filter(None, m.group(3).split("|")):
            v = FILTERS[f](v)
        return " ".join(str(x) for x in v) if isinstance(v, list) else str(v)
    return TEMPLATE.sub(value, text)


def image_name(r, variant):
    return r["image"] + ("-fips" if variant == "fips" else "")


def header(r):
    """The comment lines under build.yaml's GENERATED line (recipe `header`, filled from the recipe)."""
    return "".join(f"# {fill(line, r)}\n" for line in r["header"].splitlines())


def recipe_build_args(r, spec, variant):
    """The recipe's `build_args` ({ARG: template}), filled from the spec."""
    return {k: fill(str(v), spec, variant) for k, v in (r.get("build_args") or {}).items()}


def recipe_smoke_test(r, image, variant, spec):
    """The recipe's `smoke`: `script` (lines run in the image with `shell`, default bash, after
    `docker_args`) and `checks` ({label: regex} the output must match), filled from the spec.
    For -fips OpenSSL must also default to fips=yes."""
    s = r.get("smoke")
    if not s:
        raise SystemExit(f"builds/{r['name']}: neither recipe.yaml `smoke` nor a smoke_test in its hook")
    script = "; ".join([fill(line, spec, variant) for line in s["script"]] + [FIPS_OPENSSL])
    checks = {fill(k, spec, variant): fill(v, spec, variant) for k, v in s["checks"].items()}
    if variant == "fips":
        checks["openssl fips=yes"] = r"properties *= *fips=yes"
    run_checks(image, script, checks, shell=s.get("shell", "bash"), docker_args=s.get("docker_args", []))
