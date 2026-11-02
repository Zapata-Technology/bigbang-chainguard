#!/usr/bin/env python3
"""Build Big Bang's NeuVector registry-adapter image from upstream's source, FROM the org.

Chainguard publishes no NeuVector registry-adapter image. The adapter is Harbor's
pluggable scanner for NeuVector: an HTTPS API (port 9443) that takes Harbor's scan
requests and has the NeuVector controller scan them over its internal gRPC. The NeuVector
chart deploys it with `cve.adapter.enabled` (off in Big Bang's defaults). Iron Bank's
recipe builds neuvector/registry-adapter with upstream's `make build` (go build, cgo:
NeuVector's shared system package needs it) and runs /usr/local/bin/adapter as `nobody` on
UBI minimal. This builds the same program at the tag Big Bang deploys:

  1. read the Iron Bank tag Big Bang deploys (discovery/images.json)
  2. read Iron Bank's hardening_manifest.yaml (its version) and Dockerfile (the binary
     path, entrypoint and user) from repo1.dso.mil, pinned to a commit
  3. download upstream's source for that tag from GitHub (pinned by sha256) and read its
     go.mod (the Go line) and the tag's commit
  4. pin the org's build and runtime images by digest (bbbuild.go_images, as
     bb-mattermost-operator.py does)
  5. raise GO_BUMPS to their newest release: v0.2.11's go.mod pins x/net v0.58.0, which
     carries a Critical CVE (GO-2026-6612), and go-jose v4.1.4 (a fixable High)
  6. write builds/registry-adapter/build.yaml

Both binaries are cgo builds (upstream's are), linked against the runtime image's glibc.
FIPS as in bb-mattermost-operator.py: "toolchain" with the org's go-fips (Chainguard's
FIPS Go, OpenSSL), else "native" (Wolfi's Go against Go's own FIPS 140-3 module,
GOFIPS140, GODEBUG fips140=on by default).

  bb-registry-adapter.py [--org <org>] [--ref master]       regenerate build.yaml
  bb-registry-adapter.py --build [--test] [--push]          also build both images

--test runs each image: -h lists the adapter's flags; started with no cluster it logs
START with the tag as its version and stops at the missing internal certificate (so it
initialised, and for -fips its FIPS crypto loaded); it runs as 65534; the binary's
build info shows a cgo build with the raised modules and, for -fips, the FIPS build; and
for -fips that OpenSSL defaults to fips=yes.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bbbuild  # noqa: E402

NAME = "registry-adapter"
IB_IMAGE = "ironbank/opensource/neuvector/registry-adapter"
IB_PROJECT = "opensource/neuvector/neuvector/registry-adapter"
IMAGE = "bigbang-registry-adapter"
UPSTREAM = "neuvector/registry-adapter"
SOURCE = "https://codeload.github.com/" + UPSTREAM + "/tar.gz/refs/tags/{tag}"
# Modules raised past what upstream's go.mod pins. v0.2.11: x/net v0.58.0 has GO-2026-6612
# (Critical); go-jose v4.1.4 a fixable High.
GO_BUMPS = ["golang.org/x/net", "github.com/go-jose/go-jose/v4"]
# Iron Bank runs as UBI's `nobody`; the uid, numeric so runAsNonRoot can check it.
USERS = {"nobody": "65534:65534"}


def recipe(manifest, dockerfile):
    """Iron Bank's version, binary path (its entrypoint) and user."""
    version = (manifest.get("labels") or {}).get("org.opencontainers.image.version")
    copy = re.search(r"^COPY\s+--from=\w+\s+(\S+)\s+(\S+)\s*$", dockerfile, re.M)
    entry = re.search(r"^ENTRYPOINT\s+\[\s*\"([^\"]+)\"", dockerfile, re.M)
    users = re.findall(r"^USER\s+(\S+)", dockerfile, re.M)
    if not version:
        raise SystemExit("Iron Bank's manifest has no org.opencontainers.image.version label; update recipe()")
    if not (copy and entry and users and re.search(r"^\s*(RUN.*)?make build", dockerfile, re.M)):
        raise SystemExit("Iron Bank's Dockerfile changed shape (make build / COPY --from / ENTRYPOINT / USER); "
                         "update recipe()")
    dest = copy.group(2)
    binary = dest + os.path.basename(copy.group(1)) if dest.endswith("/") else dest
    if binary != entry.group(1):
        raise SystemExit(f"Iron Bank copies the adapter to {binary} but runs {entry.group(1)}; update recipe()")
    user = users[-1]
    uid = user if re.fullmatch(r"\d+(:\d+)?", user) else USERS.get(user)
    if not uid:
        raise SystemExit(f"Iron Bank runs as {user!r}: add its uid to USERS")
    return {"upstream": version, "binary": binary, "user": user, "uid": uid if ":" in uid else f"{uid}:{uid}"}


def source(tag):
    """Upstream's source tarball for `tag`, with the modules to raise."""
    src = bbbuild.go_source(SOURCE.format(tag=tag))
    return {"tag": tag, "source": src["source"], "sha256": src["sha256"], "go": src["go"], "commit": src["commit"],
            "go_get": [bbbuild.go_latest(m) for m in GO_BUMPS]}


def generate(a):
    bb_tag = bbbuild.bigbang_tag(IB_IMAGE)
    commit, manifest, dockerfile = bbbuild.ironbank_recipe(IB_PROJECT, a.ref)
    ib = recipe(manifest, dockerfile)
    if ib["upstream"] != bb_tag:
        print(f"Iron Bank's recipe at {commit[:12]} is for {ib['upstream']}; Big Bang deploys {bb_tag}: building {bb_tag}")
    src = source(bb_tag)
    images, repos = bbbuild.go_images(a.org, src["go"])
    spec = {
        "ironbank": {"image": f"{IB_IMAGE}:{bb_tag}", "source": f"{bbbuild.REPO1}/dsop/{IB_PROJECT}",
                     "commit": commit, **ib},
        "image": {v: {"name": IMAGE + ("-fips" if v == "fips" else ""), "tag": bb_tag, **images[v]}
                  for v in ("standard", "fips")},
        "adapter": src,
        "user": ib["uid"],
        "notes": [f"built from {UPSTREAM} {bb_tag}'s source like Iron Bank's recipe (upstream's `make build`, "
                  "cgo) with `adapter.go_get` raised; Chainguard has no registry-adapter image",
                  "image.fips.fips: toolchain = cgo with the org's go-fips; native = the org has no go-fips image, so "
                  "Wolfi's Go builds it against Go's own FIPS 140-3 module (GOFIPS140, fips140=on by default)"],
    }
    bbbuild.write_spec(NAME, spec, f"# Big Bang's NeuVector registry-adapter built from {UPSTREAM}'s source FROM\n"
                                   "# the Chainguard org's images, following Iron Bank's recipe at `ironbank.commit`.\n")
    print(f"registry-adapter {bb_tag} (FIPS: {spec['image']['fips']['fips']}): build in "
          f"{'/'.join(repos['build'].values())} -dev, run on {'/'.join(repos['run'].values())}, go.mod Go {src['go']}, "
          f"raised {' '.join(src['go_get'])}")
    return spec


def build_args(spec, variant):
    s = spec["adapter"]
    return {"ADAPTER_SOURCE": s["source"], "ADAPTER_SHA256": s["sha256"], "ADAPTER_VERSION": s["tag"],
            "ADAPTER_GO_GET": " ".join(s["go_get"]),
            "GO_PACKAGE": spec["image"][variant].get("go_package", ""), "FIPS": spec["image"][variant].get("fips", ""),
            "BINARY": spec["ironbank"]["binary"], "RUN_USER": spec["user"]}


def docker(*args, check=False):
    # A timeout, so an adapter that waits for a controller instead of stopping fails the test.
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=300)


def smoke_test(image, variant, spec):
    failed = []
    helped = docker("run", "--rm", image, "-h")
    out = (helped.stdout + helped.stderr).strip()
    print("  -h: " + out.replace("\n", "\n  "))
    if not re.search(r"-join_port\b", out):
        failed.append(f"-h (exit {helped.returncode}, no -join_port flag)")
    # No cluster: the adapter logs START with its version, then stops because the internal
    # certificate NeuVector's chart mounts (/etc/neuvector/certs/internal) isn't there.
    started = docker("run", "--rm", image)
    out = (started.stdout + started.stderr).strip()
    print("  start without a cluster: " + out[-600:].replace("\n", "\n  "))
    if not re.search(rf"START.*version={re.escape(spec['adapter']['tag'])}\b", out):
        failed.append(f"start (no START with version={spec['adapter']['tag']})")
    if "failed to reload internal certificate" not in out:
        failed.append("start (did not stop at the missing internal certificate)")
    user = docker("image", "inspect", "--format", "{{.Config.User}}", image).stdout.strip()
    print(f"  user: {user}")
    if user != spec["user"]:
        failed.append(f"user {user!r}, want {spec['user']}")
    failed += bbbuild.go_build_problems(image, spec["ironbank"]["binary"], variant, spec["image"][variant],
                                       spec["adapter"]["go_get"], cgo=True)
    if failed:
        raise SystemExit(f"{image}: failed {', '.join(failed)}")


if __name__ == "__main__":
    bbbuild.run(NAME, __doc__, generate, build_args, smoke_test)
