"""What runs: the deployed release, what an install would change, and the health report
status, install and rollback end with."""
import json

import yaml

import objstore
from bbcluster import kubectl, read_secret, report, sh
from bbcommon import deep_merge, load_mapping, running_image_problems
from bbinstall.checkout import _build_registry
from bbinstall.common import NAMESPACE, STATE_SECRET, say, step
from bbinstall.credentials import pull_token_note
from bbinstall.signatures import signature_note, signature_problem
from bbinstall.sitefile import enabled_packages
from bbinstall.tls import SELF_SIGNED_CA, tls_problem
from orgtags import REGISTRY

def deployed():
    """(chart version, user-supplied values) of the bigbang release, or (None, {})."""
    out = sh("helm", "list", "-n", NAMESPACE, "-o", "json", check=False, capture=True) or "[]"
    rel = next((r for r in json.loads(out) if r.get("name") == "bigbang"), None)
    if not rel:
        return None, {}
    values = sh("helm", "get", "values", "bigbang", "-n", NAMESPACE, "-o", "json", check=False, capture=True)
    return rel.get("chart", "").rsplit("-", 1)[-1], json.loads(values or "{}") or {}


def plan_lines(old_version, old_values, new_version, new_values, defaults, old_state, new_mode):
    """What an install would change, without values that could be secret: the version,
    FIPS, packages turned on or off, and image repository/tag changes."""
    from release import image_changes   # noqa: E402 (same scripts directory)
    if old_version is None:
        return [f"fresh install of Big Bang {new_version} ({new_mode}): "
                + ", ".join(enabled_packages(deep_merge(json.loads(json.dumps(defaults)), new_values)))]
    lines = []
    if old_version != new_version:
        lines.append(f"Big Bang {old_version} -> {new_version}")
    if old_state.get("mode") and old_state["mode"] != new_mode:
        lines.append(f"images {old_state['mode']} -> {new_mode}")
    before = set(enabled_packages(deep_merge(json.loads(json.dumps(defaults)), old_values)))
    after = set(enabled_packages(deep_merge(json.loads(json.dumps(defaults)), new_values)))
    lines += [f"turn on {p}" for p in sorted(after - before)] + [f"turn off {p}" for p in sorted(before - after)]
    lines += [f"image {c[2:]}" for c in image_changes(yaml.safe_dump(old_values), yaml.safe_dump(new_values))
              if "removed" not in c and "new at" not in c]
    return lines


def status_report(co, fips, problems=(), failed=False):
    hrs = json.loads(kubectl("get", "helmrelease", "-A", "-o", "json", check=False) or '{"items": []}')["items"]
    step(f"HelmReleases ({len(hrs)})")
    bad = 0
    for hr in sorted(hrs, key=lambda h: h["metadata"]["name"]):
        cond = {c["type"]: c for c in hr.get("status", {}).get("conditions", [])}.get("Ready", {})
        ok = cond.get("status") == "True"
        bad += not ok
        say(f"  {'OK  ' if ok else 'FAIL'} {hr['metadata']['name']:28} {'' if ok else cond.get('message', '')[:160]}")
    namespaces = sorted({hr["spec"].get("targetNamespace") or hr["metadata"]["namespace"] for hr in hrs}
                        | {"flux-system"} | ({objstore.NAMESPACE} if co.in_cluster_seaweedfs() else set()))
    images, unhealthy = set(), []
    for ns in namespaces:
        out = kubectl("get", "pod", "-n", ns, "-o", "json", check=False)
        for p in json.loads(out)["items"] if out else []:
            images |= {c["image"] for c in p["spec"].get("initContainers", []) + p["spec"]["containers"]}
            if p["status"].get("phase") == "Succeeded":
                continue
            if not all(s.get("ready") for s in p["status"].get("containerStatuses", [{}])):
                unhealthy.append(ns)
    if unhealthy:
        step("pods that aren't healthy")
        report(sorted(set(unhealthy)))
    mirror = (read_secret(NAMESPACE, STATE_SECRET) or {}).get("registry-mirror")
    if mirror:   # judge the site registry's copies as the images they were copied from
        build = _build_registry(co)
        images = {f"{build if build and i.split('/')[-1].startswith('bigbang-') else REGISTRY + '/' + co.org}"
                  f"/{i[len(mirror.rstrip('/')) + 1:]}" if i.startswith(mirror.rstrip("/") + "/") else i
                  for i in images}
    policy = running_image_problems(images, co.org, _build_registry(co), load_mapping(co.org),
                                    fips=fips) if co.org else []
    step(f"images: {len(images)} running, {len(policy)} not from the conversion")
    for p in policy:
        say(f"  {p}")
    for p in problems:
        say(f"PROBLEM: {p}")
    state = read_secret(NAMESPACE, STATE_SECRET) or {}
    for note in (deployed_version_note(co), pull_token_note(state), signature_note(state)):
        if note:
            say(note)
    tls = tls_problem(state)
    signature = signature_problem(state)
    for p in (tls, signature):
        if p:
            say(f"PROBLEM: {p}")
    access_report(state)
    healthy = not (failed or bad or unhealthy or policy or tls or signature)
    say(f"\n{'HEALTHY' if healthy else 'NOT HEALTHY'}: {len(hrs) - bad}/{len(hrs)} HelmReleases ready")
    return 0 if healthy else 1


def access_report(state):
    """Where the apps are: the gateways' external addresses (DNS for the domain points
    there) and each VirtualService host."""
    out = kubectl("get", "virtualservice", "-A", "-o", "json", check=False)
    hosts = sorted({h for v in (json.loads(out)["items"] if out else []) for h in v["spec"].get("hosts", [])
                    if "." in h and "*" not in h})
    out = kubectl("get", "service", "-n", "istio-gateway", "-o", "json", check=False)
    gateways = []
    for s in json.loads(out)["items"] if out else []:
        addrs = [i.get("ip") or i.get("hostname") for i in s.get("status", {}).get("loadBalancer", {}).get("ingress", [])]
        if s["spec"].get("type") == "LoadBalancer":
            gateways.append(f"{s['metadata']['name']}: {', '.join(a for a in addrs if a) or 'no external address yet'}")
    if not (hosts or gateways):
        return
    step("where to find it")
    for g in gateways:
        say(f"  gateway {g} (point the domain's DNS here)")
    for h in hosts:
        say(f"  https://{h}")
    if state.get("tls-mode") == "self-signed":
        say("  WARNING: TLS is self-signed (the default, for test sites). For production, re-run install "
            "with --tls-issuer <ClusterIssuer> or --tls-cert/--tls-key.")
        say(f"  Until then, trust its CA: kubectl -n cert-manager get secret {SELF_SIGNED_CA} "
            "-o jsonpath='{.data.ca\\.crt}' | base64 -d > bigbang-ca.crt")
    if state.get("keycloak-admin-password"):
        say(f"  Keycloak admin password: kubectl -n {NAMESPACE} get secret {STATE_SECRET} "
            "-o jsonpath='{.data.keycloak-admin-password}' | base64 -d")


def deployed_version_note(co):
    """A line saying the cluster is behind this checkout (re-run install to upgrade)."""
    out = sh("helm", "list", "-n", NAMESPACE, "-o", "json", check=False, capture=True) or "[]"
    rel = next((r for r in json.loads(out) if r.get("name") == "bigbang"), None)
    if not rel:
        return ""
    deployed = rel.get("chart", "").rsplit("-", 1)[-1]
    if deployed and deployed != co.version:
        return (f"update available: the cluster runs Big Bang {deployed}, this checkout has {co.version}; "
                "re-run bb-install.py install to upgrade")
    return ""
