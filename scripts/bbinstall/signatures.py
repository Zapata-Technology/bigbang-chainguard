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
# kubectl's full names: the policy, and the ClusterPolicy releases before Big Bang 4.2.0 wrote.
POLICY_RESOURCE = "imagevalidatingpolicies.policies.kyverno.io"
LEGACY_RESOURCE = "clusterpolicies.kyverno.io"


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
    """A Kyverno ImageValidatingPolicy: every Pod image from cgr.dev/<org> needs Chainguard's
    keyless signature, and each image built here (`built`: (pattern, subject)) builds.yml's,
    the same signers bb-signatures.py accepts in CI. Audit records failures in policy
    reports; Deny refuses the Pod (and, through autogen, its Deployment or Job).

    Not a kyverno.io ClusterPolicy: Kyverno 1.20 (Big Bang 4.2.0) still accepts one but marks
    it Ready=False ("legacy kyverno.io policies are no longer supported") and enforces
    nothing, so unsigned images ran. Kyverno 1.19 serves policies.kyverno.io/v1 too.
    verifyImageSignatures returns 0 for an image outside matchImageReferences, so each
    validation picks its own images by prefix rather than requiring every image > 0."""
    checks = [("chainguard", f"{REGISTRY}/{org}/", SIGNATURE_ISSUERS, signature_identity(org_id))]
    if built:
        checks.append(("builthere", built[0].rstrip("*"), BUILD_SIGNATURE_ISSUER, built[1]))
    every = "(images.containers + images.initContainers + images.ephemeralContainers)"
    return {"apiVersion": "policies.kyverno.io/v1", "kind": "ImageValidatingPolicy",
            "metadata": {"name": SIGNATURE_POLICY, "annotations": {
                "policies.kyverno.io/title": "Chainguard image signatures",
                "policies.kyverno.io/description": f"Images from cgr.dev/{org} must carry Chainguard's signature"
                                                   + (f" under org {org_id}" if org_id else "") + "."}},
            "spec": {
                "validationActions": ["Deny"] if mode == "enforce" else ["Audit"],
                "failurePolicy": "Fail",
                "webhookConfiguration": {"timeoutSeconds": 30},
                "evaluation": {"background": {"enabled": False}},
                "matchConstraints": {"resourceRules": [{
                    "apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"],
                    "resources": ["pods"]}]},
                "matchImageReferences": [{"glob": prefix + "*"} for _, prefix, _, _ in checks],
                # Big Bang puts its pull secret in every package namespace, Kyverno's too: a
                # cluster-scoped policy reads secrets from Kyverno's namespace.
                "credentials": {"secrets": ["private-registry"]},
                "validationConfigurations": {"mutateDigest": False, "verifyDigest": False, "required": True},
                "attestors": [{"name": name, "cosign": {
                    "keyless": {"identities": [{"issuerRegExp": issuer, "subjectRegExp": subject}]},
                    "ctlog": {"url": "https://rekor.sigstore.dev"}}} for name, _, issuer, subject in checks],
                "validations": [{
                    "expression": f"{every}.filter(i, i.startsWith({json.dumps(prefix)}))"
                                  f".all(i, verifyImageSignatures(i, [attestors.{name}]) > 0)",
                    "message": f"an image from {prefix} has no valid signature from its signer"}
                    for name, prefix, _, _ in checks]}}


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


def cluster_signatures(org, org_id, mode, wait, built=None):
    # The ClusterPolicy an earlier release wrote: Kyverno 1.20 no longer enforces it.
    kubectl("delete", LEGACY_RESOURCE, SIGNATURE_POLICY, "--ignore-not-found", check=False)
    if mode == "off":
        kubectl("delete", POLICY_RESOURCE, SIGNATURE_POLICY, "--ignore-not-found", check=False)
        return True
    if not apply_when_ready([signature_policy(org, org_id, mode, built)], POLICY_RESOURCE, "kyverno",
                            ("get", POLICY_RESOURCE, SIGNATURE_POLICY), wait):
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
    out = kubectl("get", POLICY_RESOURCE, SIGNATURE_POLICY, "-o", "json", check=False)
    if not out:
        return f"no ImageValidatingPolicy {SIGNATURE_POLICY}: re-run install"
    status = json.loads(out).get("status", {}).get("conditionStatus", {})
    if status.get("ready") is True:
        return ""
    why = status.get("message") or next((c.get("message") for c in status.get("conditions", [])
                                         if c.get("status") != "True" and c.get("message")), "no status")
    return f"signature policy not ready: {why[:200]}"


def signature_note(state):
    """A line when the signature policy only audits: a cluster installed before enforce
    became the default keeps audit, and unsigned images still run there."""
    if state.get("signatures") != "audit":
        return ""
    return ("image signatures are only audited: Pods with unsigned images still run and Kyverno only "
            "reports them; re-run bb-install.py install --verify-signatures enforce to refuse them")
