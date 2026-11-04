# Shared by .github/fips-vm.sh and .github/rke2-vm.sh (sourced, not run): boot an AlmaLinux 9
# VM under QEMU/KVM on a GitHub runner, reach it over ssh, copy files in and collect its logs.
# The callers set VM_NAME (and may set VM_DIR, VM_CPUS, VM_MEMORY, VM_DISK) before sourcing.

# AlmaLinux 9 GenericCloud, pinned by build and checksum (from CHECKSUM beside the image).
# RHEL 9 is what most Big Bang sites run their nodes on, and AlmaLinux ships the same kernel.
IMAGE=AlmaLinux-9-GenericCloud-9.8-20261005.x86_64.qcow2
IMAGE_URL=https://repo.almalinux.org/almalinux/9.8/cloud/x86_64/images/$IMAGE
IMAGE_SHA256=25942b554efaac077e2f60d6bb27bd3332c171454cc21bbd33e5f79244d7570f

VM_DIR=${VM_DIR:-${RUNNER_TEMP:-/tmp}/$VM_NAME}
# The runner has 2 CPUs and 7 GB; leave the runner itself (kubectl, the harness) about 1.5 GB.
# More vCPUs than the runner has: Kubernetes schedules by requests, and one 2-CPU node couldn't
# fit Kyverno with Headlamp and Grafana ("Insufficient cpu"). kind's four nodes share the same
# 2 CPUs the same way, so the tests' requests fit as they do there.
VM_CPUS=${VM_CPUS:-4}
VM_MEMORY=${VM_MEMORY:-5632}
VM_DISK=${VM_DISK:-40G}
SSH_PORT=2222
SSH_OPTS=(-i "$VM_DIR/id" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

vm() {
  ssh "${SSH_OPTS[@]}" -p "$SSH_PORT" -o ConnectTimeout=10 -o BatchMode=yes ci@127.0.0.1 "$@"
}

# Copy files into the ci user's home in the VM.
vm_copy() {
  scp -q "${SSH_OPTS[@]}" -P "$SSH_PORT" "$@" ci@127.0.0.1:
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

# Download the image, start the VM and wait for its first boot; prints the boot id.
vm_start() {
  [ -w /dev/kvm ] || { echo "::error::/dev/kvm is missing or not writable: no hardware virtualization on this runner" >&2; exit 1; }
  mkdir -p "$VM_DIR"
  (
    cd "$VM_DIR"
    echo "== image $IMAGE" >&2
    curl -fsSL --retry 5 -o "$IMAGE" "$IMAGE_URL"
    echo "$IMAGE_SHA256  $IMAGE" | sha256sum -c - >&2
    # A copy-on-write disk over the image, with room for the packages' images.
    qemu-img create -q -f qcow2 -F qcow2 -b "$IMAGE" disk.qcow2 "$VM_DISK"

    # A key for this run only; the VM is gone when the job ends. ECDSA P-384, not Ed25519: in
    # FIPS mode RHEL's sshd refuses Ed25519 keys (the login after the FIPS reboot failed).
    ssh-keygen -q -t ecdsa -b 384 -N "" -f id
    cat > user-data <<EOF
#cloud-config
users:
  - name: ci
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys: ["$(cat id.pub)"]
EOF
    printf 'instance-id: %s\nlocal-hostname: %s\n' "$VM_NAME" "$VM_NAME" > meta-data
    cloud-localds seed.img user-data meta-data

    # User-mode networking: the VM reaches the internet through the runner, and the runner
    # reaches the VM's ssh and the Kubernetes API on localhost.
    qemu-system-x86_64 -enable-kvm -cpu host -smp "$VM_CPUS" -m "$VM_MEMORY" \
      -drive file=disk.qcow2,if=virtio -drive file=seed.img,if=virtio,format=raw \
      -netdev user,id=n0,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22,hostfwd=tcp:127.0.0.1:6443-:6443 \
      -device virtio-net-pci,netdev=n0 \
      -display none -serial "file:$VM_DIR/console.log" -daemonize -pidfile "$VM_DIR/qemu.pid"
  ) >&2
  local t0=$SECONDS id
  id=$(wait_boot 600)
  echo "== booted in $((SECONDS - t0)) s: $(vm uname -r)" >&2
  echo "$id"
}

# SELinux permissive: k3s's and RKE2's SELinux policies are extra RPM repositories, and
# SELinux is not what these tests are about.
vm_selinux_permissive() {
  vm "sudo setenforce 0; sudo sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config"
}

# The VM's kernel log, the journal of each unit named, memory and the serial console, into
# $VM_DIR/logs (uploaded as the job's artifact).
vm_logs() {
  mkdir -p "$VM_DIR/logs"
  vm sudo journalctl -k --no-pager > "$VM_DIR/logs/kernel.log" 2>&1 || true
  for unit in "$@"; do
    vm sudo journalctl -u "$unit" --no-pager > "$VM_DIR/logs/$unit.log" 2>&1 || true
  done
  vm free -m > "$VM_DIR/logs/memory.txt" 2>&1 || true
  vm df -h > "$VM_DIR/logs/disk.txt" 2>&1 || true
  cp "$VM_DIR/console.log" "$VM_DIR/logs/" 2>/dev/null || true
}
