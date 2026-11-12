"""egress-audit.py: hosts a run reached that a workflow's harden-runner list doesn't name."""
from conftest import load_script

audit = load_script("egress-audit")

WORKFLOW = """
jobs:
  build:
    steps:
      - uses: step-security/harden-runner@x
        with:
          egress-policy: block
          allowed-endpoints: >
            *.cgr.dev:443
            github.com:443
            proxy.golang.org:443
"""

# harden-runner's post step, as `gh run view --log` prints it (Nov 5: cosign reached the
# timestamp authority although no list named it).
LOG = "\n".join(
    "release\tPost Harden the runner\t2026-11-05T21:55:53Z Thu, 05 Nov 2026 21:55:47 GMT:" + ln for ln in [
        "domain resolved: timestamp.sigstore.dev., ip address: 34.49.147.4, TTL: 60",
        "endpoint called ip address:port 34.49.147.4:443, domain: timestamp.sigstore.dev., pid: 2728, "
        "process: cosign, status: ",
        "domain resolved: apk.cgr.dev., ip address: 1.2.3.4, TTL: 30",
        "domain resolved: github.com., ip address: 140.82.112.3, TTL: 30",
        "domain resolved: agent.api.stepsecurity.io., ip address: 5.6.7.8, TTL: 30",
        "domain resolved: productionresultssa12.blob.core.windows.net., ip address: 9.9.9.9, TTL: 30",
    ])


def test_allowed_reads_every_list_and_drops_ports():
    assert audit.allowed(WORKFLOW) == {"*.cgr.dev", "github.com", "proxy.golang.org"}


def test_an_unlisted_host_is_reported_with_the_process_that_called_it():
    # apk.cgr.dev is covered by the wildcard; GitHub's and StepSecurity's hosts are skipped.
    assert audit.unlisted(audit.reached(LOG), audit.allowed(WORKFLOW)) == {"timestamp.sigstore.dev": {"cosign"}}
