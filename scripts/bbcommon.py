"""Shared helpers: repo settings, chart default values (with subcharts), deep merge, image nodes."""
import io
import os
import re
import tarfile

import yaml

IRONBANK_HOSTS = ("registry1.dso.mil", "registry.dso.mil")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# libyaml's loaders build the same objects as PyYAML's Python ones, ~10x faster: rendered
# charts are tens of MB of YAML. PyYAML's wheels ship libyaml; without it, the Python ones.
SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
BASE_LOADER = getattr(yaml, "CBaseLoader", yaml.BaseLoader)


def safe_load(text):
    """yaml.safe_load, with libyaml when PyYAML has it."""
    return yaml.load(text, Loader=SAFE_LOADER)


def setting(name, arg=None):
    """A repo setting: the command-line value, else $<name>, else the <name> file at the repo root.

    Lines starting with # in the file are comments; a file with only comments is unset.
    """
    if arg:
        return arg.strip()
    if os.environ.get(name):
        return os.environ[name].strip()
    p = os.path.join(ROOT, name)
    if not os.path.exists(p):
        return ""
    lines = [ln.strip() for ln in open(p, encoding="utf-8") if ln.strip() and not ln.lstrip().startswith("#")]
    return lines[0] if lines else ""


# mapping.yaml's sections and image-entry options. A key not listed here is a typo (or a
# new feature that needs adding here, in bb-convert.py and in docs/how-it-works.md); load_mapping
# refuses it instead of silently ignoring it.
MAPPING_SECTIONS = {"images", "hubs", "value_roots", "template_defaults", "overrides", "version_labels",
                    "bash_wait_jobs", "bash_commands", "post_renderers", "chart_registry_builds", "no_post_renderers",
                    "unpinned", "ignore",
                    "scan_reviewed"}
IMAGE_OPTIONS = {
    "cg": str, "fips": (str, type(None)), "version": (str, int, float), "suffix": str,
    "stays": str, "until": str, "build": str, "ref": str, "tag": str, "note": str,
    "fallback": str, "strict": bool, "group": str, "same_patch": str, "version_from": str,
    "min_version": str,
}
# One of these says where the image goes; the rest only adjust it.
IMAGE_TARGETS = {"cg", "stays", "build", "ref"}


def validate_mapping(mapping, source="mapping.yaml"):
    """Raise ValueError listing every unknown section, unknown option, wrong type, or image
    entry with no target (cg, stays, build or ref)."""
    problems = [f"unknown section {k!r}" for k in mapping if k not in MAPPING_SECTIONS]
    for ib, m in (mapping.get("images") or {}).items():
        if isinstance(m, str):
            continue
        if not isinstance(m, dict):
            problems.append(f"images.{ib}: expected a Chainguard name or a mapping, got {m!r}")
            continue
        for k, v in m.items():
            if k not in IMAGE_OPTIONS:
                problems.append(f"images.{ib}: unknown option {k!r}")
            elif not isinstance(v, IMAGE_OPTIONS[k]):
                problems.append(f"images.{ib}.{k}: {v!r} has the wrong type")
        if not IMAGE_TARGETS & set(m):
            problems.append(f"images.{ib}: needs one of {', '.join(sorted(IMAGE_TARGETS))}")
        if m.get("fallback") not in (None, "ironbank"):
            problems.append(f"images.{ib}.fallback: only 'ironbank' is supported")
    for job in (mapping.get("bash_commands") or {}).values():
        for ref in job:
            if set(ref) != {"kind", "name", "container"}:
                problems.append(f"bash_commands entry {ref!r}: needs exactly kind, name, container")
    if problems:
        raise ValueError(f"{source}:\n  " + "\n  ".join(problems))


def load_mapping(org, path=None, overlays=()):
    """mapping.yaml with orgs/<org>.yaml (that org's own choices) and then each of
    `overlays` (e.g. compat/<test>.yaml) applied over it.

    An overlay's `images` entries replace mapping.yaml's entry for the same Iron Bank
    image; its other sections are merged in (a compat file's `test` section is not
    part of the mapping and is dropped).
    """
    path = path or os.path.join(ROOT, "mapping.yaml")
    mapping = yaml.safe_load(open(path, encoding="utf-8"))
    validate_mapping(mapping, path)
    org_file = os.path.join(ROOT, "orgs", f"{org}.yaml")
    for p in ([org_file] if org and os.path.exists(org_file) else []) + list(overlays):
        local = yaml.safe_load(open(p, encoding="utf-8")) or {}
        local.pop("test", None)
        validate_mapping(local, p)
        mapping["images"].update(local.pop("images", {}) or {})
        deep_merge(mapping, local)
    return mapping


def split_ref(ref):
    """'registry1.dso.mil/ironbank/x/y:1.2' -> ('ironbank/x/y', '1.2'); tag may be None."""
    for h in IRONBANK_HOSTS:
        if ref.startswith(h + "/"):
            ref = ref[len(h) + 1:]
    ref = ref.split("@")[0]
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = ref.rsplit(":", 1)
        return repo, tag
    return ref, None


def ironbank_refs(info):
    """Every (Iron Bank repo, tag) a discovered release uses: rendered images, plus images
    set only in values (test jobs that are off by default render nothing)."""
    refs = {split_ref(r) for r in info["rendered"]}
    for n in info["values"]:
        v = n["value"]
        if n["shape"] == "string":
            repo, tag = split_ref(v)
            refs.add((repo, tag or n.get("tag")))
        elif n["shape"] in ("repo", "name"):
            key = "repository" if n["shape"] == "repo" else "image"
            full = f'{v["registry"]}/{v[key]}' if v.get("registry") else v[key]
            refs.add((split_ref(full)[0], v.get("tag", v.get("release"))))
    return refs


def build_registry(arg=None):
    """Where images this repo builds are pushed (BUILD_REGISTRY). Empty: they stay on Iron Bank."""
    reg = setting("BUILD_REGISTRY", arg).rstrip("/")
    if reg.split("/")[0] == "cgr.dev":
        raise SystemExit("BUILD_REGISTRY cannot be on cgr.dev: Chainguard registries do not accept pushes")
    return reg


def load_build(name):
    """builds/<name>/build.yaml, or None if the image has not been generated."""
    p = os.path.join(ROOT, "builds", name, "build.yaml")
    return yaml.safe_load(open(p)) if os.path.exists(p) else None


def build_target(m, registry):
    """(standard ref, fips ref) for a mapping.yaml `build:` entry, or None if it stays on Iron Bank."""
    spec = load_build(m["build"])
    if not (registry and spec):
        return None
    return tuple(f"{registry}/{spec['image'][v]['name']}:{spec['image'][v]['tag']}" for v in ("standard", "fips"))


# Images kind itself runs (CNI, DNS, storage): cluster plumbing, not Big Bang.
CLUSTER_IMAGES = ("registry.k8s.io/", "docker.io/kindest/")


def image_name(ref):
    """'cgr.dev/org/kubectl-fips:1.37-dev@sha256:..' -> 'kubectl-fips'."""
    return ref.split("@")[0].rsplit("/", 1)[-1].split(":")[0]


def running_image_problems(images, org, registry, mapping, fips=False):
    """Why each image a cluster runs breaks the conversion's promise ([] if none).

    Allowed: the org (cgr.dev/<org>), BUILD_REGISTRY, an Iron Bank image the mapping
    keeps there (stays, fallback, group), and kind's own images. With fips, every org
    and build image must be a -fips one unless the mapping says it has no FIPS variant.
    """
    imgs = mapping["images"]
    keep = {k for k, v in imgs.items() if isinstance(v, dict)
            and ("stays" in v or v.get("fallback") == "ironbank" or v.get("group") or "build" in v)}
    no_fips = {v["cg"] for v in imgs.values() if isinstance(v, dict) and "cg" in v and v.get("fips", "x") is None}
    problems = []
    for ref in sorted(set(images)):
        ours = ref.startswith(f"cgr.dev/{org}/") or bool(registry) and ref.startswith(registry + "/")
        if ours:
            if fips and not image_name(ref).endswith("-fips") and image_name(ref) not in no_fips:
                problems.append(f"{ref}: not a FIPS image")
        elif ref.startswith(IRONBANK_HOSTS):
            if split_ref(ref)[0] not in keep:
                problems.append(f"{ref}: Iron Bank image the mapping converts")
        elif not ref.startswith(CLUSTER_IMAGES):
            problems.append(f"{ref}: not from cgr.dev/{org}, {registry or 'a build registry'} or Iron Bank")
    return problems


# Chainguard's signers. Org images are signed by Chainguard's issuer, under the org's ID;
# Chainguard's public images from GitHub Actions in its chainguard-images organization.
# bb-signatures.py (CI) and bb-install.py's Kyverno policy (the cluster) both use these.
SIGNATURE_ISSUERS = r"^https://(issuer\.enforce\.dev|token\.actions\.githubusercontent\.com)$"


def signature_identity(org_id):
    org = re.escape(org_id) if org_id else r"[0-9a-f]+"
    return rf"^(https://issuer\.enforce\.dev/{org}/|https://github\.com/chainguard-images/)"


# The images this repo builds: builds.yml signs them keyless from GitHub Actions, on main.
BUILD_SIGNATURE_ISSUER = r"^https://token\.actions\.githubusercontent\.com$"


def build_signer(arg=None):
    """owner/repo whose builds.yml signs the images this repo builds: the BUILD_SIGNER
    setting, else $GITHUB_REPOSITORY (CI), else '' (then derived from the registry)."""
    return setting("BUILD_SIGNER", arg) or os.environ.get("GITHUB_REPOSITORY", "")


def build_signature_identity(registry, signer=""):
    """Subject regexp of builds.yml's keyless signature on main for images in `registry`
    (<host>/<path>): `signer`'s workflow exactly, else, on ghcr.io/<owner>, that of any repo
    of the owner (ghcr.io names are lowercase, GitHub's aren't: matched without case).
    None when neither is known: those images then go unchecked."""
    if signer:
        repo = re.escape(signer)
    elif registry.startswith("ghcr.io/") and registry.count("/") >= 1:
        repo = f"(?i:{re.escape(registry.split('/')[1])})/[^/]+"
    else:
        return None
    return rf"^https://github\.com/{repo}/\.github/workflows/builds\.yml@refs/heads/main$"


def is_bigbang4(values):
    """True for Big Bang 4 values (packages.<name>); 3.x values have `addons:`."""
    return "addons" not in values and isinstance(values.get("packages"), dict)


def bigbang_packages(values):
    """{Big Bang key: package values} for every package in Big Bang values. Big Bang 4
    keeps every package under `packages:` (key `packages.<name>`); 3.x kept them at the
    top level and under `addons:`, among other settings, so there a package is a key with
    a git source. 3.x also had `packages:` (custom packages only), so `addons:` marks it."""
    if is_bigbang4(values):
        return {f"packages.{k}": x for k, x in values["packages"].items() if isinstance(x, dict)}
    found = {k: x for k, x in values.items() if k != "wrapper"}
    found.update({f"addons.{k}": x for k, x in (values.get("addons") or {}).items()})
    return {k: x for k, x in found.items()
            if isinstance(x, dict) and isinstance(x.get("git"), dict) and x["git"].get("repo")}


def deep_merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            deep_merge(a[k], v)
        else:
            a[k] = v
    return a


class _Chart:
    """A chart directory or a .tgz, read without unpacking to disk."""

    def __init__(self, path=None, tar=None, prefix=""):
        self.path, self.tar, self.prefix = path, tar, prefix

    def read(self, name):
        if self.path:
            p = os.path.join(self.path, name)
            return open(p, encoding="utf-8").read() if os.path.exists(p) else None
        try:
            return self.tar.extractfile(self.prefix + name).read().decode("utf-8")
        except (KeyError, AttributeError):
            return None

    def subcharts(self):
        if self.path:
            d = os.path.join(self.path, "charts")
            for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
                p = os.path.join(d, n)
                if n.endswith(".tgz"):
                    t = tarfile.open(p)
                    yield _Chart(tar=t, prefix=t.getnames()[0].split("/")[0] + "/")
                elif os.path.isdir(p):
                    yield _Chart(path=p)
        else:
            seen = set()
            base = self.prefix + "charts/"
            for m in self.tar.getmembers():
                if not m.name.startswith(base):
                    continue
                rest = m.name[len(base):]
                top = rest.split("/")[0]
                if top in seen:
                    continue
                seen.add(top)
                if top.endswith(".tgz") and "/" not in rest:
                    t = tarfile.open(fileobj=io.BytesIO(self.tar.extractfile(m).read()))
                    yield _Chart(tar=t, prefix=t.getnames()[0].split("/")[0] + "/")
                elif "/" in rest:
                    yield _Chart(tar=self.tar, prefix=base + top + "/")


def chart_defaults(path):
    """Default values of a chart with every subchart's defaults under its alias."""
    return _defaults(_Chart(path=path))


def _defaults(chart):
    meta = safe_load(chart.read("Chart.yaml") or "{}") or {}
    values = safe_load(chart.read("values.yaml") or "{}") or {}
    aliases = {d["name"]: d.get("alias", d["name"]) for d in meta.get("dependencies") or []}
    for sub in chart.subcharts():
        sub_meta = safe_load(sub.read("Chart.yaml") or "{}") or {}
        name = sub_meta.get("name")
        if not name or sub_meta.get("type") == "library":
            continue
        key = aliases.get(name, name)
        sub_values = _defaults(sub)
        sub_values.pop("global", None)
        values[key] = deep_merge(sub_values, values.get(key) or {})
    return values


def _is_ironbank(s):
    return isinstance(s, str) and (s.startswith(IRONBANK_HOSTS) or s.startswith("ironbank/")
                                   or "/ironbank/" in s or s.startswith(("gitlab/gitlab-org", "registry1")))


def image_nodes(values, path=()):
    """Yield (path, shape, node) for every Iron Bank image setting in a values tree.

    shapes: "string"   key: registry1.dso.mil/ironbank/x:tag  (tag optional)
            "registry" chart-wide registry setting (global.imageRegistry, ...)
            "repo"     {registry?, repository, tag?, digest?}
            "hub"      {hub, tag?}  (istio style: image name comes from elsewhere)
            "name"     {image|name: <repo>, tag}
    """
    if isinstance(values, dict):
        hit = None
        reg = values.get("registry")
        if _is_ironbank(values.get("repository")) or (
                isinstance(reg, str) and reg.startswith(IRONBANK_HOSTS) and isinstance(values.get("repository"), str)):
            hit = "repo"
        elif _is_ironbank(values.get("hub")):
            hit = "hub"
        elif _is_ironbank(values.get("image")) and "tag" in values and isinstance(values.get("tag"), str):
            hit = "name"
        if hit:
            yield path, hit, values
            # Kyverno: per-image defaultRegistry outranks a blank global registry
            if isinstance(values.get("defaultRegistry"), str) and values["defaultRegistry"].startswith(IRONBANK_HOSTS):
                yield path + ("defaultRegistry",), "registry", values["defaultRegistry"]
        elif not isinstance(values.get("repository"), str) and not isinstance(values.get("image"), str):
            # chart-wide registry (global.imageRegistry, global.image.registry, registry):
            # wins over per-image settings in many charts
            for k in ("registry", "imageRegistry"):
                if isinstance(values.get(k), str) and values[k].startswith(IRONBANK_HOSTS):
                    yield path + (k,), "registry", values[k]
        for k, v in values.items():
            if hit and k in ("repository", "registry", "defaultRegistry", "tag", "release", "hub", "image", "digest"):
                continue
            yield from image_nodes(v, path + (k,))
    elif isinstance(values, list):
        for i, v in enumerate(values):
            yield from image_nodes(v, path + (i,))
    elif _is_ironbank(values) and ("/" in values) and " " not in values:
        yield path, "string", values
