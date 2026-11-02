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

# AlmaLinux 9 GenericCloud, pinned by build and checksum (from CHECKSUM beside the image).
# RHEL 9's FIPS mode is what fips-mode-setup turns on, and AlmaLinux ships the same kernel.
IMAGE=AlmaLinux-9-GenericCloud-9.8-20261005.x86_64.qcow2
IMAGE_URL=https://repo.almalinux.org/almalinux/9.8/cloud/x86_64/images/$IMAGE
IMAGE_SHA256=25942b554efaac077e2f60d6bb27bd3332c171454cc21bbd33e5f79244d7570f
# k3s on the Kubernetes minor kind runs in compat.yml; binary and installer by checksum.
K3S_VERSION=v1.34.12+k3s1
K3S_SHA256=ef1df26b26276c0f6d962391c94e12f9362d1f1e30ec7a7926e9ddb84b0d66ce
K3S_INSTALL_SHA256=40b487f0d8ef4f5d1bf422e7bb6228cc7789c40ecc66c5ab067d396bbee9816e

VM_DIR=${VM_DIR:-${RUNNER_TEMP:-/tmp}/fips-vm}
# The runner has 2 CPUs and 7 GB; leave the runner itself (kubectl, the harness) about 1.5 GB.
VM_CPUS=${VM_CPUS:-2}
VM_MEMORY=${VM_MEMORY:-5632}
SSH_PORT=2222

vm() {
  ssh -i "$VM_DIR/id" -p "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes ci@127.0.0.1 "$@"
}

# Wait until ssh answers with a boot id other than $2 (empty: any), at most $1 seconds.
wait_boot() {
  local end=$((SECONDS + $1)) id
  while :; do
    id=$(vm cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)
    [ -n "$id" ] && [ "$id" != "${2:-}" ] && { echo "$id"; return 0; }
    if [ $SECONDS -ge $end ]; then
      echo "the VM did not boot in $1 s; its console:" >&2
      tail -60 "$VM_DIR/console.log" >&2 || true
      return 1
    fi
    sleep 5
  done
}

boot() {
  [ -w /dev/kvm ] || { echo "::error::/dev/kvm is missing or not writable: no hardware virtualization on this runner"; exit 1; }
  mkdir -p "$VM_DIR"
  cd "$VM_DIR"

  echo "== image $IMAGE"
  curl -fsSL --retry 5 -o "$IMAGE" "$IMAGE_URL"
  echo "$IMAGE_SHA256  $IMAGE" | sha256sum -c -
  # A copy-on-write disk over the image, with room for the packages' images.
  qemu-img create -q -f qcow2 -F qcow2 -b "$IMAGE" disk.qcow2 40G

  # A key for this run only; the VM is gone when the job ends.
  ssh-keygen -q -t ed25519 -N "" -f id
  cat > user-data <<EOF
#cloud-config
users:
  - name: ci
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys: ["$(cat id.pub)"]
EOF
  printf 'instance-id: fips-vm\nlocal-hostname: fips-vm\n' > meta-data
  cloud-localds seed.img user-data meta-data

  # User-mode networking: the VM reaches the internet through the runner, and the runner
  # reaches the VM's ssh and the Kubernetes API on localhost.
  qemu-system-x86_64 -enable-kvm -cpu host -smp "$VM_CPUS" -m "$VM_MEMORY" \
    -drive file=disk.qcow2,if=virtio -drive file=seed.img,if=virtio,format=raw \
    -netdev user,id=n0,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22,hostfwd=tcp:127.0.0.1:6443-:6443 \
    -device virtio-net-pci,netdev=n0 \
    -display none -serial "file:$VM_DIR/console.log" -daemonize -pidfile "$VM_DIR/qemu.pid"
  local t0=$SECONDS boot1
  boot1=$(wait_boot 600)
  echo "== booted in $((SECONDS - t0)) s: $(vm uname -r)"

  # FIPS mode the way RHEL documents it for an installed system: fips-mode-setup sets the
  # FIPS crypto policy, adds fips=1 to the kernel command line and rebuilds the initramfs
  # with the FIPS self-tests; it takes effect on the next boot.
  vm 'command -v fips-mode-setup >/dev/null || sudo dnf -y -q install crypto-policies-scripts'
  vm sudo fips-mode-setup --enable
  # SELinux permissive: k3s's SELinux policy is an extra RPM repository, and SELinux is not
  # what this test is about. The FIPS behaviour under test comes from the kernel and the
  # images' own crypto modules.
  vm "sudo setenforce 0; sudo sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config"
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
  scp -q -i id -P "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
    k3s k3s-install.sh ci@127.0.0.1:
  # No Traefik, ServiceLB or metrics-server: Big Bang brings what a test needs, and the VM's
  # memory goes to the packages.
  vm 'sudo install -m 755 k3s /usr/local/bin/k3s && sudo env INSTALL_K3S_SKIP_DOWNLOAD=true \
    INSTALL_K3S_SKIP_SELINUX_RPM=true INSTALL_K3S_SELINUX_WARN=true \
    INSTALL_K3S_EXEC="server --disable=traefik --disable=servicelb --disable=metrics-server" \
    sh k3s-install.sh'
  vm 'until sudo k3s kubectl get node >/dev/null 2>&1; do sleep 3; done'
  vm sudo k3s kubectl wait --for=condition=Ready node --all --timeout=300s
  # The kubeconfig names https://127.0.0.1:6443, which the port forward above serves.
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

logs() {
  mkdir -p "$VM_DIR/logs"
  vm sudo journalctl -k --no-pager > "$VM_DIR/logs/kernel.log" 2>&1 || true
  vm sudo journalctl -u k3s --no-pager > "$VM_DIR/logs/k3s.log" 2>&1 || true
  vm free -m > "$VM_DIR/logs/memory.txt" 2>&1 || true
  cp "$VM_DIR/console.log" "$VM_DIR/logs/" 2>/dev/null || true
}

case "${1:-}" in
  boot) boot ;;
  assert) assert ;;
  logs) logs ;;
  *) echo "usage: $0 boot|assert|logs" >&2; exit 2 ;;
esac
