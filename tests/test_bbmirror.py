"""bbmirror.py: the kind tests' local mirror of Big Bang's package repositories."""
import subprocess

import pytest

import bbmirror

KC = "https://repo1.dso.mil/big-bang/product/packages/keycloak.git"


def git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def upstream(tmp_path):
    """A package repository with one chart commit tagged 1.0.0-bb.0."""
    src = tmp_path / "src"
    (src / "chart").mkdir(parents=True)
    (src / "chart" / "Chart.yaml").write_text("name: keycloak\n")
    git("init", "-q", str(src))
    git("add", ".", cwd=src)
    git("commit", "-q", "-m", "chart", cwd=src)
    git("tag", "-a", "1.0.0-bb.0", "-m", "1.0.0-bb.0", cwd=src)
    return src


def values(enabled=True, repo=KC, tag="1.0.0-bb.0"):
    return {"packages": {
        "keycloak": {"enabled": enabled, "git": {"repo": repo, "path": "./chart", "tag": tag}},
        "pinned": {"enabled": True, "git": {"repo": KC.replace("keycloak", "pinned"), "branch": "main"}},
        "elsewhere": {"enabled": True, "git": {"repo": "https://example.com/x.git", "tag": "1"}},
    }}


def test_only_repo1_packages_pinned_to_a_tag_are_mirrored():
    assert [(k, r, t) for k, _, r, t in bbmirror.sources(values())] == [("packages.keycloak", KC, "1.0.0-bb.0")]


def test_the_mirror_keeps_the_repo1_path(tmp_path):
    assert bbmirror.bare_path(str(tmp_path), KC) == str(tmp_path / "big-bang/product/packages/keycloak.git")


def test_fetch_takes_the_tag_one_commit_deep_and_only_once(tmp_path, upstream, monkeypatch):
    # A file:// URL's path, nested under the mirror, passes Windows' 260-character limit.
    monkeypatch.setattr(bbmirror, "bare_path", lambda dest, repo: str(tmp_path / "m.git"))
    dest = str(tmp_path / "mirror")
    assert bbmirror.fetch(dest, upstream.as_uri(), "1.0.0-bb.0", attempts=1)
    assert bbmirror.has_tag(dest, upstream.as_uri(), "1.0.0-bb.0")
    assert bbmirror.fetch(dest, upstream.as_uri(), "1.0.0-bb.0", attempts=1)   # already there
    assert not bbmirror.fetch(dest, upstream.as_uri(), "9.9.9", attempts=1, pause=0)


def mirror_with_keycloak(tmp_path, upstream):
    dest = str(tmp_path / "mirror")
    bare = bbmirror.bare_path(dest, KC)
    git("init", "-q", "--bare", bare)
    git("fetch", "-q", "--depth", "1", upstream.as_uri(), "+refs/tags/1.0.0-bb.0:refs/tags/1.0.0-bb.0", cwd=bare)
    return dest


def test_rewrite_points_mirrored_packages_at_the_mirror_and_leaves_the_rest(tmp_path, upstream):
    dest = mirror_with_keycloak(tmp_path, upstream)
    v = values()
    assert bbmirror.rewrite(v, dest, "http://172.18.0.1:18418/") == ["packages.keycloak"]
    assert v["packages"]["keycloak"]["git"]["repo"] == \
        "http://172.18.0.1:18418/big-bang/product/packages/keycloak.git"
    assert v["packages"]["elsewhere"]["git"]["repo"] == "https://example.com/x.git"


def test_a_tag_the_mirror_lacks_stays_on_repo1_when_it_cant_be_fetched(tmp_path, upstream, monkeypatch):
    dest = mirror_with_keycloak(tmp_path, upstream)
    asked = []
    monkeypatch.setattr(bbmirror, "fetch", lambda *a, **k: asked.append(a[2]) or False)
    v = values(tag="0.9.0-bb.0")
    assert bbmirror.rewrite(v, dest, "http://h:1") == []
    assert v["packages"]["keycloak"]["git"]["repo"] == KC and asked == ["0.9.0-bb.0"]
    # A disabled package is never fetched for.
    asked.clear()
    assert bbmirror.rewrite(values(enabled=False, tag="0.9.0-bb.0"), dest, "http://h:1") == [] and asked == []


def test_cgi_output_becomes_status_headers_and_body():
    status, headers, body = bbmirror.cgi_response(b"Status: 404 Not Found\r\nContent-Type: text/plain\r\n\r\nnope")
    assert (status, headers, body) == (404, [("Content-Type", "text/plain")], b"nope")
    assert bbmirror.cgi_response(b"Content-Type: x\n\nok")[0] == 200


def test_a_chunked_request_body_is_read_whole():
    import io
    raw = io.BytesIO(b"3\r\nabc\r\n2;x=y\r\nde\r\n0\r\n\r\n")
    assert bbmirror.read_body(raw, {"Transfer-Encoding": "chunked"}) == b"abcde"
    assert bbmirror.read_body(io.BytesIO(b"hello"), {"Content-Length": "5"}) == b"hello"


def test_a_shallow_clone_at_the_tag_comes_back_over_smart_http(tmp_path, upstream):
    dest = mirror_with_keycloak(tmp_path, upstream)
    server = bbmirror.serve(dest, host="127.0.0.1")
    try:
        v = values()
        bbmirror.rewrite(v, dest, f"http://127.0.0.1:{server.server_port}", fetch_missing=False)
        out = tmp_path / "clone"
        git("clone", "-q", "--depth", "1", "--branch", "1.0.0-bb.0", v["packages"]["keycloak"]["git"]["repo"], str(out))
        assert (out / "chart" / "Chart.yaml").read_text() == "name: keycloak\n"
    finally:
        server.shutdown()
