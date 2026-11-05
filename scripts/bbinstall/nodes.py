"""What the check reads from each node: the kernel's inotify limits. NeuVector's enforcer
exits ("couldn't initialize inotify: too many open files") at the common default of 128
instances per user (docs/testing.md, the RKE2 test), and nothing in the cluster's API shows
the limits, so a short-lived pod on each node reads them and is deleted."""
import json
import os
import re
import tempfile
import time

from bbcluster import apply_secret, dockerconfig, kubectl, sh

INOTIFY = {"max_user_instances": 8192, "max_user_watches": 524288}
PROBE_NAMESPACE = "bb-install-check"
PULL_SECRET = "private-registry"
WAIT = 180                      # seconds for every probe pod to pull its image and finish


def probe_image(co, fips):
    """The org's busybox image (exact tag) the overlays already deploy, for the probe pods."""
    rx = re.compile(r"(\S+/busybox(-fips)?:[\w.-]+)")
    for f in reversed(co.overlays(fips)):          # the FIPS overlay layers over the standard one
        for m in rx.finditer(open(f, encoding="utf-8").read()):
            if bool(m.group(2)) == fips:
                return m.group(1)
    return None


def probe_pod(name, node, image, pull_secret=True):
    """A pod on `node` that prints the two limits. It passes Pod Security `restricted` and Big
    Bang's Kyverno policies (non-root, no privilege, limits, no service account token)."""
    spec = {
        "nodeName": node, "restartPolicy": "Never", "automountServiceAccountToken": False,
        "tolerations": [{"operator": "Exists"}],
        "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "inotify", "image": image,
            "command": ["cat"] + [f"/proc/sys/fs/inotify/{k}" for k in INOTIFY],
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                          "limits": {"cpu": "100m", "memory": "32Mi"}},
        }],
    }
    if pull_secret:
        spec["imagePullSecrets"] = [{"name": PULL_SECRET}]
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": PROBE_NAMESPACE,
                         "labels": {"app.kubernetes.io/name": "bb-install-check"},
                         "annotations": {"bb-install/node": node}},
            "spec": spec}


def inotify_problems(readings):
    """PROBLEM lines for nodes whose limits ({node: {limit: value}}) are below INOTIFY."""
    out = []
    for node, got in sorted(readings.items()):
        low = [f"{k}={got[k]}" for k in INOTIFY if got.get(k, 0) < INOTIFY[k]]
        if low:
            out.append(f"node {node}: inotify limits too low for NeuVector's enforcer ({', '.join(low)}); "
                       "set " + " and ".join(f"fs.inotify.{k}={v}" for k, v in INOTIFY.items())
                       + " (a file in /etc/sysctl.d, then sysctl --system)")
    return out


def parse_output(text):
    """{limit: value} from a probe pod's output, or None when it isn't two numbers."""
    nums = text.split()
    if len(nums) != len(INOTIFY) or not all(n.isdigit() for n in nums):
        return None
    return dict(zip(INOTIFY, map(int, nums)))


def node_inotify(image, login, wait=WAIT, clock=time.time, sleep=time.sleep):
    """({node: {limit: value}}, [nodes that couldn't be read]) for the cluster's Linux nodes.
    The probe namespace (and its pull secret, from `login` when given) is deleted after."""
    nodes = json.loads(kubectl("get", "node", "-l", "kubernetes.io/os=linux", "-o", "json"))["items"]
    names = [n["metadata"]["name"] for n in nodes]
    readings, unread = {}, []
    try:
        kubectl("create", "namespace", PROBE_NAMESPACE, check=False)
        if login:
            with tempfile.TemporaryDirectory(prefix="bb-install-") as d:
                cfg = os.path.join(d, "config.json")
                json.dump(dockerconfig([(image.split("/")[0], *login)]), open(cfg, "w"))
                apply_secret(PROBE_NAMESPACE, PULL_SECRET, "generic", "--type=kubernetes.io/dockerconfigjson",
                             f"--from-file=.dockerconfigjson={cfg}")
        pods = {n: probe_pod(f"inotify-{i}", n, image, pull_secret=bool(login)) for i, n in enumerate(names)}
        sh("kubectl", "apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List",
                                                            "items": list(pods.values())}), capture=True, check=False)
        end = clock() + wait
        while True:
            items = json.loads(kubectl("-n", PROBE_NAMESPACE, "get", "pod", "-o", "json", check=False)
                               or '{"items": []}')["items"]
            done = {p["metadata"]["annotations"].get("bb-install/node"): p["status"].get("phase")
                    for p in items if p["status"].get("phase") in ("Succeeded", "Failed")}
            if len(done) == len(names) or clock() >= end:
                break
            sleep(5)
        for n, pod in pods.items():
            got = None
            if done.get(n) == "Succeeded":
                got = parse_output(kubectl("-n", PROBE_NAMESPACE, "logs", pod["metadata"]["name"], check=False) or "")
            if got:
                readings[n] = got
            else:
                unread.append(n)
    finally:
        kubectl("delete", "namespace", PROBE_NAMESPACE, "--wait=false", "--ignore-not-found", check=False)
    return readings, unread
