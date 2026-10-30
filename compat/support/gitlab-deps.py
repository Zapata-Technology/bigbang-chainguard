#!/usr/bin/env python3
"""Setup for compat/gitlab*.yaml: the PostgreSQL and Redis GitLab connects to.

GitLab's chart dropped its bundled PostgreSQL and Redis in v10 ("You must configure an
Redis connection ... external Redis became required"); a site provides both. Run by
bb-compat.py (a `setup` command) with COMPAT_OUT, COMPAT_DOCKERCFG and COMPAT_FIPS:
  1. in namespace gitlab-deps (Big Bang creates `gitlab` itself), a pull secret and one
     Deployment + Service each for PostgreSQL and Redis, on the org images the overlays
     map Big Bang's own PostgreSQL and Redis to (docs/images.md of this run), -fips in FIPS
  2. write $COMPAT_OUT/site-gitlab.yaml: Big Bang's addons.gitlab.database and redis
     settings pointing at them, with throwaway test passwords (14+ characters for FIPS)
"""
import os
import re
import subprocess
import sys

import yaml

NS = "gitlab-deps"
# Test-only credentials for a throwaway kind cluster (FIPS needs 112+ bits).
DB_PASSWORD = "compat-gitlab-db-not-a-secret"
REDIS_PASSWORD = "compat-gitlab-redis-not-a-secret"


def run(*cmd, stdin=None):
    p = subprocess.run(cmd, input=stdin, text=True, capture_output=True)
    if p.returncode:
        sys.exit(f"{' '.join(cmd[:4])} failed:\n{p.stderr[-1500:]}")
    return p.stdout


def mapped(images_md, pattern, fips):
    """The org image docs/images.md maps an Iron Bank image matching `pattern` to."""
    org = re.search(r"are in `(cgr\.dev/[^`]+)`", images_md).group(1)
    for line in images_md.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) > 3 and re.search(pattern, cells[1]) and cells[2] != "Iron Bank":
            new = cells[3 if fips else 2]
            return new if "/" in new.split(":")[0] else f"{org}/{new}"
    sys.exit(f"docs/images.md maps no image matching {pattern}")


def workload(name, image, port, env=None, args=None):
    pod = {"imagePullSecrets": [{"name": "private-registry"}],
           "containers": [{"name": name, "image": image, "ports": [{"containerPort": port}],
                           "env": [{"name": k, "value": v} for k, v in (env or {}).items()],
                           **({"args": args} if args else {}),
                           "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"}]
                           if name == "postgres" else [],
                           "readinessProbe": {"tcpSocket": {"port": port}, "periodSeconds": 5}}],
           "volumes": [{"name": "data", "emptyDir": {}}] if name == "postgres" else []}
    labels = {"app": name}
    return [{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": NS},
             "spec": {"selector": {"matchLabels": labels},
                      "template": {"metadata": {"labels": labels}, "spec": pod}}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": NS},
             "spec": {"selector": labels, "ports": [{"port": port, "targetPort": port}]}}]


def main():
    out, fips = os.environ["COMPAT_OUT"], bool(os.environ.get("COMPAT_FIPS"))
    images_md = open(os.path.join(out, "docs", "images.md"), encoding="utf-8").read()
    postgres, redis = mapped(images_md, r"/postgres(ql)?:", fips), mapped(images_md, r"/redis\d*(-slim)?:", fips)
    run("kubectl", "create", "namespace", NS)
    run("kubectl", "-n", NS, "create", "secret", "generic", "private-registry", "--type=kubernetes.io/dockerconfigjson",
        f"--from-file=.dockerconfigjson={os.environ['COMPAT_DOCKERCFG']}")
    docs = workload("postgres", postgres, 5432,
                    env={"POSTGRES_USER": "gitlab", "POSTGRES_PASSWORD": DB_PASSWORD, "POSTGRES_DB": "gitlab",
                         "PGDATA": "/var/lib/postgresql/data/pgdata"}) + \
        workload("redis", redis, 6379, args=["--requirepass", REDIS_PASSWORD])
    run("kubectl", "apply", "-f", "-", stdin=yaml.safe_dump_all(docs))
    for name in ("postgres", "redis"):
        run("kubectl", "-n", NS, "rollout", "status", f"deploy/{name}", "--timeout=300s")
    print(f"PostgreSQL {postgres} and Redis {redis} ready in {NS}")
    site = {"addons": {"gitlab": {
        "database": {"host": f"postgres.{NS}.svc.cluster.local", "port": 5432, "database": "gitlab",
                     "username": "gitlab", "password": DB_PASSWORD},
        "redis": {"password": REDIS_PASSWORD},
        "values": {"global": {"redis": {"host": f"redis.{NS}.svc.cluster.local", "port": 6379}}}}}}
    with open(os.path.join(out, "site-gitlab.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(site, f)


if __name__ == "__main__":
    main()
