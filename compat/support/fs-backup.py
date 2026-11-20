#!/usr/bin/env python3
"""Velero's file-system backup proven end to end (compat/velero.yaml): data on a
PersistentVolume is backed up, the namespace deleted, restored, and the data read back.

  python3 compat/support/fs-backup.py setup   # `setup`: the namespace, pull secret, PVC and pod
  python3 compat/support/fs-backup.py check   # `check`: backup, delete, restore, read back

`setup` runs before Big Bang with COMPAT_OUT, COMPAT_DOCKERCFG and COMPAT_FIPS: it creates
the compat-backup namespace with a private-registry pull secret, a ConfigMap, and a pod that
writes a random token to a file on a PVC, using the org's busybox the overlays deploy. It
keeps the token in $COMPAT_OUT/fs-backup.json.

`check` waits for Velero's location and node agent, backs the namespace up with
defaultVolumesToFsBackup (what bb-install.py upgrade asks for) and requires a completed
PodVolumeBackup, deletes the namespace, restores it, and requires the restored pod to read
the same token from its restored volume, and the ConfigMap to be back. A backup that holds
only Kubernetes objects passes a phase check but fails here.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import time

NS = "compat-backup"
BACKUP = RESTORE = "compat-fs-backup"
POD, PVC = "data-writer", "compat-data"


def run(*cmd, stdin=None, check=True):
    p = subprocess.run(cmd, input=stdin, text=True, capture_output=True)
    if check and p.returncode:
        raise SystemExit(f"{' '.join(cmd[:6])} failed:\n{p.stdout[-1500:]}\n{p.stderr[-1500:]}")
    return p


def kubectl(*args, **kw):
    return run("kubectl", *args, **kw)


def apply(doc):
    kubectl("apply", "-f", "-", stdin=json.dumps(doc))


def busybox(out, fips):
    """The org's busybox (exact tag) from the overlays this test installs, as
    bbinstall.nodes.probe_image finds it: the cluster already pulls it."""
    files = ["values-chainguard.yaml"] + (["values-chainguard-fips.yaml"] if fips else [])
    for f in reversed(files):           # the FIPS overlay layers over the standard one
        path = os.path.join(out, "overlays", f)
        if not os.path.exists(path):
            continue
        for m in re.finditer(r"(\S+/busybox(-fips)?:[\w.-]+)", open(path, encoding="utf-8").read()):
            if bool(m.group(2)) == fips:
                return m.group(1)
    raise SystemExit("no busybox image in the overlays")


def pod(image, token):
    """A pod that writes `token` to its volume once (a restored volume keeps the old one)."""
    return {
        "apiVersion": "v1", "kind": "Pod", "metadata": {"name": POD, "namespace": NS},
        "spec": {
            "imagePullSecrets": [{"name": "private-registry"}],
            "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532, "fsGroup": 65532,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": "writer", "image": image,
                "command": ["sh", "-c", f"[ -s /data/token ] || echo {token} > /data/token; exec sleep 86400"],
                "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                "readinessProbe": {"exec": {"command": ["test", "-s", "/data/token"]}, "periodSeconds": 3},
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "100m", "memory": "32Mi"}},
            }],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": PVC}}],
        },
    }


def setup():
    out, fips = os.environ["COMPAT_OUT"], bool(os.environ.get("COMPAT_FIPS"))
    token = secrets.token_hex(12)
    kubectl("create", "namespace", NS, check=False)
    secret = kubectl("-n", NS, "create", "secret", "generic", "private-registry",
                     "--type=kubernetes.io/dockerconfigjson",
                     f"--from-file=.dockerconfigjson={os.environ['COMPAT_DOCKERCFG']}",
                     "--dry-run=client", "-o", "yaml").stdout
    kubectl("apply", "-f", "-", stdin=secret)
    apply({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "compat-data", "namespace": NS},
           "data": {"answer": "backed-up"}})
    apply({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": PVC, "namespace": NS},
           "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "64Mi"}}}})
    image = busybox(out, fips)
    apply(pod(image, token))
    json.dump({"token": token, "image": image}, open(os.path.join(out, "fs-backup.json"), "w"))
    print(f"compat-backup: {POD} writes a token to {PVC} ({image})")


def wait_phase(kind, name, timeout=600):
    end = time.time() + timeout
    phase = ""
    while time.time() < end:
        phase = kubectl("-n", "velero", "get", kind, name, "-o", "jsonpath={.status.phase}", check=False).stdout
        if phase == "Completed":
            return
        if phase in ("Failed", "PartiallyFailed", "FailedValidation"):
            print(kubectl("-n", "velero", "get", kind, name, "-o", "jsonpath={.status}", check=False).stdout)
            raise SystemExit(f"{kind} {name}: {phase}")
        time.sleep(10)
    raise SystemExit(f"{kind} {name} still '{phase}' after {timeout} s")


def read_token():
    return kubectl("-n", NS, "exec", POD, "--", "cat", "/data/token").stdout.strip()


def check():
    want = json.load(open(os.path.join(os.environ["COMPAT_OUT"], "fs-backup.json")))["token"]
    kubectl("-n", "velero", "wait", "backupstoragelocation/default", "--for=jsonpath={.status.phase}=Available",
            "--timeout=300s")
    kubectl("-n", "velero", "rollout", "status", "daemonset/node-agent", "--timeout=300s")
    kubectl("-n", NS, "wait", f"pod/{POD}", "--for=condition=Ready", "--timeout=300s")
    if read_token() != want:
        raise SystemExit("the pod didn't write its token")
    apply({"apiVersion": "velero.io/v1", "kind": "Backup", "metadata": {"name": BACKUP, "namespace": "velero"},
           "spec": {"includedNamespaces": [NS], "storageLocation": "default", "snapshotVolumes": False,
                    "defaultVolumesToFsBackup": True, "ttl": "1h0m0s"}})
    wait_phase("backup", BACKUP)
    pvbs = json.loads(kubectl("-n", "velero", "get", "podvolumebackups.velero.io", "-l",
                              f"velero.io/backup-name={BACKUP}", "-o", "json").stdout)["items"]
    done = [p for p in pvbs if (p.get("status") or {}).get("phase") == "Completed"]
    print(f"backup {BACKUP}: {len(done)} of {len(pvbs)} pod volume backups completed")
    if not done:
        raise SystemExit("the backup copied no pod volume: it holds objects only, no data")

    kubectl("delete", "namespace", NS, "--wait=true", "--timeout=300s")
    apply({"apiVersion": "velero.io/v1", "kind": "Restore", "metadata": {"name": RESTORE, "namespace": "velero"},
           "spec": {"backupName": BACKUP, "includedNamespaces": [NS]}})
    wait_phase("restore", RESTORE)
    kubectl("-n", NS, "wait", f"pod/{POD}", "--for=condition=Ready", "--timeout=300s")
    got = read_token()
    if got != want:
        raise SystemExit(f"restored volume holds {got!r}, not the token written before the backup")
    answer = kubectl("-n", NS, "get", "configmap", "compat-data", "-o", "jsonpath={.data.answer}").stdout
    if answer != "backed-up":
        raise SystemExit("the ConfigMap wasn't restored")
    print("restored: the PVC's data and the ConfigMap came back")


if __name__ == "__main__":
    {"setup": setup, "check": check}[sys.argv[1]]()
