# Add a cluster-side setting to the installer

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

### Add a cluster-side setting to the installer
Settings applied after Big Bang (TLS, the signature policy) follow one pattern, a module
each in `scripts/bbinstall/` (`tls.py`, `signatures.py`): a `*_choice` that reads the flag
or the remembered value from `Credentials`, a manifest function,
`apply_when_ready(docs, crd, namespace, check, wait)` (`common.py`: waits for the package's
CRD and namespace and its admission webhook) and a `*_problem` that `status_report`
(`status.py`) reports. `install.py` calls the first three; the flag goes in `bb-install.py`.

