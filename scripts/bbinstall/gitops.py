"""install --gitops: a directory to commit (Big Bang's GitRepository and HelmRelease, the
values files) that Flux keeps the cluster matching; credentials stay in a cluster Secret."""
import os
import shutil

import yaml

from bbinstall.chart import BIGBANG_GIT, mirrored
from bbinstall.common import NAMESPACE

GITOPS_SECRET = "bb-install-values"    # bigbang/<this>: the credential values (never in git)


GITOPS_README = """# Big Bang {version} on Chainguard images, deployed by Flux

Written by `bb-install.py install --gitops`. Commit this directory to your own git
repository; Flux then keeps the cluster matching it. Nothing here is secret: the pull
credentials, S3 keys and generated passwords are in the cluster Secret
`bigbang/{secret}`, which this HelmRelease reads last.

- `bigbang.yaml`: Big Bang's chart from repo1 at {version} (no Iron Bank login) and its
  HelmRelease, with the values below in order.
- `values/`: the overlays from bigbang-chainguard{release} and your site file. Change
  the site file here; to upgrade, re-run `install --gitops` from a newer checkout.

Keep it in sync (your repository's URL and this directory's path):

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata: {{name: site, namespace: flux-system}}
spec: {{interval: 5m, url: https://git.example.com/you/site.git, ref: {{branch: main}}}}
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata: {{name: bigbang, namespace: flux-system}}
spec: {{interval: 10m, prune: true, path: ./{path}, sourceRef: {{kind: GitRepository, name: site}}}}
```
"""


def gitops_manifests(version, value_maps, mirror=None):
    """Big Bang's GitRepository (repo1 or the mirror, the tag) and HelmRelease reading
    `value_maps` (ConfigMaps, in order) and then the credentials Secret."""
    repo = {"apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "GitRepository",
            "metadata": {"name": "bigbang", "namespace": NAMESPACE},
            "spec": {"interval": "10m", "url": mirrored(BIGBANG_GIT, mirror), "ref": {"tag": version}}}
    release = {"apiVersion": "helm.toolkit.fluxcd.io/v2", "kind": "HelmRelease",
               "metadata": {"name": "bigbang", "namespace": NAMESPACE},
               "spec": {"interval": "10m", "timeout": "15m",
                        "chart": {"spec": {"chart": "./chart", "reconcileStrategy": "Revision",
                                           "sourceRef": {"kind": "GitRepository", "name": "bigbang"}}},
                        "install": {"remediation": {"retries": 3}},
                        "upgrade": {"remediation": {"retries": 3}},
                        "valuesFrom": [{"kind": "ConfigMap", "name": n} for n in value_maps]
                        + [{"kind": "Secret", "name": GITOPS_SECRET}]}}
    return [repo, release]


def write_gitops(out, co, fips, site_path, mirror=None):
    """A kustomization directory: the values files as ConfigMaps (layered in the same order
    as the helm path), Big Bang's GitRepository and HelmRelease, and a README."""
    os.makedirs(os.path.join(out, "values"), exist_ok=True)
    generators = []
    for i, f in enumerate(co.overlays(fips) + [site_path]):
        name = f"{i:02d}-{os.path.basename(f)}"
        shutil.copyfile(f, os.path.join(out, "values", name))
        generators.append({"name": f"bigbang-values-{i:02d}", "files": [f"values.yaml=values/{name}"]})
    # Kustomize doesn't rewrite valuesFrom names, so no hash suffix; the watch label makes
    # Flux reconcile the HelmRelease when a values ConfigMap changes instead.
    kust = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "namespace": NAMESPACE,
            "resources": ["bigbang.yaml"], "configMapGenerator": generators,
            "generatorOptions": {"disableNameSuffixHash": True,
                                 "labels": {"reconcile.fluxcd.io/watch": "Enabled"}}}
    with open(os.path.join(out, "kustomization.yaml"), "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(kust, f, sort_keys=False)
    with open(os.path.join(out, "bigbang.yaml"), "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump_all(gitops_manifests(co.version, [g["name"] for g in generators], mirror), f,
                           sort_keys=False)
    with open(os.path.join(out, "README.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(GITOPS_README.format(version=co.version, secret=GITOPS_SECRET,
                                     release=f" {co.release}" if co.release else "",
                                     path=os.path.basename(os.path.normpath(out))))
