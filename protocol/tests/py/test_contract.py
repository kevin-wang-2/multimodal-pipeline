import json

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from conftest import FIXTURES, make_validator


def test_schemas_are_valid_2020_12(schemas):
    for name, s in schemas.items():
        Draft202012Validator.check_schema(s)
        assert s["$schema"] == "https://json-schema.org/draft/2020-12/schema", name
        assert s["$id"] == f"urn:mmp:protocol:1:{name}", name


def test_format_checker_actually_checks():
    """守住 format 校验没有被静默跳过（缺库时 jsonschema 会直接放行）。"""
    fc = FormatChecker()
    assert not fc.conforms("yesterday", "date-time")
    assert fc.conforms("2026-09-10T17:00:00+08:00", "date-time")
    assert not fc.conforms("not a uri", "uri")


def test_every_schema_and_def_has_fixtures(schemas):
    """每个 schema 至少有 root 或某个 $defs 的 valid fixture，防止 schema 漂到没人测的地方。"""
    for name in schemas:
        assert any((FIXTURES / name).glob("*/valid/*.json")), f"no valid fixtures for {name}"


def test_fixture(schemas, registry, fixture_case):
    schema_name, defn, expect_valid, path = fixture_case
    validator = make_validator(schemas, registry, schema_name, defn)
    data = json.loads(path.read_text())
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if expect_valid:
        assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)
    else:
        assert errors, f"{path.name} was expected to be INVALID but passed"


def test_m0_digest_sample_is_the_real_hash(registry, schemas):
    """M0 样本回指 testdata/m0_hum_then_speech.wav 的完整 sha256。"""
    d = json.loads((FIXTURES / "digest/root/valid/m0_hum_then_speech.json").read_text())
    assert d["media_id"] == "sha256:6b4a2b8c89e0bcf240920e7922d8d77666f7b61a11ba2ba7389f8156986220f1"
    assert d["segments"][0]["label_status"] == "unclassified" and d["segments"][0]["labels"] == []
    assert "caption" not in d.get("global", {})
