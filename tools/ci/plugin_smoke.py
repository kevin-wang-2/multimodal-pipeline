"""Select plugin-owned smoke suites from a Git diff, or execute them serially."""
from __future__ import annotations

import argparse
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import string
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[2]
# Only the A <-> engine contract and its transport implementations require every plugin.
INTERNAL_PROTOCOL = (
    "protocol/schemas/engine-io.schema.json",
    "node/mmp_node/engine_io.py",
    "node/engines/common/io.py",
)


def manifests(root: Path = ROOT) -> dict[str, dict]:
    suites = {}
    for entry in sorted((root / "node/engines").glob("*/__main__.py")):
        path = entry.parent / "ci.toml"
        with path.open("rb") as stream:
            suite = tomllib.load(stream)
        if not suite["smoke"]:
            raise ValueError(f"{path}: smoke must contain at least one command")
        suite["watch"] = [f"{entry.parent.relative_to(root).as_posix()}/*", *suite.get("watch", [])]
        suites[entry.parent.name] = suite
    return suites


def select(changed: list[str], suites: dict[str, dict], *, full: bool = False) -> list[str]:
    if full or any(path in INTERNAL_PROTOCOL for path in changed):
        return list(suites)
    return [name for name, suite in suites.items()
            if any(fnmatchcase(path, pattern) for path in changed for pattern in suite["watch"])]


def changed_paths(base: str, head: str, event: str) -> list[str]:
    if event == "pull_request":
        base = subprocess.check_output(["git", "merge-base", base, head], cwd=ROOT, text=True).strip()
    # --no-renames includes both paths of a move, so the old owner is tested too.
    raw = subprocess.check_output(["git", "diff", "--name-only", "--no-renames", "-z", base, head], cwd=ROOT)
    return [os.fsdecode(path) for path in raw.split(b"\0") if path]


def run(names: list[str], suites: dict[str, dict]) -> None:
    checked_gpu = False
    for name in names:
        suite = suites[name]
        print(f"::group::Plugin smoke: {name}", flush=True)
        if suite.get("gpu", False) and not checked_gpu:
            subprocess.run(["nvidia-smi"], check=True)
            checked_gpu = True
        env = {**os.environ, **suite.get("env", {})}
        for command in suite["smoke"]:
            argv = [sys.executable if arg == "{python}" else string.Template(arg).substitute(env)
                    for arg in command]
            print(json.dumps(argv, ensure_ascii=False), flush=True)
            subprocess.run(argv, cwd=ROOT, env=env, check=True)
        print("::endgroup::", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--base", default=os.environ.get("BASE_SHA", ""))
    plan.add_argument("--head", default="HEAD")
    plan.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", "pull_request"))
    plan.add_argument("--all", action="store_true")
    execute = sub.add_parser("run")
    execute.add_argument("--plugins", default=os.environ.get("SMOKE_PLUGINS"), required=False)
    args = parser.parse_args()
    suites = manifests()
    if args.command == "run":
        run(json.loads(args.plugins), suites)
        return
    full = args.all or args.event == "workflow_dispatch" or (args.base and set(args.base) == {"0"})
    if not full and not args.base:
        parser.error("a comparison base is required; use --all for a full smoke")
    changed = [] if full else changed_paths(args.base, args.head, args.event)
    names = select(changed, suites, full=full)
    print(json.dumps({"changed": changed, "plugins": names}, ensure_ascii=False, indent=2))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"plugins={json.dumps(names, separators=(',', ':'))}\n")


if __name__ == "__main__":
    main()
