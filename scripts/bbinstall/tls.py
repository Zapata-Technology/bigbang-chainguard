"""TLS for Big Bang's public gateway: which certificate it serves (site file, own, a
ClusterIssuer or a self-signed CA), the cert-manager objects, and whether it was issued."""
import json

from bbcluster import kubectl
from bbinstall.common import apply_when_ready, say
from bbinstall.sitefile import package_values

GATEWAY_CERT = "public-cert"           # the Secret Big Bang's public gateway serves
SELF_SIGNED_CA = "bb-install-ca"       # cert-manager/<this>: the self-signed CA to trust


def _site_sets_gateway_certs(site):
    public = (((package_values(site, "istioGateway").get("values") or {}).get("gateways") or {}).get("public") or {})
    return bool(public.get("gatewayCerts"))


def tls_choice(a, creds, site, enabled):
    """How the public gateway gets its certificate, or None without an Istio gateway:
    site (the site file sets gatewayCerts), own (--tls-cert/--tls-key), issuer (a
    cert-manager ClusterIssuer the site runs: ACME, Vault, a company CA) or self-signed (a
    CA cert-manager makes; the default). The choice is kept so re-runs don't need the flags."""
    if not {"packages.istiod", "packages.istioGateway"} <= set(enabled):
        return None
    if _site_sets_gateway_certs(site):
        return "site"
    if a.tls_cert or a.tls_key:
        if not (a.tls_cert and a.tls_key):
            raise SystemExit("--tls-cert and --tls-key go together")
        creds.values.update({"tls-mode": "own", "tls-cert": open(a.tls_cert, encoding="utf-8").read(),
                             "tls-key": open(a.tls_key, encoding="utf-8").read()})
    elif a.tls_issuer:
        creds.values.update({"tls-mode": "issuer", "tls-issuer": a.tls_issuer})
    elif a.tls_self_signed:
        creds.values["tls-mode"] = "self-signed"
    mode = creds.values.setdefault("tls-mode", "self-signed")
    if mode != "own":
        creds.values.pop("tls-cert", None)
        creds.values.pop("tls-key", None)
    if mode != "issuer":
        creds.values.pop("tls-issuer", None)
    if mode in ("issuer", "self-signed") and "packages.certManager" not in enabled:
        raise SystemExit(f"TLS {mode} uses cert-manager: leave certManager on, or pass --tls-cert and --tls-key")
    return mode


def tls_manifests(domain, mode, issuer=None):
    """cert-manager objects for the gateway certificate: a self-signed CA and its issuer
    (self-signed), then the wildcard Certificate Big Bang's public gateway serves."""
    docs = []
    if mode == "self-signed":
        issuer = SELF_SIGNED_CA
        docs += [
            {"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer", "metadata": {"name": "bb-install-selfsigned"},
             "spec": {"selfSigned": {}}},
            {"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
             "metadata": {"name": SELF_SIGNED_CA, "namespace": "cert-manager"},
             "spec": {"isCA": True, "commonName": f"Big Bang {domain} CA", "secretName": SELF_SIGNED_CA,
                      "duration": "87600h", "privateKey": {"algorithm": "ECDSA", "size": 256},
                      "issuerRef": {"name": "bb-install-selfsigned", "kind": "ClusterIssuer"}}},
            {"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer", "metadata": {"name": SELF_SIGNED_CA},
             "spec": {"ca": {"secretName": SELF_SIGNED_CA}}},
        ]
    docs.append({"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
                 "metadata": {"name": GATEWAY_CERT, "namespace": "istio-gateway"},
                 "spec": {"secretName": GATEWAY_CERT, "dnsNames": [f"*.{domain}", domain],
                          "duration": "2160h", "renewBefore": "360h",
                          "privateKey": {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
                          "issuerRef": {"name": issuer, "kind": "ClusterIssuer"}}})
    return docs


def gateway_tls(domain, mode, issuer, wait):
    """Apply the cert-manager objects once Big Bang has installed cert-manager and made the
    istio-gateway namespace (never pre-create it: Helm won't adopt it). With a certificate
    from values (own, site), remove a Certificate an earlier run left, so they don't fight."""
    if mode in ("own", "site"):
        kubectl("-n", "istio-gateway", "delete", "certificate", GATEWAY_CERT, "--ignore-not-found", check=False)
        say(f"the public gateway serves {'your certificate' if mode == 'own' else 'the site file certificate'}")
        return True
    if not apply_when_ready(tls_manifests(domain, mode, issuer), "certificates.cert-manager.io", "istio-gateway",
                            ("-n", "istio-gateway", "get", "certificate", GATEWAY_CERT), wait):
        say("warning: cert-manager wasn't ready to issue the gateway certificate; re-run install")
        return False
    say(f"cert-manager issues *.{domain} from {'a self-signed CA' if mode == 'self-signed' else issuer} "
        f"into istio-gateway/{GATEWAY_CERT}")
    return True


def tls_problem(state):
    """Why the gateway certificate cert-manager issues isn't Ready, or ''."""
    if state.get("tls-mode") not in ("issuer", "self-signed"):
        return ""
    out = kubectl("-n", "istio-gateway", "get", "certificate", GATEWAY_CERT, "-o", "json", check=False)
    if not out:
        return f"no istio-gateway/{GATEWAY_CERT} Certificate: re-run install"
    ready = {c["type"]: c for c in json.loads(out).get("status", {}).get("conditions", [])}.get("Ready", {})
    if ready.get("status") == "True":
        return ""
    return f"gateway certificate not issued yet: {ready.get('message', 'no status')[:200]}"
