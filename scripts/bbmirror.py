#!/usr/bin/env python3
"""A local mirror of Big Bang's package git repositories for the kind tests, so Flux in a
test cluster never clones repo1.dso.mil.

  bbmirror.py fetch [--bigbang work/bigbang] [--dest work/bigbang/.git-mirror]
      every package's repository at the tag Big Bang pins, one commit deep. compat.yml's
      plan job runs it once per Big Bang version; the mirror rides in the cached clone.

repo1.dso.mil refuses GitHub's runners now and then. Each kind test had its own Flux
cloning ~10 package repositories from it, and one bad quarter hour failed whichever tests
were fetching (2026-11-20: keycloak FIPS, both tries). Now repo1 is asked once per Big Bang
version, with retries, and each test's Flux clones from the runner instead.

bb-compat.py calls rewrite() on the values it installs (fetching an enabled package's tag
the mirror lacks, such as the older release an upgrade test starts from) and serve() to
answer Flux: `git http-backend` behind a small HTTP server on the kind network's gateway.
A package the mirror can't get keeps its repo1 URL, so a mirror problem is never worse
than before.
"""
import argparse
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bbcommon import ROOT, bigbang_packages, git_network  # noqa: E402

REPO1 = "https://repo1.dso.mil/"
DEFAULT_DEST = os.path.join(ROOT, "work", "bigbang", ".git-mirror")


def sources(values):
    """[(package key, package values, repo1 URL, tag)] for each package Big Bang clones
    from repo1 at a tag (a branch or commit pin isn't mirrored)."""
    found = []
    for key, pkg in bigbang_packages(values).items():
        git = pkg.get("git") if isinstance(pkg.get("git"), dict) else {}
        repo, tag = git.get("repo") or "", git.get("tag")
        if repo.startswith(REPO1) and tag:
            found.append((key, pkg, repo, str(tag)))
    return found


def bare_path(dest, repo):
    """The mirror's bare repository for a repo1 URL: the URL's path under `dest`."""
    # (":" dropped: a file:// URL's drive letter, in the tests, would restart the path.)
    path = urllib.parse.urlparse(repo).path.strip("/").replace(":", "")
    path = path if path.endswith(".git") else path + ".git"
    return os.path.join(dest, *path.split("/"))


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def has_tag(dest, repo, tag):
    bare = bare_path(dest, repo)
    return os.path.isdir(bare) and git("rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}",
                                       cwd=bare).returncode == 0


def fetch(dest, repo, tag, attempts=4, pause=30, deadline=None):
    """Fetch one tag (one commit deep) into the mirror; True once the mirror has it.
    `deadline` (time.monotonic()): no new attempt starts after it."""
    if has_tag(dest, repo, tag):
        return True
    bare = bare_path(dest, repo)
    os.makedirs(bare, exist_ok=True)
    if not os.path.exists(os.path.join(bare, "HEAD")):
        git("init", "-q", "--bare", bare)
    err = ""
    for i in range(attempts):
        if deadline is not None and time.monotonic() > deadline:
            err = err or "out of time"
            break
        p = git_network("fetch", "-q", "--depth", "1", "--no-tags", repo,
                        f"+refs/tags/{tag}:refs/tags/{tag}", cwd=bare, timeout=180)
        if p.returncode == 0:
            return True
        err = p.stderr.strip()
        if i < attempts - 1:
            time.sleep(pause * (i + 1))   # repo1.dso.mil's bad minutes come and go
    print(f"git mirror: {repo} {tag}: {err[:200]}", file=sys.stderr)
    return False


def fetch_all(dest, values, attempts=4, pause=30, budget=900):
    """Every tagged repo1 package in `values`, within `budget` seconds (a partial mirror
    still helps: the rest stays on repo1); returns the (repo, tag) pairs it couldn't get."""
    wanted = sorted({(repo, tag) for _, _, repo, tag in sources(values)})
    deadline = time.monotonic() + budget
    with ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(lambda rt: fetch(dest, *rt, attempts=attempts, pause=pause, deadline=deadline),
                            wanted))
    return [rt for rt, ok in zip(wanted, got) if not ok]


def package_node(values, key):
    """values' dict at the Big Bang key "a.b", created if missing."""
    node = values
    for k in key.split("."):
        node = node.setdefault(k, {})
    return node


def rewrite(values, chart_values, dest, url, fetch_missing=True, attempts=3, pause=20):
    """Point each package the mirror holds at `url`, in `values`: the override values a test
    installs on top of the chart's `chart_values`, where each package's repo and tag are.
    An enabled package's tag the mirror lacks is fetched first. Returns the package keys
    now served from the mirror."""
    done = []
    for key, _, repo, tag in sources(chart_values):
        node = package_node(values, key)
        tag = str((node.get("git") or {}).get("tag") or tag)   # a test's own pin wins
        if not has_tag(dest, repo, tag) and not (fetch_missing and node.get("enabled") is True
                                                 and fetch(dest, repo, tag, attempts, pause)):
            continue
        rel = os.path.relpath(bare_path(dest, repo), dest).replace(os.sep, "/")
        node.setdefault("git", {})["repo"] = f"{url.rstrip('/')}/{rel}"
        done.append(key)
    return done


# --- serving: git's smart HTTP protocol (Flux's go-git doesn't speak the dumb one) ---

def read_body(rfile, headers):
    if headers.get("Transfer-Encoding", "").lower() == "chunked":
        body = b""
        while True:
            size = int(rfile.readline().split(b";")[0].strip() or b"0", 16)
            if size == 0:
                rfile.readline()
                return body
            body += rfile.read(size)
            rfile.readline()
    length = int(headers.get("Content-Length") or 0)
    return rfile.read(length) if length else b""


def cgi_response(out):
    """(status, [(header, value)], body) from `git http-backend`'s CGI output."""
    for sep in (b"\r\n\r\n", b"\n\n"):
        if sep in out:
            head, body = out.split(sep, 1)
            break
    else:
        head, body = b"", out
    status, headers = 200, []
    for line in head.decode("latin-1").splitlines():
        name, _, value = line.partition(":")
        if name.lower() == "status":
            status = int(value.split()[0])
        elif name:
            headers.append((name, value.strip()))
    return status, headers, body


def handler(root):
    class Handler(BaseHTTPRequestHandler):
        def backend(self):
            path, _, query = self.path.partition("?")
            body = read_body(self.rfile, self.headers)
            env = {"PATH": os.environ.get("PATH", ""), "GIT_PROJECT_ROOT": root, "GIT_HTTP_EXPORT_ALL": "1",
                   "PATH_INFO": urllib.parse.unquote(path), "QUERY_STRING": query,
                   "REQUEST_METHOD": self.command, "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                   "CONTENT_LENGTH": str(len(body)), "REMOTE_ADDR": self.client_address[0],
                   "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                   "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", "")}
            p = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True)
            status, headers, payload = cgi_response(p.stdout)
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = backend

        def log_message(self, *args):   # one line per request would bury the test's output
            pass
    return Handler


def serve(root, host="0.0.0.0", port=0):
    """Serve the mirror in a background thread; returns the server (.server_port)."""
    server = ThreadingHTTPServer((host, port), handler(os.path.abspath(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def kind_gateway(network="kind"):
    """The IPv4 gateway of kind's docker network: the runner, as the cluster's pods see it."""
    p = subprocess.run(["docker", "network", "inspect", network, "--format",
                        "{{range .IPAM.Config}}{{.Gateway}} {{end}}"], capture_output=True, text=True)
    return next((g for g in p.stdout.split() if "." in g), None) if p.returncode == 0 else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--bigbang", default=os.path.join(ROOT, "work", "bigbang"))
    f.add_argument("--dest", default=DEFAULT_DEST)
    a = ap.parse_args()
    values = yaml.safe_load(open(os.path.join(a.bigbang, "chart", "values.yaml"), encoding="utf-8"))
    missed = fetch_all(a.dest, values)
    total = len({(r, t) for _, _, r, t in sources(values)})
    print(f"git mirror: {total - len(missed)} of {total} package repositories in {a.dest}")
    for repo, tag in missed:
        print(f"  not mirrored (Flux clones it from repo1): {repo} {tag}")
    return 0   # a partial mirror still helps; the rest falls back to repo1


if __name__ == "__main__":
    sys.exit(main())
