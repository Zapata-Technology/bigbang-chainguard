"""Which Flux release a set of Flux controller versions belongs to.

Big Bang installs Flux from its base/flux kustomization: an upstream Flux release's
install manifest (CRDs and controllers) with Big Bang's patches. When the org's
Chainguard controllers are a newer minor than that release's, they need the CRDs of
their own release (notification-controller 1.10 does not start on Flux 2.9's CRDs),
so the overlay installs that release instead, with the same Big Bang patches.
"""
import json
import os
import re
import urllib.request

import yaml

from orgtags import _open

CONTROLLERS = ("source-controller", "kustomize-controller", "helm-controller", "notification-controller")
# In Flux's install manifest but not in Big Bang's Flux; the overlay removes them.
NOT_IN_BIGBANG = ("image-automation-controller", "image-reflector-controller", "source-watcher")
API = "https://api.github.com/repos/fluxcd/flux2/releases?per_page=30"
RAW = "https://raw.githubusercontent.com/fluxcd/flux2/{tag}/manifests/bases/{controller}/kustomization.yaml"
INSTALL = "https://github.com/fluxcd/flux2/releases/download/{tag}/install.yaml"
# release_for() answers, per set of versions: bb-convert asks for the standard and the FIPS
# controllers (the same versions), and its prefetch's dry run asks too. Each lookup reads
# up to 120 files from GitHub.
_RELEASES = {}


def _get(url):
    headers = {"Accept": "application/vnd.github+json"} if "api.github.com" in url else {}
    if os.environ.get("GITHUB_TOKEN") and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    with _open(urllib.request.Request(url, headers=headers), 60) as r:
        return r.read().decode()


def _line(version):
    return tuple(int(x) for x in version.lstrip("v").split(".")[:2])


def release_for(versions):
    """The newest Flux release whose controllers are on the same MAJOR.MINOR as
    `versions` ({controller: version}); returns (tag, {controller: version}) or None."""
    key = tuple(sorted(versions.items()))
    if key not in _RELEASES:
        _RELEASES[key] = _release_for(versions)
    return _RELEASES[key]


def _release_for(versions):
    for rel in json.loads(_get(API)):
        if rel.get("prerelease") or rel.get("draft"):
            continue
        tag, found = rel["tag_name"], {}
        for c in CONTROLLERS:
            m = re.search(r"releases/download/v(\d+\.\d+\.\d+)/", _get(RAW.format(tag=tag, controller=c)))
            if not m:
                break
            found[c] = m.group(1)
        if len(found) == len(CONTROLLERS) and all(_line(found[c]) == _line(versions[c]) for c in versions):
            return tag, found
    return None


def remove_extras():
    """Kustomize patches deleting the controllers Big Bang doesn't run."""
    return [{"patch": yaml.safe_dump({"$patch": "delete", "apiVersion": "apps/v1", "kind": "Deployment",
                                      "metadata": {"name": n, "namespace": "flux-system"}})}
            for n in NOT_IN_BIGBANG]


def seccomp_patch():
    """A kustomize patch giving every Flux controller the RuntimeDefault seccomp profile.
    Big Bang's patch replaces the container securityContext, which drops Flux's own
    seccompProfile, and Pod Security Admission's `restricted` level (RKE2's CIS profile
    enforces it) then refuses every controller pod. Set on the pod, after Big Bang's patch."""
    return {"patch": yaml.safe_dump({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "all"},
                                     "spec": {"template": {"spec": {"securityContext": {
                                         "seccompProfile": {"type": "RuntimeDefault"}}}}}}),
            "target": {"kind": "Deployment"}}


def bigbang_patches(bigbang_version, work=None):
    """Big Bang's base/flux patches (pull secret, security context, resources)."""
    local = os.path.join(work, "bigbang", "base", "flux", "kustomization.yaml") if work else None
    if local and os.path.exists(local):
        text = open(local, encoding="utf-8").read()
    else:
        text = _get(f"https://repo1.dso.mil/big-bang/bigbang/-/raw/{bigbang_version}/base/flux/kustomization.yaml")
    return yaml.safe_load(text).get("patches", [])
