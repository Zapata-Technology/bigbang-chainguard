#!/usr/bin/env python3
"""Run the whole conversion for your Chainguard org, start to finish.

  bb-run.py --org <org> [--build-registry ghcr.io/<owner>] [--save]
            [--skip-fetch] [--skip-discovery] [--build] [--strict-versions]

Steps:
  1. bb-fetch.py           clone Big Bang (BIGBANG_VERSION) and every package into work/
  2. bb-images.py          discover every image Big Bang deploys -> discovery/images.json
  3. bb-convert.py --missing
                           list the Chainguard repos your org lacks; stops here if any are
                           missing (add them, or list exceptions in orgs/<org>.yaml)
  4. bb-<image>.py         with a build registry: regenerate the specs for the images this
                           repo builds (with --build, also build and smoke-test them)
  5. bb-convert.py         write overlays/, flux/, flux-fips/, docs/
  6. bb-verify.py          render Big Bang with the overlays, standard and --fips

--save writes --org and --build-registry into the CHAINGUARD_ORG and BUILD_REGISTRY
files so later runs (and CI) use them without flags.
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bbcommon import ROOT, build_registry  # noqa: E402
from orgtags import org_name  # noqa: E402

BUILDERS = ["grafana-plugins", "base", "devops-tester", "vault", "installer"]   # installer reads base's


def step(title, *args, ok=(0,)):
    print(f"\n== {title}", flush=True)
    rc = subprocess.run([sys.executable, *args], cwd=ROOT).returncode
    if rc not in ok:
        raise SystemExit(f"{title} failed (exit {rc})")
    return rc


def save(name, value):
    path = os.path.join(ROOT, name)
    lines = [ln for ln in open(path, encoding="utf-8")] if os.path.exists(path) else []
    comments = [ln for ln in lines if ln.lstrip().startswith("#")]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(comments)
        f.write(value + "\n")
    print(f"saved {name}: {value}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org", help="Chainguard org (default: $CHAINGUARD_ORG, else the CHAINGUARD_ORG file)")
    ap.add_argument("--build-registry", help="where this repo's own images go (default: $BUILD_REGISTRY, else the "
                                             "BUILD_REGISTRY file; empty keeps them on Iron Bank)")
    ap.add_argument("--save", action="store_true", help="write --org / --build-registry into the settings files")
    ap.add_argument("--skip-fetch", action="store_true", help="reuse work/ from an earlier run")
    ap.add_argument("--skip-discovery", action="store_true", help="reuse discovery/images.json")
    ap.add_argument("--build", action="store_true", help="also docker build and smoke-test this repo's images")
    ap.add_argument("--strict-versions", action="store_true", help="never use a newer MINOR than Iron Bank's")
    a = ap.parse_args()

    org = org_name(a.org)
    registry = build_registry(a.build_registry)
    os.environ["CHAINGUARD_ORG"] = org
    if registry:
        os.environ["BUILD_REGISTRY"] = registry
    if a.save:
        save("CHAINGUARD_ORG", org)
        if registry:
            save("BUILD_REGISTRY", registry)
    print(f"org: cgr.dev/{org}\nbuild registry: {registry or '(none: those images stay on Iron Bank)'}")

    work = os.path.join(ROOT, "work")
    if not a.skip_fetch:
        step("fetch Big Bang and its packages", os.path.join(HERE, "bb-fetch.py"))
    if not a.skip_discovery:
        step("discover images", os.path.join(HERE, "bb-images.py"), "--bigbang", os.path.join(work, "bigbang"),
             "--pkgs", os.path.join(work, "pkgs"), "--values", os.path.join(ROOT, "discovery", "render-inputs.yaml"),
             "-o", os.path.join(ROOT, "discovery", "images.json"))
    if step("scan the charts (sh scripts that need bash, empty config defaults)", os.path.join(HERE, "bb-scan.py"),
            "--pkgs", os.path.join(work, "pkgs"), "--bigbang", os.path.join(work, "bigbang"), ok=(0, 1)):
        raise SystemExit("\nbb-scan.py found something new (listed above). Handle it and add it to mapping.yaml "
                         "scan_reviewed (README, \"escape hatches\"), then run again.")
    strict = ["--strict-versions"] if a.strict_versions else []
    if step("check the org has every repo", os.path.join(HERE, "bb-convert.py"), "--missing", *strict, ok=(0, 1)):
        raise SystemExit("\nThe org is missing repositories (listed above). Add them in the Chainguard console, or "
                         f"keep those images on Iron Bank with entries in orgs/{org}.yaml, then run again.")
    if registry:
        for b in BUILDERS:
            step(f"generate builds/{b}", os.path.join(HERE, f"bb-{b}.py"), *(["--build", "--test"] if a.build else []))
    step("write the overlays", os.path.join(HERE, "bb-convert.py"), *strict)
    for mode in ([], ["--fips"]):
        step("verify " + ("FIPS" if mode else "standard"), os.path.join(HERE, "bb-verify.py"),
             "--bigbang", os.path.join(work, "bigbang"), "--pkgs", os.path.join(work, "pkgs"), *mode)
    print("\nDone. Deploy with the README's helm command"
          + (", after pushing this repo's images (bb-<image>.py --build --test --push)." if registry else "."))


if __name__ == "__main__":
    main()
