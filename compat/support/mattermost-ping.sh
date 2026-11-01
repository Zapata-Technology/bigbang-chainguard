#!/bin/sh
# Mattermost answers its health endpoint (a compat `check` command), through the API
# server's service proxy:  sh compat/support/mattermost-ping.sh
# Passes when /api/v4/system/ping says "status":"OK"; when the reply also carries the
# database and file store checks (get_server_status), those must be OK too.
set -e
url="/api/v1/namespaces/mattermost/services/mattermost:8065/proxy/api/v4/system/ping?get_server_status=true"
i=0
until reply=$(kubectl get --raw "$url" 2>&1) && echo "$reply" | grep -q '"status":"OK"'; do
  i=$((i+1))
  if [ $i -ge 30 ]; then echo "no OK from Mattermost after 30 tries: $reply"; exit 1; fi
  sleep 10
done
echo "$reply"
for key in database_status filestore_status; do
  if echo "$reply" | grep -q "\"$key\""; then
    echo "$reply" | grep -q "\"$key\":\"OK\"" || { echo "$key is not OK"; exit 1; }
    echo "$key: OK"
  fi
done
