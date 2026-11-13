"""The installer behind scripts/bb-install.py (its command line and help). Run it as

  python scripts/bb-install.py <command> ...

The modules import scripts/ helpers (bbcluster, bbcommon, objstore, orgtags) as top-level
modules, so scripts/ must be on sys.path, as bb-install.py and the tests arrange:

  common       the state Secret's name, say/step/confirm, apply_when_ready
  checkout     what this checkout deploys (Checkout), mirrored_checkout, the build registry
  sitefile     the site file: init, Big Bang 3 keys and migrate_site, values layering
  chart        Big Bang's chart (fetch_chart, clone_chart, chart_defaults), git mirrors
  credentials  Credentials (the bigbang/bb-install Secret), pull tokens, check_logins
  images       the images an install deploys and a pull test of each
  nodes        each node's inotify limits, read by a short-lived pod (check)
  preflight    prepare and preflight: what check, install and upgrade start with
  tls          the gateway certificate (tls_choice, tls_manifests, tls_problem)
  signatures   the Kyverno signature policy (signature_choice, signature_policy, ...)
  gitops       install --gitops (write_gitops)
  status       status_report, what runs (deployed) and plan_lines
  install      install and --plan
  commands     chart, check, upgrade (and --to, --list), status, rollback, uninstall
  releases     published releases: resolve --to, the running release, fetch and verify one
"""
