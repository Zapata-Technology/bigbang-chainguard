#!/bin/sh
# Steps of compat/vault-upgrade.yaml, against Big Bang's Vault in namespace vault:
#   sh compat/support/vault-upgrade.sh write   # on the old Vault: a KV v2 secret
#   sh compat/support/vault-upgrade.sh roll    # after Big Bang's upgrade: replace the server pod
#   sh compat/support/vault-upgrade.sh read    # on the new Vault: version 2.x, the secret is back
# Vault's StatefulSet updates OnDelete (the upstream chart's default, so an operator
# chooses when each server restarts): the new image runs only once the pod is deleted.
# Big Bang's post-start hook (autoInit) initialised Vault and keeps the root token and
# unseal keys in Secret vault-token; it unseals the new pod with them. The root token
# goes to the pod on stdin, never on a command line or in output.
set -e
NS=vault
SEL=app.kubernetes.io/name=vault,component=server
VALUE=written-by-vault-1.21-before-the-upgrade

pod() { kubectl -n $NS get pod -l $SEL -o jsonpath='{.items[0].metadata.name}'; }

vault_cmd() {
  { kubectl -n $NS get secret vault-token -o jsonpath='{.data.key}' | base64 -d; echo; } |
    kubectl -n $NS exec -i "$(pod)" -c vault -- sh -c 'read -r VAULT_TOKEN; export VAULT_TOKEN; exec vault "$@"' vault "$@"
}

sts_image() { kubectl -n $NS get sts -l app.kubernetes.io/name=vault -o jsonpath='{.items[0].spec.template.spec.containers[0].image}'; }
pod_image() { kubectl -n $NS get pod -l $SEL -o jsonpath='{.items[0].spec.containers[0].image}'; }
sts_upgraded() { sts_image | grep -q bigbang-vault; }
pod_upgraded() { pod_image | grep -q bigbang-vault; }

wait_for() {   # wait_for <seconds> <description> <command...>
  n=$1 what=$2; shift 2
  i=0
  until "$@"; do
    i=$((i+10))
    if [ $i -ge "$n" ]; then echo "timed out after ${n}s waiting for $what"; return 1; fi
    sleep 10
  done
}

case "$1" in
  write)
    wait_for 300 "Secret vault-token (Big Bang's autoInit)" kubectl -n $NS get secret vault-token -o name
    echo "running: $(pod_image)"
    vault_cmd version
    vault_cmd secrets enable -path=compat kv-v2
    vault_cmd kv put compat/upgrade answer="$VALUE"
    vault_cmd kv get -field=answer compat/upgrade
    ;;
  roll)
    # Flux applies the upgraded HelmRelease first; then replace the pod.
    wait_for 900 "the StatefulSet to name bigbang-vault" sts_upgraded
    echo "StatefulSet now runs $(sts_image); deleting $(pod)"
    kubectl -n $NS delete pod "$(pod)" --wait=true
    wait_for 600 "the new pod to run bigbang-vault" pod_upgraded
    kubectl -n $NS wait pod -l $SEL --for=condition=Ready --timeout=600s
    ;;
  read)
    echo "running: $(pod_image)"
    vault_cmd version | tee /dev/stderr | grep -q '^Vault v2\.' || { echo "the server is not Vault 2.x"; exit 1; }
    vault_cmd status | grep -E '^(Sealed|Version|Storage Type)'
    got=$(vault_cmd kv get -field=answer compat/upgrade)
    [ "$got" = "$VALUE" ] || { echo "compat/upgrade answer is '$got', wrote '$VALUE'"; exit 1; }
    echo "compat/upgrade survived the upgrade: answer=$got"
    ;;
  *) echo "usage: vault-upgrade.sh write|roll|read"; exit 2 ;;
esac
