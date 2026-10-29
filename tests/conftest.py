"""Make scripts/ importable; load the hyphenated command scripts as modules."""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
sys.path.insert(0, SCRIPTS)


def load_script(name):
    """Import scripts/<name>.py (e.g. bb-convert) as a module."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), os.path.join(SCRIPTS, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fixture_text(name):
    return open(os.path.join(FIXTURES, name), encoding="utf-8").read()


class FakeTags:
    """Stands in for orgtags.OrgTags: {repo: [tags]}; a repo not listed is missing."""

    def __init__(self, repos, org="example.com"):
        self.repos = repos
        self.org = org

    def tags(self, repo):
        return self.repos.get(repo)


@pytest.fixture
def fake_tags():
    return FakeTags
