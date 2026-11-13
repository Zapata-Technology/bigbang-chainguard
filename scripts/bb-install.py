#!/usr/bin/env python3
"""Install Big Bang on Chainguard images with one command, and look after it.

  bb-install.py check [--site my-site.yaml] [--fips]
                     test every setting and change nothing: tools, the cluster (Kubernetes
                     version, StorageClass, the upgrade path from what runs now), the site
                     file (Big Bang renders with it: a wrong or Big Bang 3 key fails here),
                     and a pull of every image your packages deploy with your logins
                     (which also proves your Chainguard plan covers them)
  bb-install.py upgrade [--site my-site.yaml] [--fips]
                     the install below for a cluster that already runs Big Bang: converts a
                     Big Bang 3 site file to Big Bang 4's keys (keeping a backup), shows what
                     will change, asks, takes a Velero backup when Velero runs (--no-backup
                     skips it; a failed backup stops the upgrade), then installs
  bb-install.py init [--site my-site.yaml] [--domain example.com] [--enable a,b]
                     write a starter site file (no credentials in it)
  bb-install.py install [--site my-site.yaml] [--fips]
                     the checks above (stops before changing anything if one fails), pull
                     secrets, Flux, SeaweedFS (random S3 keys), Big Bang with the overlays in
                     the right order, then wait and report; writes the site file first (as
                     init does) when it doesn't exist
  bb-install.py status
                     HelmReleases, pods that aren't healthy (with their last log lines),
                     and running images that aren't the conversion's
  bb-install.py install ... --plan
                     what an install would change (version, FIPS, packages, image tags)
  bb-install.py rollback [--to N] [--yes]
                     the bigbang release back to an earlier revision, then wait and report
  bb-install.py uninstall [--delete-data] [--keep-flux] [--yes]
                     Big Bang, SeaweedFS (its volume and S3 keys stay unless
                     --delete-data), Flux

`install` is safe to re-run: it is also how you upgrade, switch to FIPS (--fips) or
back, and apply a changed site file. Big Bang's chart comes from repo1's public git archive
(no Iron Bank login); --chart takes a local chart or an oci:// ref instead. It deploys what this checkout's overlays were
generated for (the org and object storage are read from their headers), so regenerate
them first (bb-run.py) if you changed mapping.yaml or the org.

Credentials never go in the site file or on a command line, and are never printed. Each
is taken from the first of: the environment, the site file's registryCredentials, the
cluster (a previous run keeps them in the bigbang/bb-install Secret), or a hidden prompt.

  CGR_USERNAME, CGR_PASSWORD     cgr.dev pull token. Without one, and logged in to chainctl,
                                 `install` makes a one-year token itself (asks first unless
                                 --yes); --new-pull-token replaces the stored one
  BUILD_REGISTRY_USERNAME, BUILD_REGISTRY_PASSWORD (or BUILD_REGISTRY_TOKEN alone)
                                 only if the images this repo builds are private
  REGISTRY1_USERNAME, REGISTRY1_PASSWORD
                                 only if an enabled package keeps an Iron Bank image
  S3_ACCESS_KEY, S3_SECRET_KEY   only with an external S3 (OBJECT_STORAGE_ENDPOINT);
                                 for the in-cluster SeaweedFS they are generated
"""
import argparse
import os
import sys

try:
    import yaml  # noqa: F401 (checked here so a missing PyYAML says how to get it)
except ImportError:
    sys.exit("PyYAML is missing: pip install pyyaml")

# The installer is the bbinstall package next to this file; this file is its command line.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bbcommon import ROOT  # noqa: E402
from bbinstall.commands import chart, check, rollback, status, uninstall, upgrade  # noqa: E402
from bbinstall.install import install  # noqa: E402
from bbinstall.sitefile import init  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--overlays", default=ROOT, help="directory holding overlays/, flux/ and seaweedfs/ "
                                                     "(default: this checkout)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="write a starter site file")
    p.add_argument("--site", default="my-site.yaml")
    p.add_argument("--domain")
    p.add_argument("--enable", help="comma-separated packages to turn on, e.g. keycloak,velero")
    p.add_argument("--force", action="store_true")
    p.add_argument("--yes", action="store_true", help="don't ask anything")
    p = argparse.ArgumentParser(add_help=False)   # check, install and upgrade share these
    p.add_argument("--site", default="my-site.yaml",
                   help="the site file (default my-site.yaml; written first, as by init, if it doesn't exist)")
    p.add_argument("--domain", help="for a new site file: the domain the packages are served under")
    p.add_argument("--enable", help="for a new site file: comma-separated packages to turn on")
    p.add_argument("--fips", action="store_true", help="FIPS images (re-run without it to switch back)")
    p.add_argument("--chart", help="Big Bang chart: a local path or an oci:// ref (default: downloaded from "
                                   "repo1's git archive, no Iron Bank login needed)")
    p.add_argument("--version", help="Big Bang chart version (default: the overlays')")
    p.add_argument("--timeout", type=int, default=2700, help="seconds to wait for the packages (default 2700)")
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--small-cluster", action="store_true",
                   help="lower Flux's CPU/memory requests (kind or another small test cluster)")
    p.add_argument("--yes", action="store_true", help="never prompt (credentials from the environment, the "
                                                      "cluster, or a pull token made with your chainctl login)")
    p.add_argument("--tls-cert", help="the gateway's certificate (PEM, *.<domain>; with --tls-key)")
    p.add_argument("--tls-key", help="its private key (PEM)")
    p.add_argument("--tls-issuer", help="a cert-manager ClusterIssuer of yours to issue *.<domain> (ACME, Vault, ...)")
    p.add_argument("--tls-self-signed", action="store_true",
                   help="a self-signed CA made by cert-manager (the default when nothing else is set)")
    p.add_argument("--gitops", metavar="DIR",
                   help="instead of helm: write DIR (Big Bang's HelmRelease and the values) to commit to your "
                        "git repo, and apply it; credentials stay in a cluster Secret")
    p.add_argument("--verify-signatures", choices=["off", "audit", "enforce"],
                   help="a Kyverno policy requiring Chainguard's signature on cgr.dev/<org> images and "
                        "builds.yml's on the images built here (remembered; default enforce when Kyverno "
                        "is on, off when disconnected)")
    p.add_argument("--build-signer", metavar="OWNER/REPO",
                   help="the repo whose builds.yml signs the images built here (remembered; default "
                        "$BUILD_SIGNER, else any repo of the ghcr.io owner)")
    p.add_argument("--org-id", help="Chainguard org ID signatures must be under (chainctl iam organizations "
                                    "list; default $CHAINGUARD_ORG_ID, else any Chainguard signer)")
    p.add_argument("--plan", action="store_true",
                   help="show what would change (version, FIPS, packages, image tags) and stop")
    p.add_argument("--registry-mirror", metavar="PREFIX",
                   help="the site registry holding the images (air-gapped; bb-airgap.py lists them): "
                        "cgr.dev/<org> and the build registry become PREFIX; remembered, \"\" turns it off")
    p.add_argument("--git-mirror", metavar="URL",
                   help="a git server mirroring repo1 (same paths under URL) for Big Bang's chart and "
                        "packages; remembered, \"\" turns it off")
    p.add_argument("--no-pull-check", action="store_true",
                   help="don't test each registry login with a pull (when only the nodes can reach a "
                        "registry, with their own identity: ECR, ACR, Artifact Registry)")
    p.add_argument("--no-node-check", action="store_true",
                   help="check: don't read each node's inotify limits (NeuVector needs them raised; the "
                        "check runs a short-lived pod on each node to read them)")
    p.add_argument("--new-pull-token", action="store_true",
                   help="replace the stored cgr.dev pull token with a new one from chainctl")
    common = p
    sub.add_parser("check", parents=[common], help="test every setting; changes nothing (a short-lived pod "
                                                      "per node reads its inotify limits, then is deleted)")
    sub.add_parser("install", parents=[common], help="install, or re-apply after a change")
    p = sub.add_parser("upgrade", parents=[common],
                       help="upgrade a running Big Bang (converts a Big Bang 3 site file)")
    p.add_argument("--no-backup", action="store_true",
                   help="don't take a Velero backup first (taken when Velero runs with a usable location)")
    p.add_argument("--backup-timeout", type=int, default=1800, help="seconds to wait for that backup (default 1800)")
    p = sub.add_parser("chart", help="download Big Bang's chart into work/charts (the installer image bundles it)")
    p.add_argument("--version", help="Big Bang version (default: the overlays')")
    sub.add_parser("status", help="what's running and what isn't")
    p = sub.add_parser("rollback", help="return the bigbang release to an earlier revision")
    p.add_argument("--to", type=int, help="revision (default: the one before the current)")
    p.add_argument("--timeout", type=int, default=2700)
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p = sub.add_parser("uninstall", help="remove Big Bang, SeaweedFS and Flux")
    p.add_argument("--delete-data", action="store_true", help="also SeaweedFS's volume and the stored keys")
    p.add_argument("--keep-flux", action="store_true")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    a = ap.parse_args()
    return {"init": init, "check": check, "install": install, "upgrade": upgrade, "chart": chart,
            "status": status, "rollback": rollback, "uninstall": uninstall}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
