"""bb-compat.py's decisions that need no cluster: the Iron Bank skip, its credentials, and which images count as run."""
import json
import sys

import yaml

from conftest import load_script

compat = load_script("bb-compat")
BOTH = {"REGISTRY1_USERNAME": "u", "REGISTRY1_PASSWORD": "p"}


def test_registry1_skip_only_for_tests_that_need_it():
    assert compat.registry1_skip({}, {}) is None
    assert compat.registry1_skip({"registry1": True}, BOTH) is None
    why = compat.registry1_skip({"registry1": True}, {})
    assert "REGISTRY1_USERNAME and REGISTRY1_PASSWORD not set" in why
    assert compat.registry1_skip({"registry1": True}, {"REGISTRY1_USERNAME": "u"}).endswith("REGISTRY1_PASSWORD not set")


def test_registry1_test_without_credentials_is_skipped_before_anything_runs(tmp_path, monkeypatch, capsys):
    f = tmp_path / "needs-ironbank.yaml"
    f.write_text(yaml.safe_dump({"images": {"ironbank/x": {"build": "x"}},
                                 "test": {"registry1": True, "enable": ["addons.x"]}}))
    for k in compat.REGISTRY1_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    ran = []
    # Nothing may be built, converted or deployed: any subprocess call fails the test.
    monkeypatch.setattr(compat.subprocess, "run", lambda *a, **k: ran.append(a) or (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(sys, "argv", ["bb-compat.py", str(f), "--fips"])
    assert compat.main() == 0
    out = capsys.readouterr().out
    assert "RESULT needs-ironbank.yaml (FIPS): SKIPPED (needs Iron Bank pulls" in out
    assert "::notice title=SKIPPED needs-ironbank.yaml (FIPS)::" in out
    assert not ran


def test_registry1_credentials_reach_the_cluster_pull_secret(monkeypatch):
    monkeypatch.setenv("CGR_TOKEN", "t")
    monkeypatch.delenv("GHCR_TOKEN", raising=False)
    for k, v in BOTH.items():
        monkeypatch.setenv(k, v)
    values, cfg = compat.credentials()
    assert [r["registry"] for r in values["registryCredentials"]] == ["cgr.dev", "registry1.dso.mil"]
    assert set(cfg["auths"]) == {"cgr.dev", "registry1.dso.mil"}
    monkeypatch.delenv("REGISTRY1_PASSWORD")
    values, _ = compat.credentials()
    assert [r["registry"] for r in values["registryCredentials"]] == ["cgr.dev"]


def test_only_started_containers_count_as_exercised():
    started_images = load_script("bbcluster").started_images
    pod = lambda status: {"spec": {"initContainers": [{"name": "init", "image": "org/init:1"}],
                                   "containers": [{"name": "app", "image": "org/app:1"}]},
                          "status": status}
    assert started_images(pod({"phase": "Pending"})) == set()
    waiting = {"name": "app", "image": "org/app:1", "state": {"waiting": {"reason": "ImagePullBackOff"}}}
    failed_init = {"name": "init", "image": "org/init:1", "state": {"terminated": {"exitCode": 1}}}
    assert started_images(pod({"containerStatuses": [waiting], "initContainerStatuses": [failed_init]})) == set()
    done_init = {"name": "init", "image": "org/init:1", "state": {"terminated": {"exitCode": 0}}}
    running = {"name": "app", "image": "org/app@sha256:ab", "state": {"running": {}}}
    assert started_images(pod({"containerStatuses": [running], "initContainerStatuses": [done_init]})) == \
        {"org/init:1", "org/app:1", "org/app@sha256:ab"}


def _pod(name, statuses, containers=("app",)):
    return {"metadata": {"name": name},
            "spec": {"containers": [{"name": c, "image": f"org/{c}:1"} for c in containers]},
            "status": {"containerStatuses": statuses}}


def test_terminations_are_oom_kills_and_non_zero_exits_now_or_before_a_restart():
    terminations = load_script("bbcluster").terminations
    oom = {"name": "app", "restartCount": 2, "state": {"running": {}},
           "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}
    assert terminations(_pod("p", [oom])) == [("app", "OOMKilled", 137)]
    crashed = {"name": "app", "state": {"terminated": {"reason": "Error", "exitCode": 1}}}
    assert terminations(_pod("p", [crashed])) == [("app", "Error", 1)]
    done = {"name": "app", "state": {"terminated": {"reason": "Completed", "exitCode": 0}}}
    assert terminations(_pod("p", [done])) == []


def test_an_oom_kill_fails_the_test_and_other_exits_do_not():
    exits = {("gitlab", "migrations-x", "migrations"): ("OOMKilled", 137),
             ("gitlab", "webservice-y", "dependencies"): ("Error", 1)}
    [p] = compat.oom_problems(exits)
    assert p.startswith("gitlab/migrations-x container migrations was OOMKilled")


def test_log_keeper_keeps_logs_of_a_restarted_container_after_its_pod_is_gone(tmp_path, monkeypatch):
    bbcluster = load_script("bbcluster")
    status = {"name": "app", "restartCount": 1, "state": {"running": {}},
              "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}
    pods = {"items": [_pod("migrations-x", [status])]}

    def kubectl(*args, check=True):
        if args[:2] == ("get", "pod"):
            return json.dumps(pods)
        if args[0] == "logs":
            return "previous run\n" if "--previous" in args else "current run\n"
        return ""
    monkeypatch.setattr(bbcluster, "kubectl", kubectl)
    keeper = bbcluster.PodLogKeeper(lambda: ["gitlab"], str(tmp_path))
    keeper.snapshot()
    pods["items"] = []          # Flux uninstalled the release: nothing left to read
    keeper.snapshot()
    logs = tmp_path / "logs" / "gitlab"
    assert (logs / "migrations-x.app.log").read_text() == "current run\n"
    assert (logs / "migrations-x.app.previous.log").read_text() == "previous run\n"
    assert keeper.exits == {("gitlab", "migrations-x", "app"): ("OOMKilled", 137)}
    assert "gitlab/migrations-x app: OOMKilled (exit 137)" in (tmp_path / "exits.txt").read_text()


def test_progress_deadline_keeps_the_overlays_post_renderers(tmp_path):
    import os
    import subprocess
    pins = {"kustomize": {"images": [{"name": "org/gitlab-webservice", "digest": "sha256:aa"}]}}
    fips_pins = {"kustomize": {"images": [{"name": "org/gitlab-webservice-fips", "digest": "sha256:bb"}]}}
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "values-chainguard.yaml").write_text(
        yaml.safe_dump({"addons": {"gitlab": {"postRenderers": [pins]}}}))
    (tmp_path / "overlays" / "values-chainguard-fips.yaml").write_text(
        yaml.safe_dump({"addons": {"gitlab": {"postRenderers": [fips_pins]}}}))
    script = os.path.join(os.path.dirname(__file__), "..", "compat", "support", "progress-deadline.py")
    for fips, expect in (("", pins), ("1", fips_pins)):
        subprocess.run([sys.executable, script, "addons.gitlab", "2400"], check=True, capture_output=True,
                       env=dict(os.environ, COMPAT_OUT=str(tmp_path), COMPAT_FIPS=fips))
        out = yaml.safe_load((tmp_path / "site-gitlab-deadline.yaml").read_text())
        first, patch = out["addons"]["gitlab"]["postRenderers"]
        assert first == expect     # Helm replaces lists: the digest pins must stay
        [p] = patch["kustomize"]["patches"]
        assert p["target"] == {"kind": "Deployment"}
        assert yaml.safe_load(p["patch"]) == [{"op": "add", "path": "/spec/progressDeadlineSeconds", "value": 2400}]


def _watch(tmp_path, monkeypatch, pods, jobs=(), logs=None, clock=None):
    """A PodLogKeeper over fake kubectl output: `pods` and `jobs` for namespace ns, `logs`
    {(pod, container, previous): text}."""
    bbcluster = load_script("bbcluster")
    logs = logs or {}

    def kubectl(*args, check=True):
        if args[:2] == ("get", "pod"):
            return json.dumps({"items": pods})
        if args[:2] == ("get", "job"):
            return json.dumps({"items": list(jobs)})
        if args[0] == "logs":
            return logs.get((args[3], args[5], "--previous" in args), "")
        return ""
    monkeypatch.setattr(bbcluster, "kubectl", kubectl)
    return bbcluster.PodLogKeeper(lambda: ["ns"], str(tmp_path), clock=clock or (lambda: 0))


def test_live_prints_each_containers_first_error_line_once(tmp_path, monkeypatch, capsys):
    running = {"name": "app", "state": {"running": {}}}
    keeper = _watch(tmp_path, monkeypatch, [_pod("web-1", [running])],
                    logs={("web-1", "app", False): "starting\npanic: runtime error\npanic: again\n"})
    keeper.snapshot()
    keeper.snapshot()
    out = capsys.readouterr().out
    assert out.count(" LIVE ") == 1
    assert "LIVE ns/web-1 app: panic: runtime error" in out
    assert keeper.abort_reasons() == []      # one error line alone doesn't stop the test


def test_crash_looping_on_an_error_stops_the_test_but_a_waiting_init_container_does_not(tmp_path, monkeypatch):
    def crashing(restarts, log):
        s = {"name": "app", "restartCount": restarts, "state": {"waiting": {"reason": "CrashLoopBackOff"}},
             "lastState": {"terminated": {"reason": "Error", "exitCode": 1}}}
        return _pod("migrations-x", [s]), {("migrations-x", "app", True): log}
    pod, logs = crashing(2, "ERROR: out of shared memory\nrake aborted!\n")
    keeper = _watch(tmp_path, monkeypatch, [pod], logs=logs)
    keeper.snapshot()
    assert keeper.abort_reasons() == []      # two restarts: it may still recover
    pod, logs = crashing(3, "ERROR: out of shared memory\nrake aborted!\n")
    keeper = _watch(tmp_path, monkeypatch, [pod], logs=logs)
    keeper.snapshot()
    [why] = keeper.abort_reasons()
    assert "ns/migrations-x container app restarted 3 times" in why and "out of shared memory" in why
    # GitLab's `dependencies` init container exits 1 until the migrations are done: no error line.
    pod, logs = crashing(9, "Checking database migrations are up-to-date\nWaiting for migrations\n")
    keeper = _watch(tmp_path, monkeypatch, [pod], logs=logs)
    keeper.snapshot()
    assert keeper.abort_reasons() == []


def test_oom_kill_and_failed_job_stop_the_test(tmp_path, monkeypatch):
    oom = {"name": "app", "restartCount": 1, "state": {"running": {}},
           "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}
    failed = {"metadata": {"name": "migrations-x"},
              "status": {"conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded",
                                         "message": "Job has reached the specified backoff limit"}]}}
    keeper = _watch(tmp_path, monkeypatch, [_pod("web-1", [oom])], jobs=[failed])
    keeper.snapshot()
    reasons = keeper.abort_reasons()
    assert "ns/web-1 container app was OOMKilled" in reasons
    assert any(r.startswith("Job ns/migrations-x failed: BackoffLimitExceeded") for r in reasons)


def test_an_image_that_cant_be_pulled_stops_the_test_after_five_minutes(tmp_path, monkeypatch):
    now = [1000]
    stuck = {"name": "app", "state": {"waiting": {"reason": "ImagePullBackOff", "message": "not found"}}}
    keeper = _watch(tmp_path, monkeypatch, [_pod("web-1", [stuck])], clock=lambda: now[0])
    keeper.snapshot()
    now[0] += 299
    keeper.snapshot()
    assert keeper.abort_reasons() == []      # a slow pull may still finish
    now[0] += 1
    keeper.snapshot()
    [why] = keeper.abort_reasons()
    assert why.startswith("ns/web-1 container app: ImagePullBackOff for 300s")


def test_wait_ready_ends_as_soon_as_the_watcher_has_a_reason(monkeypatch):
    bbcluster = load_script("bbcluster")
    monkeypatch.setattr(bbcluster, "kubectl", lambda *a, **k: json.dumps({"items": []}))
    monkeypatch.setattr(bbcluster.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("waited")))
    ok, problems = bbcluster.wait_ready(["ns"], 3600, abort=lambda: ["ns/web-1 container app was OOMKilled"])
    assert not ok and problems == ["stopped early: ns/web-1 container app was OOMKilled"]
