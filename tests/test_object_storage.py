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
                 "region": "us-east-1", "prefix": "bb", "insecure": True, "ssl": "false"}
    assert storage.tokens("https://s3.example.com", "r", "p")["insecure"] is False
    assert storage.tokens("https://s3.example.com", "r", "p")["ssl"] == "true"
    with pytest.raises(SystemExit):
        storage.tokens("seaweedfs:8333", "r", "p")


def test_fill():
    t = storage.tokens("http://s3:8333", "us-east-1", "bb")
    assert storage.fill({"a": "{insecure}", "b": "{bucket:x}-{region}", "c": ["{host}"], "d": 1, "e": "{ssl}"}, t) == \
        {"a": True, "b": "bb-x-us-east-1", "c": ["s3:8333"], "d": 1, "e": "false"}


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
    assert tree["v"]["secretContents"]["cloud"].startswith("[default]") and "<access key>" in tree["v"]["secretContents"]["cloud"]


def test_every_package_lists_credentials_and_buckets():
    for name, pkg in SPEC["packages"].items():
        assert pkg["values"], name
        assert pkg.get("credentials"), name
        assert pkg.get("buckets"), name


def test_object_storage_setting(monkeypatch):
    import objstore
    monkeypatch.delenv("OBJECT_STORAGE", raising=False)
    monkeypatch.setattr(objstore, "setting", lambda name, arg=None: arg or "")
    assert objstore.kind() == "seaweedfs"
    assert objstore.kind("MinIO") == "minio"
    with pytest.raises(SystemExit):
        objstore.kind("ceph")
    assert objstore.endpoint() == objstore.DEFAULT_ENDPOINT


def test_seaweedfs_manifests_require_s3_auth_and_create_buckets():
    import objstore
    files = objstore.seaweedfs_files("cgr.dev/o/seaweedfs:v4.51", "cgr.dev/o/seaweedfs:v4.51-dev",
                                     ["bigbang-tempo", "bigbang-velero"], "# h\n")
    docs = list(yaml.safe_load_all(files["seaweedfs.yaml"]))
    server = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert "-s3.config=/etc/seaweedfs/s3.json" in server["containers"][0]["args"]
    assert server["securityContext"]["runAsNonRoot"] is True
    job = next(d for d in docs if d["kind"] == "Job")["spec"]["template"]["spec"]["containers"][0]
    assert job["image"].endswith("-dev") and "s3.bucket.create -name bigbang-velero" in job["command"][2]
    assert yaml.safe_load(files["kustomization.yaml"])["namespace"] == "seaweedfs"


def test_minio_removes_the_seaweedfs_files():
    from conftest import FakeTags
    convert = load_script("bb-convert")
    c = convert.Converter({"images": {}}, FakeTags({}))
    files = {"docs/images.md": "", "docs/required-images.md": ""}
    out = convert.object_storage_files("minio", "http://x:1", c, FakeTags({}), "3.35.1", files)
    assert out and all(v is None for v in out.values())
    assert "OBJECT_STORAGE=minio" in files["docs/images.md"]


def test_credentials_only_for_enabled_packages():
    import objstore
    spec = objstore.load_spec()
    _, paths, _, _ = objstore.build(spec, objstore.tokens(objstore.DEFAULT_ENDPOINT, "us-east-1", "bigbang"), set())
    assert {objstore.package_of(p) for p in paths} >= {"tempo", "mimir", "velero"}
    only = objstore.credentials_for(paths, ["tempo", "velero"])
    assert only and {objstore.package_of(p) for p in only} == {"tempo", "velero"}
    # Mimir's Secret is created whenever its keys are set, so they must not leak in.
    assert not any(".mimir." in p for p in only)


def test_package_of():
    import objstore
    assert objstore.package_of("addons.mimir.s3ObjectStorage.accessKey") == "mimir"
    assert objstore.package_of("tempo.objectStorage.accessSecret") == "tempo"
