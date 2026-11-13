"""Dependabot's pull requests skip the expensive workflows (the billed RKE2 runner, the
half-hour CVE report, the FIPS-kernel VM) and still run CI, the builds and the kind tests,
which exercise the actions it bumps."""
import os

import pytest
import yaml

from conftest import ROOT

SKIP = "github.event.pull_request.user.login != 'dependabot[bot]'"


def jobs(name):
    return yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8"))["jobs"]


@pytest.mark.parametrize("workflow, job", [("rke2.yml", "rke2"), ("cve-report.yml", "report"),
                                           ("fips-kernel.yml", "plan")])
def test_expensive_jobs_skip_dependabot_pull_requests_only(workflow, job):
    cond = jobs(workflow)[job]["if"]
    assert SKIP in cond
    # Scheduled and manual runs (and everyone else's pull requests) are unchanged.
    assert cond.startswith("github.event_name != 'pull_request' ||")


def test_the_fips_kernel_test_job_follows_its_plan():
    assert jobs("fips-kernel.yml")["test"]["needs"] == "plan"


@pytest.mark.parametrize("workflow", ["ci.yml", "builds.yml", "compat.yml"])
def test_the_checks_that_exercise_bumped_actions_still_run_for_dependabot(workflow):
    for name, job in jobs(workflow).items():
        assert "dependabot" not in str(job.get("if", "")), f"{workflow}:{name}"
