#!/usr/bin/env python3
"""Build Big Bang's gitaly-init-cgroups image from Gitaly's source, FROM the org.

Chainguard publishes no gitaly-init-cgroups image. It is GitLab's chart's init container
for Gitaly's cgroups (`gitlab.gitaly.cgroups.enabled`, off in Big Bang's defaults): it
finds the Gitaly pod's cgroup under CGROUP_PATH, chowns it to Gitaly's uid (1000) so
Gitaly can manage its own subtree, and writes the path to OUTPUT_PATH. Big Bang pulls
GitLab's own CNG build from registry1 (gitlab/gitlab-org/build/cng/gitaly-init-cgroups,
<release>-ubi); Iron Bank has no recipe for it. GitLab's CNG recipe is the reference:
gitaly-init-cgroups/Dockerfile copies /usr/local/bin/setup_cgroups out of the gitaly
image onto scratch with CMD ["/setup_cgroups"], and gitaly/Dockerfile builds it from
Gitaly's tools/gitaly-init-cgroups module (CGO_ENABLED=0, -trimpath). This builds the
same module at the Gitaly release Big Bang's tag names:

  1. read the tag Big Bang deploys (discovery/images.json): v19.4.2-ubi is Gitaly v19.4.2
  2. read CNG's two Dockerfiles at that tag from gitlab.com (the binary path and command,
     and the module it builds), checking they still have that shape
  3. download the module's directory of Gitaly's source for that tag from gitlab.com
     (GitLab's archive of one path, pinned by sha256) and read its go.mod (the Go line)
     and the tag's commit
  4. pin the org's build and runtime images by digest (bbbuild.go_images)
  5. write builds/gitaly-init-cgroups/build.yaml

The binary is static (as CNG builds it). FIPS as in bb-mattermost-operator.py. It runs as
root, as CNG's (scratch, no USER) does and the chart requires (it chowns cgroup files).

  bb-gitaly-init-cgroups.py [--org <org>]                     regenerate build.yaml
  bb-gitaly-init-cgroups.py --build [--test] [--push]         also build both images

--test runs each image the way the chart does, against a pod cgroup tree made in a
temporary directory: it must find the pod's slice by its uid, chown the slice and its
cgroup.procs, cgroup.threads and cgroup.subtree_control to 1000:1000, and write the slice's
path to OUTPUT_PATH; with an unknown pod uid it must fail. Then the user, and the build
info (static, or the FIPS build; for -fips, OpenSSL fips=yes).
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bbbuild  # noqa: E402

NAME = "gitaly-init-cgroups"
BB_IMAGE = "gitlab/gitlab-org/build/cng/gitaly-init-cgroups"
IMAGE = "bigbang-gitaly-init-cgroups"
GITLAB = "https://gitlab.com"
CNG_RAW = GITLAB + "/gitlab-org/build/CNG/-/raw/{tag}/{path}"
# GitLab's archive of one directory of the repository at a tag (deterministic: git archive).
SOURCE = GITLAB + "/gitlab-org/gitaly/-/archive/{tag}/gitaly-{tag}.tar.gz?path={module}"
LICENSE = GITLAB + "/gitlab-org/gitaly/-/raw/{tag}/LICENSE"
RUN_USER = "0:0"
# Modules raised past what the module's go.mod pins: none needed at 19.4.2 (grype finds no
# fixable Critical); listed here as the other builders do, to raise one when it does.
GO_BUMPS = []


def gitaly_tag(bb_tag):
    """Big Bang's tag (v19.4.2-ubi) -> the Gitaly release (v19.4.2)."""
    m = re.fullmatch(r"(v\d+\.\d+\.\d+)(-ubi|-fips)*", bb_tag)
    if not m:
        raise SystemExit(f"{BB_IMAGE}:{bb_tag} is not v<GitLab release>[-ubi]; update gitaly_tag()")
    return m.group(1)


def recipe(init_dockerfile, gitaly_dockerfile):
    """From CNG's Dockerfiles: the binary path and command, and the Gitaly module directory."""
    copy = re.search(r"^COPY\s+--from=gitaly\s+(\S+)\s+(\S+)\s*$", init_dockerfile, re.M)
    cmd = re.search(r"^CMD\s+\[\s*\"([^\"]+)\"", init_dockerfile, re.M)
    build = re.search(r"go build -C \"\$\{BUILD_DIR\}/(\S+?)\"[^\n]*-o (\S+) \.", gitaly_dockerfile)
    if not (copy and cmd and build):
        raise SystemExit("CNG's gitaly-init-cgroups or gitaly Dockerfile changed shape "
                         "(COPY --from=gitaly / CMD / go build -C); update recipe()")
    if copy.group(1) != build.group(2):
        raise SystemExit(f"CNG copies {copy.group(1)} but builds {build.group(2)}; update recipe()")
    dest = copy.group(2)
    binary = dest + os.path.basename(copy.group(1)) if dest.endswith("/") else dest
    if binary != cmd.group(1):
        raise SystemExit(f"CNG copies the binary to {binary} but runs {cmd.group(1)}; update recipe()")
    return {"binary": binary, "module": build.group(1)}


def source(tag, module):
    """The module's directory at `tag` (url, sha256, Go line, commit) and Gitaly's LICENSE,
    which sits outside it (MIT: the image carries it)."""
    src = bbbuild.go_source(SOURCE.format(tag=tag, module=module), gomod=f"{module}/go.mod")
    license_url = LICENSE.format(tag=tag)
    return {"tag": tag, "source": src["source"], "sha256": src["sha256"], "go": src["go"], "commit": src["commit"],
            "module": module, "go_get": [bbbuild.go_latest(m) for m in GO_BUMPS],
            "license": license_url, "license_sha256": hashlib.sha256(bbbuild.fetch(license_url)).hexdigest()}


def generate(a):
    bb_tag = bbbuild.bigbang_tag(BB_IMAGE)
    tag = gitaly_tag(bb_tag)
    init_df = bbbuild.fetch(CNG_RAW.format(tag=tag, path="gitaly-init-cgroups/Dockerfile")).decode()
    gitaly_df = bbbuild.fetch(CNG_RAW.format(tag=tag, path="gitaly/Dockerfile")).decode()
    cng = recipe(init_df, gitaly_df)
    src = source(tag, cng["module"])
    images, repos = bbbuild.go_images(a.org, src["go"])
    spec = {
        "cng": {"image": f"registry1.dso.mil/{BB_IMAGE}:{bb_tag}",
                "source": f"{GITLAB}/gitlab-org/build/CNG/-/tree/{tag}/gitaly-init-cgroups", **cng},
        "image": {v: {"name": IMAGE + ("-fips" if v == "fips" else ""), "tag": bb_tag, **images[v]}
                  for v in ("standard", "fips")},
        "gitaly": src,
        "user": RUN_USER,
        "notes": [f"built from Gitaly {tag}'s {cng['module']} module as GitLab's CNG builds it (Chainguard has no "
                  "gitaly-init-cgroups image; Iron Bank has no recipe); the tag keeps Big Bang's",
                  "runs as root like CNG's: the chart runs it as root to chown the pod's cgroup to Gitaly's uid",
                  "image.fips.fips: toolchain = cgo with the org's go-fips; native = the org has no go-fips image, so "
                  "Wolfi's Go builds it against Go's own FIPS 140-3 module (GOFIPS140, fips140=on by default)"],
    }
    bbbuild.write_spec(NAME, spec, "# GitLab's gitaly-init-cgroups built from Gitaly's source FROM the Chainguard\n"
                                   "# org's images, following GitLab's CNG recipe (`cng.source`).\n")
    print(f"gitaly-init-cgroups {bb_tag} (Gitaly {tag}, FIPS: {spec['image']['fips']['fips']}): build in "
          f"{'/'.join(repos['build'].values())} -dev, run on {'/'.join(repos['run'].values())}, go.mod Go {src['go']}")
    return spec


def build_args(spec, variant):
    g = spec["gitaly"]
    return {"GITALY_SOURCE": g["source"], "GITALY_SHA256": g["sha256"], "GITALY_MODULE": g["module"],
            "GITALY_GO_GET": " ".join(g["go_get"]),
            "GITALY_LICENSE": g["license"], "GITALY_LICENSE_SHA256": g["license_sha256"],
            "GO_PACKAGE": spec["image"][variant].get("go_package", ""), "FIPS": spec["image"][variant].get("fips", ""),
            "BINARY": spec["cng"]["binary"], "RUN_USER": spec["user"]}


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=300)


def cgroup_tree(root, pod_uid):
    """A pod's cgroup slice the way the kubelet's systemd driver names it, under kubepods.slice."""
    k8s = pod_uid.replace("-", "_")
    slice_dir = os.path.join(root, "cgroup", "kubepods-burstable.slice", f"kubepods-burstable-pod{k8s}.slice")
    os.makedirs(slice_dir)
    for f in ("cgroup.procs", "cgroup.threads", "cgroup.subtree_control"):
        open(os.path.join(slice_dir, f), "w").close()
    os.makedirs(os.path.join(root, "out"))
    return f"/run/gitaly/cgroup/kubepods-burstable.slice/kubepods-burstable-pod{k8s}.slice"


def run_like_the_chart(image, root, pod_uid):
    # The chart's env; the temporary tree stands in for the node's /sys/fs/cgroup/kubepods.slice.
    return docker("run", "--rm", "-v", f"{root}/cgroup:/run/gitaly/cgroup/", "-v", f"{root}/out:/init-secrets",
                  "-e", f"GITALY_POD_UID={pod_uid}", "-e", "CGROUP_PATH=/run/gitaly/cgroup/",
                  "-e", "OUTPUT_PATH=/init-secrets/gitaly-pod-cgroup", image)


def smoke_test(image, variant, spec):
    failed = []
    root = tempfile.mkdtemp(prefix="gic-")
    try:
        os.chmod(root, 0o755)
        pod_uid = str(uuid.uuid4())
        expect = cgroup_tree(root, pod_uid)
        ran = run_like_the_chart(image, root, pod_uid)
        out = (ran.stdout + ran.stderr).strip()
        print("  run like the chart: " + out.replace("\n", "\n  "))
        if ran.returncode:
            failed.append(f"run (exit {ran.returncode})")
        written = open(os.path.join(root, "out", "gitaly-pod-cgroup")).read() \
            if os.path.exists(os.path.join(root, "out", "gitaly-pod-cgroup")) else ""
        print(f"  OUTPUT_PATH: {written}")
        if written.rstrip("/") != expect:
            failed.append(f"OUTPUT_PATH has {written!r}, want {expect}")
        slice_dir = os.path.join(root, "cgroup", *expect.split("/")[4:])
        changed = [f for f in ("", "cgroup.procs", "cgroup.threads", "cgroup.subtree_control")
                   if (os.stat(os.path.join(slice_dir, f)).st_uid, os.stat(os.path.join(slice_dir, f)).st_gid)
                   != (1000, 1000)]
        print(f"  not chowned to 1000:1000: {changed or 'none'}")
        if changed:
            failed.append(f"not chowned to 1000:1000: {changed}")
        other = run_like_the_chart(image, root, str(uuid.uuid4()))
        if other.returncode == 0 or "failed to find cgroup path" not in other.stdout + other.stderr:
            failed.append(f"an unknown pod uid did not fail (exit {other.returncode})")
    finally:
        # The container chowned some files to 1000; the runner can still remove them (it owns the directories).
        shutil.rmtree(root, ignore_errors=True)
    user = docker("image", "inspect", "--format", "{{.Config.User}}", image).stdout.strip()
    print(f"  user: {user}")
    if user != spec["user"]:
        failed.append(f"user {user!r}, want {spec['user']}")
    failed += bbbuild.go_build_problems(image, spec["cng"]["binary"], variant, spec["image"][variant],
                                       spec["gitaly"]["go_get"])
    if failed:
        raise SystemExit(f"{image}: failed {', '.join(failed)}")


if __name__ == "__main__":
    bbbuild.run(NAME, __doc__, generate, build_args, smoke_test)
