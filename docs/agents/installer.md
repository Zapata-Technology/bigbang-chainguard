# Change how Big Bang is deployed

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

### Change how Big Bang is deployed
The user-facing path is `scripts/bb-install.py`, and users never run the steps by hand:
add a new step to the installer (and to `check` when it can fail before anything changes),
not to the docs. `bb-install.py` is the command line (flags, help); the installer is the
`scripts/bbinstall/` package, one module per concern (its `__init__.py` lists them):
`install.py` runs the steps, `preflight.py` the checks they start with, `commands.py` the
other commands. Tests patch a name in the module that looks it up (`credentials.read_secret`). `docs/install.md` describes the installer. Cluster helpers it shares with the kind harness live in
`scripts/bbcluster.py`. `compat/installer.yaml` (`installer: true`) runs it end to end on
kind, then `check`, and `upgrade` of the same site file in Big Bang 3's keys; `tests/test_install.py` covers its decisions without a cluster. A new credential or
generated value goes in the `bigbang/bb-install` Secret through `Credentials`
(`bbinstall/credentials.py`; a pull token it makes with chainctl too: `chainctl_pull_token`
captures the output), never on a
command line or in output.

