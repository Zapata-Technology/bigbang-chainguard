#!/usr/bin/env bash
# Boot an AlmaLinux 9 VM running RKE2 with the CIS profile, a default StorageClass and Pod
# Security Admission enforcing `restricted`, and give the runner a kubeconfig for it. Most
# Big Bang sites run RKE2 that way; kind and k3s exercise neither the CIS profile nor PSA.
# .github/workflows/rke2.yml runs this, then installs Big Bang with scripts/bb-install.py.
#
#   .github/rke2-vm.sh boot     # needs a writable /dev/kvm, qemu-system-x86, qemu-utils and
#                               # cloud-image-utils; writes $VM_DIR/kubeconfig
#   .github/rke2-vm.sh assert   # fails unless the CIS profile is in effect: etcd runs as the
#                               # etcd user and PSA refuses a privileged pod
#   .github/rke2-vm.sh events   # in the background: every event, kept in $VM_DIR/logs/events.log
#                               # (the API server forgets events after an hour)
#   .github/rke2-vm.sh logs     # the VM's journal for RKE2 and the kernel, kubelet and audit
#                               # logs, into $VM_DIR/logs
set -euo pipefail

VM_NAME=rke2-vm
# Big Bang's default packages on one node: more vCPUs than the runner's 2 CPUs (Kubernetes
# schedules by requests), and as much of its 7 GB as the runner can spare.
VM_CPUS=${VM_CPUS:-8}
VM_MEMORY=${VM_MEMORY:-6144}
VM_DISK=${VM_DISK:-60G}
# shellcheck source=vm-lib.sh
. "$(dirname "$0")/vm-lib.sh"   # the AlmaLinux 9 image, vm, vm_copy, vm_start, vm_logs

# RKE2 on the Kubernetes minor kind and the FIPS VM's k3s run. The release's checksum file is
# pinned, and it vouches for the tarball and the airgap images; install.sh from the same tag.
RKE2_VERSION=v1.35.9+rke2r1
RKE2_SUMS_SHA256=f3dd12b903fc7fa7d98933102069508e7f95322c7da530bd3936e2d7a5f11bce
RKE2_INSTALL_SHA256=2d24db2184dd6b1a5e281fa45cc9a8234c889394721746f89b5fe953fdaaf40a
# The airgap images RKE2 itself runs (core and the default CNI, Canal), so nothing of RKE2's
# comes from Docker Hub.
RKE2_ARTIFACTS="rke2.linux-amd64.tar.gz rke2-images-core.linux-amd64.tar.zst rke2-images-canal.linux-amd64.tar.zst"

# RKE2 ships no storage; local-path-provisioner is the usual single-node default. Manifest by
# checksum, images by digest (its helper image is an unpinned `busybox` in the manifest).
LOCAL_PATH_VERSION=v0.0.37
LOCAL_PATH_SHA256=9781b39c24f3f651bd6d6e41b561e04e4904bbdb6d4f8c7a6009df3a702dcd65
LOCAL_PATH_IMAGE=docker.io/rancher/local-path-provisioner:v0.0.37@sha256:e757967a5ec338f6a9b371c5a9688bedaa8c3578ea3dd4db329ea0084be0a86f
BUSYBOX_IMAGE=docker.io/library/busybox:1.37.0@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec

# Namespaces Pod Security Admission leaves alone; every other namespace must pass
# `restricted`. RKE2's CIS default exempts the first three. The rest run node agents that need
# the host, which `restricted` (and `baseline`) forbid, so a site on RKE2's CIS profile has to
# exempt them too: istio-system (ztunnel, Istio's ambient node proxy), neuvector (the
# enforcer, privileged), monitoring (node-exporter: host network, PID and filesystem), alloy
# (reads pod logs from the host) and local-path-storage (its helper pods mount host paths).
PSA_EXEMPT="kube-system cis-operator-system tigera-operator istio-system neuvector monitoring alloy local-path-storage"

KUBECTL=/var/lib/rancher/rke2/bin/kubectl
KCFG=/etc/rancher/rke2/rke2.yaml

boot() {
  vm_start >/dev/null
  cd "$VM_DIR"
  vm_selinux_permissive

  echo "== RKE2 $RKE2_VERSION, CIS profile"
  local rel=https://github.com/rancher/rke2/releases/download/${RKE2_VERSION/+/%2B}
  local exempt
  exempt=$(printf '"%s", ' $PSA_EXEMPT)
  vm sudo bash -euo pipefail -s <<EOF
mkdir -p /root/rke2 && cd /root/rke2
curl -fsSL --retry 5 -o sha256sum-amd64.txt "$rel/sha256sum-amd64.txt"
echo "$RKE2_SUMS_SHA256  sha256sum-amd64.txt" | sha256sum -c -
curl -fsSL --retry 5 -o install.sh "https://raw.githubusercontent.com/rancher/rke2/${RKE2_VERSION/+/%2B}/install.sh"
echo "$RKE2_INSTALL_SHA256  install.sh" | sha256sum -c -
for f in $RKE2_ARTIFACTS; do curl -fsSL --retry 5 -o "\$f" "$rel/\$f"; done
grep -E " ($(echo "$RKE2_ARTIFACTS" | tr ' ' '|'))\$" sha256sum-amd64.txt | sha256sum -c -

# RKE2's CIS hardening guide: the etcd user owns etcd's data, and the kernel parameters the
# kubelet refuses to start without (protect-kernel-defaults) come from rke2-cis-sysctl.conf.
useradd -r -c "etcd user" -s /sbin/nologin -M etcd -U
mkdir -p /etc/rancher/rke2 /var/lib/rancher/rke2/agent/images
mv rke2-images-*.tar.zst /var/lib/rancher/rke2/agent/images/
cat > /etc/rancher/rke2/config.yaml <<'CFG'
profile: cis
write-kubeconfig-mode: "0600"
pod-security-admission-config-file: /etc/rancher/rke2/psa.yaml
# Big Bang brings Istio's ingress gateway; RKE2's metrics-server stays, as at a site.
disable: [rke2-ingress-nginx]
CFG
# RKE2's CIS default (restricted, enforced) with the exemptions above.
cat > /etc/rancher/rke2/psa.yaml <<'PSA'
apiVersion: apiserver.config.k8s.io/v1
kind: AdmissionConfiguration
plugins:
  - name: PodSecurity
    configuration:
      apiVersion: pod-security.admission.config.k8s.io/v1
      kind: PodSecurityConfiguration
      defaults: {enforce: restricted, enforce-version: latest, audit: restricted, audit-version: latest,
                 warn: restricted, warn-version: latest}
      exemptions: {usernames: [], runtimeClasses: [], namespaces: [${exempt%, }]}
PSA
INSTALL_RKE2_ARTIFACT_PATH=/root/rke2 sh install.sh
cp /usr/local/share/rke2/rke2-cis-sysctl.conf /etc/sysctl.d/60-rke2-cis.conf
sysctl -p /etc/sysctl.d/60-rke2-cis.conf
systemctl enable --now rke2-server
EOF
  vm "for i in \$(seq 100); do sudo $KUBECTL --kubeconfig $KCFG get node >/dev/null 2>&1 && exit 0; sleep 3; done
      echo 'RKE2 API not up in 300 s' >&2; sudo journalctl -u rke2-server --no-pager | tail -40 >&2; exit 1"
  vm sudo $KUBECTL --kubeconfig $KCFG wait --for=condition=Ready node --all --timeout=600s
  # The kubeconfig names https://127.0.0.1:6443, which the port forward serves.
  vm sudo cat $KCFG > kubeconfig
  chmod 600 kubeconfig
  export KUBECONFIG=$VM_DIR/kubeconfig
  kubectl get nodes -o wide

  echo "== local-path-provisioner $LOCAL_PATH_VERSION (default StorageClass)"
  curl -fsSL --retry 5 -o local-path-storage.yaml \
    "https://raw.githubusercontent.com/rancher/local-path-provisioner/$LOCAL_PATH_VERSION/deploy/local-path-storage.yaml"
  echo "$LOCAL_PATH_SHA256  local-path-storage.yaml" | sha256sum -c -
  sed -e "s|image: docker.io/rancher/local-path-provisioner:$LOCAL_PATH_VERSION|image: $LOCAL_PATH_IMAGE|" \
      -e "s|image: docker.io/library/busybox\$|image: $BUSYBOX_IMAGE|" local-path-storage.yaml > local-path.yaml
  grep -q "$LOCAL_PATH_IMAGE" local-path.yaml && grep -q "$BUSYBOX_IMAGE" local-path.yaml
  kubectl apply -f local-path.yaml
  kubectl annotate storageclass local-path storageclass.kubernetes.io/is-default-class=true --overwrite
  kubectl -n local-path-storage rollout status deploy/local-path-provisioner --timeout=300s
  assert
  # RKE2's own charts (CoreDNS, Canal, metrics-server) finish installing after the node is
  # Ready; Big Bang's preflight wants a settled cluster.
  kubectl -n kube-system wait --for=condition=complete job --all --timeout=600s || true
  kubectl get pods -A
}

# A pass only means something on the CIS profile: fail, never warn, when it isn't in effect.
assert() {
  export KUBECONFIG=$VM_DIR/kubeconfig
  local etcd_user out
  etcd_user=$(vm "ps -o user= -C etcd | head -1")
  echo "etcd runs as: $etcd_user; kube-apiserver: $(vm "pgrep -a kube-apiserver | grep -o -- '--admission-control-config-file=[^ ]*'")"
  if [ "$etcd_user" != etcd ]; then
    echo "::error::etcd runs as '$etcd_user', not the etcd user: RKE2's CIS profile is not in effect"
    exit 1
  fi
  # A privileged pod in a namespace without an exemption must be refused.
  out=$(kubectl run psa-probe -n default --image="$BUSYBOX_IMAGE" --dry-run=server \
        --overrides='{"spec":{"containers":[{"name":"psa-probe","image":"'"$BUSYBOX_IMAGE"'","securityContext":{"privileged":true}}]}}' 2>&1 || true)
  if ! grep -q "violates PodSecurity" <<<"$out"; then
    echo "::error::Pod Security Admission accepted a privileged pod in default: $out"
    exit 1
  fi
  echo "PSA refuses a privileged pod in default: $(grep -o 'violates PodSecurity "[^"]*"' <<<"$out")"
}

events() {
  mkdir -p "$VM_DIR/logs"
  # One line per event: namespace, type, reason, object, message (tab-separated), as
  # scripts/cluster-check.py --events reads them. The watch ends now and then; start it again.
  local tmpl='{{.metadata.namespace}}{{"\t"}}{{.type}}{{"\t"}}{{.reason}}{{"\t"}}{{.involvedObject.kind}}/{{.involvedObject.name}}{{"\t"}}{{.message}}{{"\n"}}'
  KUBECONFIG=$VM_DIR/kubeconfig nohup setsid bash -c \
    'while :; do kubectl get events -A --watch -o go-template="$0" >> "$1" 2>/dev/null; sleep 5; done' \
    "$tmpl" "$VM_DIR/logs/events.log" > /dev/null 2>&1 < /dev/null &
  echo "events: $VM_DIR/logs/events.log"
}

logs() {
  vm_logs rke2-server
  vm sudo tail -c 20000000 /var/lib/rancher/rke2/agent/logs/kubelet.log > "$VM_DIR/logs/kubelet.log" 2>&1 || true
  vm sudo tail -c 20000000 /var/lib/rancher/rke2/server/logs/audit.log > "$VM_DIR/logs/audit.log" 2>&1 || true
}

case "${1:-}" in
  boot) boot ;;
  assert) assert ;;
  events) events ;;
  logs) logs ;;
  *) echo "usage: $0 boot|assert|events|logs" >&2; exit 2 ;;
esac
