"""Build Big Bang's MinIO Operator and its sidecar from upstream's source, FROM the org.

The hook of builds/minio-operator and builds/minio-operator-sidecar. Chainguard publishes
no MinIO Operator images. Big Bang's minio-operator package runs the operator
(ironbank/opensource/minio/operator) and hands it the sidecar image
(ironbank/opensource/minio/operator-sidecar) for every MinIO Tenant pod: the
`validate-arguments` init container and the `sidecar` container. Both programs live in
github.com/minio/operator: the operator is the root module (./cmd/operator), the sidecar
its own module in sidecar/ (./cmd/sidecar). Iron Bank's recipes copy each binary out of
upstream's image onto UBI minimal, with no USER (root). Upstream archived the repository
in 2026 and Iron Bank moved both recipes to iron-bank-archives, so no newer release will
come; Big Bang still deploys operator v7.1.1 with sidecar v7.1.0. These builds build
the same tags from source:

  1. read the Iron Bank tag Big Bang deploys (discovery/images.json)
  2. read Iron Bank's archived hardening_manifest.yaml (the upstream image and tag) and
     Dockerfile (the binary path and entrypoint), pinned to a commit
  3. download minio/operator's source for that tag from GitHub (pinned by sha256) and
     read the module's go.mod (the Go line) and the tag's commit
  4. pin the org's build and runtime images by digest (bbbuild.go_images)
  5. raise the recipe's go_bumps to their newest release. Upstream's go.mod pins
     golang.org/x/crypto to v0.36.0 with a `replace` (8 Critical CVEs) and x/net at
     v0.38.0 (GO-2026-6612, Critical): the replace is dropped (`drop_replace`) so x/crypto
     can move
  6. return builds/<name>/build.yaml

Both binaries are static (CGO_ENABLED=0), as upstream's Makefile builds them, with its
version ldflags. FIPS as in the mattermost-operator hook ("toolchain" with the org's
go-fips, else "native": Go's own FIPS 140-3 module). The images run as 65532: Iron Bank
sets no user, and Big Bang's charts run both as their own non-root uids anyway. Both
build with builds/minio-operator/Dockerfile, each to its own final stage (`program`).

--test runs each image: --help lists the program's command (`smoke_run.help_rx`); started
with `smoke_run.run_args` and no cluster it stops at the missing in-cluster config (so it
initialised, and for -fips its FIPS crypto loaded); it runs as 65532; the binary's build
info shows the raised modules and the FIPS or static build; and for -fips that OpenSSL
defaults to fips=yes.

Recipe keys: program (operator or sidecar: the Dockerfile stage), ironbank {image,
project, group}, upstream, source ({{tag}}), module (its directory in the repository),
package (the Go package), version_pkg (the package upstream's version ldflags set),
go_bumps, drop_replace, user, dockerfile (the build directory whose Dockerfile and
context it uses), smoke_run {help_args, help_rx, run_args, run_rx}, notes.
"""
import re
import subprocess

import bbbuild


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


def source(r, tag):
    """minio/operator's source tarball for `tag`: the program's module Go line and the modules to raise."""
    gomod = f"{r['module']}/go.mod"
    src = bbbuild.go_source(bbbuild.fill(r["source"], {**r, "tag": tag}), gomod=gomod)
    pkg = r["version_pkg"]
    return {"tag": tag, "source": src["source"], "sha256": src["sha256"], "go": src["go"], "commit": src["commit"],
            "module": r["module"], "package": r["package"],
            "ldflags": f"-s -w -X {pkg}.ReleaseTag={tag.lstrip('v')} -X {pkg}.Version={tag} "
                       f"-X {pkg}.ShortCommitID={src['commit'][:7]}",
            "drop_replace": list(r["drop_replace"]), "go_get": [bbbuild.go_latest(m) for m in r["go_bumps"]]}


def generate(r, a):
    ib = r["ironbank"]
    bb_tag = bbbuild.bigbang_tag(ib["image"])
    commit, manifest, dockerfile = bbbuild.ironbank_recipe(ib["project"], a.ref, group=ib["group"])
    found = recipe(manifest, dockerfile, ib["project"].rsplit("/", 1)[1])
    if found["upstream"] != bb_tag:
        print(f"Iron Bank's recipe at {commit[:12]} copies {found['upstream']}; Big Bang deploys {bb_tag}: building {bb_tag}")
    src = source(r, bb_tag)
    images, repos = bbbuild.go_images(a.org, src["go"])
    spec = {
        "ironbank": {"image": f"{ib['image']}:{bb_tag}", "source": f"{bbbuild.REPO1}/{ib['group']}/{ib['project']}",
                     "commit": commit, **found},
        "image": {v: {"name": bbbuild.image_name(r, v), "tag": bb_tag, **images[v]} for v in ("standard", "fips")},
        "minio": src,
        "user": r["user"],
        # Both programs build with one Dockerfile (and its directory as context), each to
        # its own final stage.
        "dockerfile": r["dockerfile"],
        "context": r["dockerfile"],
        "target": r["program"],
        "notes": [bbbuild.fill(n, {**r, "tag": bb_tag}) for n in r["notes"]],
    }
    print(f"{r['name']} {bb_tag} (FIPS: {spec['image']['fips']['fips']}): build in {'/'.join(repos['build'].values())} -dev, "
          f"run on {'/'.join(repos['run'].values())}, go.mod Go {src['go']}, raised {' '.join(src['go_get'])}")
    return spec


def build_args(spec, variant, r=None):
    # In the hook rather than each recipe: both builds pass the same arguments.
    m = spec["minio"]
    return {"MINIO_SOURCE": m["source"], "MINIO_SHA256": m["sha256"], "MINIO_MODULE": m["module"],
            "MINIO_PACKAGE": m["package"], "MINIO_LDFLAGS": m["ldflags"],
            "MINIO_DROP_REPLACE": " ".join(m["drop_replace"]), "MINIO_GO_GET": " ".join(m["go_get"]),
            "GO_PACKAGE": spec["image"][variant].get("go_package", ""), "FIPS": spec["image"][variant].get("fips", ""),
            "BINARY": spec["ironbank"]["binary"], "RUN_USER": spec["user"]}


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=300)


def smoke_test(image, variant, spec, r):
    """--help lists `help_rx`; started with `run_args` and no cluster, the output matches
    `run_rx` (it initialised and stopped where a cluster was needed); the user; the build info."""
    s = r["smoke_run"]
    failed = []
    helped = docker("run", "--rm", image, *s["help_args"])
    out = (helped.stdout + helped.stderr).strip()
    print("  help: " + out[-800:].replace("\n", "\n  "))
    if not re.search(s["help_rx"], out):
        failed.append(f"help (exit {helped.returncode}, no {s['help_rx']})")
    # The version ldflags took: help ends with "VERSION: <ReleaseTag> - <Version>".
    if spec["minio"]["tag"] not in out.rsplit("VERSION:", 1)[-1]:
        failed.append(f"help shows no version {spec['minio']['tag']} (version ldflags)")
    started = docker("run", "--rm", image, *s["run_args"])
    out = (started.stdout + started.stderr).strip()
    print("  start without a cluster: " + out[-600:].replace("\n", "\n  "))
    if not re.search(s["run_rx"], out, re.I):
        failed.append(f"start (no {s['run_rx']})")
    user = docker("image", "inspect", "--format", "{{.Config.User}}", image).stdout.strip()
    print(f"  user: {user}")
    if user != spec["user"]:
        failed.append(f"user {user!r}, want {spec['user']}")
    failed += bbbuild.go_build_problems(image, spec["ironbank"]["binary"], variant, spec["image"][variant],
                                       spec["minio"]["go_get"])
    if failed:
        raise SystemExit(f"{image}: failed {', '.join(failed)}")
