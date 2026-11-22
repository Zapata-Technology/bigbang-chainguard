# A compat (kind) test fails

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

### A compat (kind) test fails
1. `python scripts/ci-triage.py <pr>` shows each failure's evidence and whether it's a flake.
   For more, download the evidence: `gh run download <run id> -n compat-<test>-<mode>` (pass or fail;
   `docs/testing.md` "Reading a test's logs" lists the files). Start with `compat.log`
   (`PROBLEM` (a `stopped early:` one names the cause), `LIVE` (each container's first
   error line, when it happened), `RESULT`, `EXITED`), then `diag/exits.txt` and the crashed container's
   `diag/logs/<ns>/<pod>.<container>.previous.log`; Pending pods: `diag/cluster/nodes.txt`.
2. Flux uninstalls a release whose install fails, so `kubectl logs` after the fact finds
   nothing; the harness keeps every container's log while the test runs. For anything
   else (a CR's status, a file in a pod), add a `setup:` line
   `sh compat/support/watch.sh <name> "<kubectl command>"` and a `debug:` line
   `cat $COMPAT_OUT/watch-<name>.log`.
3. A container that exits with an empty log, or anything that takes more than one kind
   run to understand: reproduce it with `docker run` on your machine first (minutes, not a
   30-minute run). Use what the pod uses: `helm template` the package chart with the
   overlay's values for the container spec and ConfigMaps, the same image digest, user
   (`-u <runAsUser>`), env, args and mounts. Mount config files read-only at mode 0644:
   a bind mount from Windows is world-writable, and mysqld ignores a world-writable
   `my.cnf`, which hides the very failure. A database may log to a file rather than
   stderr (Big Bang's MySQL: `<datadir>/stderr.err`); add a `debug:` line that reads it.
   Sidecars share the pod's network: `docker run --network container:<db>`.
4. Fix the cause in `mapping.yaml` (or a builder), not in the test, unless the test itself
   is wrong. Record what you found in `docs/testing.md` ("What the runtime tests found").

