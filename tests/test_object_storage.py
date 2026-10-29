"""The object-storage overlay generator (SeaweedFS or any S3 instead of MinIO)."""
import os

import pytest
import yaml

from conftest import ROOT, load_script

storage = load_script("bb-object-storage")
SPEC = yaml.safe_load(open(os.path.join(ROOT, "object-storage.yaml")))


def test_tokens():
    t = storage.tokens("http://seaweedfs-s3.seaweedfs.svc:8333/", "us-east-1", "bb")
    assert t == {"endpoint": "http://seaweedfs-s3.seaweedfs.svc:8333", "host": "seaweedfs-s3.seaweedfs.svc:8333",
                 "region": "us-east-1", "prefix": "bb", "insecure": True}
    assert storage.tokens("https://s3.example.com", "r", "p")["insecure"] is False
    with pytest.raises(SystemExit):
        storage.tokens("seaweedfs:8333", "r", "p")


def test_fill():
    t = storage.tokens("http://s3:8333", "us-east-1", "bb")
    assert storage.fill({"a": "{insecure}", "b": "{bucket:x}-{region}", "c": ["{host}"], "d": 1}, t) == \
        {"a": True, "b": "bb-x-us-east-1", "c": ["s3:8333"], "d": 1}


def test_build_disables_minio_and_skips_optional_packages():
    t = storage.tokens("http://s3:8333", "us-east-1", "bb")
    overlay, creds, buckets, chosen = storage.build(SPEC, t, set())
    assert overlay["addons"]["minio"]["enabled"] is False
    assert overlay["addons"]["minioOperator"]["enabled"] is False
    assert "loki" not in chosen and "loki" not in overlay
    assert overlay["addons"]["thanos"]["objectStorage"]["endpoint"] == "s3:8333"
    assert overlay["addons"]["gitlab"]["objectStorage"]["endpoint"] == "http://s3:8333"
    assert "bb-velero" in buckets
    assert all(not str(v).startswith("<") for v in yaml.safe_dump(overlay).split())  # no credentials


def test_with_loki():
    t = storage.tokens("http://s3:8333", "us-east-1", "bb")
    overlay, creds, buckets, chosen = storage.build(SPEC, t, {"loki"})
    assert overlay["loki"]["strategy"] == "scalable"
    assert overlay["loki"]["values"]["loki"]["storage"]["s3"] == {"s3ForcePathStyle": True, "insecure": True}
    assert "loki.objectStorage.accessKey" in creds and "bb-loki-chunks" in buckets


def test_credentials_template_has_only_placeholders():
    tree = storage.credentials_template(["a.objectStorage.accessKey", "a.objectStorage.accessSecret",
                                         "v.secretContents.cloud"])
    assert tree["a"]["objectStorage"] == {"accessKey": "<access key>", "accessSecret": "<secret key>"}
    assert tree["v"]["secretContents"]["cloud"].startswith("<")


def test_every_package_lists_credentials_and_buckets():
    for name, pkg in SPEC["packages"].items():
        assert pkg["values"], name
        assert pkg.get("credentials"), name
        assert pkg.get("buckets"), name
