"""Registry and S3 credentials and the cgr.dev pull token: where each comes from, how a
login is tested, and the bigbang/bb-install Secret that keeps them. Values are never printed."""
import getpass
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time

from bbcluster import apply_secret, read_secret
from bbinstall.checkout import _build_registry_host
from bbinstall.common import NAMESPACE, STATE_SECRET, confirm, say

TOKEN_USER = "token"                    # sent with a token alone; the pull test shows if the registry takes it
TRIES = 3                               # logins typed at the prompt, each tested by a pull
PULL_TOKEN_DAYS = 365                  # chainctl's longest pull token (8760h)


class Credentials:
    """Registry and S3 credentials, from the environment, the site file, the cluster's
    bb-install Secret, or a hidden prompt. Values are never printed."""

    def __init__(self, site, interactive):
        self.site = {r.get("registry"): r for r in site.get("registryCredentials") or [] if isinstance(r, dict)}
        self.stored = read_secret(NAMESPACE, STATE_SECRET) or {}
        self.interactive = interactive
        self.values = dict(self.stored)

    def registry(self, key, registry, env_user, env_pass, why, required=True, make=None, check=None,
                 env_token=None):
        """(username, password) for a registry, or None when it's optional and not given.
        `make` creates credentials when none are found (a chainctl pull token).

        `check(login)` says why a login (or None: no login) can't pull, else None. A login
        from the environment, the site file or the cluster that fails it stops the install,
        unless a prompt can take another; a typed one is asked for again, TRIES times.

        `env_token` names a variable holding a token alone (a registry such as ghcr.io that
        needs no particular username); at the prompt, Enter for the username does the same."""
        user, pw, source = os.environ.get(env_user), os.environ.get(env_pass), "environment"
        if env_token and not pw and os.environ.get(env_token):
            user, pw = user or TOKEN_USER, os.environ[env_token]
        if not (user and pw) and registry in self.site:
            user, pw, source = self.site[registry].get("username"), self.site[registry].get("password"), "site file"
        if user and pw:
            self.values.pop(f"{key}-created", None)   # not a token this installer made
        else:
            user, pw = self.stored.get(f"{key}-username"), self.stored.get(f"{key}-password")
            source = "stored (an earlier run)"
        if not (user and pw) and make:
            user, pw = make() or (None, None)
            source = "new pull token"
            if user and pw:
                self.values[f"{key}-created"] = time.strftime("%Y-%m-%d")
        login = (user, pw) if user and pw else None
        problem = check(login) if check and (login or not required) else None
        if problem:
            replace = f"set {env_user} and {env_pass}" + (f" (or {env_token})" if env_token else "")
            if not self.interactive:
                raise SystemExit(f"{registry}: {'the ' + source + ' login' if login else 'without a login'} "
                                 f"can't pull: {problem}\n{replace} to one that can ({why})")
            say(f"{registry}: {'the ' + source + ' login' if login else 'without a login'} can't pull: {problem}")
            login = None
        # Asked unless an optional registry was just shown to work without a login.
        if not login and self.interactive and (problem or required or not check):
            login = self._ask(registry, why, required, check, token_ok=bool(env_token))
        if not login:
            if required:
                raise SystemExit(f"no {registry} credentials: set {env_user} and {env_pass} ({why})")
            return None
        self.values[f"{key}-username"], self.values[f"{key}-password"] = login
        return login

    def _ask(self, registry, why, required, check, token_ok):
        """A login typed at a hidden prompt and, with `check`, tested; asked again if it fails."""
        say(f"{registry}: {why}")
        for left in range(TRIES - 1, -1, -1):
            pw = getpass.getpass(f"  {registry} {'token or ' if token_ok else ''}password"
                                 f"{'' if required else ' (Enter to skip)'}: ").strip()
            if not pw:
                if required:
                    say("  a password is needed")
                    continue
                problem = check(None) if check else None
                if not problem:
                    return None
                say(f"  without a login: {problem}")
            else:
                user = input(f"  {registry} username{' (Enter for the token alone)' if token_ok else ''}: ").strip()
                user = user or (TOKEN_USER if token_ok else "")
                if not user:
                    say("  a username is needed")
                    continue
                problem = check((user, pw)) if check else None
                if not problem:
                    say("  login works")
                    return user, pw
                say(f"  that login can't pull: {problem}")
            if left:
                say(f"  try again ({left} {'try' if left == 1 else 'tries'} left)")
        raise SystemExit(f"{registry}: no login that can pull after {TRIES} tries")

    def s3(self, generate):
        access = os.environ.get("S3_ACCESS_KEY") or self.stored.get("s3-access-key")
        secret = os.environ.get("S3_SECRET_KEY") or self.stored.get("s3-secret-key")
        if not (access and secret):
            if generate:
                # Kept in the cluster and reused on every run: SeaweedFS's data stays readable.
                access, secret = "bb" + secrets.token_hex(10), secrets.token_urlsafe(32)
            elif self.interactive:
                access = input("  S3 access key: ").strip()
                secret = getpass.getpass("  S3 secret key: ").strip()
            if not (access and secret):
                raise SystemExit("no S3 keys for the external object storage: set S3_ACCESS_KEY and S3_SECRET_KEY")
        self.values["s3-access-key"], self.values["s3-secret-key"] = access, secret
        return access, secret

    def forget(self, key):
        """Drop stored credentials for `key` (to replace them)."""
        for k in [k for k in self.stored if k.startswith(f"{key}-")]:
            self.stored.pop(k)
            self.values.pop(k, None)

    def generated(self, key, make):
        """A value generated once and kept (e.g. Keycloak's admin password)."""
        if not self.values.get(key):
            self.values[key] = make()
        return self.values[key]

    def save(self, mode):
        self.values["mode"] = mode
        with tempfile.TemporaryDirectory(prefix="bb-install-") as d:
            args = []
            for k, v in sorted(self.values.items()):
                f = os.path.join(d, k)
                with open(f, "w", encoding="utf-8") as fh:
                    fh.write(v)
                args.append(f"--from-file={k}={f}")
            apply_secret(NAMESPACE, STATE_SECRET, "generic", *args)


def chainctl_logged_in():
    if not shutil.which("chainctl"):
        return False
    p = subprocess.run(["chainctl", "auth", "status", "-o", "json"], capture_output=True, text=True)
    try:
        return p.returncode == 0 and json.loads(p.stdout).get("valid") is True
    except ValueError:
        return False


def chainctl_pull_token(org):
    """(username, password) of a new one-year cgr.dev pull token for <org>, made with the
    user's chainctl login. The output is captured, never printed."""
    p = subprocess.run(["chainctl", "auth", "pull-token", "create", "--repository=oci", f"--parent={org}",
                        f"--ttl={PULL_TOKEN_DAYS * 24}h", f"--name=bb-install-{time.strftime('%Y%m%d-%H%M%S')}",
                        "--description=Big Bang image pulls (bigbang-chainguard bb-install.py)", "-o", "json"],
                       capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"chainctl couldn't create a pull token for {org} (exit {p.returncode})")
    try:
        d = json.loads(p.stdout)
        return d["identity_id"], d["token"]
    except (ValueError, KeyError):
        raise SystemExit("chainctl's pull-token output wasn't the expected JSON; set CGR_USERNAME and CGR_PASSWORD")


def pull_token_maker(org, interactive, ask):
    """make= for Credentials.registry: a chainctl pull token, if chainctl is logged in and
    (when `ask`) the user agrees."""
    def make():
        if not chainctl_logged_in():
            return None
        if ask and not (interactive and confirm(f"No cgr.dev pull token given. Create a one-year token for {org} "
                                                "with your chainctl login?", default=True)):
            return None
        say("creating a cgr.dev pull token with chainctl (kept in the cluster, not printed)")
        return chainctl_pull_token(org)
    return make


def pull_token_note(state, today=None):
    """A line when the pull token this installer made expires within 30 days."""
    created = state.get("cgr-created")
    if not created:
        return ""
    try:
        start = time.mktime(time.strptime(created, "%Y-%m-%d"))
    except ValueError:
        return ""
    left = int((start + PULL_TOKEN_DAYS * 86400 - (today or time.time())) // 86400)
    if left > 30:
        return ""
    return (f"the cgr.dev pull token made on {created} {'expired' if left < 0 else f'expires in {left} days'}: "
            "re-run bb-install.py install --new-pull-token")


def check_logins(co, site):
    """{registry host: login or None} for the check, from the environment, the site file, the
    cluster (an earlier install) or, for cgr.dev, a short-lived token from your chainctl
    login. Nothing is stored or printed, and no pull token is made."""
    stored = read_secret(NAMESPACE, STATE_SECRET) or {}
    in_site = {r.get("registry"): r for r in site.get("registryCredentials") or [] if isinstance(r, dict)}

    def first(host, key, env_user, env_pass, env_token=None):
        if os.environ.get(env_user) and os.environ.get(env_pass):
            return os.environ[env_user], os.environ[env_pass]
        if env_token and os.environ.get(env_token):
            return TOKEN_USER, os.environ[env_token]
        if in_site.get(host, {}).get("username") and in_site[host].get("password"):
            return in_site[host]["username"], in_site[host]["password"]
        if stored.get(f"{key}-username") and stored.get(f"{key}-password"):
            return stored[f"{key}-username"], stored[f"{key}-password"]
        return None
    logins = {"cgr.dev": first("cgr.dev", "cgr", "CGR_USERNAME", "CGR_PASSWORD")}
    if not logins["cgr.dev"] and chainctl_logged_in():
        p = subprocess.run(["chainctl", "auth", "token", "--audience", "cgr.dev"], capture_output=True, text=True)
        if p.returncode == 0 and p.stdout.strip():
            logins["cgr.dev"] = ("_token", p.stdout.strip())
    build = _build_registry_host(co)
    if build:
        logins[build] = first(build, "build", "BUILD_REGISTRY_USERNAME", "BUILD_REGISTRY_PASSWORD",
                              "BUILD_REGISTRY_TOKEN")
    return logins
