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
     org has (bbbuild.GO_BUILD_IMAGES, GO_RUNTIME_IMAGES)
  5. raise the recipe's go_bumps to their newest release: upstream's go.mod pins x/net
     v0.58.0, which carries a Critical CVE (GO-2026-6612)
  6. return builds/mattermost-operator/build.yaml

The standard binary is static (CGO_ENABLED=0), like upstream's. The FIPS binary:

  toolchain  the org has go-fips: built the way upstream's Dockerfile.fips builds it
             (CGO_ENABLED=1, -tags=requirefips) with Chainguard's FIPS Go, so its crypto
             goes through the runtime image's FIPS OpenSSL
  native     it doesn't (kubectl-fips -dev offers no FIPS Go package, only go-1.N): built
             with Wolfi's Go against Go's own FIPS 140-3 module (GOFIPS140), which also
             makes GODEBUG fips140=on the binary's default

--test runs each image: --help lists the operator's flags; started with no cluster it
logs its Go version and stops at the missing kubeconfig (so it initialised, and for
-fips its FIPS crypto loaded); it runs as 65532; the binary's build info shows the
raised modules and, for -fips, the FIPS build (toolchain: cgo with a FIPS crypto
GOEXPERIMENT; native: GOFIPS140 with fips140=on by default); and for -fips that OpenSSL
defaults to fips=yes.

Recipe keys: ironbank {image, project}, upstream, source ({{tag}}), go_bumps, user, notes.
"""
import re
import subprocess
import time

import bbbuild


def recipe(manifest, dockerfile, upstream):
    """The upstream tag Iron Bank copies, the binary path and the user it runs as."""
    tag = next((r["tag"].split(":", 1)[1] for r in manifest.get("resources", [])
                if r.get("tag", "").startswith(upstream + ":") and "@sha256:" in r.get("url", "")
                and not r["tag"].endswith((".arm64", ".amd64"))), None)
    copy = re.search(r"^COPY\s+--from=\w+\s+(\S+)\s+(\S+)", dockerfile, re.M)
    entry = re.search(r"^ENTRYPOINT\s+\[\s*\"([^\"]+)\"", dockerfile, re.M)
    user = re.search(r"^USER\s+(\S+)", dockerfile, re.M)
    if not tag:
        raise SystemExit(f"Iron Bank's manifest no longer copies {upstream}:<tag>; update recipe()")
    if not (copy and entry and user) or copy.group(2) != entry.group(1):
        raise SystemExit("Iron Bank's Dockerfile changed shape (COPY --from / ENTRYPOINT / USER); update recipe()")
    return {"upstream": tag, "binary": entry.group(1), "user": user.group(1)}


def source(r, tag):
    """Upstream's source tarball for `tag`: url, sha256, its go.mod's Go line, the commit."""
    src = bbbuild.go_source(bbbuild.fill(r["source"], {**r, "tag": tag}))
    return {"tag": tag, "source": src["source"], "sha256": src["sha256"], "go": src["go"], "commit": src["commit"],
            # upstream's Makefile stamps `date -u +%Y%m%d.%H%M%S`; the commit time keeps it reproducible
            "build_time": time.strftime("%Y%m%d.%H%M%S", time.gmtime(src["time"])),
            "go_get": [bbbuild.go_latest(m) for m in r["go_bumps"]]}


def generate(r, a):
    ib_image, ib_project = r["ironbank"]["image"], r["ironbank"]["project"]
    bb_tag = bbbuild.bigbang_tag(ib_image)
    commit, manifest, dockerfile = bbbuild.ironbank_recipe(ib_project, a.ref)
    ib = recipe(manifest, dockerfile, r["upstream"])
    if ib["upstream"] != bb_tag:
        print(f"Iron Bank's recipe at {commit[:12]} copies {ib['upstream']}; Big Bang deploys {bb_tag}: building {bb_tag}")
    src = source(r, bb_tag)
    images, repos = bbbuild.go_images(a.org, src["go"])
    spec = {
        "ironbank": {"image": f"{ib_image}:{bb_tag}", "source": f"{bbbuild.REPO1}/dsop/{ib_project}",
                     "commit": commit, **ib},
        "image": {v: {"name": bbbuild.image_name(r, v), "tag": bb_tag, **images[v]}
                  for v in ("standard", "fips")},
        "operator": src,
        "user": r["user"],
        "notes": [bbbuild.fill(n, {**r, "tag": bb_tag}) for n in r["notes"]],
    }
    print(f"mattermost-operator {bb_tag} (FIPS: {spec['image']['fips']['fips']}): build in {'/'.join(repos['build'].values())} -dev, run on "
          f"{'/'.join(repos['run'].values())}, go.mod Go {src['go']}, raised {' '.join(src['go_get'])}")
    return spec


def docker(*args, check=False):
    # A timeout, so an operator that waits for a cluster instead of stopping fails the test.
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=300)


def smoke_test(image, variant, spec, r=None):
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
    failed += bbbuild.go_build_problems(image, binary, variant, spec["image"][variant],
                                       spec["operator"]["go_get"])
    if failed:
        raise SystemExit(f"{image}: failed {', '.join(failed)}")
