#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
from pathlib import Path
from typing import Any


def discover_repo_root(explicit_repo_root: str | None) -> Path:
    candidates: list[Path] = []

    if explicit_repo_root:
        candidates.append(Path(explicit_repo_root).expanduser().resolve())

    env_root = os.environ.get("OPENCART_PIPELINE_REPO_ROOT")
    if env_root:
        candidates.append(Path(env_root).expanduser().resolve())

    def walk_up(start: Path) -> list[Path]:
        return [start, *start.parents]

    candidates.extend(walk_up(Path.cwd().resolve()))
    candidates.extend(walk_up(Path(__file__).resolve().parent))

    seen: set[Path] = set()

    for candidate in candidates:
        if candidate in seen:
            continue

        seen.add(candidate)

        if (
            (candidate / "tools").is_dir()
            or (candidate / ".secrets").is_dir()
            or (candidate / "input").is_dir()
            or (candidate / "exports").is_dir()
            or (candidate / "runs").is_dir()
            or (candidate / "logs").is_dir()
        ):
            return candidate

    raise RuntimeError("Could not auto-detect repo root for OpenCart config resolution.")


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        if key:
            values[key] = value

    return values


def _resolve_value(
    explicit_value: str | None,
    env_key: str,
    env_file_values: dict[str, str],
    fallback: str = "",
) -> str:
    if explicit_value is not None and str(explicit_value).strip():
        return str(explicit_value).strip()

    env_value = os.environ.get(env_key)
    if env_value is not None and str(env_value).strip():
        return str(env_value).strip()

    file_value = env_file_values.get(env_key)
    if file_value is not None and str(file_value).strip():
        return str(file_value).strip()

    return fallback


def resolve_opencart_config(
    *,
    repo_root: Path,
    store_base: str | None = None,
    admin_path: str | None = None,
    username: str | None = None,
    password: str | None = None,
    profile: str | None = None,
    export_profile: str | None = None,
    export_file: str | None = None,
    stock_import_profile: str | None = None,
    stock_import_file: str | None = None,
) -> dict[str, str]:
    env_file = repo_root / ".secrets" / "opencart.env"
    env_file_values = load_env_file(env_file)

    return {
        "repo_root": str(repo_root),
        "env_file": str(env_file),

        "store_base": _resolve_value(
            store_base,
            "OPENCART_STORE_BASE",
            env_file_values,
            "",
        ),
        "admin_path": _resolve_value(
            admin_path,
            "OPENCART_ADMIN_PATH",
            env_file_values,
            "",
        ),
        "username": _resolve_value(
            username,
            "OPENCART_ADMIN_USER",
            env_file_values,
            "",
        ),
        "password": _resolve_value(
            password,
            "OPENCART_ADMIN_PASS",
            env_file_values,
            "",
        ),

        # Generic/import profile fallback.
        "profile": _resolve_value(
            profile,
            "OPENCART_IMPORT_PROFILE",
            env_file_values,
            "",
        ),

        # Bridge export profile and output file.
        "export_profile": _resolve_value(
            export_profile,
            "OPENCART_BRIDGE_EXPORT_PROFILE",
            env_file_values,
            "Bridge",
        ),
        "export_file": _resolve_value(
            export_file,
            "OPENCART_BRIDGE_EXPORT_FILE",
            env_file_values,
            str(repo_root / "input" / "opencart_export.csv"),
        ),

        # Later stock import profile and upload file.
        "stock_import_profile": _resolve_value(
            stock_import_profile,
            "OPENCART_STOCK_IMPORT_PROFILE",
            env_file_values,
            "stock-only",
        ),
        "stock_import_file": _resolve_value(
            stock_import_file,
            "OPENCART_STOCK_IMPORT_FILE",
            env_file_values,
            str(repo_root / "runs" / "latest" / "oc_stock.csv"),
        ),
    }


def _shell_assignment(key: str, value: str) -> str:
    return f"{key}={shlex.quote(value)}"


def _export_shell(args: argparse.Namespace) -> int:
    repo_root = discover_repo_root(args.repo_root)

    config = resolve_opencart_config(
        repo_root=repo_root,
        store_base=args.store_base,
        admin_path=args.admin_path,
        username=args.username,
        password=args.password,
        profile=args.profile,
        export_profile=args.export_profile,
        export_file=args.export_file,
        stock_import_profile=args.stock_import_profile,
        stock_import_file=args.stock_import_file,
    )

    print(_shell_assignment("OPENCART_STORE_BASE", config["store_base"]))
    print(_shell_assignment("OPENCART_ADMIN_PATH", config["admin_path"]))
    print(_shell_assignment("OPENCART_ADMIN_USER", config["username"]))
    print(_shell_assignment("OPENCART_ADMIN_PASS", config["password"]))
    print(_shell_assignment("OPENCART_IMPORT_PROFILE", config["profile"]))

    print(_shell_assignment("OPENCART_BRIDGE_EXPORT_PROFILE", config["export_profile"]))
    print(_shell_assignment("OPENCART_BRIDGE_EXPORT_FILE", config["export_file"]))

    print(_shell_assignment("OPENCART_STOCK_IMPORT_PROFILE", config["stock_import_profile"]))
    print(_shell_assignment("OPENCART_STOCK_IMPORT_FILE", config["stock_import_file"]))

    print(_shell_assignment("OPENCART_ENV_FILE", config["env_file"]))

    return 0


def _export_json(args: argparse.Namespace) -> int:
    repo_root = discover_repo_root(args.repo_root)

    config = resolve_opencart_config(
        repo_root=repo_root,
        store_base=args.store_base,
        admin_path=args.admin_path,
        username=args.username,
        password=args.password,
        profile=args.profile,
        export_profile=args.export_profile,
        export_file=args.export_file,
        stock_import_profile=args.stock_import_profile,
        stock_import_file=args.stock_import_file,
    )

    safe_config: dict[str, Any] = dict(config)

    if safe_config.get("password"):
        safe_config["password"] = "***"

    print(json.dumps(safe_config, ensure_ascii=False, indent=2))
    return 0


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", default=None)

    parser.add_argument("--store-base", default=None)
    parser.add_argument("--admin-path", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--profile", default=None)

    parser.add_argument("--export-profile", default=None)
    parser.add_argument("--export-file", default=None)

    parser.add_argument("--stock-import-profile", default=None)
    parser.add_argument("--stock-import-file", default=None)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve OpenCartStockSync runtime configuration."
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    export_shell = subparsers.add_parser(
        "export-shell",
        help="Print shell assignments for resolved OpenCart settings.",
    )
    add_common_args(export_shell)

    export_json = subparsers.add_parser(
        "export-json",
        help="Print resolved OpenCart settings as JSON. Password is masked.",
    )
    add_common_args(export_json)

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.command == "export-shell":
        return _export_shell(args)

    if args.command == "export-json":
        return _export_json(args)

    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())