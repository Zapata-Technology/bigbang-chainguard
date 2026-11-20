"""bbbuild.freeze_go: the nightly run keeps the Go module versions build.yaml pins."""
import copy

import bbbuild

OLD = {"source": "https://x/v1.0.tar.gz",
       "operator": {"go_get": ["golang.org/x/net@v0.60.0", "google.golang.org/grpc@v1.80.0"]},
       "plugins": [{"id": "redis", "rebuild": {"source": "https://r/v2", "go_get": ["golang.org/x/net@v0.61.0"]}}]}


def newest():
    new = copy.deepcopy(OLD)
    new["operator"]["go_get"] = ["golang.org/x/net@v0.63.0", "google.golang.org/grpc@v1.80.0",
                                 "github.com/new/mod@v1.2.0"]
    new["plugins"][0]["rebuild"]["go_get"] = ["golang.org/x/net@v0.63.0"]
    return new


def test_the_committed_versions_are_kept_and_reported():
    new = newest()
    held = bbbuild.frozen_go(new, OLD)
    assert new["operator"]["go_get"] == ["golang.org/x/net@v0.60.0", "google.golang.org/grpc@v1.80.0",
                                         "github.com/new/mod@v1.2.0"]   # a module just added: newest
    assert new["plugins"][0]["rebuild"]["go_get"] == ["golang.org/x/net@v0.61.0"]   # per plugin, by id
    assert ("operator.go_get", "golang.org/x/net", "v0.60.0", "v0.63.0") in held
    assert len(held) == 2


def test_raise_go_or_a_moved_source_takes_the_newest():
    new = newest()
    assert bbbuild.frozen_go(new, OLD, raise_go=True) is None
    assert new["operator"]["go_get"][0] == "golang.org/x/net@v0.63.0"
    moved = newest()
    moved["source"] = "https://x/v1.1.tar.gz"   # its go.mod may need newer modules: never downgrade
    assert bbbuild.frozen_go(moved, OLD) is None
    assert moved["operator"]["go_get"][0] == "golang.org/x/net@v0.63.0"
    assert bbbuild.frozen_go(newest(), None) is None   # a new build: nothing to keep
