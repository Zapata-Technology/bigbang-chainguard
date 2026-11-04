"""What every part of the installer shares: where it keeps its state in the cluster, how it
talks to the user, and how a setting applied after Big Bang waits for its package."""
import time

import yaml

from bbcluster import kubectl, sh

NAMESPACE = "bigbang"
STATE_SECRET = "bb-install"            # credentials and install mode, kept in the cluster


def say(msg):
    print(msg, flush=True)


def step(title):
    say(f"\n== {title}")


def confirm(question, default):
    answer = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not answer else answer.startswith("y")


def apply_when_ready(docs, crd, namespace, check, wait):
    """Apply `docs` once Big Bang has installed `crd` and made `namespace` (never pre-create
    it: Helm won't adopt it), retrying while the package's admission webhook comes up,
    until `kubectl <check>` finds the result or `wait` seconds pass."""
    manifest = yaml.safe_dump_all(docs)
    end = time.time() + wait
    while True:
        if kubectl("get", "crd", crd, check=False) and kubectl("get", "namespace", namespace, check=False):
            sh("kubectl", "apply", "-f", "-", stdin=manifest, check=False, capture=True)
            if kubectl(*check, check=False):
                return True
        if time.time() > end:
            return False
        time.sleep(15)
