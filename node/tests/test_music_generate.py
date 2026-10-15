import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from conftest import build_stack
from engines.music_generate import CAPABILITIES, ENGINE, ENGINE_VERSION
from engines.music_generate.runner import render, request
from engines.music_generate.symbolic.abc import parse_abc
from engines.music_generate.symbolic.transfer import convert_score
from mmp_node.engine_io import EngineProcess, EngineResult
from mmp_node.registry import EngineSpec


@pytest.mark.parametrize("arguments", [
    {"style": " ", "lyrics": "[Verse]\nHello"},
    {"style": "pop", "lyrics": ""},
    {"style": "pop", "lyrics": "Hello", "options": {"seed": True}},
    {"style": "pop", "lyrics": "Hello", "options": {"seed": 2**32}},
    {"style": "pop", "lyrics": "Hello", "score": " "},
    {"style": "pop", "options": {"mode": "song"}},
    {"style": "pop", "lyrics": "Hello", "options": {"mode": "unknown"}},
])
def test_invalid_inputs_fail_before_loading(arguments):
    with pytest.raises(ValueError):
        request({"arguments": arguments})


async def test_music_queue_score_reuse_delivery_and_cache(monkeypatch, tmp_path, media_server):
    events = []
    active = 0

    class FakeProcess:
        def __init__(self, spec, *args):
            self.alive, self.busy, self.last_used = False, 0, time.monotonic()

        async def start(self):
            self.alive = True

        async def run(self, job, timeout_sec):
            nonlocal active
            active += 1
            assert active == 1, "music jobs must not overlap on the GPU"
            values = request(job)
            events.append(values)
            await asyncio.sleep(.02)
            active -= 1
            return EngineResult({
                "audio": {"kind": "media", "content_type": "audio/flac", "data_base64": "AAAA" * 2000},
                "score": {"kind": "text", "text": values.get("abc", "X:1\nK:C\nC D E F")},
                "metadata": {"kind": "json", "value": {
                    "sample_rate": 48000, "channels": 2, "duration_sec": 60, "seed": values["seed"],
                    "truncated": {"abc": False, "semantic": False}, "attention": "cudnn",
                    "ar_execution": "cuda_graph", "model_dtype": "bfloat16", "vae_dtype": "float32",
                    "ode_steps": 32,
                    "mode": values["mode"],
                }},
            }, {}, compute_ms=20)

        async def shutdown(self):
            self.alive = False

    monkeypatch.setattr("mmp_node.engine_io.EngineProcess", FakeProcess)
    stack = await build_stack(tmp_path, engines={"music_generate": {
        "module": "engines.music_generate", "timeout_sec": 900}}, scheduler__vram_total_mb=16303)
    try:
        body = {"type": "music.generate", "arguments": {"style": "piano pop", "lyrics": "[Verse]\nHello"},
                "delivery": {"audio": media_server.put_endpoint("/song.flac")}}
        first, second = await asyncio.gather(stack.submit(**body), stack.submit(**{
            **body, "arguments": {**body["arguments"], "options": {"seed": 42}},
            "delivery": {"audio": media_server.put_endpoint("/other.flac")}}))
        done = await stack.finish(first.json()["job_id"])
        await stack.finish(second.json()["job_id"])
        assert done["status"] == "done", done
        assert done["source"]["engine"] == ENGINE
        assert done["source"]["engine_version"] == ENGINE_VERSION
        assert done["outputs"]["audio"]["delivered"] is True
        cached = (await stack.submit(**{**body, "wait": 5})).json()
        assert cached["cached"] and cached["usage"]["compute_ms"] == 0
        assert len(events) == 2
        score = done["outputs"]["score"]["text"].replace("C D E F", "G A B c")
        revised = (await stack.submit(**{**body, "arguments": {**body["arguments"], "score": score},
                                         "wait": 5})).json()
        assert revised["status"] == "done" and not revised["cached"]
        assert events[-1]["abc"] == score
        assert revised["outputs"]["score"]["text"] == score
        assert stack.node.pool.loaded() == []
        instrumental = {**body, "arguments": {"style": "piano pop", "lyrics": "前奏，主旋律，渐弱收尾",
                                             "options": {"mode": "instrumental"}}}
        generated = (await stack.submit(**{**instrumental, "wait": 5})).json()
        assert generated["status"] == "done" and not generated["cached"], generated
        assert generated["outputs"]["metadata"]["value"]["mode"] == "instrumental"
        again = (await stack.submit(**{**instrumental, "wait": 5})).json()
        assert again["cached"] and again["usage"]["compute_ms"] == 0
        changed = {**instrumental, "arguments": {**instrumental["arguments"], "lyrics": "主旋律，变奏，尾奏"}}
        assert not (await stack.submit(**{**changed, "wait": 5})).json()["cached"]
        no_structure = {**instrumental, "arguments": {"style": "piano pop", "options": {"mode": "instrumental"}}}
        assert (await stack.submit(**{**no_structure, "wait": 5})).json()["status"] == "done"
    finally:
        await stack.client.aclose()
        await stack.link.close()
        await stack.node.close()


async def test_full_song_frame_can_exceed_64_mib(tmp_path):
    script = tmp_path / "large_audio.py"
    script.write_text(
        "import json,sys\n"
        "print(json.dumps({'type':'hello','engine':'large','engine_version':'1','protocol_version':'3.0'}),flush=True)\n"
        "for line in sys.stdin:\n"
        " m=json.loads(line)\n"
        " if m['type']=='shutdown': break\n"
        " if m['type']=='run':\n"
        "  print(json.dumps({'type':'result','id':m['id'],'outputs':{'audio':{'kind':'media','content_type':'audio/flac','data_base64':'A'*(65*1024*1024)}}}),flush=True)\n")
    proc = EngineProcess(EngineSpec("large", "large", [sys.executable, str(script)], tmp_path, 30, []), 30, "3.0")
    try:
        await proc.start()
        result = await proc.run({"job_id": "large-song", "task_type": "music.generate", "tier": "gpu",
                                 "arguments": {"style": "pop", "lyrics": "Hello"}}, 30)
        assert len(result.outputs["audio"]["data_base64"]) == 65 * 1024 * 1024
    finally:
        await proc.shutdown()


NATIVE_SCORE = '''X:1
T:
M:4/4
L:1/32
Q:1/4=88
V: Vocal clef=treble name="Vocal Melody" snm="Vocal"
V: Ins clef=treble name="Ins Melody" snm="Inst."
K:C
% intro
V: Vocal
Z|
V: Ins
C8D8E8F8|
% verse
V: Vocal
"C"^F16-F16-|F8G8A8B8|
V: Ins
c32|c32|
% outro
V: Vocal
Z|
V: Ins
G16C16|
'''


def test_instrumental_preserves_lead_ties_accidentals_harmony_and_fills():
    source = parse_abc(NATIVE_SCORE)
    text, check = convert_score(NATIVE_SCORE)
    target = parse_abc(text)
    assert not target.voices["Vocal"].notes
    for note in source.voices["Vocal"].notes:
        assert note in target.voices["Ins"].notes
    assert source.voices["Vocal"].notes[0][2] == 5  # tie crossing the barline
    assert source.voices["Vocal"].notes[0][1] == 66  # F sharp retained through tie
    assert target.voices["Ins"].notes[:4] == source.voices["Ins"].notes[:4]
    assert target.voices["Ins"].notes[-2:] == source.voices["Ins"].notes[-2:]
    assert target.voices["Vocal"].bars == source.voices["Vocal"].bars
    assert target.bpm == source.bpm
    assert check["affected_ins_notes"] and check["vocal_notes_after"] == 0
    assert convert_score(text)[0] == text  # Reusing an instrumental score is stable.


def test_partial_ins_overlap_keeps_unoccupied_segments():
    source = NATIVE_SCORE.replace('"C"^F16-F16-|F8G8A8B8|', '"C"z8C8z16|Z|')
    text, check = convert_score(source)
    voice = parse_abc(text).voices["Ins"]
    # The original high C continues before/after the lead note in the second bar.
    assert [4, 72, 1] in voice.notes
    assert [5, 60, 1] in voice.notes
    assert [6, 72, 2] in voice.notes
    assert len(check["affected_ins_notes"]) == 1


def test_voice_transfer_preserves_meter_key_changes_and_chord_onsets():
    text = NATIVE_SCORE.replace('"C"^F16-F16-|F8G8A8B8|', '"C"F32|G32|')
    text = text.replace('V: Vocal\nZ|\nV: Ins\nG16C16|',
                        'V: Vocal\nM:3/4\nK:G\n"G"z8"D7"z16|\nV: Ins\nM:3/4\nK:G\nF8G8A8|')
    converted, _ = convert_score(text)
    source, target = parse_abc(text), parse_abc(converted)
    assert target.voices["Ins"].bars == source.voices["Ins"].bars
    assert target.voices["Ins"].keys == source.voices["Ins"].keys
    assert target.voices["Ins"].notes[-3:] == source.voices["Ins"].notes[-3:]
    # The serializer may repeat the active chord at bar starts, but never shifts a change.
    def changes(events):
        return [event for i, event in enumerate(events) if not i or event[1] != events[i - 1][1]]
    assert changes(target.voices["Vocal"].chords) == changes(source.voices["Vocal"].chords)


def test_music_capability_matches_language_neutral_fixture():
    fixture = Path(__file__).resolve().parents[2] / "protocol/fixtures/capability/root/valid/music_generate.json"
    assert CAPABILITIES == [json.loads(fixture.read_text())]


@pytest.mark.parametrize("structure", ["", "[Intro] 稀疏钢琴\n[Verse] 旋律展开\n[Outro] 渐弱", "前奏 → 主题 → 变奏 → 尾奏"])
def test_render_instrumental_uses_structure_without_singing_it(structure):
    from types import SimpleNamespace
    import threading
    calls = []

    class Pipe:
        def plan(self, **kwargs):
            calls.append(("plan", kwargs))
            return SimpleNamespace(abc=NATIVE_SCORE, timing={"seconds": 1.25}, truncated=False)

        def __call__(self, **kwargs):
            calls.append(("render", kwargs))
            assert not parse_abc(kwargs["abc"]).voices["Vocal"].notes
            assert kwargs["lyrics"] == "[Intro]\n\n[Verse]\n\n[Outro]"
            return SimpleNamespace(abc=kwargs["abc"])

    values = request({"arguments": {"style": "warm piano", "lyrics": structure,
                                    "options": {"mode": "instrumental"}}})
    song, converted, timing = render(Pipe(), values, threading.Event())
    assert calls[0][0] == "plan" and calls[1][0] == "render"
    assert not structure or structure in calls[0][1]["style"]
    assert converted["source_score"] == NATIVE_SCORE and song.abc
    assert timing["planning"] == 1250
    assert values["lyrics"] == structure  # Rendering does not mutate the request/cache identity.
    calls.clear()
    render(Pipe(), {**values, "abc": NATIVE_SCORE}, threading.Event())
    assert len(calls) == 1 and calls[0][0] == "render"


def test_instrumental_planning_truncation_and_cancel_stop_before_render():
    from types import SimpleNamespace
    import threading
    cancelled = threading.Event()
    values = request({"arguments": {"style": "piano", "options": {"mode": "instrumental"}}})

    class Pipe:
        def plan(self, **kwargs):
            return SimpleNamespace(abc=NATIVE_SCORE, timing={"seconds": 0}, truncated=True)

        def __call__(self, **kwargs):
            pytest.fail("invalid or cancelled plans must never render")

    with pytest.raises(ValueError, match="truncated"):
        render(Pipe(), values, cancelled)
    cancelled.set()
    with pytest.raises(InterruptedError):
        render(Pipe(), {**values, "abc": NATIVE_SCORE}, cancelled)


def test_malformed_instrumental_score_is_rejected():
    with pytest.raises(ValueError):
        convert_score(NATIVE_SCORE.replace("C8D8E8F8", "C8D8E8"))
