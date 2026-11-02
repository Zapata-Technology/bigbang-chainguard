"""Shared code for the images this repo builds FROM the Chainguard org's images.

Each builder (bb-grafana-plugins.py, bb-base.py) converts one of Big Bang's own
Iron Bank images: it reads Iron Bank's recipe (hardening_manifest.yaml and
Dockerfile, pinned to a repo1 commit), pins the org images it builds FROM by
digest, and writes builds/<name>/build.yaml. The common command line then builds,
smoke-tests and pushes the standard and FIPS images. The Go builders
(bb-mattermost-operator.py, bb-tempo-query.py) share the go_* helpers: the source
tarball, the org's build and runtime images, and the binary's build info.
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request

import yaml

from bbcommon import ROOT, build_registry, ironbank_refs, load_mapping
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


# --- Go programs built from upstream's source (bb-mattermost-operator.py, bb-tempo-query.py) ---
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
# What each FIPS build records in the binary's build info.
GO_FIPS_BUILDS = {
    "toolchain": [r"^build\tCGO_ENABLED=1$",
                  r"^build\tGOEXPERIMENT=\S*(systemcrypto|opensslcrypto|boringcrypto|strictfipsruntime)"],
    "native": [r"^build\tCGO_ENABLED=0$", r"^build\tGOFIPS140=\S+", r"^build\tDefaultGODEBUG=\S*fips140=(on|only)"],
}


def go_line(gomod):
    m = re.search(r"^go\s+(\d+\.\d+(?:\.\d+)?)\s*$", gomod, re.M)
    if not m:
        raise SystemExit("upstream's go.mod has no `go` line; update go_line()")
    return m.group(1)


def go_source(url):
    """A GitHub source tarball: {source, sha256, go (its go.mod's line), commit, time}.

    GitHub's archives are `git archive`: the commit id is the pax comment, and every
    entry carries the commit time (a reproducible stand-in for a build date)."""
    data = fetch(url)
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        members = t.getmembers()
        gomod = next((t.extractfile(m).read().decode() for m in members
                      if m.name.count("/") == 1 and m.name.endswith("/go.mod")), None)
        commit, when = t.pax_headers.get("comment", ""), members[0].mtime
    if gomod is None or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SystemExit(f"{url} has no top-level go.mod or commit id")
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


def go_build_problems(image, binary, variant, image_spec, go_get):
    """What is wrong with a Go image's binary: a raised module not at its version, a FIPS
    build that isn't one (image_spec["fips"]: toolchain or native), a standard build that
    isn't static, or a -fips runtime whose OpenSSL doesn't default to fips=yes."""
    info = build_info(file_from(image, binary))
    settings = "\n".join(info)
    print("  build info: " + "; ".join(i.replace("\t", " ") for i in info
                                        if i.startswith("build") or any(m.split("@")[0] + "\t" in i for m in go_get)))
    failed = [f"{m.split('@')[0]} is not {m.split('@')[1]}" for m in go_get
              if "dep\t{}\t{}".format(*m.split("@")) not in settings]
    if variant == "fips":
        mode = image_spec["fips"]
        missing = [rx for rx in GO_FIPS_BUILDS[mode] if not re.search(rx, settings, re.M)]
        if missing:
            failed.append(f"FIPS Go build ({mode}): no {', '.join(missing)}")
        cnf = file_from(image, "/etc/ssl/openssl.cnf").decode(errors="replace")
        if not re.search(r"^\s*(default_)?properties\s*=\s*fips=yes", cnf, re.M):
            failed.append("openssl fips=yes")
        else:
            print("  openssl: fips=yes")
    elif "build\tCGO_ENABLED=0" not in settings:
        failed.append("static build (CGO_ENABLED=0)")
    return failed


def bigbang_tag(ib_image):
    """The tag of an Iron Bank image Big Bang deploys (the newest, if packages differ)."""
    discovered = json.load(open(os.path.join(ROOT, "discovery", "images.json")))
    tags = {tag for info in discovered["packages"].values() for repo, tag in ironbank_refs(info)
            if repo == ib_image and tag}
    if not tags:
        raise SystemExit(f"Big Bang does not deploy {ib_image} (discovery/images.json)")
    return max(tags, key=lambda t: [int(x) for x in re.findall(r"\d+", t)])


def ironbank_recipe(project, ref):
    """(commit, hardening_manifest, Dockerfile text) of dsop/<project> at <ref>."""
    quoted = urllib.parse.quote(f"dsop/{project}", safe="")
    commit = json.loads(fetch(f"{REPO1}/api/v4/projects/{quoted}/repository/commits/{ref}"))["id"]
    raw = f"{REPO1}/dsop/{project}/-/raw/{commit}"
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
    with open(os.path.join(out, "build.yaml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# GENERATED by scripts/bb-{name}.py - do not edit by hand.\n" + header)
        yaml.safe_dump(spec, f, sort_keys=False, width=200)
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


def logs(container):
    return subprocess.run(["docker", "logs", "--tail", "50", container], capture_output=True, text=True).stdout


def main(name, doc, generate, build_args, smoke_test):
    """Command line shared by the builders.

    generate(args) -> spec          writes builds/<name>/build.yaml
    build_args(spec, variant) -> {} docker --build-arg values
    smoke_test(ref, variant, spec)  raises SystemExit on failure
    """
    ap = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org", help="Chainguard org (default: $CHAINGUARD_ORG, else the CHAINGUARD_ORG file)")
    ap.add_argument("--build-registry", help="push target (default: $BUILD_REGISTRY, else the BUILD_REGISTRY file)")
    ap.add_argument("--ref", default="master", help="Iron Bank repo branch or commit to read (default: master)")
    ap.add_argument("--version", help="build on this version line instead of Big Bang's (\"*\": newest), "
                                      "where the builder supports it")
    ap.add_argument("--force", action="store_true",
                    help="build even if the mapping doesn't use this image (upgrade tests)")
    ap.add_argument("--no-generate", action="store_true", help="build from the existing build.yaml")
    ap.add_argument("--build", action="store_true", help="docker build the standard and FIPS images")
    ap.add_argument("--test", action="store_true", help="smoke-test each built image (implies --build)")
    ap.add_argument("--push", action="store_true", help="push both images to BUILD_REGISTRY (implies --build)")
    ap.add_argument("--pushed-file", help="append each pushed image as <repo>@<digest> (for signing)")
    ap.add_argument("--built-file", help="append each built image reference (for scanning)")
    a = ap.parse_args()

    building = a.build or a.test or a.push
    org = org_name(a.org)
    spec_file = os.path.join(ROOT, "builds", name, "build.yaml")
    standalone = os.path.exists(spec_file) and (yaml.safe_load(open(spec_file)) or {}).get("standalone")
    # A standalone image (the installer) isn't a Big Bang image the mapping names.
    if building and not a.force and not standalone and not any(isinstance(v, dict) and v.get("build") == name
                                            for v in load_mapping(org)["images"].values()):
        print(f"skipped: the mapping for {org} doesn't use builds/{name} (mapping.yaml or orgs/{org}.yaml)")
        return 0
    out = os.path.join(ROOT, "builds", name)
    spec = yaml.safe_load(open(os.path.join(out, "build.yaml"))) if a.no_generate else generate(a)
    if not building:
        return 0
    registry = build_registry(a.build_registry)
    if a.push and not registry:
        raise SystemExit("--push needs BUILD_REGISTRY (a registry you can push to, e.g. ghcr.io/<owner>)")
    for variant, img in spec["image"].items():
        ref = f"{registry or 'bb-local'}/{img['name']}:{img['tag']}"
        print(f"== {ref}", flush=True)
        args = {f"{k.upper()}_IMAGE": img[k] for k in ("base", "dev") if k in img}
        args.update(build_args(spec, variant))
        cmd = ["docker", "build", "--pull", "-t", ref, "-f", os.path.join(ROOT, "builds", name, "Dockerfile")]
        for k, v in args.items():
            cmd += ["--build-arg", f"{k}={v}"]
        subprocess.run(cmd + [os.path.join(ROOT, "builds", spec.get("context", name))], check=True)
        if a.built_file:
            with open(a.built_file, "a") as f:
                f.write(ref + "\n")
        if a.test:
            smoke_test(ref, variant, spec)
        if a.push:
            subprocess.run(["docker", "push", ref], check=True)
            if a.pushed_file:
                digest = subprocess.run(["docker", "inspect", "--format", "{{index .RepoDigests 0}}", ref],
                                        capture_output=True, text=True, check=True).stdout.strip()
                with open(a.pushed_file, "a") as f:
                    f.write(digest + "\n")
    return 0


def run(name, doc, generate, build_args, smoke_test):
    sys.exit(main(name, doc, generate, build_args, smoke_test))
