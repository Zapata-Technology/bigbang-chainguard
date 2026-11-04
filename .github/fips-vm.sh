#!/usr/bin/env bash
# Boot a RHEL-compatible VM whose kernel runs in FIPS mode, run k3s in it and give the
# runner a kubeconfig for it. kind's nodes share the runner's Ubuntu kernel, which is not in
# FIPS mode, so the kind tests can't show what a FIPS kernel (a STIG'd RHEL node, an AKS
# FIPS node pool) changes for the FIPS overlay. .github/workflows/fips-kernel.yml runs this.
#
#   .github/fips-vm.sh boot     # needs a writable /dev/kvm, qemu-system-x86, qemu-utils and
#                               # cloud-image-utils; writes $VM_DIR/kubeconfig
#   .github/fips-vm.sh assert   # fails unless the VM's /proc/sys/crypto/fips_enabled is 1
#   .github/fips-vm.sh logs     # the VM's journal for k3s and the kernel, into $VM_DIR
set -euo pipefail

VM_NAME=fips-vm
# shellcheck source=vm-lib.sh
. "$(dirname "$0")/vm-lib.sh"   # the AlmaLinux 9 image, vm, vm_start, wait_boot, vm_logs

# k3s on the Kubernetes minor kind runs in compat.yml; binary and installer by checksum.
K3S_VERSION=v1.35.9+k3s1
K3S_SHA256=897acb21cca57d477a211102251a6591450ce767344a9faaa121a1007ca9fa9a
K3S_INSTALL_SHA256=8598e002e61d658fed7b7542fc6d2c66d8da6eae69e088830105d2ee1ffb6d91

boot() {
  local t0 boot1
  boot1=$(vm_start)
  cd "$VM_DIR"

  # FIPS mode the way RHEL documents it for an installed system: fips-mode-setup sets the
  # FIPS crypto policy, adds fips=1 to the kernel command line and rebuilds the initramfs
  # with the FIPS self-tests; it takes effect on the next boot.
  vm 'command -v fips-mode-setup >/dev/null || sudo dnf -y -q install crypto-policies-scripts'
  vm sudo fips-mode-setup --enable
  # The FIPS behaviour under test comes from the kernel and the images' own crypto modules.
  vm_selinux_permissive
  vm sudo systemctl reboot || true
  t0=$SECONDS
  wait_boot 600 "$boot1" >/dev/null
  echo "== rebooted in $((SECONDS - t0)) s"
  assert

  echo "== k3s $K3S_VERSION"
  local rel=https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION/+/%2B}
  curl -fsSL --retry 5 -o k3s "$rel/k3s"
  curl -fsSL --retry 5 -o k3s-install.sh "https://raw.githubusercontent.com/k3s-io/k3s/${K3S_VERSION/+/%2B}/install.sh"
  echo "$K3S_SHA256  k3s" | sha256sum -c -
  echo "$K3S_INSTALL_SHA256  k3s-install.sh" | sha256sum -c -
  vm_copy k3s k3s-install.sh
  # No Traefik, ServiceLB or metrics-server: Big Bang brings what a test needs, and the VM's
  # memory goes to the packages.
  vm 'sudo install -m 755 k3s /usr/local/bin/k3s && sudo env INSTALL_K3S_SKIP_DOWNLOAD=true \
    INSTALL_K3S_SKIP_SELINUX_RPM=true INSTALL_K3S_SELINUX_WARN=true \
    INSTALL_K3S_EXEC="server --disable=traefik --disable=servicelb --disable=metrics-server" \
    sh k3s-install.sh'
  # The full path: RHEL's sudo resets PATH to secure_path, which has no /usr/local/bin, so
  # `sudo k3s` was "command not found" and an unbounded wait hung the job for an hour.
  vm 'for i in $(seq 100); do sudo /usr/local/bin/k3s kubectl get node >/dev/null 2>&1 && exit 0; sleep 3; done
      echo "k3s API not up in 300 s" >&2; sudo journalctl -u k3s --no-pager | tail -40 >&2; exit 1'
  vm sudo /usr/local/bin/k3s kubectl wait --for=condition=Ready node --all --timeout=300s
  # The kubeconfig names https://127.0.0.1:6443, which the port forward serves.
  vm sudo cat /etc/rancher/k3s/k3s.yaml > kubeconfig
  chmod 600 kubeconfig
  KUBECONFIG=$VM_DIR/kubeconfig kubectl get nodes -o wide
}

# The test only means something on a FIPS kernel: fail, never warn, when it isn't one.
assert() {
  local fips policy
  fips=$(vm cat /proc/sys/crypto/fips_enabled)
  policy=$(vm update-crypto-policies --show 2>/dev/null || echo unknown)
  echo "fips_enabled=$fips crypto-policy=$policy kernel=$(vm uname -r)"
  echo "cmdline: $(vm cat /proc/cmdline)"
  if [ "$fips" != 1 ]; then
    echo "::error::the VM's kernel is not in FIPS mode (/proc/sys/crypto/fips_enabled=$fips)"
    exit 1
  fi
}

case "${1:-}" in
  boot) boot ;;
  assert) assert ;;
  logs) vm_logs k3s ;;
  *) echo "usage: $0 boot|assert|logs" >&2; exit 2 ;;
esac
