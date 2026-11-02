#!/usr/bin/env python3
"""Build Big Bang's mattermost-operator image from upstream's source, FROM the org.

Chainguard publishes no mattermost-operator image. Iron Bank's recipe copies
/mattermost-operator out of upstream's image (mattermost/mattermost-operator:<tag>)
onto distroless static, run as nonroot (65532). This builds the same binary from the
same upstream tag instead:

  1. read the Iron Bank tag Big Bang deploys (discovery/images.json)
  2. read Iron Bank's hardening_manifest.yaml (the upstream image it copies) and
     Dockerfile (the binary path, entrypoint and user) from repo1.dso.mil, pinned to
     a commit
  3. download upstream's source for that tag from GitHub (pinned by sha256) and read
     its go.mod (the Go line) and the tag's commit and time (upstream's version ldflags)
  4. pin the org's build image (go / go-fips -dev on go.mod's line, else kubectl /
     kubectl-fips -dev plus Wolfi's Go) and runtime image (glibc-openssl /
     glibc-openssl-fips, else kubectl / kubectl-fips) by digest, the first pair the
     org has (BUILD_IMAGES, RUNTIME_IMAGES)
  5. raise GO_BUMPS to their newest release: upstream's go.mod pins x/net v0.58.0,
     which carries a Critical CVE (GO-2026-6612)
  6. write builds/mattermost-operator/build.yaml

The standard binary is static (CGO_ENABLED=0), like upstream's. The FIPS binary:

  toolchain  the org has go-fips: built the way upstream's Dockerfile.fips builds it
             (CGO_ENABLED=1, -tags=requirefips) with Chainguard's FIPS Go, so its crypto
             goes through the runtime image's FIPS OpenSSL
  native     it doesn't (kubectl-fips -dev offers no FIPS Go package, only go-1.N): built
             with Wolfi's Go against Go's own FIPS 140-3 module (GOFIPS140), which also
             makes GODEBUG fips140=on the binary's default

  bb-mattermost-operator.py [--org <org>] [--ref master]       regenerate build.yaml
  bb-mattermost-operator.py --build [--test] [--push]          also build both images

--test runs each image: --help lists the operator's flags; started with no cluster it
logs its Go version and stops at the missing kubeconfig (so it initialised, and for
-fips its FIPS crypto loaded); it runs as 65532; the binary's build info shows the
raised modules and, for -fips, the FIPS build (toolchain: cgo with a FIPS crypto
GOEXPERIMENT; native: GOFIPS140 with fips140=on by default); and for -fips that OpenSSL
defaults to fips=yes.
"""
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bbbuild  # noqa: E402
from orgtags import OrgTags, org_name  # noqa: E402

NAME = "mattermost-operator"
IB_IMAGE = "ironbank/opensource/mattermost/mattermost-operator"
IB_PROJECT = "opensource/mattermost/mattermost-operator"
IMAGE = "bigbang-mattermost-operator"
UPSTREAM = "mattermost/mattermost-operator"
SOURCE = "https://codeload.github.com/" + UPSTREAM + "/tar.gz/refs/tags/{tag}"
# Modules raised past what upstream's go.mod pins: x/net v0.58.0 has GO-2026-6612 (Critical).
GO_BUMPS = ["golang.org/x/net"]
# Org image pairs to build in and run on, in order of preference: the first the org has.
# The Go images carry the toolchain (go-fips: Chainguard's FIPS Go); kubectl -dev gets
# Wolfi's Go from apk. glibc-openssl(-fips) is a minimal runtime with the FIPS OpenSSL a
# cgo FIPS binary needs; kubectl(-fips), which every Big Bang org has, has the same libraries.
BUILD_IMAGES = [{"standard": "go", "fips": "go-fips"}, {"standard": "kubectl", "fips": "kubectl-fips"}]
RUNTIME_IMAGES = [{"standard": "glibc-openssl", "fips": "glibc-openssl-fips"},
                  {"standard": "kubectl", "fips": "kubectl-fips"}]
# Wolfi's Go, for a build image without it (the FIPS build then uses Go's own FIPS module).
GO_PACKAGE = "go"
# What each FIPS build records in the binary's build info.
FIPS_BUILDS = {
    "toolchain": [r"^build\tCGO_ENABLED=1$",
                  r"^build\tGOEXPERIMENT=\S*(systemcrypto|opensslcrypto|boringcrypto|strictfipsruntime)"],
    "native": [r"^build\tCGO_ENABLED=0$", r"^build\tGOFIPS140=\S+", r"^build\tDefaultGODEBUG=\S*fips140=(on|only)"],
}


def recipe(manifest, dockerfile):
    """The upstream tag Iron Bank copies, the binary path and the user it runs as."""
    upstream = next((r["tag"].split(":", 1)[1] for r in manifest.get("resources", [])
                     if r.get("tag", "").startswith(UPSTREAM + ":") and "@sha256:" in r.get("url", "")
                     and not r["tag"].endswith((".arm64", ".amd64"))), None)
    copy = re.search(r"^COPY\s+--from=\w+\s+(\S+)\s+(\S+)", dockerfile, re.M)
    entry = re.search(r"^ENTRYPOINT\s+\[\s*\"([^\"]+)\"", dockerfile, re.M)
    user = re.search(r"^USER\s+(\S+)", dockerfile, re.M)
    if not upstream:
        raise SystemExit(f"Iron Bank's manifest no longer copies {UPSTREAM}:<tag>; update recipe()")
    if not (copy and entry and user) or copy.group(2) != entry.group(1):
        raise SystemExit("Iron Bank's Dockerfile changed shape (COPY --from / ENTRYPOINT / USER); update recipe()")
    return {"upstream": upstream, "binary": entry.group(1), "user": user.group(1)}


def go_line(gomod):
    m = re.search(r"^go\s+(\d+\.\d+(?:\.\d+)?)\s*$", gomod, re.M)
    if not m:
        raise SystemExit("upstream's go.mod has no `go` line; update go_line()")
    return m.group(1)


def source(tag):
    """Upstream's source tarball for `tag`: url, sha256, its go.mod's Go line, the commit."""
    url = SOURCE.format(tag=tag)
    data = bbbuild.fetch(url)
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        members = t.getmembers()
        gomod = next((t.extractfile(m).read().decode() for m in members
                      if m.name.count("/") == 1 and m.name.endswith("/go.mod")), None)
        # GitHub's archives are `git archive`: the commit id is the pax comment, and every
        # entry carries the commit time.
        commit, when = t.pax_headers.get("comment", ""), members[0].mtime
    if gomod is None or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SystemExit(f"{url} has no top-level go.mod or commit id")
    return {"tag": tag, "source": url, "sha256": hashlib.sha256(data).hexdigest(), "go": go_line(gomod),
            "commit": commit,
            # upstream's Makefile stamps `date -u +%Y%m%d.%H%M%S`; the commit time keeps it reproducible
            "build_time": time.strftime("%Y%m%d.%H%M%S", time.gmtime(when)),
            "go_get": [bbbuild.go_latest(m) for m in GO_BUMPS]}


def first_pair(tags, pairs):
    """The first {variant: repo} pair whose repositories the org has (both variants)."""
    for pair in pairs:
        if all(tags.tags(r) for r in pair.values()):
            return pair
    raise SystemExit(f"the org has none of {[list(p.values()) for p in pairs]}: add one pair "
                     "(AGENTS.md, \"Add a repository to the org\")")


def generate(a):
    bb_tag = bbbuild.bigbang_tag(IB_IMAGE)
    commit, manifest, dockerfile = bbbuild.ironbank_recipe(IB_PROJECT, a.ref)
    ib = recipe(manifest, dockerfile)
    if ib["upstream"] != bb_tag:
        print(f"Iron Bank's recipe at {commit[:12]} copies {ib['upstream']}; Big Bang deploys {bb_tag}: building {bb_tag}")
    src = source(bb_tag)
    tags = OrgTags(org_name(a.org))
    build_repos, run_repos = first_pair(tags, BUILD_IMAGES), first_pair(tags, RUNTIME_IMAGES)
    has_go = build_repos["standard"] == "go"
    build = bbbuild.org_images(a.org, build_repos, src["go"] if has_go else "*")
    run = bbbuild.org_images(a.org, run_repos, "*", dev=False)
    spec = {
        "ironbank": {"image": f"{IB_IMAGE}:{bb_tag}", "source": f"{bbbuild.REPO1}/dsop/{IB_PROJECT}",
                     "commit": commit, **ib},
        "image": {v: {"name": IMAGE + ("-fips" if v == "fips" else ""), "tag": bb_tag,
                      "dev": build[v]["dev"], "base": run[v]["base"],
                      "go_package": "" if has_go else GO_PACKAGE,
                      **({"fips": "toolchain" if has_go else "native"} if v == "fips" else {})}
                  for v in ("standard", "fips")},
        "operator": src,
        "user": "65532:65532",   # distroless's nonroot:nonroot, numeric so runAsNonRoot can check it
        "notes": [f"built from {UPSTREAM} {bb_tag}'s source (Chainguard has no mattermost-operator image) "
                  f"with `operator.go_get` raised; Iron Bank copies the binary out of upstream's image",
                  "image.fips.fips: toolchain = built like upstream's Dockerfile.fips (cgo, -tags=requirefips) "
                  "with the org's go-fips; native = the org has no go-fips image, so Wolfi's Go builds it against "
                  "Go's own FIPS 140-3 module (GOFIPS140, fips140=on by default)"],
    }
    bbbuild.write_spec(NAME, spec, f"# Big Bang's mattermost-operator built from {UPSTREAM}'s source FROM the\n"
                                   "# Chainguard org's images, following Iron Bank's recipe at `ironbank.commit`.\n")
    print(f"mattermost-operator {bb_tag} (FIPS: {spec['image']['fips']['fips']}): build in {'/'.join(build_repos.values())} -dev, run on "
          f"{'/'.join(run_repos.values())}, go.mod Go {src['go']}, raised {' '.join(src['go_get'])}")
    return spec


def build_args(spec, variant):
    o = spec["operator"]
    return {"OPERATOR_SOURCE": o["source"], "OPERATOR_SHA256": o["sha256"], "OPERATOR_COMMIT": o["commit"],
            "OPERATOR_BUILD_TIME": o["build_time"], "OPERATOR_GO_GET": " ".join(o["go_get"]),
            "GO_PACKAGE": spec["image"][variant].get("go_package", ""), "FIPS": spec["image"][variant].get("fips", ""),
            "BINARY": spec["ironbank"]["binary"], "RUN_USER": spec["user"]}


def docker(*args, check=False):
    # A timeout, so an operator that waits for a cluster instead of stopping fails the test.
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=300)


def file_from(image, path):
    """The bytes of `path` in `image` (no shell needed: docker create + cp)."""
    cid = docker("create", image, check=True).stdout.strip()
    try:
        out = subprocess.run(["docker", "cp", "-L", f"{cid}:{path}", "-"], capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(out)) as t:
            return t.extractfile(t.getmembers()[0]).read()
    finally:
        docker("rm", "-f", cid)


def build_info(binary):
    """The Go build info lines (path, dep, build) embedded in a binary."""
    return sorted({m.decode(errors="replace") for m in
                   re.findall(rb"(?:^|\n)((?:path|mod|dep|build)\t[^\n]+)", binary)})


def smoke_test(image, variant, spec):
    binary = spec["ironbank"]["binary"]
    failed = []
    helped = docker("run", "--rm", image, "--help")
    print("  --help: " + (helped.stdout + helped.stderr).strip().replace("\n", "\n  "))
    if helped.returncode or not re.search(r"-metrics-addr", helped.stdout + helped.stderr):
        failed.append(f"--help (exit {helped.returncode})")
    # No cluster: the operator logs its Go version, then GetConfigOrDie stops it. Reaching
    # the log means the binary initialised (a FIPS binary panics first if FIPS is unavailable).
    started = docker("run", "--rm", image)
    out = (started.stdout + started.stderr).strip()
    print("  start without a cluster: " + out[-600:].replace("\n", "\n  "))
    if not re.search(r"Go Version: go1\.", out):
        failed.append("start (no Go version logged)")
    if not re.search(r"kubeconfig|KUBERNETES_SERVICE_HOST|in-cluster", out, re.I):
        failed.append("start (did not stop at the missing kubeconfig)")
    user = docker("image", "inspect", "--format", "{{.Config.User}}", image).stdout.strip()
    print(f"  user: {user}")
    if user != spec["user"]:
        failed.append(f"user {user!r}, want {spec['user']}")
    info = build_info(file_from(image, binary))
    print("  build info: " + "; ".join(i.replace("\t", " ") for i in info
                                        if i.startswith("build") or any(m.split("@")[0] in i
                                                                        for m in spec["operator"]["go_get"])))
    for mod in spec["operator"]["go_get"]:
        name, version = mod.split("@")
        if f"dep\t{name}\t{version}" not in "\n".join(info):
            failed.append(f"{name} is not {version}")
    settings = "\n".join(info)
    if variant == "fips":
        mode = spec["image"]["fips"]["fips"]
        missing = [rx for rx in FIPS_BUILDS[mode] if not re.search(rx, settings, re.M)]
        if missing:
            failed.append(f"FIPS Go build ({mode}): no {', '.join(missing)}")
        cnf = file_from(image, "/etc/ssl/openssl.cnf").decode(errors="replace")
        if not re.search(r"^\s*(default_)?properties\s*=\s*fips=yes", cnf, re.M):
            failed.append("openssl fips=yes")
        else:
            print("  openssl: fips=yes")
    elif "build\tCGO_ENABLED=0" not in settings:
        failed.append("static build (CGO_ENABLED=0)")
    if failed:
        raise SystemExit(f"{image}: failed {', '.join(failed)}")


if __name__ == "__main__":
    bbbuild.run(NAME, __doc__, generate, build_args, smoke_test)
