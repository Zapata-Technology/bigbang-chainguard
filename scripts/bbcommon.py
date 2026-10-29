"""Shared helpers: repo settings, chart default values (with subcharts), deep merge, image nodes."""
import io
import os
import tarfile

import yaml

IRONBANK_HOSTS = ("registry1.dso.mil", "registry.dso.mil")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def load_mapping(org, path=None):
    """mapping.yaml with orgs/<org>.yaml (that org's own choices) applied over it.

    An org file's `images` entries replace mapping.yaml's entry for the same Iron Bank
    image; its other sections are merged in.
    """
    mapping = yaml.safe_load(open(path or os.path.join(ROOT, "mapping.yaml")))
    p = os.path.join(ROOT, "orgs", f"{org}.yaml")
    if org and os.path.exists(p):
        local = yaml.safe_load(open(p)) or {}
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
    meta = yaml.safe_load(chart.read("Chart.yaml") or "{}") or {}
    values = yaml.safe_load(chart.read("values.yaml") or "{}") or {}
    aliases = {d["name"]: d.get("alias", d["name"]) for d in meta.get("dependencies") or []}
    for sub in chart.subcharts():
        sub_meta = yaml.safe_load(sub.read("Chart.yaml") or "{}") or {}
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
