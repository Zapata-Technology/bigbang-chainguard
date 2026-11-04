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


def test_image_pull_errors_fail():
    problems = check.pod_problems([pod("a", phase="Pending", ready=False, waiting="ImagePullBackOff")])
    assert problems[0] == "ns/a container app: ImagePullBackOff (pull access denied)"
    assert problems[1] == "ns/a is Pending, not Ready"


def test_admission_refusals_fail_and_recovered_pulls_are_notes():
    lines = "\n".join([
        "neuvector\tWarning\tFailedCreate\tDaemonSet/neuvector-enforcer-pod\terror creating: pods "
        "\"neuvector-enforcer-pod-x\" is forbidden: violates PodSecurity \"restricted:latest\": privileged",
        "neuvector\tWarning\tFailedCreate\tDaemonSet/neuvector-enforcer-pod\terror creating: pods "
        "\"neuvector-enforcer-pod-x\" is forbidden: violates PodSecurity \"restricted:latest\": privileged",
        "app\tWarning\tFailedCreate\tReplicaSet/app-1\tadmission webhook \"validate.kyverno.svc-fail\" denied the request",
        "tempo\tWarning\tFailed\tPod/tempo-0\tFailed to pull image \"cgr.dev/org/tempo:2\": 503",
        "tempo\tNormal\tPulled\tPod/tempo-0\tSuccessfully pulled image",
        "a line without five fields",
    ])
    events = check.parse_event_lines(lines)
    assert len(events) == 5
    problems, notes = check.event_problems(events)
    assert len(problems) == 2   # the repeated refusal once
    assert problems[0].startswith("neuvector/DaemonSet/neuvector-enforcer-pod refused admission (FailedCreate)")
    assert "kyverno" in problems[1]
    assert notes == ["tempo/Pod/tempo-0 pull failed at some point: Failed to pull image \"cgr.dev/org/tempo:2\": 503"]


def test_json_events_match_the_watched_lines():
    doc = {"items": [{"metadata": {"namespace": "ns"}, "type": "Warning", "reason": "FailedCreate",
                      "involvedObject": {"kind": "ReplicaSet", "name": "x"}, "message": "m"}]}
    assert check.json_events(doc) == [("ns", "Warning", "FailedCreate", "ReplicaSet/x", "m")]
