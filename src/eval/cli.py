from __future__ import annotations

import json
import argparse
import os
from pathlib import Path
import subprocess
import sys

try:
    from eval.engine.case_sync import sync_cases
    from eval.engine.common import (
        load_app_config,
        load_targets,
        BASE_DIR,
        configured_cases_dir,
        DEFAULT_GENERATED_TESTS_FILE,
        configured_promptfoo_config,
        DEFAULT_RAW_EVAL_FILE,
        DEFAULT_RUNS_DIR,
        configured_skills_dir,
        DEFAULT_SKILLS_DIR,
        EvalConfigError,
        ensure_directory,
        load_judge_provider,
    )
    from eval.engine.dashboard import run_dashboard
    from eval.engine.reporting import generate_summary
except ImportError:  # pragma: no cover - python file execution path
    from eval.engine.case_sync import sync_cases
    from eval.engine.common import (
        load_app_config,
        load_targets,
        BASE_DIR,
        configured_cases_dir,
        DEFAULT_GENERATED_TESTS_FILE,
        configured_promptfoo_config,
        DEFAULT_RAW_EVAL_FILE,
        DEFAULT_RUNS_DIR,
        configured_skills_dir,
        DEFAULT_SKILLS_DIR,
        EvalConfigError,
        ensure_directory,
        load_judge_provider,
    )
    from eval.engine.dashboard import run_dashboard
    from eval.engine.reporting import generate_summary


def _run_command(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    assert process.stdout is not None
    lines: list[str] = []
    for line in process.stdout:
        print(line, end="")
        lines.append(line)
    exit_code = process.wait()
    return int(exit_code), "".join(lines)


def _archive_runner_log(*, run_id: str | None, log_output: str) -> None:
    if not run_id:
        return
    log_dir = ensure_directory(DEFAULT_RUNS_DIR / str(run_id) / "logs")
    (log_dir / "runner.log").write_text(log_output, encoding="utf-8")


def _npm_env() -> dict[str, str]:
    env = os.environ.copy()
    python_executable = str((BASE_DIR.parent / ".venv" / "bin" / "python").resolve())
    existing_pythonpath = env.get("PYTHONPATH")
    env["PROMPTFOO_PYTHON"] = python_executable
    env["PYTHONPATH"] = (
        f"{BASE_DIR.parent}:{existing_pythonpath}" if existing_pythonpath else str(BASE_DIR.parent)
    )
    return env


def _generate_local_token_if_needed(target_id: str) -> str | None:
    if os.getenv("VERIFY_AUTH_TOKEN"):
        return None
    if target_id not in {"test", load_app_config().default_target_id}:
        return None
    script = BASE_DIR.parent / "scripts" / "generate_ibe_token.sh"
    if not script.exists():
        return None
    try:
        token = subprocess.check_output(
            [str(script), "-e", "local"],
            cwd=str(BASE_DIR.parent),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=20,
        ).strip()
    except Exception:
        return None
    return token or None


def _promptfoo_command(*args: str) -> list[str]:
    local_bin = BASE_DIR / "node_modules" / ".bin" / "promptfoo"
    if local_bin.exists():
        return [str(local_bin), *args]

    package_json = json.loads((BASE_DIR / "package.json").read_text(encoding="utf-8"))
    version = (
        package_json.get("devDependencies", {}).get("promptfoo")
        if isinstance(package_json.get("devDependencies"), dict)
        else None
    )
    normalized_version = str(version or "latest").strip().lstrip("^")
    return ["npx", "--yes", f"promptfoo@{normalized_version}", *args]


def _prepare_promptfoo_env(args: argparse.Namespace) -> dict[str, str]:
    env = _npm_env()
    judge_provider = load_judge_provider()
    env["OPENAI_API_KEY"] = judge_provider.api_key
    env["SMARTBOT_EVAL_JUDGE_MODEL"] = judge_provider.model
    env["SMARTBOT_EVAL_PROMPTFOO_SKIP_JUDGE"] = "1"
    if judge_provider.base_url:
        env["OPENAI_BASE_URL"] = judge_provider.base_url
        env["OPENAI_API_BASE_URL"] = judge_provider.base_url

    target_id = str(getattr(args, "target", "") or "").strip() or load_app_config().default_target_id
    env["SMARTBOT_EVAL_TARGET"] = target_id
    generated_token = _generate_local_token_if_needed(target_id)
    if generated_token and not env.get("VERIFY_AUTH_TOKEN"):
        env["VERIFY_AUTH_TOKEN"] = generated_token
    env["SMARTBOT_EVAL_CASES_DIR"] = str(Path(args.cases_dir).resolve())
    if args.case:
        env["SMARTBOT_EVAL_CASE_PATTERN"] = args.case
    if args.tag:
        env["SMARTBOT_EVAL_TAG"] = args.tag
    if args.skill:
        env["SMARTBOT_EVAL_SKILL"] = args.skill
    return env


def _compile_generated_tests(env: dict[str, str]) -> None:
    try:
        from eval.engine.test_generator import generate_tests
    except ImportError:  # pragma: no cover - python file execution path
        from eval.engine.test_generator import generate_tests

    old_env = os.environ.copy()
    os.environ.update(env)
    try:
        tests = generate_tests()
    finally:
        os.environ.clear()
        os.environ.update(old_env)

    ensure_directory(DEFAULT_GENERATED_TESTS_FILE.parent)
    DEFAULT_GENERATED_TESTS_FILE.write_text(
        json.dumps(tests, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SmartBot Promptfoo eval workspace")
    subparsers = parser.add_subparsers(dest="command", required=True)

    targets_parser = subparsers.add_parser("targets", help="List configured targets")
    targets_sub = targets_parser.add_subparsers(dest="targets_command", required=True)
    targets_sub.add_parser("list", help="List configured targets")

    cases_parser = subparsers.add_parser("cases", help="Manage target cases")
    cases_sub = cases_parser.add_subparsers(dest="cases_command", required=True)
    sync_parser = cases_sub.add_parser("sync", help="Generate seed cases from backend skills")
    sync_parser.add_argument("--target", default=load_app_config().default_target_id)
    sync_parser.add_argument("--skills-dir", default=str(DEFAULT_SKILLS_DIR))
    sync_parser.add_argument("--refresh-generated", action="store_true")

    sync_alias = subparsers.add_parser("sync-cases", help="Backward-compatible alias of `cases sync`")
    sync_alias.add_argument("--target", default=load_app_config().default_target_id)
    sync_alias.add_argument("--skills-dir", default=str(DEFAULT_SKILLS_DIR))
    sync_alias.add_argument("--refresh-generated", action="store_true")

    run_parser = subparsers.add_parser("run", help="Run promptfoo evals against a selected backend")
    run_parser.add_argument("--target", default=load_app_config().default_target_id)
    run_parser.add_argument("--case")
    run_parser.add_argument("--tag")
    run_parser.add_argument("--skill")
    run_parser.add_argument("--refresh-generated", action="store_true")
    run_parser.add_argument("--open-ui", action="store_true")
    run_parser.add_argument("--cases-dir", default=str(configured_cases_dir()))
    run_parser.add_argument("--skills-dir", default=str(DEFAULT_SKILLS_DIR))

    history_parser = subparsers.add_parser("history", help="List archived runs")
    history_parser.add_argument("--target", default="")

    serve_parser = subparsers.add_parser("serve", help="Open the SmartBot eval dashboard")
    serve_parser.add_argument("--host", default=load_app_config().dashboard_host)
    serve_parser.add_argument("--port", type=int, default=load_app_config().dashboard_port)
    serve_parser.add_argument("--no-open", action="store_true")

    view_parser = subparsers.add_parser("view", help="Alias of serve")
    view_parser.add_argument("--host", default=load_app_config().dashboard_host)
    view_parser.add_argument("--port", type=int, default=load_app_config().dashboard_port)
    view_parser.add_argument("--no-open", action="store_true")

    subparsers.add_parser("promptfoo-view", help="Open the original Promptfoo viewer")

    return parser


def _handle_targets_list() -> int:
    for target in load_targets().values():
        print(f"{target.id}\t{target.protocol}\t{target.base_url}")
    return 0


def _handle_sync_cases(args: argparse.Namespace) -> int:
    auto_cases_dir = Path(args.auto_cases_dir).resolve() if getattr(args, "auto_cases_dir", None) else Path(args.skills_dir).resolve()
    target_id = str(args.target).strip()
    written = sync_cases(
        skills_dir=Path(args.skills_dir).resolve(),
        target_id=target_id,
        auto_cases_dir=ensure_directory(Path(args.cases_dir).resolve() / target_id / "auto"),
        refresh_generated=bool(args.refresh_generated),
    )
    print(f"sync-cases completed, wrote {len(written)} file(s)")
    for path in written:
        print(path)
    return 0


def _handle_run(args: argparse.Namespace) -> int:
    if args.refresh_generated:
        sync_cases(
            skills_dir=Path(args.skills_dir).resolve(),
            target_id=str(args.target).strip(),
            auto_cases_dir=ensure_directory(Path(args.cases_dir).resolve() / str(args.target).strip() / "auto"),
            refresh_generated=True,
        )

    env = _prepare_promptfoo_env(args)
    _compile_generated_tests(env)
    judge_provider = load_judge_provider()
    filters = {
        "case": args.case or None,
        "tag": args.tag or None,
        "skill": args.skill or None,
        "refresh_generated": bool(args.refresh_generated),
    }
    exit_code, log_output = _run_command(
        _promptfoo_command(
            "eval",
            "-c",
            str(configured_promptfoo_config()),
            "-o",
            str(DEFAULT_RAW_EVAL_FILE),
        ),
        cwd=BASE_DIR,
        env=env,
    )
    if DEFAULT_RAW_EVAL_FILE.exists():
        summary_paths = generate_summary(
            raw_eval_path=DEFAULT_RAW_EVAL_FILE,
            judge_provider=judge_provider,
            filters=filters,
        )
        summary_payload = json.loads(summary_paths.json_path.read_text(encoding="utf-8"))
        _archive_runner_log(
            run_id=str(summary_payload.get("run_id") or "").strip() or None,
            log_output=log_output,
        )
        print(f"[eval-cli] summary json: {summary_paths.json_path}")
        print(f"[eval-cli] summary markdown: {summary_paths.markdown_path}")
    if exit_code == 0 and args.open_ui:
        run_dashboard(
            host=load_app_config().dashboard_host,
            port=load_app_config().dashboard_port,
            open_browser=True,
        )
    return exit_code


def _handle_history(args: argparse.Namespace) -> int:
    run_root = DEFAULT_RUNS_DIR
    if not run_root.exists():
        print("no archived runs")
        return 0
    for run_dir in sorted(run_root.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue
        summary_path = run_dir / "summary.json"
        if not summary_path.exists():
            continue
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        run_payload = (
            json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            if (run_dir / "run.json").exists()
            else {}
        )
        if args.target:
            cases = payload.get("cases") or []
            if not any(case.get("target_id") == args.target for case in cases if isinstance(case, dict)):
                continue
        stats = payload.get("stats") or {}
        run_id = payload.get("run_id") or run_payload.get("run_id") or run_dir.name
        promptfoo_eval_id = payload.get("promptfoo_eval_id") or payload.get("eval_id") or run_payload.get("promptfoo_eval_id")
        print(
            f"{run_id}\t"
            f"promptfoo={promptfoo_eval_id}\t"
            f"success={stats.get('successes',0)} failure={stats.get('failures',0)} error={stats.get('errors',0)}"
        )
    return 0


def _handle_serve(args: argparse.Namespace) -> int:
    run_dashboard(host=args.host, port=args.port, open_browser=not args.no_open)
    return 0


def _handle_promptfoo_view(args: argparse.Namespace) -> int:
    env = _npm_env()
    judge_provider = load_judge_provider()
    env["OPENAI_API_KEY"] = judge_provider.api_key
    if judge_provider.base_url:
        env["OPENAI_BASE_URL"] = judge_provider.base_url
        env["OPENAI_API_BASE_URL"] = judge_provider.base_url
    exit_code, _ = _run_command(
        _promptfoo_command("view", "--yes"),
        cwd=BASE_DIR,
        env=env,
    )
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "targets":
        return _handle_targets_list()
    if args.command == "cases" and args.cases_command == "sync":
        setattr(args, "cases_dir", str(configured_cases_dir()))
        return _handle_sync_cases(args)
    if args.command == "sync-cases":
        setattr(args, "cases_dir", str(configured_cases_dir()))
        return _handle_sync_cases(args)
    if args.command == "run":
        return _handle_run(args)
    if args.command == "history":
        return _handle_history(args)
    if args.command == "serve":
        return _handle_serve(args)
    if args.command == "view":
        return _handle_serve(args)
    if args.command == "promptfoo-view":
        return _handle_promptfoo_view(args)
    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvalConfigError as exc:
        print(f"[eval-cli] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
