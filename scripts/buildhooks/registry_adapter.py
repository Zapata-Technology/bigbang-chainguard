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
  4. pin the org's build and runtime images by digest (bbbuild.go_images, as the
     mattermost-operator hook does)
  5. raise the recipe's go_bumps to their newest release: v0.2.11's go.mod pins x/net
     v0.58.0, which carries a Critical CVE (GO-2026-6612), and go-jose v4.1.4 (a fixable High)
  6. return builds/registry-adapter/build.yaml

Both binaries are cgo builds (upstream's are), linked against the runtime image's glibc.
FIPS as in the mattermost-operator hook: "toolchain" with the org's go-fips (Chainguard's
FIPS Go), else "native" (Wolfi's Go against Go's own FIPS 140-3 module, GOFIPS140,
GODEBUG fips140=on by default), here with cgo.

--test runs each image: -h lists the adapter's flags; started with no cluster it logs
START with the tag as its version and stops at the missing internal certificate (so it
initialised, and for -fips its FIPS crypto loaded); it runs as 65534; the binary's
build info shows a cgo build with the raised modules and, for -fips, the FIPS build; and
for -fips that OpenSSL defaults to fips=yes.

Recipe keys: ironbank {image, project}, upstream, source ({{tag}}), go_bumps, users
({name: uid:gid} for a named USER), notes.
"""
import os
import re
import subprocess

import bbbuild


def recipe(manifest, dockerfile, users):
    """Iron Bank's version, binary path (its entrypoint) and user, with its numeric uid:gid."""
    version = (manifest.get("labels") or {}).get("org.opencontainers.image.version")
    copy = re.search(r"^COPY\s+--from=\w+\s+(\S+)\s+(\S+)\s*$", dockerfile, re.M)
    entry = re.search(r"^ENTRYPOINT\s+\[\s*\"([^\"]+)\"", dockerfile, re.M)
    found = re.findall(r"^USER\s+(\S+)", dockerfile, re.M)
    if not version:
        raise SystemExit("Iron Bank's manifest has no org.opencontainers.image.version label; update recipe()")
    if not (copy and entry and found and re.search(r"^\s*(RUN.*)?make build", dockerfile, re.M)):
        raise SystemExit("Iron Bank's Dockerfile changed shape (make build / COPY --from / ENTRYPOINT / USER); "
                         "update recipe()")
    dest = copy.group(2)
    binary = dest + os.path.basename(copy.group(1)) if dest.endswith("/") else dest
    if binary != entry.group(1):
        raise SystemExit(f"Iron Bank copies the adapter to {binary} but runs {entry.group(1)}; update recipe()")
    user = found[-1]
    uid = user if re.fullmatch(r"\d+(:\d+)?", user) else users.get(user)
    if not uid:
        raise SystemExit(f"Iron Bank runs as {user!r}: add its uid to the recipe's users")
    return {"upstream": version, "binary": binary, "user": user, "uid": uid if ":" in uid else f"{uid}:{uid}"}


def source(r, tag):
    """Upstream's source tarball for `tag`, with the modules to raise."""
    src = bbbuild.go_source(bbbuild.fill(r["source"], {**r, "tag": tag}))
    return {"tag": tag, "source": src["source"], "sha256": src["sha256"], "go": src["go"], "commit": src["commit"],
            "go_get": [bbbuild.go_latest(m) for m in r["go_bumps"]]}


def generate(r, a):
    ib_image, ib_project = r["ironbank"]["image"], r["ironbank"]["project"]
    bb_tag = bbbuild.bigbang_tag(ib_image)
    commit, manifest, dockerfile = bbbuild.ironbank_recipe(ib_project, a.ref)
    ib = recipe(manifest, dockerfile, r.get("users") or {})
    if ib["upstream"] != bb_tag:
        print(f"Iron Bank's recipe at {commit[:12]} is for {ib['upstream']}; Big Bang deploys {bb_tag}: building {bb_tag}")
    src = source(r, bb_tag)
    images, repos = bbbuild.go_images(a.org, src["go"])
    spec = {
        "ironbank": {"image": f"{ib_image}:{bb_tag}", "source": f"{bbbuild.REPO1}/dsop/{ib_project}",
                     "commit": commit, **ib},
        "image": {v: {"name": bbbuild.image_name(r, v), "tag": bb_tag, **images[v]}
                  for v in ("standard", "fips")},
        "adapter": src,
        "user": ib["uid"],
        "notes": [bbbuild.fill(n, {**r, "tag": bb_tag}) for n in r["notes"]],
    }
    print(f"registry-adapter {bb_tag} (FIPS: {spec['image']['fips']['fips']}): build in "
          f"{'/'.join(repos['build'].values())} -dev, run on {'/'.join(repos['run'].values())}, go.mod Go {src['go']}, "
          f"raised {' '.join(src['go_get'])}")
    return spec


def docker(*args, check=False):
    # A timeout, so an adapter that waits for a controller instead of stopping fails the test.
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=300)


def smoke_test(image, variant, spec, r=None):
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
