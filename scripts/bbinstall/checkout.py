"""What this checkout (or --overlays DIR) deploys: the overlays, Flux and SeaweedFS files,
the org and registries their headers name, and a copy rewritten for a site's registry."""
import atexit
import os
import re
import shutil
import tempfile

import objstore
from bbcommon import ROOT
from orgtags import REGISTRY

class Checkout:
    """The overlays, Flux and SeaweedFS files this checkout (or --overlays DIR) holds."""

    def __init__(self, root):
        self.root = root
        std = os.path.join(root, "overlays", "values-chainguard.yaml")
        if not os.path.exists(std):
            raise SystemExit(f"{std} is missing: generate the overlays first (scripts/bb-run.py)")
        header = open(std, encoding="utf-8").read(800)
        m = re.search(r"Generated for (cgr\.dev/[^\s:]+)", header)
        self.registry = m.group(1) if m else None
        self.org = self.registry.split("/", 1)[1] if self.registry else None
        m = re.search(r"Big Bang (\d+\.\d+\.\d+)", header)
        self.version = m.group(1) if m else open(os.path.join(ROOT, "BIGBANG_VERSION")).read().strip()
        release = os.path.join(root, "RELEASE")    # in release archives (scripts/release.py)
        self.release = open(release, encoding="utf-8").read().strip() if os.path.exists(release) else None
        self.object_storage = os.path.join(root, "overlays", "values-object-storage.yaml")
        self.s3 = None
        if os.path.exists(self.object_storage):
            head = open(self.object_storage, encoding="utf-8").read(600)
            m = re.search(r"S3 object storage at (\S+) instead of MinIO, for: ([^.\n]+)", head)
            if m:
                self.s3 = {"endpoint": m.group(1), "packages": [p.strip() for p in m.group(2).split(",")]}

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def overlays(self, fips):
        files = [self.path("overlays", "values-chainguard.yaml")]
        if fips:
            files.append(self.path("overlays", "values-chainguard-fips.yaml"))
        if self.s3:
            files.append(self.object_storage)
        return files

    def in_cluster_seaweedfs(self):
        return bool(self.s3) and self.s3["endpoint"] == objstore.DEFAULT_ENDPOINT


def mirrored_checkout(co, mirror):
    """A copy of the overlays, Flux and SeaweedFS files with cgr.dev/<org> and the build
    registry replaced by the site's registry prefix (an air-gapped site, bb-airgap.py).
    Headers ("Generated for cgr.dev/<org>:") keep the org, so the rest reads the same."""
    d = tempfile.mkdtemp(prefix="bb-install-mirror-")
    atexit.register(shutil.rmtree, d, True)
    hosts = [f"{REGISTRY}/{co.org}"] + ([_build_registry(co)] if _build_registry(co) else [])
    rx = re.compile("(" + "|".join(re.escape(h) for h in hosts) + r")(?=/|['\"]?\s*$)", re.M)
    for name in ("overlays", "flux", "flux-fips", "seaweedfs", "seaweedfs-fips", "RELEASE"):
        src = co.path(name)
        files = [src] if os.path.isfile(src) else [os.path.join(dp, f) for dp, _, fn in os.walk(src) for f in fn]
        for s in files:
            t = os.path.join(d, os.path.relpath(s, co.root))
            os.makedirs(os.path.dirname(t), exist_ok=True)
            with open(t, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(rx.sub(mirror.rstrip("/"), open(s, encoding="utf-8").read()))
    return Checkout(d)


def _build_registry_host(co):
    """The registry host of the images this repo builds, if the overlays use any."""
    text = open(co.path("overlays", "values-chainguard.yaml"), encoding="utf-8").read()
    hosts = {m.split("/")[0] for m in re.findall(r"\b([a-z0-9.-]+\.[a-z]{2,}/[\w./-]+/bigbang-[\w-]+)", text)}
    hosts.discard("cgr.dev")
    return sorted(hosts)[0] if hosts else None


def _build_registry(co):
    text = open(co.path("overlays", "values-chainguard.yaml"), encoding="utf-8").read()
    m = re.search(r"\b([a-z0-9.-]+\.[a-z]{2,}/[\w.-]+)/bigbang-[\w-]+", text)
    return m.group(1) if m else ""
