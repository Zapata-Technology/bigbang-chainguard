#!/bin/sh
# TEST ONLY, for compat/vault-upgrade-upstream.yaml: does what Big Bang's Vault autoInit
# post-start hook does, from the runner, for upstream's hashicorp/vault 1.21 image. That
# image has no curl, which the hook needs, so the test's starting install turns the hook
# off (upgrade_from.values) and this initialises and unseals Vault instead, and keeps the
# root token and `vault operator init` output in Secret vault-token in the hook's format
# (keys `key` and `init.out`). After the upgrade, bigbang-vault's own hook reads that
# Secret and unseals the new pod, as it would after Big Bang's own autoInit.
#   sh compat/support/vault-upstream-init.sh start   # a `setup` command: runs in the background
# Its log ($COMPAT_OUT/vault-upstream-init.log) never holds the token or the unseal keys:
# they go to Vault and kubectl on stdin or in a private temp dir, never on a command line.
NS=vault
SEL=app.kubernetes.io/name=vault,component=server

pod() { kubectl -n $NS get pod -l $SEL -o jsonpath='{.items[0].metadata.name}' 2>/dev/null; }

# 0: unsealed, 2: sealed (both mean the API answers), 1: not reachable yet.
status() { kubectl -n $NS exec "$1" -c vault -- vault status >/dev/null 2>&1; }

run() {
  i=0
  while :; do
    p=$(pod)
    if [ -n "$p" ]; then
      status "$p"; rc=$?
      [ $rc -eq 0 ] || [ $rc -eq 2 ] && break
    fi
    i=$((i+1)); [ $i -lt 180 ] || { echo "no Vault server answering after 30 minutes"; return 1; }
    sleep 10
  done
  echo "Vault server $p answers (vault status exit $rc)"
  if kubectl -n $NS exec "$p" -c vault -- vault status -format=json 2>/dev/null | grep -q '"initialized": *true'; then
    echo "already initialised; nothing to do"
    return 0
  fi
  dir=$(mktemp -d) && chmod 700 "$dir" || return 1
  trap 'rm -rf "$dir"' EXIT
  kubectl -n $NS exec "$p" -c vault -- vault operator init > "$dir/init.out" || { echo "vault operator init failed"; return 1; }
  awk '/Initial Root Token:/ {printf "%s", $4}' "$dir/init.out" > "$dir/key"
  [ -s "$dir/key" ] || { echo "no root token in the init output"; return 1; }
  kubectl -n $NS create secret generic vault-token --from-file=key="$dir/key" --from-file=init.out="$dir/init.out" \
    || { echo "could not create Secret vault-token"; return 1; }
  echo "Secret vault-token created"
  # Each key goes to the pod on stdin and to vault inside it. `vault operator unseal -`
  # does not read stdin: it sends "-" as the key, which Vault refuses ("'key' must be a
  # valid hex or base64 string", PR #281's first run).
  for k in $(awk '/^Unseal Key [0-9]+:/ {print $4}' "$dir/init.out" | head -3); do
    printf '%s\n' "$k" | kubectl -n $NS exec -i "$p" -c vault -- sh -c 'read -r k; exec vault operator unseal "$k"' >/dev/null \
      || { echo "an unseal key was refused"; return 1; }
  done
  status "$p" && echo "Vault initialised and unsealed" || { echo "Vault is still sealed"; return 1; }
}

case "$1" in
  start)
    nohup sh "$0" run > "$COMPAT_OUT/vault-upstream-init.log" 2>&1 &
    echo "initialising Vault in the background -> $COMPAT_OUT/vault-upstream-init.log"
    ;;
  run) run ;;
  *) echo "usage: vault-upstream-init.sh start|run"; exit 2 ;;
esac
