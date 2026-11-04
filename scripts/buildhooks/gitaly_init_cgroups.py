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
  5. return builds/gitaly-init-cgroups/build.yaml

The binary is static (as CNG builds it). FIPS as in the mattermost-operator hook. It runs
as root, as CNG's (scratch, no USER) does and the chart requires (it chowns cgroup files).

--test runs each image the way the chart does, against a pod cgroup tree made in a
temporary directory: it must find the pod's slice by its uid, chown the slice and its
cgroup.procs, cgroup.threads and cgroup.subtree_control to 1000:1000, and write the slice's
path to OUTPUT_PATH; with an unknown pod uid it must fail. Then the user, and the build
info (static, or the FIPS build; for -fips, OpenSSL fips=yes).

Recipe keys: bigbang_image (the image Big Bang deploys: not Iron Bank's), gitlab, cng_raw
({{tag}}, {{path}}), source ({{tag}}, {{module}}), license ({{tag}}), go_bumps, user, notes.
"""
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import uuid

import bbbuild


def gitaly_tag(bb_tag):
    """Big Bang's tag (v19.4.2-ubi) -> the Gitaly release (v19.4.2)."""
    m = re.fullmatch(r"(v\d+\.\d+\.\d+)(-ubi|-fips)*", bb_tag)
    if not m:
        raise SystemExit(f"gitaly-init-cgroups:{bb_tag} is not v<GitLab release>[-ubi]; update gitaly_tag()")
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


def source(r, tag, module):
    """The module's directory at `tag` (url, sha256, Go line, commit) and Gitaly's LICENSE,
    which sits outside it (MIT: the image carries it)."""
    values = {**r, "tag": tag, "module": module}
    src = bbbuild.go_source(bbbuild.fill(r["source"], values), gomod=f"{module}/go.mod")
    license_url = bbbuild.fill(r["license"], values)
    return {"tag": tag, "source": src["source"], "sha256": src["sha256"], "go": src["go"], "commit": src["commit"],
            "module": module, "go_get": [bbbuild.go_latest(m) for m in r["go_bumps"]],
            "license": license_url, "license_sha256": hashlib.sha256(bbbuild.fetch(license_url)).hexdigest()}


def generate(r, a):
    bb_image = r["bigbang_image"]
    bb_tag = bbbuild.bigbang_tag(bb_image)
    tag = gitaly_tag(bb_tag)
    init_df = bbbuild.fetch(bbbuild.fill(r["cng_raw"], {**r, "tag": tag, "path": "gitaly-init-cgroups/Dockerfile"})).decode()
    gitaly_df = bbbuild.fetch(bbbuild.fill(r["cng_raw"], {**r, "tag": tag, "path": "gitaly/Dockerfile"})).decode()
    cng = recipe(init_df, gitaly_df)
    src = source(r, tag, cng["module"])
    images, repos = bbbuild.go_images(a.org, src["go"])
    spec = {
        "cng": {"image": f"registry1.dso.mil/{bb_image}:{bb_tag}",
                "source": f"{r['gitlab']}/gitlab-org/build/CNG/-/tree/{tag}/gitaly-init-cgroups", **cng},
        "image": {v: {"name": bbbuild.image_name(r, v), "tag": bb_tag, **images[v]}
                  for v in ("standard", "fips")},
        "gitaly": src,
        "user": r["user"],
        "notes": [bbbuild.fill(n, {**r, "tag": tag, "module": cng["module"]}) for n in r["notes"]],
    }
    print(f"gitaly-init-cgroups {bb_tag} (Gitaly {tag}, FIPS: {spec['image']['fips']['fips']}): build in "
          f"{'/'.join(repos['build'].values())} -dev, run on {'/'.join(repos['run'].values())}, go.mod Go {src['go']}")
    return spec


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


def smoke_test(image, variant, spec, r=None):
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
