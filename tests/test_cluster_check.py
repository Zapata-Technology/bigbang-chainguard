"""cluster-check.py's verdict on a whole cluster (the RKE2 test), without a cluster."""
from conftest import load_script

check = load_script("cluster-check")


def pod(name, phase="Running", ready=True, restarts=0, waiting=None, ns="ns", last=None):
    s = {"name": "app", "ready": ready, "restartCount": restarts, "state": {"running": {}}}
    if waiting:
        s["state"] = {"waiting": {"reason": waiting, "message": "pull access denied"}}
    if last:
        s["lastState"] = {"terminated": last}
    return {"metadata": {"namespace": ns, "name": name}, "status": {"phase": phase, "containerStatuses": [s]}}


def test_healthy_pods_and_finished_jobs_pass():
    assert check.pod_problems([pod("a"), pod("job-x", phase="Succeeded", ready=False)]) == []


def test_a_job_that_retried_and_finished_passes():
    assert check.pod_problems([pod("helm-install-x", phase="Succeeded", ready=False, restarts=1)]) == []


def test_not_ready_pending_and_failed_pods_fail():
    problems = check.pod_problems([pod("a", ready=False), pod("b", phase="Pending"), pod("c", phase="Failed")])
    assert problems == ["ns/a is Running, not Ready", "ns/b is Pending, not Ready", "ns/c is Failed, not Ready"]


def test_a_pod_with_no_container_status_yet_is_not_ready():
    p = {"metadata": {"namespace": "ns", "name": "new"}, "status": {"phase": "Running"}}
    assert check.pod_problems([p]) == ["ns/new is Running, not Ready"]


def test_restarts_fail_past_the_allowance_and_name_the_last_exit():
    p = pod("a", restarts=2, last={"reason": "OOMKilled", "exitCode": 137})
    assert check.pod_problems([p]) == ["ns/a container app restarted 2 times (last: OOMKilled, exit 137)"]
    assert check.pod_problems([p], max_restarts=2) == []


def test_startup_restarts_then_settled_are_notes():
    # NeuVector's exporter exits until the controller answers, then runs.
    now = check.datetime.datetime(2026, 11, 5, 16, 30, tzinfo=check.datetime.timezone.utc)
    p = pod("exporter", restarts=4, last={"reason": "Error", "exitCode": 1})
    p["status"]["containerStatuses"][0]["state"] = {"running": {"startedAt": "2026-11-05T16:10:00Z"}}
    notes = []
    assert check.pod_problems([p], now=now, notes=notes) == []
    assert "restarted 4 times" in notes[0] and "while starting" in notes[0]
    # Recently restarted, or restarting too often: still a problem.
    p["status"]["containerStatuses"][0]["state"] = {"running": {"startedAt": "2026-11-05T16:25:00Z"}}
    assert check.pod_problems([p], now=now, notes=[])
    p["status"]["containerStatuses"][0].update(restartCount=6, state={"running": {"startedAt": "2026-11-05T16:00:00Z"}})
    assert check.pod_problems([p], now=now, notes=[])


def test_settling_waits_for_startup_restarts_only():
    now = check.datetime.datetime(2026, 11, 5, 16, 30, tzinfo=check.datetime.timezone.utc)
    p = pod("exporter", restarts=4)
    p["status"]["containerStatuses"][0]["state"] = {"running": {"startedAt": "2026-11-05T16:28:00Z"}}
    assert check.settling([p], now=now) == check.SETTLED - 120
    assert check.settling([pod("calm")], now=now) == 0
    p["status"]["containerStatuses"][0]["restartCount"] = 9      # a crash loop: no waiting for it
    assert check.settling([p], now=now) == 0


def test_image_pull_errors_fail():
    problems = check.pod_problems([pod("a", phase="Pending", ready=False, waiting="ImagePullBackOff")])
    assert problems[0] == "ns/a container app: ImagePullBackOff (pull access denied)"
    assert problems[1] == "ns/a is Pending, not Ready"


def test_admission_refusals_fail_and_recovered_pulls_are_notes():
    lines = "\n".join([
        "neuvector\tWarning\tFailedCreate\tDaemonSet/neuvector-enforcer-pod\terror creating: pods "
        "\"neuvector-enforcer-pod-x\" is forbidden: violates PodSecurity \"restricted:latest\": privileged",
        "neuvector\tWarning\tFailedCreate\tDaemonSet/neuvector-enforcer-pod\t(combined from similar events): "
        "error creating: pods \"neuvector-enforcer-pod-y\" is forbidden: violates PodSecurity \"restricted:latest\": privileged",
        "app\tWarning\tFailedCreate\tReplicaSet/app-1\tadmission webhook \"validate.kyverno.svc-fail\" denied the request",
        "tempo\tWarning\tFailed\tPod/tempo-0\tFailed to pull image \"cgr.dev/org/tempo:2\": 503",
        "tempo\tNormal\tPulled\tPod/tempo-0\tSuccessfully pulled image",
        "a line without five fields",
    ])
    events = check.parse_event_lines(lines)
    assert len(events) == 5
    problems, notes = check.event_problems(events)
    assert len(problems) == 2   # the repeated refusal once
    assert problems[0].startswith("neuvector/DaemonSet/neuvector-enforcer-pod refused admission (FailedCreate) "
                                  "[seen 2 times]: error creating: pods is forbidden")
    assert "kyverno" in problems[1]
    assert notes == ["tempo/Pod/tempo-0 pull failed at some point: Failed to pull image \"cgr.dev/org/tempo:2\": 503"]


def test_helmreleases_not_ready_fail_with_flux_s_reason():
    def hr(name, status, msg=""):
        return {"metadata": {"namespace": "bigbang", "name": name},
                "status": {"conditions": [{"type": "Ready", "status": status, "message": msg}]}}
    problems = check.helmrelease_problems([hr("kyverno", "True"), hr("istiod", "False", "timeout waiting for istiod"),
                                           {"metadata": {"namespace": "bigbang", "name": "new"}}])
    assert problems == ["HelmRelease bigbang/istiod not Ready: timeout waiting for istiod",
                        "HelmRelease bigbang/new not Ready: no status yet"]


def test_json_events_match_the_watched_lines():
    doc = {"items": [{"metadata": {"namespace": "ns"}, "type": "Warning", "reason": "FailedCreate",
                      "involvedObject": {"kind": "ReplicaSet", "name": "x"}, "message": "m"}]}
    assert check.json_events(doc) == [("ns", "Warning", "FailedCreate", "ReplicaSet/x", "m")]


def test_one_deployment_refused_under_two_templates_is_one_problem():
    msg = 'Error creating: pods "{}" is forbidden: violates PodSecurity "restricted:latest": seccompProfile'
    events = [("flux-system", "Warning", "FailedCreate", f"ReplicaSet/helm-controller-{h}",
               msg.format(f"helm-controller-{h}-x")) for h in ("6b69db45bf", "769c8f4f9f")]
    problems, _ = check.event_problems(events)
    assert problems == ["flux-system/ReplicaSet/helm-controller refused admission (FailedCreate) [seen 2 times]: "
                        'Error creating: pods is forbidden: violates PodSecurity "restricted:latest": seccompProfile']
