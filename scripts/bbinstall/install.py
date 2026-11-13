"""install: credentials, Flux, SeaweedFS, Big Bang (helm or --gitops), then TLS and the
signature policy; and --plan, what it would change."""
import json
import os
import secrets
import sys
import tempfile
import time

import yaml

import objstore
from bbcluster import apply_secret, dockerconfig, hr_namespaces, install_flux, kubectl, read_secret, \
    reconcile_helmreleases, report, sh, wait_ready
from bbcommon import deep_merge
from bbinstall.chart import chart_ref, git_mirror_values
from bbinstall.checkout import _build_registry_host
from bbinstall.common import NAMESPACE, STATE_SECRET, confirm, say, step
from bbinstall.credentials import Credentials, pull_token_maker
from bbinstall.gitops import GITOPS_SECRET, write_gitops
from bbinstall.images import build_images, deployed_images, image_problems, pull_check
from bbinstall import releases
from bbinstall.preflight import prepare
from bbinstall.signatures import built_signer, cluster_signatures, signature_choice, signature_egress_values
from bbinstall.sitefile import package_values
from bbinstall.status import deployed, plan_lines, status_report
from bbinstall.tls import GATEWAY_CERT, gateway_tls, tls_choice

def seaweedfs(co, fips, pull, s3):
    """Deploy the generated seaweedfs/ kustomization with its two Secrets; wait for buckets."""
    ns = objstore.NAMESPACE
    with tempfile.TemporaryDirectory(prefix="bb-install-") as d:
        cfg = os.path.join(d, "config.json")
        json.dump(dockerconfig(pull), open(cfg, "w"))
        apply_secret(ns, "private-registry", "generic", "--type=kubernetes.io/dockerconfigjson",
                     f"--from-file=.dockerconfigjson={cfg}")
        s3json = os.path.join(d, "s3.json")
        open(s3json, "w", encoding="utf-8").write(objstore.s3_config(*s3))
        apply_secret(ns, "seaweedfs-s3", "generic", f"--from-file=s3.json={s3json}")
    # The bucket Job's pod template can't change once created, and every new SeaweedFS digest
    # (an org patch, a new release) changes its image: replace it. The new Job creates only
    # the buckets that are missing (objstore.seaweedfs_files).
    kubectl("-n", ns, "delete", "job", "seaweedfs-buckets", "--ignore-not-found", "--wait=true", check=False)
    sh("kubectl", "apply", "-k", co.path("seaweedfs-fips" if fips else "seaweedfs"), capture=True)
    kubectl("-n", ns, "rollout", "status", "deploy/seaweedfs", "--timeout=600s")
    job = kubectl("-n", ns, "wait", "job/seaweedfs-buckets", "--for=condition=complete", "--timeout=600s",
                  check=False)
    if job is None or "condition met" not in job:
        logs = kubectl("-n", ns, "logs", "job/seaweedfs-buckets", "--tail=30", check=False) or ""
        raise SystemExit(f"SeaweedFS's bucket Job didn't complete:\n{logs}")


def object_storage_values(co, enabled, s3):
    """Credential values for the enabled packages that use S3, and only those (Big Bang
    creates Mimir's credential Secret whenever its keys are set, even with Mimir off)."""
    spec = objstore.load_spec()
    extra = {p for p in co.s3["packages"] if spec["packages"].get(p, {}).get("optional")}
    _, paths, _, _ = objstore.build(spec, objstore.tokens(co.s3["endpoint"], "us-east-1", "bigbang"), extra)
    names = {k.split(".")[-1] for k in enabled}
    return objstore.credentials_template(objstore.credentials_for(paths, names), *s3)


def install(a):
    co, site, defaults, enabled, problems, notes = prepare(a)
    if problems:
        raise SystemExit("\nnot installing:\n  " + "\n  ".join(problems))
    if a.plan:
        return plan(a, co, site, defaults, enabled)
    interactive = sys.stdin.isatty() and not a.yes
    context = (kubectl("config", "current-context", check=False) or "").strip() or "(no context)"
    say(f"cluster: kubectl context {context}")
    if interactive and read_secret(NAMESPACE, STATE_SECRET) is None and \
            not confirm(f"Install Big Bang on {context}?", default=False):
        raise SystemExit("not installing (--yes skips this question)")

    step("credentials")
    creds = Credentials(site, interactive=interactive)
    # Each login is tested with a pull now: one the cluster can't use otherwise shows up as
    # ImagePullBackOff only when the wait times out.
    checks = not a.no_pull_check
    if a.git_mirror is not None:
        creds.values["git-mirror"] = a.git_mirror      # "" turns the mirror off
    a.git_mirror = a.git_mirror or None
    if a.git_mirror:
        say(f"package git repositories from {a.git_mirror} instead of repo1")
    if a.new_pull_token:
        creds.forget("cgr")
    if a.registry_mirror is not None:
        creds.values["registry-mirror"] = a.registry_mirror or ""
    if a.registry_mirror:
        # Air-gapped: every image comes from the site's registry (bb-airgap.py lists them).
        host = a.registry_mirror.split("/", 1)[0]
        say(f"images from {a.registry_mirror} instead of cgr.dev and the build registry")
        got = creds.registry("mirror", host, "MIRROR_USERNAME", "MIRROR_PASSWORD",
                             "the site registry holding the images; skip if it allows anonymous pulls",
                             required=False, env_token="MIRROR_TOKEN",
                             check=checks and pull_check(deployed_images(co, a.fips, a.registry_mirror)[:1]))
        registries = [(host, *got)] if got else []
        build = None
    else:
        cgr = creds.registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD",
                             "a Chainguard pull token (chainctl auth pull-token create --repository=oci "
                             f"--parent={co.org} --ttl=8760h)",
                             make=pull_token_maker(co.org, interactive, ask=not (a.yes or a.new_pull_token)),
                             check=checks and pull_check(deployed_images(co, a.fips, co.registry)[:1]))
        registries = [("cgr.dev", *cgr)]
        build = _build_registry_host(co)
    if build:
        # Every image: on ghcr.io each package has its own access list.
        got = creds.registry("build", build, "BUILD_REGISTRY_USERNAME", "BUILD_REGISTRY_PASSWORD",
                             "images this repo builds (on ghcr.io: a read-only bot account's read:packages "
                             "token); skip if they are public", required=False, env_token="BUILD_REGISTRY_TOKEN",
                             check=checks and pull_check(build_images(co, a.fips)))
        if got:
            registries.append((build, *got))
    if any("Iron Bank" in n for n in notes):
        got = creds.registry("registry1", "registry1.dso.mil", "REGISTRY1_USERNAME", "REGISTRY1_PASSWORD",
                             "Iron Bank images some enabled packages keep", required=False)
        if got:
            registries.append(("registry1.dso.mil", *got))
        else:
            say("warning: no registry1 credentials; the packages listed above won't pull their Iron Bank images")
    s3 = creds.s3(generate=co.in_cluster_seaweedfs()) if co.s3 else None
    keycloak_password = creds.generated("keycloak-admin-password", lambda: secrets.token_urlsafe(24))
    keycloak_db_password = keycloak_database_password(creds, site, enabled)
    tls = tls_choice(a, creds, site, enabled)
    signatures, org_id = signature_choice(a, creds, enabled)
    built = None if a.registry_mirror or signatures == "off" else built_signer(a, creds, co)
    if checks and not a.registry_mirror:
        # Each login was tried on one image above; now every image the packages deploy, as
        # `check` does: an image the plan doesn't cover fails here, not as ImagePullBackOff.
        count, failed = image_problems(co, a.fips, enabled, {host: (u, p) for host, u, p in registries})
        if failed:
            raise SystemExit("not installing: these images can't be pulled with your logins (an image your "
                             "Chainguard plan doesn't cover is refused the same way):\n  " + "\n  ".join(failed))
        say(f"all {count} images can be pulled")
    # The release recorded stays the one that runs until this install succeeds (record_release).
    creds.save("fips" if a.fips else "standard")
    say("stored in the cluster (Secret bigbang/bb-install); nothing printed")

    with tempfile.TemporaryDirectory(prefix="bb-install-") as d:
        cfg = os.path.join(d, "config.json")
        json.dump(dockerconfig(registries), open(cfg, "w"))
        step("Flux")
        apply_secret("flux-system", "private-registry", "generic", "--type=kubernetes.io/dockerconfigjson",
                     f"--from-file=.dockerconfigjson={cfg}")
        if not install_flux(co.path("flux-fips" if a.fips else "flux"), small_cluster=a.small_cluster):
            report(["flux-system"])
            raise SystemExit("Flux didn't become ready (pods above)")

        if co.in_cluster_seaweedfs():
            step("SeaweedFS")
            seaweedfs(co, a.fips, registries[:1] + [r for r in registries[1:] if r[0] != "registry1.dso.mil"], s3)
            say(f"ready at {co.s3['endpoint']}")

        step("Big Bang")
        secret_values = {"registryCredentials": [{"registry": r, "username": u, "password": p}
                                                 for r, u, p in registries]}
        if co.s3:
            deep_merge(secret_values, object_storage_values(co, enabled, s3))
        if "packages.keycloak" in enabled and not _site_sets_keycloak_password(site):
            # Big Bang's default admin password ("password") is too short for Keycloak's
            # FIPS mode (14+ characters); a random one works in both modes. Read it with:
            # kubectl -n bigbang get secret bb-install -o jsonpath='{.data.keycloak-admin-password}'
            deep_merge(secret_values, {"packages": {"keycloak": {"values": {"upstream": {"secrets": {"env": {
                "stringData": {"KEYCLOAK_ADMIN_PASSWORD": keycloak_password}}}}}}}})
        if keycloak_db_password:
            # FIPS also rejects Big Bang's default database password ("keycloak"): the
            # PostgreSQL driver's SCRAM hashing needs 112+ bits. Keycloak's side and the
            # bundled PostgreSQL's (hardcoded by Big Bang) must match.
            deep_merge(secret_values, {"packages": {"keycloak": {"values": {
                "upstream": {"database": {"password": keycloak_db_password}},
                "postgresql": {"global": {"postgresql": {"auth": {"password": keycloak_db_password}}}}}}}})
        if signatures != "off":
            deep_merge(secret_values, signature_egress_values())   # Kyverno must reach Sigstore
        if a.git_mirror:
            deep_merge(secret_values, git_mirror_values(defaults, a.git_mirror))
        if tls == "own":
            deep_merge(secret_values, {"packages": {"istioGateway": {"values": {"gateways": {"public": {
                "gatewayCerts": [{"name": GATEWAY_CERT, "tls": {"cert": creds.values["tls-cert"],
                                                                "key": creds.values["tls-key"]}}]}}}}}})
        sv = os.path.join(d, "credentials.yaml")
        with open(sv, "w", encoding="utf-8") as fh:
            yaml.safe_dump(secret_values, fh)
        if a.gitops:
            # Credentials in a Secret the HelmRelease reads; everything else in files to commit.
            apply_secret(NAMESPACE, GITOPS_SECRET, "generic", f"--from-file=values.yaml={sv}")
            write_gitops(a.gitops, co, a.fips, a.site, a.git_mirror)
            sh("kubectl", "apply", "-k", a.gitops, capture=True)
            say(f"wrote {a.gitops} and applied it; commit it and let a Flux Kustomization keep it in sync "
                f"({a.gitops}/README.md)")
        else:
            ref, version = chart_ref(a)
            files = [x for f in co.overlays(a.fips) + [a.site] for x in ("-f", f)]
            sh("helm", "upgrade", "-i", "bigbang", ref, *version, "-n", NAMESPACE, "--create-namespace",
               *files, "-f", sv, "--timeout", "15m", capture=True)
        say(f"Big Bang applied ({len(enabled)} packages); Flux installs them now")
        if not a.gitops:
            # An upgrade whose new values reach a package only through its values Secret (a
            # new image tag) doesn't change its HelmRelease: have Flux apply them now, so the
            # wait below sees the new pods, not the old ones still Ready.
            reconcile_helmreleases(timeout=120)

    if tls:
        step(f"TLS for *.{site['domain']}")
        gateway_tls(site["domain"], tls, creds.values.get("tls-issuer"), wait=900)
    if signatures != "off" or (creds.stored.get("signatures") or "off") != "off":
        step("image signatures")
        cluster_signatures(co.org, org_id, signatures, wait=900, built=built)

    if a.no_wait:
        record_release(co)
        return 0
    step("waiting for the packages")
    time.sleep(10)
    ok, problems = wait_ready(hr_namespaces(), a.timeout)
    rc = status_report(co, a.fips, problems) if ok else status_report(co, a.fips, problems, failed=True)
    if rc == 0:
        record_release(co)
    return rc


def record_release(co):
    """Which release the cluster runs (upgrade --list and --to's downgrade check read it),
    written only once an install has succeeded: a failed upgrade leaves the release that
    still runs. A checkout that isn't a release leaves it unknown rather than keep an
    older one."""
    if co.release:
        releases.record_release(co.release)
    else:
        releases.forget_release()


def keycloak_database_password(creds, site, enabled):
    """A generated password for Keycloak's bundled PostgreSQL, or None to leave it alone:
    Keycloak off, the site sets one (or an external database), or Keycloak already runs on
    Big Bang's default (PostgreSQL keeps the password it started with; changing it here
    would lock Keycloak out of its own database)."""
    if "packages.keycloak" not in enabled or _site_sets_keycloak_db_password(site):
        return None
    if creds.values.get("keycloak-db-password") or not _keycloak_installed():
        return creds.generated("keycloak-db-password", lambda: secrets.token_urlsafe(24))
    say("note: Keycloak already runs on Big Bang's default database password; FIPS needs 14+ "
        "characters, so change it in PostgreSQL and the site file before switching to --fips")
    return None


def _keycloak_values(site):
    return package_values(site, "keycloak").get("values") or {}


def _site_sets_keycloak_db_password(site):
    database = package_values(site, "keycloak").get("database") or {}
    return bool((((_keycloak_values(site).get("upstream") or {}).get("database") or {}).get("password"))
                or database.get("host") or database.get("password"))


def _keycloak_installed():
    """True if a Keycloak HelmRelease already exists (its database has a password)."""
    return bool(kubectl("get", "helmrelease", "-n", NAMESPACE, "keycloak", "-o", "name", check=False))


def _site_sets_keycloak_password(site):
    env = ((_keycloak_values(site).get("upstream") or {}).get("secrets") or {}).get("env") or {}
    return bool((env.get("stringData") or {}).get("KEYCLOAK_ADMIN_PASSWORD"))


def plan(a, co, site, defaults, enabled):
    step("plan (nothing is changed)")
    version, values = deployed()
    new_values = {}
    for f in co.overlays(a.fips) + [a.site]:
        deep_merge(new_values, yaml.safe_load(open(f, encoding="utf-8")) or {})
    state = read_secret(NAMESPACE, STATE_SECRET) or {}
    lines = plan_lines(version, values, a.version, new_values, defaults, state, "fips" if a.fips else "standard")
    for line in lines or ["nothing changes"]:
        say(f"  {line}")
    say("\nrun the same command without --plan to apply it")
    return 0
