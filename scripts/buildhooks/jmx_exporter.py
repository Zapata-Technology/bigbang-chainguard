"""Rebuild Iron Bank's opensource/prometheus/jmx-exporter FROM the org's busybox.

Big Bang uses this image only as SonarQube's `inject-prometheus-exporter` init
container (with monitoring on), which runs

  /bin/sh -c "curl -s 'file:///opt/jmx_exporter/jmx_prometheus_javaagent-<v>.jar'
              --output /data/jmx_prometheus_javaagent.jar -v"

as uid 1000 with a read-only root filesystem, copying the Java agent jar into
SonarQube's volume. So it needs sh, curl and the jar at Iron Bank's path, named for
Big Bang's version (the chart's downloadURL names it). Chainguard's
jmx-exporter-iamguarded has the Bitnami layout and no curl, so this converts Iron
Bank's own recipe:

  1. read the Iron Bank tag Big Bang deploys (discovery/images.json)
  2. read Iron Bank's hardening_manifest.yaml (the javaagent jar's URL and sha256) and
     Dockerfile (the jar's directory, the uid) from repo1.dso.mil at the newest commit
     that builds that version (master may already be on a newer jar)
  3. download the jar, check its sha256 and that it is a Java agent
  4. pin cgr.dev/<org>/busybox and busybox-fips -dev images (the newest release, or the
     line --version names) by digest; their sh is busybox, and curl comes from Wolfi
  5. return builds/jmx-exporter/build.yaml

The smoke test (recipe `smoke`) runs the init container's command as SonarQube's chart
does (uid 1000, gid 0, read-only root, /data mounted) and checks the copied jar's sha256.

Recipe keys: ironbank {image, project}, org_images, packages, not_ported.
"""
import hashlib
import io
import json
import re
import urllib.error
import urllib.parse
import zipfile

import yaml

import bbbuild


def jar_name(version):
    return f"jmx_prometheus_javaagent-{version}.jar"


def recipe(manifest, dockerfile, version):
    """The javaagent jar of `version` (url, sha256), its directory and the uid, from Iron Bank's recipe."""
    jar = next((r for r in manifest.get("resources", []) if r.get("filename") == jar_name(version)), None)
    if not jar:
        raise SystemExit(f"Iron Bank's manifest has no {jar_name(version)}")
    if jar["validation"]["type"] != "sha256":
        raise SystemExit(f"{jar['url']}: Iron Bank's manifest pins it by {jar['validation']['type']}, not sha256")
    text = dockerfile.replace("\\\n", " ")
    copy = next((ln.split() for ln in text.splitlines()
                 if re.match(r"\s*COPY\s", ln) and "jmx_prometheus_javaagent-" in ln), None)
    users = re.findall(r"^\s*USER\s+(\S+)", text, re.M)
    if not (copy and users):
        raise SystemExit("Iron Bank's Dockerfile changed shape (COPY of the javaagent jar / USER); update recipe()")
    # The last USER wins: `USER 1000` (1.6.0) or `USER ${USER_ID}` with `ARG USER_ID=1001` (1.7.0).
    user = users[-1]
    if m := re.fullmatch(r"\$\{?(\w+)\}?", user):
        arg = re.search(rf"^\s*ARG\s+{m.group(1)}=(\d+)\s*$", text, re.M)
        user = arg.group(1) if arg else ""
    if not user.isdigit():
        raise SystemExit(f"Iron Bank's Dockerfile runs as {users[-1]!r}, not a numeric uid; update recipe()")
    return {"url": jar["url"], "sha256": jar["validation"]["value"], "dir": copy[-1].rstrip("/"),
            "name": jar_name(version)}, int(user)


def find_recipe(project, ref, version):
    """(commit, manifest, Dockerfile) of the newest Iron Bank commit on `ref` that builds `version`.

    Iron Bank's master moves to a new jar before Big Bang does, and the chart names the
    jar by Big Bang's version, so walk back through the manifest's history.
    """
    commit, manifest, dockerfile = bbbuild.ironbank_recipe(project, ref)
    if version in [str(t) for t in manifest.get("tags", [])]:
        return commit, manifest, dockerfile
    quoted = urllib.parse.quote(f"dsop/{project}", safe="")
    history = json.loads(bbbuild.fetch(f"{bbbuild.REPO1}/api/v4/projects/{quoted}/repository/commits"
                                       f"?path=hardening_manifest.yaml&ref_name={urllib.parse.quote(ref)}&per_page=100"))
    for c in history:
        raw = f"{bbbuild.REPO1}/dsop/{project}/-/raw/{c['id']}"
        manifest = yaml.safe_load(bbbuild.fetch(f"{raw}/hardening_manifest.yaml"))
        if version in [str(t) for t in manifest.get("tags", [])]:
            return c["id"], manifest, bbbuild.fetch(f"{raw}/Dockerfile").decode()
    raise SystemExit(f"no commit of dsop/{project} on {ref} builds {version}")


def candidate_urls(url):
    """Iron Bank's URL, then the same release without a `v` on its tag: Iron Bank's 1.6.0
    manifest says .../download/v1.6.0/..., but upstream tags releases 1.6.0 (the file
    there has the manifest's sha256, which is what pins it)."""
    alt = re.sub(r"/releases/download/v(\d)", r"/releases/download/\1", url)
    return [url] + ([alt] if alt != url else [])


def check_jar(jar):
    """Download the jar from the first URL that answers, check Iron Bank's sha256 and that
    it is a Java agent. Returns the URL it came from (the build downloads it there)."""
    urls = candidate_urls(jar["url"])
    for i, url in enumerate(urls):
        try:
            data = bbbuild.fetch(url, attempts=4 if i == len(urls) - 1 else 1)
            break
        except urllib.error.HTTPError as e:
            if e.code != 404 or i == len(urls) - 1:
                raise
    if hashlib.sha256(data).hexdigest() != jar["sha256"]:
        raise SystemExit(f"{url}: sha256 does not match Iron Bank's manifest")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if "Premain-Class:" not in z.read("META-INF/MANIFEST.MF").decode(errors="replace"):
            raise SystemExit(f"{url}: not a Java agent (no Premain-Class)")
    return url


def generate(r, a):
    ib_image, ib_project = r["ironbank"]["image"], r["ironbank"]["project"]
    bb_tag = bbbuild.bigbang_tag(ib_image)
    commit, manifest, dockerfile = find_recipe(ib_project, a.ref, bb_tag)
    jar, uid = recipe(manifest, dockerfile, bb_tag)
    jar["url"], ib_url = check_jar(jar), jar["url"]
    images = bbbuild.org_images(a.org, r["org_images"], a.version or "*")
    spec = {
        "ironbank": {"image": f"{ib_image}:{bb_tag}", "source": f"{bbbuild.REPO1}/dsop/{ib_project}",
                     "commit": commit, "manifest_tags": manifest.get("tags", []), "jar_url": ib_url},
        # Tagged <Iron Bank tag>-busybox<version>, so a busybox release gets a new tag.
        "image": {v: {"name": bbbuild.image_name(r, v),
                      "tag": f"{bb_tag}-busybox{img['tag'].removeprefix('v')}", "dev": img["dev"]}
                  for v, img in images.items()},
        "jar": jar,
        # What Big Bang's sonarqube chart sets as upstream.prometheusExporter.downloadURL.
        "download_url": f"file://{jar['dir']}/{jar['name']}",
        "packages": r["packages"],
        "user": {"id": uid},
        "not_ported": r["not_ported"],
    }
    print(f"busybox {images['standard']['tag']} / fips {images['fips']['tag']}, "
          f"{jar['name']} (Iron Bank {commit[:12]})")
    return spec
