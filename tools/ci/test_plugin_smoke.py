"""Regression tests for the resource-sensitive CI selection boundary."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

spec = importlib.util.spec_from_file_location("plugin_smoke", Path(__file__).with_name("plugin_smoke.py"))
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


@pytest.fixture
def suites():
    return ci.manifests()


@pytest.mark.parametrize("path", [
    "node/engines/music_generate/runner.py",
    "node/engines/music_generate/symbolic/transfer.py",
    "node/engines/music_generate/setup_env.py",
    "node/engines/music_generate/ci.toml",
    "node/tools/smoke_music_generate.py",
])
def test_music_changes_only_select_music(suites, path):
    assert ci.select([path], suites) == ["music_generate"]


@pytest.mark.parametrize("path", [
    "node/mmp_node/scheduler.py", "node/mmp_node/cache.py", "node/mmp_node/node.py",
    "protocol/schemas/job-api.schema.json", "protocol/schemas/ab-message.schema.json",
    "ts/packages/client/src/client.ts", "node/mmp_broker/core.py",
])
def test_public_protocol_queue_cache_do_not_start_models(suites, path):
    assert ci.select([path], suites) == []


@pytest.mark.parametrize("path", ci.INTERNAL_PROTOCOL)
def test_internal_protocol_selects_every_plugin(suites, path):
    assert ci.select([path], suites) == list(suites)


def test_cpu_tools_do_not_start_unrelated_models(suites):
    assert ci.select(["node/engines/media_image/runner.py"], suites) == ["media_image"]
    assert ci.select(["node/engines/common/media_tools.py"], suites) == ["media_audio", "media_image"]


def test_shared_plugin_implementation_selects_its_consumers(suites):
    assert ci.select(["node/engines/ocr_structured/pages.py"], suites) == ["ocr_structured", "triage_image"]
    assert ci.select(["node/engines/image_generate/runner.py"], suites) == ["image_generate", "image_generate_internal"]
    assert ci.select(["node/engines/triage_image_large.py"], suites) == ["triage_image"]


def test_manual_full_smoke_and_combined_changes(suites):
    assert ci.select([], suites, full=True) == list(suites)
    assert ci.select(["node/engines/music_generate/runner.py", "node/engines/media_image/runner.py"], suites) == ["media_image", "music_generate"]


def test_new_plugins_own_their_selection_and_commands(tmp_path):
    plugin = tmp_path / "node/engines/new_tool"
    plugin.mkdir(parents=True)
    (plugin / "__main__.py").touch()
    with pytest.raises(FileNotFoundError):
        ci.manifests(tmp_path)
    (plugin / "ci.toml").write_text('smoke = [["{python}", "smoke.py"]]\n')
    suites = ci.manifests(tmp_path)
    assert ci.select(["node/engines/new_tool/nested/code.py"], suites) == ["new_tool"]


def test_serial_runner_expands_environment_and_stops_at_failure(monkeypatch):
    calls = []
    monkeypatch.setenv("PLUGIN_PYTHON", "interpreter with spaces")

    def execute(argv, **kwargs):
        calls.append(argv)
        if argv[-1] == "fail":
            raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(ci.subprocess, "run", execute)
    with pytest.raises(subprocess.CalledProcessError):
        ci.run(["one", "two"], {
            "one": {"smoke": [["{python}", "ok"], ["$PLUGIN_PYTHON", "fail"]]},
            "two": {"smoke": [["{python}", "must-not-run"]]},
        })
    assert calls == [[sys.executable, "ok"], ["interpreter with spaces", "fail"]]


def test_git_comparison_covers_pr_history_push_and_renames(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("config", "user.email", "ci@example.invalid")
    git("config", "user.name", "CI test")
    source = tmp_path / "old.py"
    source.write_text("original\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("switch", "-qc", "feature")
    source.rename(tmp_path / "new.py")
    git("add", "-A")
    git("commit", "-qm", "rename")
    head = git("rev-parse", "HEAD")
    git("switch", "-qc", "advanced-base", base)
    (tmp_path / "base-only.py").touch()
    git("add", ".")
    git("commit", "-qm", "advance base")
    advanced = git("rev-parse", "HEAD")
    monkeypatch.setattr(ci, "ROOT", tmp_path)
    assert sorted(ci.changed_paths(advanced, head, "pull_request")) == ["new.py", "old.py"]
    assert sorted(ci.changed_paths(base, head, "push")) == ["new.py", "old.py"]
    with pytest.raises(subprocess.CalledProcessError):
        ci.changed_paths("missing-base", head, "push")
