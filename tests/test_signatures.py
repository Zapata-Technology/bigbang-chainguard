"""bb-signatures.py: every image checked, results reported in order while cosign runs in parallel."""
import subprocess
import threading

from conftest import load_script

sigs = load_script("bb-signatures")


def test_verify_all_reports_in_order_and_returns_the_failures(monkeypatch, capsys):
    calls = []
    lock = threading.Lock()

    def fake_run(cmd, **kw):
        with lock:
            calls.append(cmd[-1])
            first_try = calls.count(cmd[-1]) == 1
        # img3 fails once (parallel cosigns racing on their cache), then passes alone.
        bad = cmd[-1].endswith(":bad") or (cmd[-1].startswith("cgr.dev/o/img3:") and first_try)
        return subprocess.CompletedProcess(cmd, 1 if bad else 0, "", "Error: no signatures found\n" if bad else "")
    monkeypatch.setattr(sigs.subprocess, "run", fake_run)
    refs = [f"cgr.dev/o/img{i}:{'bad' if i in (2, 7) else '1'}" for i in range(10)]
    assert sigs.verify_all(refs, "issuer", "subject", 4) == [refs[2], refs[7]]
    assert sorted(calls) == sorted(refs + [refs[2], refs[3], refs[7]])   # each failure again, alone
    lines = [ln for ln in capsys.readouterr().out.splitlines() if not ln.startswith("    ")]
    assert lines == [f"  {'UNSIGNED or invalid:' if r.endswith(':bad') else 'ok'} {r}" for r in refs]


def test_verify_all_with_no_images():
    assert sigs.verify_all([], "issuer", "subject", 4) == []
