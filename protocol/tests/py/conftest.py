"""契约测试的公共装置：加载全部 schema 进一个 registry，按目录约定枚举 fixtures。

目录约定（与 ts 侧一致）：
  protocol/fixtures/<schema>/<def>/{valid,invalid}/<name>.json
<schema> 是 schemas/<schema>.schema.json；<def> 是 "root" 或该 schema 的 $defs 键。
"""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

PROTOCOL = Path(__file__).resolve().parents[2]
SCHEMAS = PROTOCOL / "schemas"
FIXTURES = PROTOCOL / "fixtures"


def load_schemas() -> dict[str, dict]:
    return {p.name.removesuffix(".schema.json"): json.loads(p.read_text()) for p in sorted(SCHEMAS.glob("*.schema.json"))}


def build_registry(schemas: dict[str, dict]) -> Registry:
    reg = Registry()
    for s in schemas.values():
        reg = reg.with_resource(s["$id"], Resource.from_contents(s, default_specification=DRAFT202012))
    return reg


def iter_fixtures():
    for schema_dir in sorted(FIXTURES.iterdir()):
        if not schema_dir.is_dir():
            continue
        for def_dir in sorted(schema_dir.iterdir()):
            for verdict in ("valid", "invalid"):
                for f in sorted((def_dir / verdict).glob("*.json")):
                    yield schema_dir.name, def_dir.name, verdict == "valid", f


@pytest.fixture(scope="session")
def schemas():
    return load_schemas()


@pytest.fixture(scope="session")
def registry(schemas):
    return build_registry(schemas)


def make_validator(schemas, registry, schema_name: str, defn: str) -> Draft202012Validator:
    root = schemas[schema_name]
    if defn == "root":
        schema = root
    else:
        assert defn in root.get("$defs", {}), f"{schema_name} has no $defs/{defn}"
        # 用 $ref 指向 def，让 $defs 内部的相对引用仍以 root 的 $id 解析
        schema = {"$schema": root["$schema"], "$ref": f"{root['$id']}#/$defs/{defn}"}
    return Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())


def pytest_generate_tests(metafunc):
    if "fixture_case" in metafunc.fixturenames:
        cases = list(iter_fixtures())
        ids = [f"{s}/{d}/{'valid' if v else 'invalid'}/{f.stem}" for s, d, v, f in cases]
        metafunc.parametrize("fixture_case", cases, ids=ids)
