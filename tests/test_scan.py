import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("bbscan", os.path.join(ROOT, "scripts", "bb-scan.py"))
bbscan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bbscan)


def test_sh_script_with_arrays_is_flagged():
    text = "#!/bin/sh\nPOLICIES=($(kubectl get cpol))\nfor p in \"${POLICIES[@]}\"; do :; done\n"
    [hit] = bbscan.shell_findings("wait/wait.sh", text)
    assert "array assignment" in hit and "array index" in hit


def test_bash_script_is_not_flagged():
    assert bbscan.shell_findings("wait/wait.sh", "#!/bin/bash\nA=(x)\necho ${A[0]}\n") == []


def test_posix_sh_is_not_flagged():
    assert bbscan.shell_findings("wait/wait.sh", "#!/bin/sh\nif [ -n \"$x\" ]; then echo ok; fi\n") == []


def test_sh_c_template_with_here_string_is_flagged():
    text = 'command:\n  - /bin/sh\n  - -c\n  - |\n    while read l; do echo $l; done <<< "$out"\n'
    [hit] = bbscan.shell_findings("templates/job.yaml", text)
    assert "here-string" in hit


def test_comments_are_ignored():
    text = 'command: ["/bin/sh"]\nargs:\n- -c\n- |\n  # A=(x) would need bash\n  echo ok\n'
    assert bbscan.shell_findings("templates/job.yaml", text) == []


def test_empty_default_written_with_toyaml():
    values = "tempo:\n  receivers:\n    otlp: {}\n    opencensus:\n"
    templates = ["receivers:\n  {{- toYaml .Values.tempo.receivers | nindent 2 }}\n"]
    [hit] = bbscan.null_findings("tempo", values, templates)
    assert "tempo.receivers.opencensus" in hit


def test_empty_default_not_written_is_ignored():
    values = "tempo:\n  receivers:\n    opencensus:\n"
    assert bbscan.null_findings("tempo", values, ["{{ .Values.tempo.receivers.otlp }}"]) == []


def test_helmrelease_writing_post_renderers_without_the_key_is_flagged():
    broken = "  {{- if .Values.addons.keycloak.postRenderers }}\n    {{ toYaml .Values.addons.keycloak.postRenderers }}\n"
    [hit] = bbscan.renderer_findings("bigbang/templates/keycloak/helmrelease.yaml", broken)
    assert "no_post_renderers" in hit
    good = "  postRenderers:\n  {{ toYaml .Values.tempo.postRenderers | nindent 2 }}\n"
    assert bbscan.renderer_findings("bigbang/templates/tempo/helmrelease.yaml", good) == []
