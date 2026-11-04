"""Build Big Bang's tempo-query image from upstream's source, FROM the org.

Chainguard publishes no tempo-query image. tempo-query is Tempo's Jaeger storage
backend: a gRPC server (port 7777) that answers Jaeger's storage API by querying
Tempo's HTTP API. The Tempo chart runs it beside Tempo when `tempoQuery.enabled` is set
(off in Big Bang's defaults). Iron Bank's recipe puts the binary at /tempo-query, on its
jaeger-query image (whose query service the entrypoint never starts), run as 10001.
For Tempo 2.x it copies upstream's binary out of grafana/tempo-query:<tag>; from 3.x it
builds ./cmd/tempo-query from upstream's source with some modules raised. This builds
the binary from upstream's source at the tag Big Bang deploys:

  1. read the Iron Bank tag Big Bang deploys (discovery/images.json)
  2. read Iron Bank's hardening_manifest.yaml (its version) and Dockerfile (the binary
     path, entrypoint and user) from repo1.dso.mil, pinned to a commit
  3. download grafana/tempo's source for that tag from GitHub (pinned by sha256) and read
     its go.mod (the Go line) and the tag's commit
  4. pin the org's build and runtime images by digest, as the mattermost-operator hook
     does (bbbuild.go_images: go / go-fips -dev, else kubectl / kubectl-fips -dev plus
     Wolfi's Go; glibc-openssl(-fips), else kubectl(-fips))
  5. raise the recipe's go_bumps to their newest release: Tempo 2.10.5's go.mod pins x/net
     v0.52.0, which carries a Critical CVE (GO-2026-6612), and grpc, thrift and x/text
     with fixable Highs
  6. return builds/tempo-query/build.yaml

The standard binary is static (CGO_ENABLED=0), like upstream's. FIPS as in the
mattermost-operator hook: "toolchain" with the org's go-fips (cgo, Chainguard's FIPS
Go), else "native" (Wolfi's Go against Go's own FIPS 140-3 module, GOFIPS140, with
GODEBUG fips140=on the binary's default).

--test runs each image: -h lists the -config flag; started with no Tempo it serves gRPC
on 7777 and its health service answers SERVING (a gRPC call with curl over HTTP/2); it
runs as 10001; the binary's build info shows the raised modules and, for -fips, the
FIPS build; and for -fips that OpenSSL defaults to fips=yes.

Recipe keys: ironbank {image, project}, upstream, source ({{tag}}), package, go_bumps,
grpc_port, notes.
"""
import os
import re
import subprocess
import tempfile
import time

import bbbuild


def recipe(manifest, dockerfile):
    """Iron Bank's version, binary path (its entrypoint) and user, as a numeric uid:gid.

    Two shapes: Tempo 2.x copies the binary out of upstream's image and says USER 10001:10001;
    3.x builds it and runs as a user its useradd/groupadd lines create."""
    version = (manifest.get("labels") or {}).get("org.opencontainers.image.version")
    entry = re.search(r"^ENTRYPOINT\s+\[\s*\"([^\"]+)\"", dockerfile, re.M)
    copy = re.search(r"^COPY\s+--from=\w+\s+(?:--chown=\S+\s+)?(\S+)\s+(\S+)\s*$", dockerfile, re.M)
    users = re.findall(r"^USER\s+(\S+)", dockerfile, re.M)
    if not version:
        raise SystemExit("Iron Bank's manifest has no org.opencontainers.image.version label; update recipe()")
    if not (entry and copy and users) or copy.group(2) != entry.group(1):
        raise SystemExit("Iron Bank's Dockerfile changed shape (COPY --from / ENTRYPOINT / USER); update recipe()")
    user = users[-1]
    return {"upstream": version, "binary": entry.group(1), "user": user, "uid": numeric_user(user, dockerfile)}


def numeric_user(user, dockerfile):
    """`10001:10001` for USER 10001:10001, or for a name the Dockerfile's useradd/groupadd create."""
    name, _, group = user.partition(":")
    group = group or name

    def ident(cmd, flag, who):
        if who.isdigit():
            return who
        m = re.search(rf"\b{cmd}\b[^\n&;]*?\s{flag}\s+(\d+)[^\n&;]*?\s{re.escape(who)}\s*(?:&&|\\|;|$)", dockerfile, re.M)
        if not m:
            raise SystemExit(f"Iron Bank's Dockerfile runs as {user!r} but no `{cmd} {flag} <id>` names it; update recipe()")
        return m.group(1)
    return f"{ident('useradd', '-u', name)}:{ident('groupadd', '-g', group)}"


def source(r, tag):
    """grafana/tempo's source tarball for `tag`, with the modules to raise."""
    src = bbbuild.go_source(bbbuild.fill(r["source"], {**r, "tag": tag}))
    return {"tag": tag, "source": src["source"], "sha256": src["sha256"], "go": src["go"], "commit": src["commit"],
            "package": r["package"], "go_get": [bbbuild.go_latest(m) for m in r["go_bumps"]]}


def generate(r, a):
    ib_image, ib_project = r["ironbank"]["image"], r["ironbank"]["project"]
    bb_tag = bbbuild.bigbang_tag(ib_image)
    commit, manifest, dockerfile = bbbuild.ironbank_recipe(ib_project, a.ref)
    ib = recipe(manifest, dockerfile)
    if ib["upstream"] != bb_tag:
        print(f"Iron Bank's recipe at {commit[:12]} is for {ib['upstream']}; Big Bang deploys {bb_tag}: building {bb_tag}")
    src = source(r, bb_tag)
    images, repos = bbbuild.go_images(a.org, src["go"])
    spec = {
        "ironbank": {"image": f"{ib_image}:{bb_tag}", "source": f"{bbbuild.REPO1}/dsop/{ib_project}",
                     "commit": commit, **ib},
        "image": {v: {"name": bbbuild.image_name(r, v), "tag": bb_tag, **images[v]}
                  for v in ("standard", "fips")},
        "tempo": src,
        "user": ib["uid"],   # numeric, so runAsNonRoot can check it
        "notes": [bbbuild.fill(n, {**r, "tag": bb_tag}) for n in r["notes"]],
    }
    print(f"tempo-query {bb_tag} (FIPS: {spec['image']['fips']['fips']}): build in {'/'.join(repos['build'].values())} "
          f"-dev, run on {'/'.join(repos['run'].values())}, go.mod Go {src['go']}, raised {' '.join(src['go_get'])}")
    return spec


def docker(*args, check=False):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=300)


# gRPC's health check, by hand: one length-prefixed message (an empty HealthCheckRequest),
# and the answer HealthCheckResponse{status: SERVING} is the message 08 01.
HEALTH_REQUEST = b"\x00\x00\x00\x00\x00"
HEALTH_SERVING = b"\x00\x00\x00\x00\x02\x08\x01"


def grpc_call(address, method, request):
    """POST one gRPC message with curl (HTTP/2 without TLS); returns (body bytes, headers text)."""
    if "HTTP2" not in subprocess.run(["curl", "-V"], capture_output=True, text=True).stdout:
        raise SystemExit("this curl has no HTTP/2 (gRPC needs it); run the smoke test where it does (CI's runner)")
    with tempfile.TemporaryDirectory() as d:
        req, hdr = os.path.join(d, "req"), os.path.join(d, "headers")
        with open(req, "wb") as f:
            f.write(request)
        p = subprocess.run(["curl", "-sS", "--http2-prior-knowledge", "-X", "POST", "-H", "content-type: application/grpc",
                            "-H", "te: trailers", "--data-binary", "@" + req, "-D", hdr, f"http://{address}/{method}"],
                           capture_output=True, timeout=30)
        headers = open(hdr, encoding="utf-8", errors="replace").read() if os.path.exists(hdr) else p.stderr.decode()
        return p.stdout, headers


def serves_grpc(image, port):
    """Start the image with no Tempo behind it; it must serve gRPC and report SERVING."""
    cid = docker("run", "-d", "-p", f"127.0.0.1::{port}", image, check=True).stdout.strip()
    failed = []
    try:
        for _ in range(30):
            out = bbbuild.logs(cid) + docker("logs", cid).stderr
            if "Server starts serving" in out:
                break
            time.sleep(1)
        print("  start: " + out.strip()[-600:].replace("\n", "\n  "))
        if not re.search(rf"Server starts serving.*:{port}", out):
            return [f"start (no 'Server starts serving' on :{port})"]
        address = docker("port", cid, f"{port}/tcp").stdout.split()[0]
        body, headers = grpc_call(address, "grpc.health.v1.Health/Check", HEALTH_REQUEST)
        print(f"  grpc.health.v1.Health/Check: {body.hex()} {' '.join(re.findall(r'grpc-status: *\S+', headers))}")
        if body != HEALTH_SERVING:
            failed.append(f"gRPC health is not SERVING ({body.hex() or headers.strip()[-200:]})")
    finally:
        docker("rm", "-f", cid)
    return failed


def smoke_test(image, variant, spec, r):
    failed = []
    helped = docker("run", "--rm", image, "-h")
    out = (helped.stdout + helped.stderr).strip()
    print("  -h: " + out.replace("\n", "\n  "))
    if not re.search(r"-config\b", out):
        failed.append(f"-h (exit {helped.returncode}, no -config flag)")
    failed += serves_grpc(image, r["grpc_port"])
    user = docker("image", "inspect", "--format", "{{.Config.User}}", image).stdout.strip()
    print(f"  user: {user}")
    if user != spec["user"]:
        failed.append(f"user {user!r}, want {spec['user']}")
    failed += bbbuild.go_build_problems(image, spec["ironbank"]["binary"], variant, spec["image"][variant],
                                       spec["tempo"]["go_get"])
    if failed:
        raise SystemExit(f"{image}: failed {', '.join(failed)}")
