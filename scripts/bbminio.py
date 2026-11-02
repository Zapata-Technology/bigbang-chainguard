"""Shared code for bb-minio-operator.py and bb-minio-operator-sidecar.py.

Chainguard publishes no MinIO Operator images. Big Bang's minio-operator package runs
the operator (ironbank/opensource/minio/operator) and hands it the sidecar image
(ironbank/opensource/minio/operator-sidecar) for every MinIO Tenant pod: the
`validate-arguments` init container and the `sidecar` container. Both programs live in
github.com/minio/operator: the operator is the root module (./cmd/operator), the sidecar
its own module in sidecar/ (./cmd/sidecar). Iron Bank's recipes copy each binary out of
upstream's image onto UBI minimal, with no USER (root). Upstream archived the repository
in 2026 and Iron Bank moved both recipes to iron-bank-archives, so no newer release will
come; Big Bang still deploys operator v7.1.1 with sidecar v7.1.0. These builders build
the same tags from source:

  1. read the Iron Bank tag Big Bang deploys (discovery/images.json)
  2. read Iron Bank's archived hardening_manifest.yaml (the upstream image and tag) and
     Dockerfile (the binary path and entrypoint), pinned to a commit
  3. download minio/operator's source for that tag from GitHub (pinned by sha256) and
     read the module's go.mod (the Go line) and the tag's commit
  4. pin the org's build and runtime images by digest (bbbuild.go_images)
  5. raise GO_BUMPS to their newest release. Upstream's go.mod pins golang.org/x/crypto
     to v0.36.0 with a `replace` (8 Critical CVEs) and x/net at v0.38.0 (GO-2026-6612,
     Critical): the replace is dropped (GO_DROP_REPLACE) so x/crypto can move
  6. write builds/<name>/build.yaml

Both binaries are static (CGO_ENABLED=0), as upstream's Makefile builds them, with its
version ldflags. FIPS as in bb-mattermost-operator.py ("toolchain" with the org's
go-fips, else "native": Go's own FIPS 140-3 module). The images run as 65532: Iron Bank
sets no user, and Big Bang's charts run both as their own non-root uids anyway.
"""
import os
import re
import subprocess

import bbbuild

IB_GROUP = "iron-bank-archives/dsop-archives"
UPSTREAM = "minio/operator"
SOURCE = "https://codeload.github.com/" + UPSTREAM + "/tar.gz/refs/tags/{tag}"
GO_BUMPS = ["golang.org/x/crypto", "golang.org/x/net"]
GO_DROP_REPLACE = ["golang.org/x/crypto"]
RUN_USER = "65532:65532"
# What each program is: its module directory in the repository, Go package, and the
# package whose version ldflags upstream's Makefile sets.
PROGRAMS = {
    "operator": {"module": ".", "package": "./cmd/operator", "pkg": "github.com/minio/operator/pkg"},
    "sidecar": {"module": "sidecar", "package": "./cmd/sidecar", "pkg": "github.com/minio/operator/sidecar/pkg"},
}


def recipe(manifest, dockerfile, image):
    """The upstream tag Iron Bank copies and the binary path (its entrypoint)."""
    upstream = next((r["tag"].rsplit(":", 1)[1] for r in manifest.get("resources", [])
                     if r.get("tag", "").startswith(f"quay.io/minio/{image}:") and "@sha256:" in r.get("url", "")
                     and not r["tag"].endswith((".arm64", ".amd64"))), None)
    # Iron Bank also copies upstream's CA bundle and /licenses; the binary is the copy the
    # entrypoint runs.
    copies = re.findall(r"^COPY\s+--from=\w+\s+/?(\S+)\s+(\S+)\s*$", dockerfile, re.M)
    entry = re.search(r"^ENTRYPOINT\s+\[\s*\"([^\"]+)\"", dockerfile, re.M)
    if not upstream:
        raise SystemExit(f"Iron Bank's manifest no longer copies quay.io/minio/{image}:<tag>; update recipe()")
    if not (entry and any(dest == entry.group(1) for _, dest in copies)):
        raise SystemExit("Iron Bank's Dockerfile changed shape (COPY --from / ENTRYPOINT); update recipe()")
    user = re.findall(r"^USER\s+(\S+)", dockerfile, re.M)
    return {"upstream": upstream, "binary": entry.group(1), "user": user[-1] if user else "root (none set)"}


def source(tag, program):
    """minio/operator's source tarball for `tag`: the program's module Go line and the modules to raise."""
    prog = PROGRAMS[program]
    src = bbbuild.go_source(SOURCE.format(tag=tag), gomod=os.path.join(prog["module"], "go.mod").replace("\\", "/"))
    return {"tag": tag, "source": src["source"], "sha256": src["sha256"], "go": src["go"], "commit": src["commit"],
            "module": prog["module"], "package": prog["package"],
            "ldflags": f"-s -w -X {prog['pkg']}.ReleaseTag={tag.lstrip('v')} -X {prog['pkg']}.Version={tag} "
                       f"-X {prog['pkg']}.ShortCommitID={src['commit'][:7]}",
            "drop_replace": list(GO_DROP_REPLACE), "go_get": [bbbuild.go_latest(m) for m in GO_BUMPS]}


def generate(a, name, program, ib_image, ib_project, image):
    bb_tag = bbbuild.bigbang_tag(ib_image)
    commit, manifest, dockerfile = bbbuild.ironbank_recipe(ib_project, a.ref, group=IB_GROUP)
    ib = recipe(manifest, dockerfile, ib_project.rsplit("/", 1)[1])
    if ib["upstream"] != bb_tag:
        print(f"Iron Bank's recipe at {commit[:12]} copies {ib['upstream']}; Big Bang deploys {bb_tag}: building {bb_tag}")
    src = source(bb_tag, program)
    images, repos = bbbuild.go_images(a.org, src["go"])
    spec = {
        "ironbank": {"image": f"{ib_image}:{bb_tag}", "source": f"{bbbuild.REPO1}/{IB_GROUP}/{ib_project}",
                     "commit": commit, **ib},
        "image": {v: {"name": image + ("-fips" if v == "fips" else ""), "tag": bb_tag, **images[v]}
                  for v in ("standard", "fips")},
        "minio": src,
        "user": RUN_USER,
        # Both programs build with builds/minio-operator/Dockerfile (and its directory as
        # context), each to its own final stage.
        "dockerfile": "minio-operator",
        "context": "minio-operator",
        "target": program,
        "notes": [f"built from {UPSTREAM} {bb_tag}'s source ({src['package']} in {src['module']}; Chainguard has no "
                  f"such image) with `minio.go_get` raised and `minio.drop_replace` dropped; Iron Bank (archived) "
                  "copies upstream's binary onto UBI minimal",
                  "upstream archived minio/operator in 2026: no newer release will come",
                  "MinIO Operator is AGPL-3.0: the image carries its LICENSE and CREDITS in /licenses",
                  "user 65532: Iron Bank sets none (root); Big Bang's charts run it as their own non-root uid",
                  "image.fips.fips: toolchain = cgo with the org's go-fips; native = the org has no go-fips image, so "
                  "Wolfi's Go builds it against Go's own FIPS 140-3 module (GOFIPS140, fips140=on by default)"],
    }
    bbbuild.write_spec(name, spec, f"# Big Bang's MinIO {program} built from {UPSTREAM}'s source FROM the\n"
                                   "# Chainguard org's images, following Iron Bank's archived recipe at `ironbank.commit`.\n")
    print(f"{name} {bb_tag} (FIPS: {spec['image']['fips']['fips']}): build in {'/'.join(repos['build'].values())} -dev, "
          f"run on {'/'.join(repos['run'].values())}, go.mod Go {src['go']}, raised {' '.join(src['go_get'])}")
    return spec


def build_args(spec, variant):
    m = spec["minio"]
    return {"MINIO_SOURCE": m["source"], "MINIO_SHA256": m["sha256"], "MINIO_MODULE": m["module"],
            "MINIO_PACKAGE": m["package"], "MINIO_LDFLAGS": m["ldflags"],
            "MINIO_DROP_REPLACE": " ".join(m["drop_replace"]), "MINIO_GO_GET": " ".join(m["go_get"]),
            "GO_PACKAGE": spec["image"][variant].get("go_package", ""), "FIPS": spec["image"][variant].get("fips", ""),
            "BINARY": spec["ironbank"]["binary"], "RUN_USER": spec["user"]}


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=300)


def smoke_test(image, variant, spec, help_args, help_rx, run_args, run_rx):
    """--help lists `help_rx`; started with `run_args` and no cluster, the output matches
    `run_rx` (it initialised and stopped where a cluster was needed); the user; the build info."""
    failed = []
    helped = docker("run", "--rm", image, *help_args)
    out = (helped.stdout + helped.stderr).strip()
    print("  help: " + out[-800:].replace("\n", "\n  "))
    if not re.search(help_rx, out):
        failed.append(f"help (exit {helped.returncode}, no {help_rx})")
    # The version ldflags took: help ends with "VERSION: <ReleaseTag> - <Version>".
    if spec["minio"]["tag"] not in out.rsplit("VERSION:", 1)[-1]:
        failed.append(f"help shows no version {spec['minio']['tag']} (version ldflags)")
    started = docker("run", "--rm", image, *run_args)
    out = (started.stdout + started.stderr).strip()
    print("  start without a cluster: " + out[-600:].replace("\n", "\n  "))
    if not re.search(run_rx, out, re.I):
        failed.append(f"start (no {run_rx})")
    user = docker("image", "inspect", "--format", "{{.Config.User}}", image).stdout.strip()
    print(f"  user: {user}")
    if user != spec["user"]:
        failed.append(f"user {user!r}, want {spec['user']}")
    failed += bbbuild.go_build_problems(image, spec["ironbank"]["binary"], variant, spec["image"][variant],
                                       spec["minio"]["go_get"])
    if failed:
        raise SystemExit(f"{image}: failed {', '.join(failed)}")
