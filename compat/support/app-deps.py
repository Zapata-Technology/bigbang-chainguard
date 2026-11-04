#!/usr/bin/env python3
"""Setup for compat tests whose package needs an external database: PostgreSQL (and Redis).

GitLab's chart dropped its bundled PostgreSQL and Redis in v10 ("You must configure an
Redis connection ... external Redis became required"), and a site gives Mattermost its
own PostgreSQL rather than the chart's. Run by bb-compat.py (a `setup` command) with
COMPAT_OUT, COMPAT_DOCKERCFG and COMPAT_FIPS:

  python3 compat/support/app-deps.py gitlab        # PostgreSQL and Redis
  python3 compat/support/app-deps.py mattermost    # PostgreSQL

  1. in namespace <app>-deps (Big Bang creates the package's namespace itself), a pull
     secret and one Deployment + Service each for PostgreSQL (and Redis), on the org
     images the overlays map Big Bang's own PostgreSQL and Redis to (docs/images.md of
     this run), -fips in FIPS
  2. write $COMPAT_OUT/site-<app>.yaml: Big Bang's packages.<app>.database (and redis)
     settings pointing at them, with throwaway test passwords (14+ characters for FIPS)
"""
import os
import re
import subprocess
import sys

import yaml

# Test-only credentials for a throwaway kind cluster (FIPS needs 112+ bits).
DB_PASSWORD = "compat-{app}-db-not-a-secret"
REDIS_PASSWORD = "compat-{app}-redis-not-a-secret"
APPS = ("gitlab", "mattermost")


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


def workload(ns, name, image, port, env=None, args=None):
    pod = {"imagePullSecrets": [{"name": "private-registry"}],
           "containers": [{"name": name, "image": image, "ports": [{"containerPort": port}],
                           "env": [{"name": k, "value": v} for k, v in (env or {}).items()],
                           **({"args": args} if args else {}),
                           "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"}]
                           if name == "postgres" else [],
                           "readinessProbe": {"tcpSocket": {"port": port}, "periodSeconds": 5}}],
           "volumes": [{"name": "data", "emptyDir": {}}] if name == "postgres" else []}
    labels = {"app": name}
    return [{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns},
             "spec": {"selector": {"matchLabels": labels},
                      "template": {"metadata": {"labels": labels}, "spec": pod}}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns},
             "spec": {"selector": labels, "ports": [{"port": port, "targetPort": port}]}}]


def site_values(app, ns):
    """Big Bang's settings for the package to use the databases in `ns`."""
    db = {"host": f"postgres.{ns}.svc.cluster.local", "port": 5432, "database": app,
          "username": app, "password": DB_PASSWORD.format(app=app)}
    if app == "gitlab":
        return {"packages": {"gitlab": {
            "database": db, "redis": {"password": REDIS_PASSWORD.format(app=app)},
            "values": {"global": {"redis": {"host": f"redis.{ns}.svc.cluster.local", "port": 6379}}}}}}
    return {"packages": {app: {"database": dict(db, ssl_mode="disable")}}}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in APPS:
        sys.exit(f"usage: app-deps.py {{{','.join(APPS)}}}")
    app = sys.argv[1]
    ns = f"{app}-deps"
    out, fips = os.environ["COMPAT_OUT"], bool(os.environ.get("COMPAT_FIPS"))
    images_md = open(os.path.join(out, "docs", "images.md"), encoding="utf-8").read()
    postgres = mapped(images_md, r"/postgres(ql)?:", fips)
    run("kubectl", "create", "namespace", ns)
    run("kubectl", "-n", ns, "create", "secret", "generic", "private-registry", "--type=kubernetes.io/dockerconfigjson",
        f"--from-file=.dockerconfigjson={os.environ['COMPAT_DOCKERCFG']}")
    # GitLab loads its schema (db/structure.sql) in one transaction, which takes more
    # locks than PostgreSQL's default 64 per transaction allow: "out of shared memory".
    docs = workload(ns, "postgres", postgres, 5432,
                    env={"POSTGRES_USER": app, "POSTGRES_PASSWORD": DB_PASSWORD.format(app=app), "POSTGRES_DB": app,
                         "PGDATA": "/var/lib/postgresql/data/pgdata"},
                    args=["-c", "max_locks_per_transaction=256"])
    names = ["postgres"]
    if app == "gitlab":
        redis = mapped(images_md, r"/redis\d*(-slim)?:", fips)
        docs += workload(ns, "redis", redis, 6379, args=["--requirepass", REDIS_PASSWORD.format(app=app)])
        names.append("redis")
    run("kubectl", "apply", "-f", "-", stdin=yaml.safe_dump_all(docs))
    for name in names:
        run("kubectl", "-n", ns, "rollout", "status", f"deploy/{name}", "--timeout=300s")
    print(f"{' and '.join(names)} ready in {ns} ({postgres})")
    with open(os.path.join(out, f"site-{app}.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(site_values(app, ns), f)


if __name__ == "__main__":
    main()
