"""Simplified PatchHound patch-verification workflow."""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import RunArtifacts, sha256_file, sha256_text, utc_now
from .case_io import RunPaths, allocate_run_paths, load_case
from .codex_runtime import (
    HISTORY_MCP_SERVER_NAME,
    HISTORY_MCP_TOOL_NAME,
    build_codex_config,
    build_session_config,
    check_history_tool,
    external_sandbox_read_paths,
    import_codex_sdk,
    prepare_isolated_environment,
    resolve_codex_binary,
    resolve_linux_repo,
    runtime_selection,
    select_codex_sandbox_backend,
)
from .config import (
    DEFAULT_CASES_DIR,
    DEFAULT_METADATA_DIR,
    DEFAULT_PROMPT_TEMPLATE,
    DEFAULT_RUNTIME_CONFIG,
    PhoenixConfig,
)
from .events import TurnExecution, run_turn_with_timeout
from .phoenix import PhoenixObserver
from .prompt import load_case_prompt
from .validation import ValidationResult, build_contract_repair_prompt, validate_final_response


@dataclass(frozen=True)
class AnalysisResult:
    final_response: str
    assessment: str
    confidence: str
    thread_id: str
    turn_ids: tuple[str, ...]
    run_dir: Path
    output_path: Path
    phoenix_trace_id: str | None


def run_case(
    case_id: str,
    *,
    cases_dir: Path = DEFAULT_CASES_DIR,
    prompt_template: Path = DEFAULT_PROMPT_TEMPLATE,
    runtime_config: Path = DEFAULT_RUNTIME_CONFIG,
    run_id: str | None = None,
    force_run: bool = False,
    model: str | None = None,
    codex_bin: str | None = None,
    timeout_seconds: int = 7200,
    max_contract_repairs: int = 2,
    use_history_tool: bool = True,
    history_linux_repo: Path | None = None,
    env_path: Path | None = None,
    phoenix_enabled: bool | None = None,
    phoenix_endpoint: str | None = None,
    phoenix_project: str | None = None,
    save_conversation: bool = False,
    live_output: bool = True,
) -> AnalysisResult:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_contract_repairs < 0:
        raise ValueError("max_contract_repairs must not be negative")

    case = load_case(case_id, cases_dir)
    prompt_template = prompt_template.expanduser().resolve()
    runtime_config = runtime_config.expanduser().resolve()
    prompt = load_case_prompt(case, prompt_template)
    provider, effective_model, reasoning = runtime_selection(runtime_config, model)
    run_paths = allocate_run_paths(case.case_dir, run_id=run_id, force_run=force_run)
    artifacts = RunArtifacts(run_paths, save_conversation=save_conversation)
    started_at = utc_now()
    prompt_hash = sha256_text(prompt)
    runtime_hash = sha256_file(runtime_config)
    phoenix_configuration_warning: str | None = None
    try:
        phoenix_config = PhoenixConfig.load(
            env_path=env_path,
            enabled=phoenix_enabled,
            endpoint=phoenix_endpoint,
            project_name=phoenix_project,
        )
    except Exception as exc:  # noqa: BLE001 - optional telemetry configuration is fail-open.
        phoenix_config = PhoenixConfig(enabled=False)
        phoenix_configuration_warning = (
            f"Phoenix configuration disabled: {exc.__class__.__name__}: {exc}"
        )
    phoenix_project_name = phoenix_config.effective_project_name(effective_model)
    linux_repo = resolve_linux_repo(history_linux_repo, env_path)
    artifacts.initialize_manifest(
        {
            "agent": "experiments-codex-simple",
            "case_id": case.case_id,
            "run_id": run_paths.run_id,
            "status": "running",
            "thread_id": None,
            "turn_ids": [],
            "model_provider": provider,
            "model": effective_model,
            "reasoning_effort": reasoning,
            "prompt_sha256": prompt_hash,
            "commit_message_sha256": sha256_text(case.commit_message),
            "diff_sha256": sha256_text(case.diff),
            "runtime_config_sha256": runtime_hash,
            "source_access": "read",
            "scratch_access": "write",
            "network_enabled": False,
            "native_web_search_enabled": False,
            "control_plane_network_enabled": True,
            "history_tool_enabled": use_history_tool,
            "phoenix_enabled": phoenix_config.enabled,
            "phoenix_project": phoenix_project_name if phoenix_config.enabled else None,
            "phoenix_trace_id": None,
            "started_at": started_at,
            "completed_at": None,
            "error": None,
        }
    )
    artifacts.write_model_config(
        {
            "Agent backend": "codex_sdk",
            "Protocol": "codex_app_server",
            "Model provider": provider,
            "Model": effective_model,
            "Reasoning effort": reasoning,
            "Source access": "read",
            "Scratch access": "write",
            "Agent tool network": "disabled",
            "Native web search": "disabled",
            "Control-plane network": "enabled",
            "History tool": "enabled" if use_history_tool else "disabled",
            "Phoenix": "enabled" if phoenix_config.enabled else "disabled",
            "Phoenix project": phoenix_project_name if phoenix_config.enabled else "disabled",
            "Prompt SHA-256": prompt_hash,
            "Runtime config SHA-256": runtime_hash,
        }
    )

    def status(message: str, *, success: bool = True) -> None:
        artifacts.record("runtime", "status", success, message)
        if live_output:
            print(f"[patchhound-simple] {message}", flush=True)

    def phoenix_warning(message: str) -> None:
        artifacts.record("phoenix", "warning", False, message)
        if live_output:
            print(f"[patchhound-simple] {message}", flush=True)

    phoenix: PhoenixObserver | None = None
    try:
        if phoenix_configuration_warning is not None:
            phoenix_warning(phoenix_configuration_warning)
        try:
            phoenix = PhoenixObserver.create(
                config=phoenix_config,
                case_id=case.case_id,
                run_id=run_paths.run_id,
                model=effective_model,
                commit_message=case.commit_message,
                diff=case.diff,
                prompt_sha256=prompt_hash,
                runtime_config_sha256=runtime_hash,
                history_tool_enabled=use_history_tool,
                warn=phoenix_warning,
                model_provider=provider,
                reasoning_effort=reasoning,
            )
        except Exception as exc:  # noqa: BLE001 - optional telemetry is fail-open.
            phoenix_warning(f"Phoenix setup disabled: {exc.__class__.__name__}: {exc}")
            phoenix = None
        status(f"case={case.case_id}")
        status(f"source={case.source_dir}")
        status(f"run_output_dir={run_paths.run_dir}")
        status(f"model_provider={provider} model={effective_model} reasoning={reasoning}")
        status(f"history_tool={'enabled' if use_history_tool else 'disabled'}")
        if phoenix is not None:
            status(f"phoenix=enabled trace_id={phoenix.trace_id} project={phoenix_project_name}")
            phoenix.record_checkpoint(
                "runtime.configuration",
                input_value={
                    "case_id": case.case_id,
                    "run_id": run_paths.run_id,
                    "source_dir": str(case.source_dir),
                    "run_dir": str(run_paths.run_dir),
                },
                output_value={
                    "provider": provider,
                    "model": effective_model,
                    "reasoning_effort": reasoning,
                    "approval_policy": "never",
                    "sdk_approval_mode": "deny_all",
                    "source_access": "read",
                    "scratch_access": "write",
                    "network_enabled": False,
                    "control_plane_network_enabled": True,
                    "history_tool_enabled": use_history_tool,
                    "history_server": HISTORY_MCP_SERVER_NAME if use_history_tool else None,
                    "history_tool": HISTORY_MCP_TOOL_NAME if use_history_tool else None,
                },
            )
        elif phoenix_config.enabled:
            status("phoenix=unavailable; analysis continues")
        else:
            status("phoenix=disabled")

        result = _execute_codex(
            case_id=case.case_id,
            source_dir=case.source_dir,
            prompt=prompt,
            runtime_config=runtime_config,
            run_paths=run_paths,
            artifacts=artifacts,
            phoenix=phoenix,
            codex_bin=codex_bin,
            model=model,
            timeout_seconds=timeout_seconds,
            max_contract_repairs=max_contract_repairs,
            use_history_tool=use_history_tool,
            history_linux_repo=linux_repo,
            live_output=live_output,
            status=status,
        )
        final_response, validation, thread_id, turn_ids = result
        artifacts.write_analysis(final_response)
        artifacts.ensure_history_log(enabled=use_history_tool)
        artifacts.record("filesystem", f"write {run_paths.analysis}", True)
        trace_id = phoenix.trace_id if phoenix is not None else None
        artifacts.update_manifest(
            status="completed",
            thread_id=thread_id,
            turn_ids=turn_ids,
            final_assessment=validation.assessment,
            confidence=validation.confidence,
            phoenix_trace_id=trace_id,
            completed_at=utc_now(),
            error=None,
        )
        artifacts.record("runtime", "complete", True, f"assessment={validation.assessment}")
        artifacts.write_logs()
        if phoenix is not None:
            _record_phoenix_artifacts(phoenix, run_paths)
            phoenix.complete(output=final_response, thread_id=thread_id, turn_ids=turn_ids)
            artifacts.write_logs()
        return AnalysisResult(
            final_response=final_response,
            assessment=str(validation.assessment),
            confidence=str(validation.confidence),
            thread_id=thread_id,
            turn_ids=tuple(turn_ids),
            run_dir=run_paths.run_dir,
            output_path=run_paths.analysis,
            phoenix_trace_id=trace_id,
        )
    except BaseException as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        if phoenix is not None:
            phoenix.fail(stage="workflow", exc=exc)
        artifacts.record("runtime", "interrupted" if interrupted else "failed", False, f"{exc.__class__.__name__}: {exc}")
        artifacts.update_manifest(
            status="interrupted" if interrupted else "failed",
            completed_at=utc_now(),
            phoenix_trace_id=phoenix.trace_id if phoenix is not None else None,
            error={"type": exc.__class__.__name__, "message": str(exc)},
        )
        artifacts.write_logs()
        raise


def _execute_codex(
    *,
    case_id: str,
    source_dir: Path,
    prompt: str,
    runtime_config: Path,
    run_paths: RunPaths,
    artifacts: RunArtifacts,
    phoenix: PhoenixObserver | None,
    codex_bin: str | None,
    model: str | None,
    timeout_seconds: int,
    max_contract_repairs: int,
    use_history_tool: bool,
    history_linux_repo: Path,
    live_output: bool,
    status: Any,
) -> tuple[str, ValidationResult, str, list[str]]:
    ApprovalMode, Codex, Sandbox = import_codex_sdk()
    deadline = time.monotonic() + timeout_seconds
    turn_ids: list[str] = []
    thread_id = "unknown"
    final_response = ""
    with tempfile.TemporaryDirectory(prefix="patchhound-simple-") as temporary:
        environment_span = (
            phoenix.operation_start(
                "runtime.codex_environment",
                input_value={
                    "source_dir": str(source_dir),
                    "history_tool_enabled": use_history_tool,
                },
            )
            if phoenix is not None
            else None
        )
        try:
            isolated = prepare_isolated_environment(Path(temporary), runtime_config)
            sandbox_backend = select_codex_sandbox_backend()
            if sandbox_backend == "external-landlock":
                shell_home = isolated.scratch_dir / "home"
                shell_home.mkdir(parents=True, exist_ok=True)
                isolated_env = dict(isolated.env)
                isolated_env["HOME"] = str(shell_home)
                real_codex_bin = resolve_codex_binary(codex_bin)
                config = build_codex_config(
                    codex_bin=codex_bin,
                    cwd=source_dir,
                    env=isolated_env,
                    sandbox_backend=sandbox_backend,
                    source_dir=source_dir,
                    read_paths=external_sandbox_read_paths(
                        source_dir=source_dir,
                        codex_bin=real_codex_bin,
                        history_linux_repo=history_linux_repo,
                        history_metadata_dir=DEFAULT_METADATA_DIR,
                    ),
                    writable_paths=(
                        isolated.codex_home,
                        isolated.sqlite_home,
                        isolated.scratch_dir,
                        run_paths.run_dir,
                        Path("/dev"),
                    ),
                )
                session_codex_bin = str(real_codex_bin)
            else:
                config = build_codex_config(codex_bin=codex_bin, cwd=source_dir, env=isolated.env)
                session_codex_bin = getattr(config, "codex_bin", None)
            session_config = build_session_config(
                runtime_config,
                scratch_dir=isolated.scratch_dir,
                codex_bin=session_codex_bin,
                use_history_tool=use_history_tool,
                case_id=case_id,
                history_linux_repo=history_linux_repo,
                history_query_log=run_paths.history_queries,
                sandbox_backend=sandbox_backend,
            )
        except BaseException as exc:
            if phoenix is not None:
                phoenix.operation_error(environment_span, exc)
            raise
        if phoenix is not None:
            phoenix.operation_end(
                environment_span,
                output_value={
                    "codex_home": "isolated",
                    "sqlite_home": "isolated",
                    "scratch_dir": str(isolated.scratch_dir),
                    "source_access": "read",
                    "scratch_access": "write",
                    "network_enabled": False,
                    "control_plane_network_enabled": True,
                    "sandbox_backend": sandbox_backend,
                    "approval_policy": session_config.get("approval_policy"),
                    "history_mcp_required": use_history_tool,
                },
            )
        status(f"scratch={isolated.scratch_dir}")
        status(f"codex_bin={getattr(config, 'codex_bin', None)}")
        status(f"sandbox_backend={sandbox_backend} agent_network=disabled")
        artifacts.update_manifest(sandbox_backend=sandbox_backend)
        with Codex(config=config) as codex:
            thread = codex.thread_start(
                approval_mode=ApprovalMode.deny_all,
                cwd=str(source_dir),
                ephemeral=True,
                config=session_config,
                model=model,
                sandbox=Sandbox.workspace_write if sandbox_backend == "external-landlock" else None,
            )
            thread_id = str(thread.id)
            artifacts.record("codex_sdk", "thread_start", True, f"thread_id={thread_id}")
            artifacts.update_manifest(thread_id=thread_id)
            status(f"thread_id={thread_id}")
            if phoenix is not None:
                phoenix.set_thread_id(thread_id)
                phoenix.record_thread_start(thread_id)
            if use_history_tool:
                history_span = (
                    phoenix.operation_start(
                        "history.mcp.status",
                        input_value={
                            "server": HISTORY_MCP_SERVER_NAME,
                            "tool": HISTORY_MCP_TOOL_NAME,
                            "required": True,
                        },
                    )
                    if phoenix is not None
                    else None
                )
                try:
                    checked = check_history_tool(codex, thread_id)
                except BaseException as exc:
                    if phoenix is not None:
                        phoenix.operation_error(history_span, exc)
                    raise
                if phoenix is not None:
                    phoenix.operation_end(
                        history_span,
                        output_value={
                            "available": checked,
                            "status_check_supported": checked,
                        },
                    )
                artifacts.record(
                    "codex_sdk",
                    "history_tool_status",
                    True,
                    "available" if checked else "status check unsupported by SDK",
                )

            current_prompt = prompt
            validation: ValidationResult | None = None
            for attempt in range(max_contract_repairs + 1):
                turn_name = "initial" if attempt == 0 else f"contract_repair_{attempt}"
                execution = run_turn_with_timeout(
                    codex=codex,
                    thread=thread,
                    prompt=current_prompt,
                    source_dir=source_dir,
                    approval_mode=ApprovalMode,
                    model=model,
                    timeout_seconds=_remaining_seconds(deadline),
                    turn_name=turn_name,
                    live_output=live_output,
                    artifacts=artifacts,
                    phoenix=phoenix,
                )
                _record_turn(artifacts, turn_name, current_prompt, execution)
                turn_ids.append(execution.turn_id)
                artifacts.update_manifest(turn_ids=turn_ids)
                final_response = execution.final_response.strip()
                validation = validate_final_response(final_response)
                artifacts.record(
                    "validation",
                    turn_name,
                    validation.ok,
                    ",".join(issue.code for issue in validation.issues) or "valid",
                )
                if phoenix is not None:
                    phoenix.record_validation(attempt=attempt, validation=validation)
                if validation.ok:
                    return final_response, validation, thread_id, turn_ids
                if attempt < max_contract_repairs:
                    current_prompt = build_contract_repair_prompt(validation)

    if final_response:
        artifacts.write_invalid_response(final_response)
        artifacts.record("filesystem", f"write {run_paths.invalid_response}", True)
    codes = ", ".join(issue.code for issue in validation.issues) if validation is not None else "unknown"
    raise RuntimeError(f"Codex final response failed contract validation after repairs: {codes}")


def _record_turn(artifacts: RunArtifacts, name: str, prompt: str, execution: TurnExecution) -> None:
    artifacts.record_tokens(name, execution.result)
    artifacts.record_conversation(name, prompt, execution.result, execution.events)
    artifacts.record("codex_sdk", "turn_complete", True, f"name={name} turn_id={execution.turn_id}")
    artifacts.write_logs()


def _remaining_seconds(deadline: float) -> int:
    remaining = int(deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("the experiments-codex-simple run exceeded its total timeout")
    return remaining


def _record_phoenix_artifacts(phoenix: PhoenixObserver, paths: RunPaths) -> None:
    for path in (
        paths.analysis,
        paths.manifest,
        paths.model_config,
        paths.process_log,
        paths.token_usage,
        paths.conversation,
        paths.history_queries,
        paths.invalid_response,
    ):
        if not path.is_file():
            continue
        phoenix.record_checkpoint(
            f"artifact.{path.stem}",
            input_value={"artifact": path.name},
            output_value={
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            },
            attributes={"patchhound.artifact_name": path.name},
        )
