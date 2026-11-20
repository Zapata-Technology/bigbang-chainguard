"""ci-alert.py: one issue per failing scheduled workflow, closed by the next scheduled pass."""
import pytest

from conftest import load_script

alert = load_script("ci-alert")
WF = "RKE2 - Big Bang on RKE2 with the CIS profile"


class FakeGitHub:
    """ghrest.api / api_list over a list of open issues; every write is recorded."""

    def __init__(self, monkeypatch, issues=(), label_exists=True):
        self.issues = list(issues)
        self.calls = []
        self.label_exists = label_exists
        monkeypatch.setattr(alert.ghrest, "api_list", self.api_list)
        monkeypatch.setattr(alert.ghrest, "api", self.api)

    def api_list(self, path, key=None, **kw):
        assert path.startswith("issues?labels=ci-scheduled-failure&state=open")
        return self.issues

    def api(self, path, *args, **kw):
        self.calls.append((path, args))
        if path == "labels" and self.label_exists:
            raise alert.ghrest.GhError('gh api: HTTP 422: Validation Failed ("already_exists")')
        return ""


def issue(n, workflow=WF, pr=False):
    i = {"number": n, "title": alert.title(workflow)}
    if pr:
        i["pull_request"] = {}
    return i


def test_the_action_follows_the_result_and_the_open_issue():
    assert alert.action("failure", None) == "create"
    assert alert.action("cancelled", None) == "create"    # a time limit is a failure too
    assert alert.action("failure", 5) == "comment"
    assert alert.action("success", 5) == "close"
    assert alert.action("success", None) is None


def test_a_first_failure_opens_one_labeled_issue(monkeypatch):
    gh = FakeGitHub(monkeypatch, issues=[issue(3, "Update - Big Bang and Chainguard tags")])
    assert alert.alert(WF, "failure", "https://run/1", mention="@someone") == "create"
    [(path, args)] = [c for c in gh.calls if c[0] == "issues"]
    assert "-X" in args and "POST" in args
    assert f"title={alert.title(WF)}" in args and "labels[]=ci-scheduled-failure" in args
    body = next(a for a in args if a.startswith("body="))
    assert body.startswith("body=@someone ") and "https://run/1" in body


def test_a_second_failure_comments_on_the_open_issue(monkeypatch):
    gh = FakeGitHub(monkeypatch, issues=[issue(9, pr=True), issue(4)])
    assert alert.alert(WF, "failure", "https://run/2") == "comment"
    assert [c[0] for c in gh.calls] == ["issues/4/comments"]


def test_a_scheduled_success_closes_the_open_issue(monkeypatch):
    gh = FakeGitHub(monkeypatch, issues=[issue(4)])
    assert alert.alert(WF, "success", "https://run/3") == "close"
    assert [c[0] for c in gh.calls] == ["issues/4/comments", "issues/4"]
    assert gh.calls[1][1] == ("-X", "PATCH", "-f", "state=closed")


def test_a_success_with_no_issue_writes_nothing(monkeypatch):
    gh = FakeGitHub(monkeypatch)
    assert alert.alert(WF, "success", "https://run/4") is None
    assert gh.calls == []


def test_a_label_error_other_than_exists_stops_it(monkeypatch):
    FakeGitHub(monkeypatch, label_exists=False)

    def boom(path, *args, **kw):
        raise alert.ghrest.GhError("gh api: HTTP 403: Resource not accessible by integration")
    monkeypatch.setattr(alert.ghrest, "api", boom)
    with pytest.raises(alert.ghrest.GhError):
        alert.alert(WF, "failure", "https://run/5")
