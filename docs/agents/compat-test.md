# Write a compat test

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

### Write a compat test
`compat/<name>.yaml`: `enable` (package names, e.g. `[kyverno, headlamp]`), `namespaces`, `expect` (image fragments that
must run; none running fails the test), `check` (required: commands that prove the app does
its job, not just that pods are Ready), and as needed `setup`, `values`, `values_files`, `forbid_log`, `debug`, `check`
(commands that must exit 0), `installer: true` with `installer_args`, `installer_release`
(`previous-bigbang` or `previous`: install that published release with its own installer, run
`before_upgrade` on it, then this tree's `upgrade --to` the newest; `compat/installer-upgrade-to.yaml`), `large: true` and
`timeout` (slow packages: weekly, and standard only on a release PR and on a change to the
package's overlay entry; required by the release PR's gate), `registry1: true` (pulls from Iron Bank: SKIPPED, exit 0,
without `REGISTRY1_USERNAME`/`REGISTRY1_PASSWORD`), and `upgrade_from` (`images`: the mapping
to install first; `check`: commands on that install; `upgrade`: commands after Big Bang is
upgraded to the test's mapping, before the wait; `values`: values for the starting install
only; see `compat/vault-upgrade.yaml`; data a release upgrade must keep: `compat/release-upgrade-keycloak.yaml`;
`release: previous-bigbang` starts from the previous Big Bang version's newest release
(`compat/release-upgrade-mattermost.yaml`; the org image is often the same on both sides, so
for a real schema migration pin `upgrade_from.images` to an older line and verify with
`upgrade-data.py <app> verify --migrated`: `compat/keycloak-migration.yaml`,
`compat/mattermost-migration.yaml`); `release: oldest-supported` starts from the newest release of
the Big Bang minor two before this tree's (N-2, counting the minors this repo released, so
4.1 starts from 3.35: `compat/release-upgrade-oldest.yaml`), and a release tag
(`release: 4.0.0-cg.29`) pins one), `weekly_and_release: true` (an expensive test: weekly,
on its own file's pull request and on a release PR, never for a package, support-file or
harness change; `results-gate.py` requires it only on a release PR, and only once it has
passed once, so adding one can't turn main's gate red). Unknown
keys fail `tests/test_compat_files.py`; a new key needs code in `bb-compat.py` and a line here.

`values` reach helm after every `values_files` entry, so a key set in both is taken from `values`
and the file's copy is dead (`tests/test_compat_files.py` catches the flow-style YAML a `setup`
line echoes into a file). Put a value that differs by mode (`COMPAT_FIPS`) only in the file.
