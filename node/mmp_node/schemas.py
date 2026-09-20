"""协议 schema 的加载与校验器缓存。protocol/schemas 是唯一源头；A 在运行时用它校验引擎输出。"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "protocol" / "schemas"
DIGEST_ID = "urn:mmp:protocol:1:digest"


@lru_cache(maxsize=1)
def registry() -> Registry:
    reg = Registry()
    for p in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        reg = reg.with_resource(s["$id"], Resource.from_contents(s, default_specification=DRAFT202012))
    return reg


@lru_cache(maxsize=64)
def validator_for_ref(ref: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": ref}, registry=registry(), format_checker=FormatChecker())


def validator_for(output_schema) -> Draft202012Validator | None:
    """capability.output_schema 是 URN 字串或内联 schema；None 表示不校验。"""
    if isinstance(output_schema, str):
        return validator_for_ref(output_schema)
    if isinstance(output_schema, dict):
        return Draft202012Validator(output_schema, registry=registry(), format_checker=FormatChecker())
    return None


def errors(v: Draft202012Validator, data) -> list[str]:
    return [f"{'/'.join(map(str, e.path)) or '$'}: {e.message}" for e in sorted(v.iter_errors(data), key=lambda e: list(e.path))]
