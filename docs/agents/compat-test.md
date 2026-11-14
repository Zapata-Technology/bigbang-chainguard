# Write a compat test

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

### Write a compat test
`compat/<name>.yaml`: `enable` (package names, e.g. `[kyverno, headlamp]`), `namespaces`, `expect` (image fragments that
must run; none running fails the test), `check` (required: commands that prove the app does
its job, not just that pods are Ready), and as needed `setup`, `values`, `values_files`, `forbid_log`, `debug`, `check`
(commands that must exit 0), `installer: true` with `installer_args`, `installer_release`
(`previous-bigbang` or `previous`: install that published release with its own installer, run
`before_upgrade` on it, then this tree's `upgrade --to` the newest; `compat/installer-upgrade-to.yaml`), `large: true` and
`timeout` (weekly, slow packages), `registry1: true` (pulls from Iron Bank: SKIPPED, exit 0,
without `REGISTRY1_USERNAME`/`REGISTRY1_PASSWORD`), and `upgrade_from` (`images`: the mapping
to install first; `check`: commands on that install; `upgrade`: commands after Big Bang is
upgraded to the test's mapping, before the wait; `values`: values for the starting install
only; see `compat/vault-upgrade.yaml`; data a release upgrade must keep: `compat/release-upgrade-keycloak.yaml`;
`release: previous-bigbang` starts from the previous Big Bang version's newest release
(`compat/release-upgrade-mattermost.yaml`; the org image is often the same on both sides, so
for a real schema migration pin `upgrade_from.images` to an older line and verify with
`upgrade-data.py <app> verify --migrated`: `compat/keycloak-migration.yaml`,
`compat/mattermost-migration.yaml`). Unknown
keys fail `tests/test_compat_files.py`; a new key needs code in `bb-compat.py` and a line here.

