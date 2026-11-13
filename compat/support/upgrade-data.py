#!/usr/bin/env python3
"""Data that must survive a release-to-release upgrade (compat/release-upgrade-<app>.yaml).

  python3 compat/support/upgrade-data.py keycloak seed     # upgrade_from.check: on the release
  python3 compat/support/upgrade-data.py keycloak verify   # check: after the upgrade
  python3 compat/support/upgrade-data.py mattermost seed|verify

`seed` writes data through the application, on the release a site runs now, and keeps
what it wrote (ids, the pods it ran on, their images) in $COMPAT_OUT/upgrade-<app>.json.
`verify` runs after Big Bang was upgraded to this tree and every pod is Ready: it reads
that data back through the application, so a database migration that failed, dropped a
table or lost rows fails the test, not only a pod that didn't roll. When the upgrade
didn't replace the application's pods (their image and spec didn't change), `verify`
restarts them first, so the data is always read by a pod running this tree's spec,
after a restart, from what the database kept on disk.

  keycloak:   a realm with a realm role and a user holding it, made with kcadm in the pod
              (Big Bang's bundled PostgreSQL, on a PersistentVolume). Verify: the same
              realm (its id), the user and role assignment, and the user logs in with the
              password (a password grant): Keycloak's user and credential tables migrated.
  mattermost: the first user (a system admin), a team, a post with an attached file,
              through the REST API (port-forward). Verify: the user logs in, the team and
              post are there, the post still has its file, and the file's bytes come back
              from object storage (SeaweedFS); the server reports its database OK.

Test-only credentials for a throwaway kind cluster (14+ characters for FIPS).
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

PASSWORD = "Compat-Upgrade-Password-1!"   # not a secret: kind test data
REALM, ROLE, USER = "compat-upgrade", "upgrade-role", "upgrade-user"
TEAM, MM_USER = "compat-upgrade", "upgrade-admin"
FILE_NAME = "upgrade-check.txt"

APPS = {
    # namespace, [(label selector, what it is, restart it when the upgrade didn't)]
    "keycloak": ("keycloak", [("app.kubernetes.io/name=postgresql", "PostgreSQL", True),
                              ("app.kubernetes.io/name=keycloak", "Keycloak", True)]),
    # PostgreSQL here is the test's own (compat/support/app-deps.py, emptyDir): Big Bang
    # doesn't deploy or upgrade it, so it isn't restarted.
    "mattermost": ("mattermost", [("app=mattermost", "Mattermost", True)]),
}


def run(*cmd, stdin=None, check=True):
    p = subprocess.run(cmd, input=stdin, text=True, capture_output=True)
    if check and p.returncode:
        raise SystemExit(f"{' '.join(cmd[:6])} failed ({p.returncode}):\n{p.stdout[-1500:]}\n{p.stderr[-1500:]}")
    return p


def state_path(app):
    return os.path.join(os.environ["COMPAT_OUT"], f"upgrade-{app}.json")


# --- pods ------------------------------------------------------------------------------

def pods(ns, selector):
    """{uid: (name, images)} of the running, not deleting pods matching `selector`."""
    out = json.loads(run("kubectl", "-n", ns, "get", "pod", "-l", selector, "-o", "json").stdout)
    return {p["metadata"]["uid"]: (p["metadata"]["name"], sorted(c["image"] for c in p["spec"]["containers"]))
            for p in out["items"]
            if p["status"].get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")}


def ready(ns, selector):
    out = json.loads(run("kubectl", "-n", ns, "get", "pod", "-l", selector, "-o", "json").stdout)
    # Not Succeeded/Failed ones: Mattermost's operator runs Job pods with the server's label.
    live = [p for p in out["items"] if not p["metadata"].get("deletionTimestamp")
            and p["status"].get("phase") in ("Pending", "Running")]
    return bool(live) and all(any(c["type"] == "Ready" and c["status"] == "True"
                                  for c in p["status"].get("conditions", [])) for p in live)


def snapshot(app):
    ns, groups = APPS[app]
    return {sel: {uid: list(v) for uid, v in pods(ns, sel).items()} for sel, _, _ in groups}


def unchanged(before, now):
    """Selectors whose pods are the very ones `seed` ran on (same uids): the upgrade
    didn't replace them."""
    return [sel for sel, uids in before.items() if uids and set(uids) == set(now.get(sel, {}))]


def compare(app, before):
    """Print what the upgrade did to each pod group; restart the ones it didn't replace."""
    ns, groups = APPS[app]
    now = snapshot(app)
    stay = unchanged(before, now)
    for sel, what, _ in groups:
        old = sorted({i for _, imgs in before.get(sel, {}).values() for i in imgs})
        new = sorted({i for _, imgs in now.get(sel, {}).values() for i in imgs})
        how = "not replaced" if sel in stay else "replaced"
        print(f"{what}: pods {how} by the upgrade; images {' '.join(old) or '-'} -> "
              f"{'the same' if old == new else ' '.join(new) or '-'}")
    restarted = False
    # In order, the database before its application; once the database restarted, the
    # application restarts too (fresh connections), even if the upgrade replaced it.
    for sel, what, restart in groups:
        if restart and (sel in stay or restarted):
            restarted = True
            print(f"{what}: restarting its pods, so this tree's spec reads the data after a restart", flush=True)
            run("kubectl", "-n", ns, "delete", "pod", "-l", sel, "--wait=true", "--timeout=300s")
            wait(f"{what} Ready again", lambda s=sel: ready(ns, s) and pods(ns, s), 900)


def wait(what, cond, seconds, every=5):
    end = time.time() + seconds
    while True:
        try:
            got = cond()
        except (SystemExit, OSError, urllib.error.URLError) as e:
            got, last = None, e
        else:
            last = None
        if got:
            return got
        if time.time() > end:
            raise SystemExit(f"timed out after {seconds}s waiting for {what}" + (f": {last}" if last else ""))
        time.sleep(every)


# --- Keycloak: kcadm in the pod --------------------------------------------------------

# Run in the Keycloak pod: log in (the admin, with the password in the pod's environment,
# or a realm's user), then one kcadm command. Only sh and kcadm: the image may have no
# grep or tr, so the runner parses kcadm's JSON.
KCADM = r"""cfg=/tmp/kcadm-upgrade-$$.config; trap 'rm -f $cfg' EXIT
kc=/opt/keycloak/bin/kcadm.sh
if [ "$1" = admin ]; then r=master u=$KEYCLOAK_ADMIN p=$KEYCLOAK_ADMIN_PASSWORD; shift
else r=$1 u=$2 p=$3; shift 3; fi
$kc config credentials --config $cfg --server "http://localhost:8080${KC_HTTP_RELATIVE_PATH%/}" \
  --realm "$r" --user "$u" --password "$p" >/dev/null || exit 3
[ $# -eq 0 ] || $kc "$@" --config $cfg"""


def keycloak_pod():
    pod = sorted(n for n, _ in pods(APPS["keycloak"][0], "app.kubernetes.io/name=keycloak").values())
    if not pod:
        raise SystemExit("no running Keycloak pod")
    return pod[0]


def kcadm(pod, *args, what):
    """kcadm `args` as the admin (args start with "admin") or a user; stdout, or fail with `what`."""
    p = run("kubectl", "-n", APPS["keycloak"][0], "exec", pod, "--", "sh", "-c", KCADM, "sh", *args, check=False)
    if p.returncode:
        raise SystemExit(f"Keycloak: {what} failed ({p.returncode}{', login' if p.returncode == 3 else ''}): "
                         f"{(p.stdout + p.stderr).strip()[-1500:]}")
    return p.stdout


def keycloak(mode, state):
    pod = keycloak_pod()
    if mode == "seed":
        kcadm(pod, "admin", "create", "realms", "-s", f"realm={REALM}", "-s", "enabled=true", what="creating the realm")
        kcadm(pod, "admin", "create", "roles", "-r", REALM, "-s", f"name={ROLE}", what="creating the role")
        kcadm(pod, "admin", "create", "users", "-r", REALM, "-s", f"username={USER}", "-s", "enabled=true",
              "-s", f"email={USER}@bigbang.test", "-s", "emailVerified=true", "-s", "firstName=Upgrade",
              "-s", "lastName=Check", what="creating the user")
        kcadm(pod, "admin", "set-password", "-r", REALM, "--username", USER, "--new-password", PASSWORD,
              what="setting the user's password")
        kcadm(pod, "admin", "add-roles", "-r", REALM, "--uusername", USER, "--rolename", ROLE,
              what="giving the user the role")
    realm = json.loads(kcadm(pod, "admin", "get", f"realms/{REALM}", "--fields", "id,realm", what=f"reading realm {REALM}"))
    users = json.loads(kcadm(pod, "admin", "get", "users", "-r", REALM, "-q", f"username={USER}", "-q", "exact=true",
                             what="listing the realm's users"))
    roles = json.loads(kcadm(pod, "admin", "get-roles", "-r", REALM, "--uusername", USER, what="reading the user's roles"))
    problems = check_keycloak(realm, users, roles, state.get("realm_id"))
    if problems:
        raise SystemExit("Keycloak: " + "; ".join(problems))
    # The user logs in with the password (a password grant on the realm's admin-cli client):
    # their credential survived, not only the row.
    kcadm(pod, REALM, USER, PASSWORD, what=f"{USER} logging in to {REALM} with the password set before the upgrade")
    print(f"Keycloak: realm {REALM} ({realm['id']}), user {USER} with role {ROLE}, and the user logs in")
    state.setdefault("realm_id", realm["id"])


def check_keycloak(realm, users, roles, realm_id):
    """What's wrong with the realm, user and role kcadm read (nothing: [])."""
    problems = []
    if realm_id and realm.get("id") != realm_id:
        problems.append(f"realm {REALM} has id {realm.get('id')}, not {realm_id}: it was recreated")
    if not any(u.get("username") == USER and u.get("enabled") for u in users):
        problems.append(f"no enabled user {USER} in realm {REALM}: {users}")
    if not any(r.get("name") == ROLE for r in roles):
        problems.append(f"user {USER} lost role {ROLE}: {[r.get('name') for r in roles]}")
    return problems


# --- Mattermost: REST API through a port-forward ---------------------------------------

class Mattermost:
    def __init__(self):
        self.pf, self.token, self.version = None, None, None

    def __enter__(self):
        ns = APPS["mattermost"][0]
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}/api/v4"
        self.pf = subprocess.Popen(["kubectl", "-n", ns, "port-forward", "svc/mattermost", f"{port}:8065"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait("Mattermost's API", lambda: self.call("GET", "/system/ping")[0] == 200, 300, every=3)
        return self

    def __exit__(self, *exc):
        self.pf.terminate()

    def call(self, method, path, body=None, content_type="application/json", raw=False):
        """(status, parsed reply or bytes, headers)."""
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", content_type)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                status, payload, headers = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, payload, headers = e.code, e.read(), e.headers
        self.version = headers.get("X-Version-Id") or self.version
        if raw:
            return status, payload, headers
        try:
            return status, json.loads(payload or b"null"), headers
        except ValueError:
            return status, payload.decode(errors="replace"), headers

    def ok(self, method, path, body=None, **kw):
        status, reply, headers = self.call(method, path, body, **kw)
        if status >= 300:
            raise SystemExit(f"{method} {path}: HTTP {status}: {str(reply)[:500]}")
        return reply, headers

    def login(self):
        self.token = None
        _, headers = self.ok("POST", "/users/login", {"login_id": MM_USER, "password": PASSWORD})
        self.token = headers["Token"]


def multipart(field, filename, content, boundary=None):
    """(body, content type) of a multipart/form-data upload of one file."""
    boundary = boundary or uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: text/plain\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def mattermost(mode, state):
    with Mattermost() as mm:
        if mode == "seed":
            marker = f"written before the upgrade {uuid.uuid4().hex}"
            mm.ok("POST", "/users", {"email": f"{MM_USER}@bigbang.test", "username": MM_USER, "password": PASSWORD})
            mm.login()
            team, _ = mm.ok("POST", "/teams", {"name": TEAM, "display_name": "Compat upgrade", "type": "O"})
            channel, _ = mm.ok("GET", f"/teams/{team['id']}/channels/name/town-square")
            content = f"{marker}\n".encode()
            body, ctype = multipart("files", FILE_NAME, content)
            files, _ = mm.ok("POST", f"/files?channel_id={channel['id']}", body, content_type=ctype)
            file_id = files["file_infos"][0]["id"]
            post, _ = mm.ok("POST", "/posts", {"channel_id": channel["id"], "message": marker, "file_ids": [file_id]})
            state.update(team_id=team["id"], post_id=post["id"], file_id=file_id, marker=marker,
                         version=mm.version)
            print(f"Mattermost {mm.version}: user {MM_USER}, team {TEAM}, post {post['id']} with file {file_id}")
            return
        mm.login()
        print(f"user {MM_USER} logged in with the password set before the upgrade")
        team, _ = mm.ok("GET", f"/teams/name/{TEAM}")
        if team["id"] != state["team_id"]:
            raise SystemExit(f"team {TEAM} has id {team['id']}, not {state['team_id']}: it was recreated")
        post, _ = mm.ok("GET", f"/posts/{state['post_id']}")
        if post.get("message") != state["marker"]:
            raise SystemExit(f"post {state['post_id']} says {post.get('message')!r}, not {state['marker']!r}")
        if state["file_id"] not in (post.get("file_ids") or []):
            raise SystemExit(f"post {state['post_id']} lost its file {state['file_id']}: {post.get('file_ids')}")
        status, content, _ = mm.call("GET", f"/files/{state['file_id']}", raw=True)
        if status != 200 or content != f"{state['marker']}\n".encode():
            raise SystemExit(f"file {state['file_id']}: HTTP {status}, {content[:200]!r}")
        print(f"team {TEAM}, post {state['post_id']} and its file's bytes (from object storage) are back")
        health, _ = mm.ok("GET", "/system/ping?get_server_status=true")
        if health.get("database_status", "OK") != "OK":
            raise SystemExit(f"database_status: {health.get('database_status')}")
        print(f"Mattermost {state.get('version')} -> {mm.version}; database_status "
              f"{health.get('database_status', 'not reported')}")


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in APPS or sys.argv[2] not in ("seed", "verify"):
        sys.exit(f"usage: upgrade-data.py {{{','.join(APPS)}}} {{seed,verify}}")
    app, mode = sys.argv[1:]
    step = {"keycloak": keycloak, "mattermost": mattermost}[app]
    if mode == "seed":
        state = {"pods": snapshot(app)}
        step("seed", state)
        json.dump(state, open(state_path(app), "w"), indent=1)
        print(f"seeded {app}; kept in {state_path(app)}")
        return
    if not os.path.exists(state_path(app)):
        sys.exit(f"{state_path(app)} is missing: upgrade_from.check didn't seed {app}")
    state = json.load(open(state_path(app)))
    compare(app, state["pods"])
    for attempt in range(4):   # an application can take a minute after a restart to answer
        try:
            step("verify", state)
            break
        except SystemExit as e:
            if attempt == 3 or "recreated" in str(e):
                raise
            print(f"not yet ({e}); again in 30 s", flush=True)
            time.sleep(30)
    print(f"{app}: the data written on the release is intact after the upgrade")


if __name__ == "__main__":
    main()
