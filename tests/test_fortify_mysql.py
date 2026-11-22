"""Fortify's MySQL: Bitnami's chart on the org's (non-Bitnami) mysql needs overrides.fortify.

Each flag below fixed a failure that only a kind run showed (docs/testing.md, "What the
runtime tests found"). These tests keep the flags from being dropped, and fail the nightly
bot's PR when it moves mysql to a line whose plugin directory the flag doesn't name.
"""
import os
import re

import yaml

from conftest import ROOT


def _mapping():
    return yaml.safe_load(open(os.path.join(ROOT, "mapping.yaml"), encoding="utf-8"))


def _mysql():
    return _mapping()["overrides"]["fortify"]["mysql"]


def _flag(args, name):
    hits = [a.split("=", 1)[1] for a in args if a.startswith(name + "=")]
    assert len(hits) == 1, f"overrides.fortify mysql.primary.args needs exactly one {name}=: {args}"
    return hits[0]


def test_mysqld_args_fit_chainguards_entrypoint():
    args = _mysql()["primary"]["args"]
    # The image's ENTRYPOINT already ends in mysqld; a second "mysqld" makes mysqld refuse
    # --defaults-file.
    assert "mysqld" not in args, "Chainguard's mysql ENTRYPOINT already runs mysqld"
    assert _flag(args, "--defaults-file") == "/opt/bitnami/mysql/conf/my.cnf"
    # /var/run links to /run in Chainguard's image: Big Bang's my.cnf socket under /var/run
    # made docker-entrypoint.sh link the live socket to itself during first-time init.
    assert _flag(args, "--socket") == "/run/mysqld/mysqld.sock"


def _overlay_mysql_tags():
    tags = {}
    for mode, name in (("standard", "values-chainguard.yaml"), ("fips", "values-chainguard-fips.yaml")):
        text = open(os.path.join(ROOT, "overlays", name), encoding="utf-8").read()
        # A kustomize images entry: newName .../mysql(-fips), then its newTag.
        m = re.search(r"newName: \S+/mysql(?:-fips)?\n\s+newTag: (\S+)", text)
        assert m, f"overlays/{name} has no mysql image for fortify"
        tags[mode] = m.group(1)
    return tags


def test_plugin_dir_follows_the_mysql_line_the_overlays_deploy():
    plugin_dir = _flag(_mysql()["primary"]["args"], "--plugin-dir")
    m = re.fullmatch(r"/usr/lib/mysql-(\d+\.\d+)/plugin", plugin_dir)
    assert m, f"unexpected --plugin-dir {plugin_dir}"
    for mode, tag in _overlay_mysql_tags().items():
        assert tag.startswith(m.group(1) + "."), (
            f"{mode} deploys mysql {tag} but --plugin-dir is {plugin_dir}: Chainguard keeps "
            f"plugins in /usr/lib/mysql-<major.minor>/plugin, so move the flag in mapping.yaml "
            f"overrides.fortify with the new line (and run compat/fortify.yaml)")


def test_fips_exporter_logs_in_over_tls():
    # Go's FIPS 140 mode refuses caching_sha2_password's RSA-OAEP SHA-1 password exchange
    # on a plain connection; over TLS the password goes inside the channel.
    assert "--tls.insecure-skip-verify" in _mysql()["metrics"]["extraArgs"]["primary"]


def test_fortify_has_its_kind_test_and_is_not_listed_untested():
    assert os.path.exists(os.path.join(ROOT, "compat", "fortify.yaml"))
    assert "packages.fortify" not in (_mapping().get("untested") or {})
