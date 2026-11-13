#!/usr/bin/env python3
"""Generate, build, smoke-test and push the images this repo builds FROM the org's images.

Each build is a directory builds/<name>/ with a Dockerfile, a hand-written recipe.yaml and
the generated build.yaml. The recipe says what the build is; its hook module
(scripts/buildhooks/<hook>.py) has the Python the build needs. This script:

  1. reads builds/<name>/recipe.yaml and imports its hook
  2. calls the hook's generate(): it reads what the build follows (Iron Bank's recipe pinned
     to a commit, the tag Big Bang deploys, upstream's source) and pins the org images the
     build is FROM by digest
  3. writes builds/<name>/build.yaml (GENERATED header, then the recipe's `header` lines)
  4. with --build, docker builds the standard and FIPS images from builds/<name>/Dockerfile,
     passing BASE_IMAGE / DEV_IMAGE and the build arguments (the hook's build_args(), else
     the recipe's `build_args`); --test runs the smoke test (the hook's smoke_test(), else
     the recipe's `smoke`); --push pushes both to BUILD_REGISTRY (never cgr.dev)

  bb-build.py <name> [--org <org>] [--ref master] [--version V]   regenerate builds/<name>/build.yaml
  bb-build.py <name> --build [--test] [--push]                     also build both images
  bb-build.py <name> --release --build [...]                       build builds/<name>/release.yaml
  bb-build.py --all [...]                                           every build (a recipe's `after` first)
  bb-build.py --list                                                the builds, one per line

recipe.yaml keys every build has (the hook reads the rest; see its docstring):

  description   one line: what the image is
  hook          the module in scripts/buildhooks/ (two builds can share one: minio)
  image         the image name; the FIPS image is <image>-fips
  header        build.yaml's comment lines under the GENERATED line ({{path}} from the recipe)
  after         builds to generate first (optional)
  build_args    {ARG: template}, filled from build.yaml ({{path}}, {{image.$variant.x}}, a `?`
                for empty when missing), when the hook has no build_args()
  smoke         {script: [lines], checks: {label: regex}, shell, docker_args}, filled from
                build.yaml, when the hook has no smoke_test(); -fips also checks OpenSSL fips=yes

builds/<name>/release.yaml (build.yaml's schema) is the build pinned for a newer Big Bang
release, which the nightly update's release track holds there (scripts/bb-held-builds.py)
until builds.yml has published it from main. --release builds it (never generates it), and
does nothing when it is absent or has build.yaml's tags.

A new build is a builds/<name>/ directory with a Dockerfile and a recipe.yaml, and a hook
module when no existing one fits; then add its name to builds.yml's matrix and update.yml's
builder loops (tests/test_recipes.py checks they list every recipe).
"""
import argparse
import os
import subprocess
import sys
import traceback

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bbbuild  # noqa: E402
from bbcommon import ROOT, build_registry, load_mapping  # noqa: E402
from orgtags import org_name  # noqa: E402


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?", help="the build: builds/<name>/recipe.yaml")
    ap.add_argument("--all", action="store_true", help="every build, in order; carries on past a failure")
    ap.add_argument("--list", action="store_true", help="print the builds' names and exit")
    ap.add_argument("--org", help="Chainguard org (default: $CHAINGUARD_ORG, else the CHAINGUARD_ORG file)")
    ap.add_argument("--build-registry", help="push target (default: $BUILD_REGISTRY, else the BUILD_REGISTRY file)")
    ap.add_argument("--ref", default="master", help="Iron Bank repo branch or commit to read (default: master)")
    ap.add_argument("--version", help="build on this version line instead of Big Bang's (\"*\": newest), "
                                      "where the build supports it")
    ap.add_argument("--force", action="store_true",
                    help="build even if the mapping doesn't use this image (upgrade tests)")
    ap.add_argument("--no-generate", action="store_true", help="build from the existing build.yaml")
    ap.add_argument("--release", action="store_true",
                    help="build from builds/<name>/release.yaml (implies --no-generate); nothing when it "
                         "is absent or has build.yaml's tags")
    ap.add_argument("--build", action="store_true", help="docker build the standard and FIPS images")
    ap.add_argument("--test", action="store_true", help="smoke-test each built image (implies --build)")
    ap.add_argument("--push", action="store_true", help="push both images to BUILD_REGISTRY (implies --build)")
    ap.add_argument("--pushed-file", help="append each pushed image as <repo>@<digest> (for signing)")
    ap.add_argument("--built-file", help="append each built image reference (for scanning)")
    a = ap.parse_args(argv)
    if not a.list and bool(a.name) == a.all:
        ap.error("name one build, or --all")
    return a


def generate(r, a):
    """Run the hook's generate() and write builds/<name>/build.yaml."""
    spec = bbbuild.load_hook(r).generate(r, a)
    bbbuild.write_spec(r["name"], spec, bbbuild.header(r))
    return spec


def build_args(r, hook, spec, variant):
    if hasattr(hook, "build_args"):
        return hook.build_args(spec, variant, r)
    return bbbuild.recipe_build_args(r, spec, variant)


def smoke_test(r, hook, ref, variant, spec):
    if hasattr(hook, "smoke_test"):
        return hook.smoke_test(ref, variant, spec, r)
    return bbbuild.recipe_smoke_test(r, ref, variant, spec)


def tags(spec):
    """{variant: (image name, tag)}: what a build.yaml spec publishes."""
    return {v: (img.get("name"), img.get("tag")) for v, img in (spec or {}).get("image", {}).items()}


def release_spec(out):
    """builds/<name>/release.yaml's spec when it publishes tags build.yaml doesn't, else None."""
    path = os.path.join(out, "release.yaml")
    if not os.path.exists(path):
        return None
    spec = yaml.safe_load(open(path, encoding="utf-8"))
    current = os.path.join(out, "build.yaml")
    current = yaml.safe_load(open(current, encoding="utf-8")) if os.path.exists(current) else None
    return spec if tags(spec) != tags(current) else None


def run_build(name, a):
    r = bbbuild.load_recipe(name)
    hook = bbbuild.load_hook(r)
    building = a.build or a.test or a.push
    org = org_name(a.org)
    out = os.path.join(ROOT, "builds", name)
    spec_file, no_generate = os.path.join(out, "build.yaml"), a.no_generate
    if a.release:
        if not release_spec(out):
            print(f"skipped: no builds/{name}/release.yaml with tags builds/{name}/build.yaml lacks")
            return
        spec_file, no_generate = os.path.join(out, "release.yaml"), True
    standalone = os.path.exists(spec_file) and (yaml.safe_load(open(spec_file)) or {}).get("standalone")
    # A standalone image (the installer) isn't a Big Bang image the mapping names.
    if building and not a.force and not standalone and not any(isinstance(v, dict) and v.get("build") == name
                                                               for v in load_mapping(org)["images"].values()):
        print(f"skipped: the mapping for {org} doesn't use builds/{name} (mapping.yaml or orgs/{org}.yaml)")
        return
    spec = yaml.safe_load(open(spec_file)) if no_generate else generate(r, a)
    if not building:
        return
    registry = build_registry(a.build_registry)
    if a.push and not registry:
        raise SystemExit("--push needs BUILD_REGISTRY (a registry you can push to, e.g. ghcr.io/<owner>)")
    for variant, img in spec["image"].items():
        ref = f"{registry or 'bb-local'}/{img['name']}:{img['tag']}"
        print(f"== {ref}", flush=True)
        args = {f"{k.upper()}_IMAGE": img[k] for k in ("base", "dev") if k in img}
        args.update(build_args(r, hook, spec, variant))
        # Two builds can share one Dockerfile (spec "dockerfile": the other build's directory).
        cmd = ["docker", "build", "--pull", "-t", ref, "-f",
               os.path.join(ROOT, "builds", spec.get("dockerfile", name), "Dockerfile")]
        if spec.get("target"):   # the stage to build, when the Dockerfile has one per image
            cmd += ["--target", spec["target"]]
        for k, v in args.items():
            cmd += ["--build-arg", f"{k}={v}"]
        subprocess.run(cmd + [os.path.join(ROOT, "builds", spec.get("context", name))], check=True)
        if a.built_file:
            with open(a.built_file, "a") as f:
                f.write(ref + "\n")
        if a.test:
            smoke_test(r, hook, ref, variant, spec)
        if a.push:
            subprocess.run(["docker", "push", ref], check=True)
            if a.pushed_file:
                digest = subprocess.run(["docker", "inspect", "--format", "{{index .RepoDigests 0}}", ref],
                                        capture_output=True, text=True, check=True).stdout.strip()
                with open(a.pushed_file, "a") as f:
                    f.write(digest + "\n")


def run_all(a):
    """Every build in order; one that fails doesn't stop the rest, but fails the run."""
    failed = []
    for name in bbbuild.recipe_names():
        print(f"\n== builds/{name}", flush=True)
        try:
            run_build(name, a)
        except SystemExit as e:
            if e.code not in (None, 0):
                print(e.code if isinstance(e.code, str) else f"exit {e.code}", file=sys.stderr, flush=True)
                failed.append(name)
        except Exception:   # a network or docker error in one build: report it, go on
            traceback.print_exc()
            failed.append(name)
    if failed:
        raise SystemExit(f"\nfailed: {' '.join(failed)}")


def main(argv=None):
    a = parse_args(argv)
    if a.list:
        print("\n".join(bbbuild.recipe_names()))
    elif a.all:
        run_all(a)
    else:
        run_build(a.name, a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
