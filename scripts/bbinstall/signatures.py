"""The Kyverno policy that checks Chainguard's signature on org images (and builds.yml's on
the images built here): the mode, the policy, and whether it works."""
import json
import os

from bbcluster import kubectl
from bbcommon import BUILD_SIGNATURE_ISSUER, SIGNATURE_ISSUERS, build_signature_identity, build_signer, \
    signature_identity
from bbinstall.checkout import _build_registry
from bbinstall.common import apply_when_ready, say
from orgtags import REGISTRY

SIGNATURE_POLICY = "bb-install-chainguard-signatures"


def signature_choice(a, creds, enabled):
    """off, audit or enforce (remembered), and the org ID signatures must be under. With
    Kyverno on and nothing chosen, enforce: a Pod whose org image Chainguard didn't sign (or
    whose image built here builds.yml didn't) is refused; audit only reports it in Kyverno's
    policy reports. Off by default when Kyverno is off or the site is disconnected
    (--registry-mirror: no Sigstore to check against). A cluster keeps the mode it was
    installed with."""
    if a.verify_signatures:
        creds.values["signatures"] = a.verify_signatures
    if a.org_id:
        creds.values["org-id"] = a.org_id
    default = "enforce" if "packages.kyverno" in enabled and not getattr(a, "registry_mirror", None) else "off"
    mode = creds.values.setdefault("signatures", default)
    if mode != "off" and "packages.kyverno" not in enabled:
        raise SystemExit(f"--verify-signatures {mode} uses Kyverno: leave kyverno on, or pass --verify-signatures off")
    return mode, creds.values.get("org-id") or os.environ.get("CHAINGUARD_ORG_ID")


def built_signer(a, creds, co):
    """(images, subject regexp) for the images this repo builds: <build registry>/bigbang-*
    and builds.yml's signature on main from --build-signer (remembered), $BUILD_SIGNER, or
    any repo of the ghcr.io owner. None when the overlays use none or no signer is known."""
    if a.build_signer is not None:
        creds.values["build-signer"] = a.build_signer
    registry = _build_registry(co)
    subject = registry and build_signature_identity(registry, creds.values.get("build-signer") or build_signer())
    if registry and not subject:
        say(f"note: the images built here ({registry}) aren't signature-checked: pass --build-signer <owner/repo>")
    return (f"{registry}/bigbang-*", subject) if subject else None


def signature_policy(org, org_id, mode, built=None):
    """A Kyverno ClusterPolicy: every Pod image from cgr.dev/<org> needs Chainguard's
    keyless signature, and each image built here (`built`: (pattern, subject)) builds.yml's,
    the same signers bb-signatures.py accepts in CI. Audit records failures in policy
    reports; Enforce refuses the Pod."""
    action = "Enforce" if mode == "enforce" else "Audit"
    rules = [_verify_rule("chainguard-signature", f"{REGISTRY}/{org}/*", action, SIGNATURE_ISSUERS,
                          signature_identity(org_id))]
    if built:
        rules.append(_verify_rule("built-here-signature", built[0], action, BUILD_SIGNATURE_ISSUER, built[1]))
    return {"apiVersion": "kyverno.io/v1", "kind": "ClusterPolicy",
            "metadata": {"name": SIGNATURE_POLICY, "annotations": {
                "policies.kyverno.io/title": "Chainguard image signatures",
                "policies.kyverno.io/description": f"Images from cgr.dev/{org} must carry Chainguard's signature"
                                                   + (f" under org {org_id}" if org_id else "") + "."}},
            "spec": {"background": False, "webhookTimeoutSeconds": 30, "rules": rules}}


def signature_egress_values():
    """Big Bang values letting Kyverno's admission controller reach Sigstore and the registry
    on 443. Big Bang's network policies allow it only the API server and registry1, so on a
    CNI that enforces them (RKE2's Canal) every verification timed out fetching Sigstore's
    trust root and Enforce refused every Pod (rke2.yml). The hosts sit behind CDNs, so no
    narrower block than port 443."""
    return {"packages": {"kyverno": {"values": {"bb-common": {"networkPolicies": {"egress": {
        "definitions": {"signature-verification": {"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
                                                   "ports": [{"port": 443, "protocol": "TCP"}]}},
        "from": {"kyverno-admission-controller": {"to": {"definition": {"signature-verification": True}}}}}}}}}}}


def _verify_rule(name, pattern, action, issuer, subject):
    return {"name": name, "match": {"any": [{"resources": {"kinds": ["Pod"]}}]},
            "verifyImages": [{
                "imageReferences": [pattern], "failureAction": action,
                "mutateDigest": False, "verifyDigest": False, "required": True,
                # Big Bang puts its pull secret in every package namespace, Kyverno's too.
                "imageRegistryCredentials": {"secrets": ["private-registry"]},
                "attestors": [{"entries": [{"keyless": {
                    "issuerRegExp": issuer, "subjectRegExp": subject,
                    "rekor": {"url": "https://rekor.sigstore.dev"}}}]}]}]}


def cluster_signatures(org, org_id, mode, wait, built=None):
    if mode == "off":
        kubectl("delete", "clusterpolicy", SIGNATURE_POLICY, "--ignore-not-found", check=False)
        return True
    if not apply_when_ready([signature_policy(org, org_id, mode, built)], "clusterpolicies.kyverno.io", "kyverno",
                            ("get", "clusterpolicy", SIGNATURE_POLICY), wait):
        say("warning: Kyverno wasn't ready for the signature policy; re-run install")
        return False
    say(f"Kyverno checks Chainguard's signature on cgr.dev/{org} images ({mode}"
        + (f", org ID {org_id}" if org_id else ", any Chainguard signer: pass --org-id to require your org") + ")"
        + (f", and builds.yml's on {built[0]}" if built else ""))
    return True


def signature_problem(state):
    """Why the signature policy isn't working, or ''."""
    if state.get("signatures", "off") == "off":
        return ""
    out = kubectl("get", "clusterpolicy", SIGNATURE_POLICY, "-o", "json", check=False)
    if not out:
        return f"no ClusterPolicy {SIGNATURE_POLICY}: re-run install"
    ready = {c["type"]: c for c in json.loads(out).get("status", {}).get("conditions", [])}.get("Ready", {})
    return "" if ready.get("status") == "True" else \
        f"signature policy not ready: {ready.get('message', 'no status')[:200]}"


def signature_note(state):
    """A line when the signature policy only audits: a cluster installed before enforce
    became the default keeps audit, and unsigned images still run there."""
    if state.get("signatures") != "audit":
        return ""
    return ("image signatures are only audited: Pods with unsigned images still run and Kyverno only "
            "reports them; re-run bb-install.py install --verify-signatures enforce to refuse them")
