"""End-to-end OMP patch-verification workflow."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .artifacts import RunArtifacts, sha256_file, sha256_text, utc_now
from .case_io import allocate_run_paths, load_case
from .config import (
    CODING_PLAN_API_ENV,
    CODING_PLAN_PROVIDER,
    DEEPSEEK_API_ENV,
    DEEPSEEK_PROVIDER,
    DEFAULT_CASES_DIR,
    DEFAULT_HISTORY_SERVER,
    DEFAULT_METADATA_DIR,
    DEFAULT_PROMPT_TEMPLATE,
    DEFAULT_RUNTIME_CONFIG,
    DGX_API_ENV,
    DGX_PROVIDER,
    EXPECTED_PROMPT_PEER,
    EXPERIMENT_ROOT,
    PROJECT_ROOT,
    AuthRequiredError,
    ConfigurationError,
    PhoenixConfig,
    RuntimeConfig,
    provider_base_url,
    resolve_linux_repo,
    resolve_provider_environment,
)
from .events import TurnExecution
from .history_mcp import EXPOSED_TOOL_NAME, HistoryMcpDefinition, direct_probe
from .isolation import (
    IsolationProbeResult,
    SandboxLayout,
    build_bwrap_command,
    create_layout,
    run_isolation_probe,
    run_network_connectivity_probe,
)
from .phoenix import PhoenixObserver
from .prompt import load_case_prompt
from .runtime import SidecarError, SidecarRuntime
from .validation import ValidationResult, build_contract_repair_prompt, validate_final_response


class InvalidResponseError(RuntimeError):
    """The model exhausted contract repairs without producing a valid report."""


@dataclass(frozen=True)
class AnalysisResult:
    final_response: str
    assessment: str
    confidence: str
    session_id: str
    turn_ids: tuple[str, ...]
    run_dir: Path
    output_path: Path
    phoenix_trace_id: str | None


RuntimeFactory = Callable[[list[str], RuntimeConfig], Any]
ProbeFactory = Callable[[Path, SandboxLayout], IsolationProbeResult]


def run_case(
    case_id: str,
    *,
    cases_dir: Path = DEFAULT_CASES_DIR,
    prompt_template: Path = DEFAULT_PROMPT_TEMPLATE,
    runtime_config: Path = DEFAULT_RUNTIME_CONFIG,
    run_id: str | None = None,
    force_run: bool = False,
    output_root: Path | None = None,
    timeout_seconds: int | None = None,
    max_contract_repairs: int | None = None,
    use_history_tool: bool = True,
    history_linux_repo: Path | None = None,
    history_metadata_dir: Path = DEFAULT_METADATA_DIR,
    auth_data_dir: Path | None = None,
    env_path: Path | None = None,
    phoenix_enabled: bool | None = None,
    phoenix_endpoint: str | None = None,
    phoenix_project: str | None = None,
    save_conversation: bool = False,
    live_output: bool = True,
    runtime_factory: RuntimeFactory | None = None,
    isolation_probe_factory: ProbeFactory = run_isolation_probe,
    history_definition: HistoryMcpDefinition | None = None,
    allow_unauthenticated_test_runtime: bool = False,
) -> AnalysisResult:
    config = RuntimeConfig.load(runtime_config)
    history_linux_repo = resolve_linux_repo(history_linux_repo, env_path)
    formal_comparison = True
    effective_timeout = config.timeout_seconds if timeout_seconds is None else timeout_seconds
    effective_repairs = config.max_contract_repairs if max_contract_repairs is None else max_contract_repairs
    if effective_timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    if effective_repairs < 0:
        raise ValueError("max_contract_repairs must not be negative")
    if effective_timeout != config.timeout_seconds or effective_repairs != config.max_contract_repairs or not use_history_tool:
        formal_comparison = False

    case = load_case(case_id, cases_dir)
    prompt_path = prompt_template.expanduser().resolve()
    rendered_prompt = load_case_prompt(case, prompt_path)
    _verify_prompt_parity(prompt_path)
    paths = allocate_run_paths(
        case.case_dir,
        output_dir_name=config.output_dir_name,
        output_root=output_root,
        run_id=run_id,
        force_run=force_run,
    )
    artifacts = RunArtifacts(paths, save_conversation=save_conversation)
    started_at = utc_now()
    input_hashes = {
        "prompt_template_sha256": sha256_file(prompt_path),
        "rendered_prompt_sha256": sha256_text(rendered_prompt),
        "commit_message_sha256": sha256_text(case.commit_message),
        "diff_sha256": sha256_text(case.diff),
        "prompt_sha256": sha256_text(rendered_prompt),
        "runtime_config_sha256": sha256_file(config.path),
    }
    project_revision = _project_revision()
    manifest = {
        "agent": "experiments-omp-simple",
        "agent_backend": "oh-my-pi",
        "case_id": case.case_id,
        "run_id": paths.run_id,
        "status": "running",
        "error_class": None,
        "error": None,
        "formal_comparison": formal_comparison,
        "session_id": None,
        "turn_ids": [],
        "logical_model": config.logical_model,
        "sdk_model_selector": config.model_selector,
        "requested_provider": config.provider,
        "effective_provider": None,
        "effective_model": None,
        "requested_reasoning_effort": config.reasoning_effort,
        "observed_reasoning_effort": None,
        "model_fallback": None,
        "context_compacted": None,
        "sdk_retry_count": None,
        "omp_sdk_version": None,
        "bun_version": _bun_version(),
        "omp_source_revision": config.source_revision,
        "sidecar_protocol_version": config.sidecar_protocol_version,
        "project_revision": project_revision,
        "source_access": "read",
        "scratch_access": "write",
        "network_enabled": config.network_enabled,
        "history_tool_enabled": use_history_tool,
        "history_mcp_required": config.history_mcp_required and use_history_tool,
        "history_mcp_status": None,
        "history_mcp_schema_sha256": None,
        "effective_tools": None,
        "permission_policy": None,
        "sidecar_stderr_summary": None,
        "history_mcp_stderr_summary": None,
        "isolation_probe": None,
        "network_probe": None,
        "agent_shell_isolation_backend": None,
        "ambient_discovery_disabled": True,
        "phoenix": {"enabled": None, "status": "not_initialized", "trace_id": None, "error": None},
        **input_hashes,
        "started_at": started_at,
        "completed_at": None,
    }
    artifacts.initialize_manifest(manifest)
    artifacts.write_model_config(
        {
            "Agent backend": "oh-my-pi SDK",
            "Protocol": f"jsonl-v{config.sidecar_protocol_version}",
            "Logical model": config.logical_model,
            "SDK model selector": config.model_selector,
            "Requested provider": config.provider,
            "Reasoning effort": config.reasoning_effort,
            "Server ready timeout seconds": config.server_ready_timeout_seconds,
            "MCP timeout milliseconds": config.mcp_timeout_ms,
            "Source access": "read",
            "Scratch access": "write",
            "Agent tool network": "enabled" if config.network_enabled else "disabled",
            "Control-plane network": "enabled",
            "History tool": "enabled" if use_history_tool else "disabled (non-comparable)",
            "Prompt SHA-256": input_hashes["rendered_prompt_sha256"],
            "Prompt template SHA-256": input_hashes["prompt_template_sha256"],
            "Runtime config SHA-256": input_hashes["runtime_config_sha256"],
        }
    )

    def status(message: str, *, success: bool = True, component: str = "runtime") -> None:
        artifacts.record(component, "status", success, message)
        if live_output:
            print(f"[patchhound-omp-simple] {message}", flush=True)

    phoenix: PhoenixObserver | None = None
    phoenix_error: BaseException | None = None
    phoenix_error_class: str | None = None
    final_response = ""
    sandbox_root: tempfile.TemporaryDirectory[str] | None = None
    runtime: Any | None = None
    try:
        resolved_auth = _resolve_auth_data_dir(auth_data_dir)
        if config.auth_required and runtime_factory is None and not allow_unauthenticated_test_runtime:
            _require_auth(resolved_auth)

        phoenix_config = PhoenixConfig.load(
            runtime_path=config.path,
            env_path=env_path,
            enabled=phoenix_enabled,
            endpoint=phoenix_endpoint,
            project_name=phoenix_project,
        )
        artifacts.update_manifest(
            phoenix={
                "enabled": phoenix_config.enabled,
                "project_name": phoenix_config.project_name,
                "status": "initializing",
                "trace_id": None,
                "error": None,
            }
        )
        try:
            phoenix = PhoenixObserver.create(
                config=phoenix_config,
                case_id=case.case_id,
                run_id=paths.run_id,
                model=config.logical_model,
                prompt_sha256=input_hashes["rendered_prompt_sha256"],
                input_hashes=input_hashes,
                warn=lambda warning: status(warning, success=False, component="phoenix"),
                commit_message=case.commit_message,
                diff=case.diff,
                model_provider=config.provider,
                reasoning_effort=config.reasoning_effort,
                history_tool_enabled=use_history_tool,
                source_access="read",
                scratch_access="write",
                network_enabled=config.network_enabled,
            )
            artifacts.update_manifest(
                phoenix={
                    "enabled": phoenix_config.enabled,
                    "project_name": phoenix_config.project_name,
                    "status": "connected" if phoenix is not None else "disabled",
                    "trace_id": phoenix.trace_id if phoenix is not None else None,
                    "error": None,
                }
            )
            status(
                f"Phoenix {'connected' if phoenix is not None else 'disabled'}"
                f" project={phoenix_config.project_name}",
                component="phoenix",
            )
        except Exception as exc:  # noqa: BLE001 - telemetry is fail-open.
            status(f"Phoenix unavailable: {exc.__class__.__name__}: {exc}", success=False, component="phoenix")
            artifacts.update_manifest(
                phoenix={
                    "enabled": phoenix_config.enabled,
                    "project_name": phoenix_config.project_name,
                    "status": "unavailable",
                    "trace_id": None,
                    "error": str(exc)[:1000],
                }
            )

        status(f"case={case.case_id}")
        status(f"source={case.source_dir}")
        status(f"run_output_dir={paths.run_dir}")
        if phoenix is not None:
            phoenix.checkpoint(
                "runtime.configuration",
                input_value={
                    "case_id": case.case_id,
                    "run_id": paths.run_id,
                    "source_dir": str(case.source_dir),
                    "run_dir": str(paths.run_dir),
                },
                output_value={
                    "provider": config.provider,
                    "model": config.logical_model,
                    "reasoning_effort": config.reasoning_effort,
                    "approval_policy": "auto_approve_restricted_tools",
                    "source_access": "read",
                    "scratch_access": "write",
                    "network_enabled": config.network_enabled,
                    "history_tool_enabled": use_history_tool,
                    "history_server": "patchhound_history" if use_history_tool else None,
                    "history_tool": EXPOSED_TOOL_NAME if use_history_tool else None,
                },
            )
        deadline = time.monotonic() + effective_timeout
        sandbox_root = tempfile.TemporaryDirectory(prefix=f"patchhound-omp-{case.case_id}-")
        layout = create_layout(Path(sandbox_root.name))
        provider_environment = resolve_provider_environment(config)
        provider_env_file: Path | None = None
        if provider_environment:
            provider_env_file = layout.root / "provider.env"
            for name, value in provider_environment.items():
                if any(char in value for char in "\r\n\x00"):
                    raise ConfigurationError(f"invalid value for provider environment variable {name}")
                provider_env_file.write_text(f"{name}={value}\n", encoding="utf-8")
                provider_env_file.chmod(0o600)
        agent_model_config: Path | None = None
        provider_settings = {
            DEEPSEEK_PROVIDER: (
                provider_base_url(DEEPSEEK_PROVIDER),
                "openai-responses",
                DEEPSEEK_API_ENV,
            ),
            DGX_PROVIDER: (
                provider_base_url(DGX_PROVIDER),
                "openai-responses",
                DGX_API_ENV,
            ),
            CODING_PLAN_PROVIDER: (
                provider_base_url(CODING_PLAN_PROVIDER),
                "openai-completions",
                CODING_PLAN_API_ENV,
            ),
        }.get(config.provider)
        if provider_settings is not None:
            base_url, wire_api, api_env = provider_settings
            agent_model_config = layout.root / "omp-models.yml"
            agent_model_config.write_text(
                f"providers:\n"
                f"  {config.provider}:\n"
                f"    baseUrl: {base_url}\n"
                f"    api: {wire_api}\n"
                f"    apiKey: {api_env}\n"
                "    authHeader: true\n"
                "    models:\n"
                f"      - id: {config.logical_model}\n"
                f"        name: {config.logical_model}\n"
                "        reasoning: true\n"
                "        supportsTools: true\n"
                "        thinking:\n"
                "          mode: effort\n"
                "          efforts: [low, medium, high]\n"
                "          defaultLevel: high\n"
                "        contextWindow: 256000\n"
                "        maxTokens: 32768\n",
                encoding="utf-8",
            )
        probe = isolation_probe_factory(case.source_dir, layout)
        artifacts.update_manifest(
            isolation_probe=probe.as_dict(),
            agent_shell_isolation_backend=probe.agent_shell_backend,
            agent_network_disabled=True,
            control_plane_network_enabled=True,
        )
        status("isolation probe passed", component="isolation")
        if phoenix is not None:
            phoenix.checkpoint("isolation.preflight", output_value={"isolation_probe": probe.as_dict()})
        if runtime_factory is None:
            network_probe = run_network_connectivity_probe(
                case.source_dir,
                layout,
                target_url=phoenix_config.collector_endpoint,
                agent_shell_backend=probe.agent_shell_backend,
            )
            artifacts.update_manifest(network_probe=network_probe.as_dict())
            status(
                f"network denial probe {'failed' if network_probe.reachable else 'passed'}"
                f" target={network_probe.target}",
                success=not network_probe.reachable,
                component="isolation",
            )
            if network_probe.reachable:
                raise RuntimeError("Agent data-plane network isolation probe unexpectedly connected")
            if phoenix is not None:
                phoenix.checkpoint("network.preflight", output_value=network_probe.as_dict())

        definition = history_definition
        history_probe_arguments: dict[str, Any] | None = None
        if use_history_tool:
            touched_path = _first_touched_path(case.diff)
            history_probe_arguments = {
                "kind": "file_history",
                "file_path": touched_path,
                "max_events": 1,
                "analysis_stage": "patch security review",
                "reason": "OMP bridge preflight",
            }
        if use_history_tool and definition is None:
            probe_result = direct_probe(
                case_id=case.case_id,
                linux_repo=history_linux_repo,
                metadata_dir=history_metadata_dir,
                query_log=None,
                call_arguments=history_probe_arguments,
            )
            definition = probe_result.definition
            if not probe_result.call_succeeded:
                raise ConfigurationError("direct history MCP call probe returned an error")
        if definition is not None:
            artifacts.update_manifest(history_mcp_schema_sha256=definition.schema_sha256)
            if phoenix is not None:
                phoenix.checkpoint(
                    "history.mcp.preflight",
                    input_value=history_probe_arguments,
                    output_value={
                        "server": definition.server_name,
                        "tool": definition.tool_name,
                        "schema_sha256": definition.schema_sha256,
                        "call_probe": "succeeded" if use_history_tool else "disabled",
                    },
                )

        sidecar_command = build_bwrap_command(
            layout=layout,
            source_dir=case.source_dir,
            auth_data_dir=resolved_auth,
            history_server=DEFAULT_HISTORY_SERVER if use_history_tool else None,
            history_linux_repo=history_linux_repo if use_history_tool else None,
            history_metadata_dir=history_metadata_dir if use_history_tool else None,
            history_project_root=(PROJECT_ROOT / "tools") if use_history_tool else None,
            agent_shell_backend=probe.agent_shell_backend,
            agent_model_config=agent_model_config,
            provider_env_file=provider_env_file,
            command=["/app/node_modules/.bin/bun", "/app/dist/sidecar/index.js"],
        )
        runtime = (
            runtime_factory(sidecar_command, config)
            if runtime_factory is not None
            else SidecarRuntime(
                sidecar_command,
                message_bytes=config.limits.message_bytes,
                stderr_bytes=config.limits.stderr_bytes,
                event_output_bytes=config.limits.event_output_bytes,
                total_event_bytes=config.limits.total_event_bytes,
                dispose_grace_seconds=config.dispose_grace_seconds,
                host_cwd=PROJECT_ROOT,
            )
        )
        runtime.start()
        artifacts.record("runtime", "start", True, "OMP sidecar started")
        ready = runtime.initialize(
            {
                "case_id": case.case_id,
                "run_id": paths.run_id,
                "model_selector": config.model_selector,
                "logical_model": config.logical_model,
                "reasoning_effort": config.reasoning_effort,
                "omp_sdk_version": config.omp_sdk_version,
                "bun_min_version": config.bun_min_version,
                "agent_dir": "/runtime/omp-agent",
                "provider": config.provider,
                "auth_required": config.auth_required,
                "project_root": "/runtime/history-project",
                "metadata_dir": "/runtime/history-metadata",
                "linux_repo": "/runtime/history-linux",
                "query_log": "/runtime/history_queries.md",
                "sidecar_protocol_version": config.sidecar_protocol_version,
                "server_ready_timeout_seconds": config.server_ready_timeout_seconds,
                "message_bytes": config.limits.message_bytes,
                "tool_output_bytes": config.limits.tool_output_bytes,
                "enabled_tools": list(config.enabled_tools) if use_history_tool else [t for t in config.enabled_tools if t != EXPOSED_TOOL_NAME],
                "disabled_tools": list(config.disabled_tools) + ([] if use_history_tool else [EXPOSED_TOOL_NAME]),
                "history_enabled": use_history_tool,
                "mcp_command": ["/usr/bin/python3", "-B", "/runtime/history-tools/history_mcp_server.py"],
                "mcp_schema_sha256": definition.schema_sha256 if definition is not None else None,
                "mcp_timeout_ms": config.mcp_timeout_ms,
                "mcp_probe_arguments": history_probe_arguments,
            },
            timeout=min(_remaining(deadline), float(config.server_ready_timeout_seconds)),
        )
        ready_payload = ready.payload
        artifacts.update_manifest(
            omp_sdk_version=ready_payload.get("sdkVersion"),
            bun_version=ready_payload.get("bunVersion"),
            omp_source_revision=config.source_revision,
            permission_policy=ready_payload.get("permissionPolicy"),
        )
        _verify_ready_payload(config, ready_payload, use_history_tool=use_history_tool)
        artifacts.record(
            "runtime",
            "initialize",
            True,
            f"sdk={ready_payload.get('sdkVersion')} bun={ready_payload.get('bunVersion')}",
        )
        if phoenix is not None:
            phoenix.checkpoint("runtime.initialize", output_value=ready_payload)
        started, mcp_status = runtime.start_session({}, timeout=_remaining(deadline))
        session_id = str(started.session_id)
        effective_tools = started.payload.get("effective_tools")
        artifacts.update_manifest(
            session_id=session_id,
            history_mcp_status=mcp_status.payload,
            history_mcp_stderr_summary=_safe_diagnostic(str(mcp_status.payload.get("stderr_summary")))
            if mcp_status.payload.get("stderr_summary")
            else None,
            effective_tools=effective_tools,
        )
        _verify_effective_tools(config, effective_tools, use_history_tool=use_history_tool)
        _verify_mcp_status(mcp_status.payload, definition, use_history_tool=use_history_tool)
        artifacts.record("runtime", "session.create", True, f"session_id={session_id}")
        artifacts.record(
            "mcp",
            "initialize",
            True,
            json.dumps(mcp_status.payload, ensure_ascii=False, sort_keys=True),
        )
        if phoenix is not None:
            phoenix.checkpoint("session.create", output_value={"session_id": session_id, "effective_tools": effective_tools})
            phoenix.checkpoint("mcp.initialize", output_value=mcp_status.payload)

        current_prompt = rendered_prompt
        validation: ValidationResult | None = None
        turn_ids: list[str] = []
        executions: list[TurnExecution] = []
        for attempt in range(effective_repairs + 1):
            name = "initial" if attempt == 0 else f"contract_repair_{attempt}"
            turn_span = (
                phoenix.turn_start(name=name, prompt=current_prompt, timeout_seconds=effective_timeout)
                if phoenix is not None
                else None
            )
            try:
                execution = runtime.prompt(name, current_prompt, timeout=_remaining(deadline))
            except BaseException as exc:
                if phoenix is not None:
                    phoenix.turn_error(turn_span, exc)
                raise
            executions.append(execution)
            turn_ids.append(execution.turn_id)
            artifacts.record_tokens(name, execution.usage)
            artifacts.record_conversation(
                name,
                current_prompt,
                execution.final_response,
                tuple(event.as_dict() for event in execution.events),
            )
            _record_execution(artifacts, execution)
            artifacts.update_manifest(
                turn_ids=turn_ids,
                effective_provider=execution.provider,
                effective_model=execution.model,
                observed_reasoning_effort=execution.reasoning_effort,
                model_fallback=(execution.provider != config.provider or execution.model != config.logical_model),
            )
            _verify_effective_runtime(config, execution)
            final_response = execution.final_response.strip()
            validation = validate_final_response(final_response)
            artifacts.record(
                "validation",
                name,
                validation.ok,
                ",".join(issue.code for issue in validation.issues) or "valid",
            )
            if phoenix is not None:
                phoenix.record_turn(
                    execution,
                    validation,
                    prompt=current_prompt,
                    timeout_seconds=effective_timeout,
                    turn_span=turn_span,
                )
            if validation.ok:
                break
            if attempt < effective_repairs:
                current_prompt = build_contract_repair_prompt(validation)
            artifacts.write_logs()
        if validation is None or not validation.ok:
            if final_response:
                artifacts.write_invalid_response(final_response)
            codes = ", ".join(issue.code for issue in validation.issues) if validation else "unknown"
            raise InvalidResponseError(f"OMP final response failed contract validation after repairs: {codes}")

        artifacts.write_analysis(final_response)
        last = executions[-1]
        artifacts.update_manifest(
            status="completed",
            error_class=None,
            error=None,
            final_assessment=validation.assessment,
            confidence=validation.confidence,
            effective_provider=last.provider,
            effective_model=last.model,
            observed_reasoning_effort=last.reasoning_effort,
            model_fallback=False,
            context_compacted=any(item.context_compacted for item in executions),
            sdk_retry_count=_sum_nullable(item.sdk_retry_count for item in executions),
            completed_at=utc_now(),
        )
        status(f"completed assessment={validation.assessment}")
        if sandbox_root is not None:
            artifacts.collect_history_log(Path(sandbox_root.name) / "runtime" / "history_queries.md")
        artifacts.ensure_history_log(enabled=use_history_tool)
        artifacts.record("runtime", "complete", True, f"assessment={validation.assessment}")
        artifacts.write_logs()
        return AnalysisResult(
            final_response=final_response,
            assessment=str(validation.assessment),
            confidence=str(validation.confidence),
            session_id=session_id,
            turn_ids=tuple(turn_ids),
            run_dir=paths.run_dir,
            output_path=paths.analysis,
            phoenix_trace_id=phoenix.trace_id if phoenix is not None else None,
        )
    except BaseException as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        if runtime is not None and (interrupted or isinstance(exc, TimeoutError)):
            try:
                runtime.abort(timeout=min(5, config.dispose_grace_seconds))
            except Exception as abort_exc:  # noqa: BLE001
                artifacts.record("runtime", "abort", False, str(abort_exc))
        error_class, run_status = _classify_error(exc, interrupted=interrupted)
        phoenix_error = exc
        phoenix_error_class = error_class
        artifacts.record("runtime", run_status, False, f"{exc.__class__.__name__}: {exc}")
        artifacts.update_manifest(
            status=run_status,
            error_class=error_class,
            error=f"{exc.__class__.__name__}: {exc}"[:4000],
            completed_at=utc_now(),
        )
        raise
    finally:
        dispose_success = True
        if runtime is not None:
            try:
                runtime.dispose()
            except Exception as exc:  # noqa: BLE001
                dispose_success = False
                artifacts.record("runtime", "dispose", False, str(exc))
            stderr_summary = getattr(runtime, "stderr_summary", "")
            if stderr_summary:
                artifacts.update_manifest(sidecar_stderr_summary=_safe_diagnostic(str(stderr_summary)))
            if dispose_success:
                artifacts.record("runtime", "dispose", True, "OMP sidecar disposed")
        if sandbox_root is not None:
            layout_path = Path(sandbox_root.name)
            history_log = layout_path / "runtime" / "history_queries.md"
            artifacts.collect_history_log(history_log)
            sandbox_root.cleanup()
        artifacts.write_logs()
        if phoenix is not None and not phoenix.closed:
            phoenix.checkpoint(
                "runtime.dispose",
                output_value={"success": dispose_success},
            )
            phoenix.checkpoint(
                "artifacts.finalize",
                output_value={
                    "status": artifacts.manifest.get("status", "failed"),
                    "run_dir": str(paths.run_dir),
                },
            )
            _record_phoenix_artifacts(phoenix, paths)
            phoenix.complete(
                status=str(artifacts.manifest.get("status", "failed")),
                output=final_response or None,
                error=phoenix_error,
                attributes={
                    "patchhound.session_id": artifacts.manifest.get("session_id", "unknown"),
                    "patchhound.final_assessment": artifacts.manifest.get("final_assessment", "unknown"),
                    "patchhound.confidence": artifacts.manifest.get("confidence", "unknown"),
                    "patchhound.error_class": phoenix_error_class or "none",
                },
            )
            _record_phoenix_health(artifacts, phoenix, enabled=True)
            artifacts.write_logs()


def render_case_prompt(
    case_id: str,
    *,
    cases_dir: Path = DEFAULT_CASES_DIR,
    prompt_template: Path = DEFAULT_PROMPT_TEMPLATE,
) -> str:
    case = load_case(case_id, cases_dir)
    _verify_prompt_parity(prompt_template)
    return load_case_prompt(case, prompt_template)


def _verify_prompt_parity(prompt_path: Path) -> None:
    path = prompt_path.expanduser().resolve()
    if path == DEFAULT_PROMPT_TEMPLATE.resolve() and EXPECTED_PROMPT_PEER.is_file():
        if path.read_bytes() != EXPECTED_PROMPT_PEER.read_bytes():
            raise ConfigurationError("OMP and experiments-codex-simple prompt templates are not byte-identical")


def _resolve_auth_data_dir(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit.expanduser().resolve()
    value = os.environ.get("PATCHHOUND_OMP_DATA_DIR", "").strip()
    return Path(value).expanduser().resolve() if value else None


def _require_auth(auth_data_dir: Path | None) -> None:
    if auth_data_dir is None:
        raise AuthRequiredError(
            "AUTH_REQUIRED: set PATCHHOUND_OMP_DATA_DIR to the isolated OMP data directory after login"
        )
    if auth_data_dir.is_symlink() or not auth_data_dir.is_dir():
        raise AuthRequiredError(f"AUTH_REQUIRED: OMP agent directory is missing: {auth_data_dir}")
    database = auth_data_dir / "agent.db"
    if database.is_symlink() or not database.is_file():
        raise AuthRequiredError(f"AUTH_REQUIRED: OMP agent database is missing: {database}")


def _first_touched_path(diff: str) -> str:
    match = re.search(r"(?m)^diff --git a/(.+?) b/(.+?)$", diff)
    if match:
        return _validate_touched_path(match.group(2))
    match = re.search(r"(?m)^\+\+\+ b/(.+?)$", diff)
    if match:
        return _validate_touched_path(match.group(1))
    for label in ("After", "File", "Before"):
        match = re.search(rf"(?m)^{label}:\s*(\S.*?)\s*$", diff)
        if match:
            return _validate_touched_path(match.group(1))
    raise ConfigurationError("diff does not contain a repository-relative touched path")


def _validate_touched_path(raw: str) -> str:
    value = raw.strip()
    path = PurePosixPath(value)
    if (
        not value
        or value == "/dev/null"
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ConfigurationError(f"diff contains an unsafe touched path: {value!r}")
    return path.as_posix()


def _verify_effective_runtime(config: RuntimeConfig, execution: TurnExecution) -> None:
    if execution.provider != config.provider:
        raise ConfigurationError(f"effective provider mismatch: expected {config.provider}, got {execution.provider}")
    if execution.model != config.logical_model:
        raise ConfigurationError(f"effective model mismatch: expected {config.logical_model}, got {execution.model}")
    if execution.reasoning_effort != config.reasoning_effort:
        raise ConfigurationError(
            f"effective reasoning mismatch: expected {config.reasoning_effort}, got {execution.reasoning_effort}"
        )


def _verify_ready_payload(
    config: RuntimeConfig,
    payload: dict[str, Any],
    *,
    use_history_tool: bool,
) -> None:
    if payload.get("sdkVersion") != config.omp_sdk_version:
        raise ConfigurationError(
            f"OMP SDK version mismatch: expected {config.omp_sdk_version}, got {payload.get('sdkVersion')}"
        )
    bun_version = payload.get("bunVersion")
    if not isinstance(bun_version, str) or not _version_at_least(bun_version, config.bun_min_version):
        raise ConfigurationError(f"Bun version mismatch: need >= {config.bun_min_version}, got {bun_version}")
    if payload.get("configuredModel") != config.model_selector:
        raise ConfigurationError(
            f"configured model mismatch: expected {config.model_selector}, got {payload.get('configuredModel')}"
        )
    if payload.get("configuredReasoning") != config.reasoning_effort:
        raise ConfigurationError(
            "configured reasoning mismatch: "
            f"expected {config.reasoning_effort}, got {payload.get('configuredReasoning')}"
        )
    disabled_tools = list(config.disabled_tools)
    if not use_history_tool:
        disabled_tools.append(EXPOSED_TOOL_NAME)
    expected_policy = {
        "autoApprove": True,
        "restrictedTools": True,
        "disabledTools": disabled_tools,
        "extensions": "disabled",
        "mcp": "bridge-only",
        "lsp": "disabled",
        "shellPath": "/app/bin/bash",
    }
    if payload.get("permissionPolicy") != expected_policy:
        raise ConfigurationError(f"permission policy mismatch: {payload.get('permissionPolicy')}")


def _verify_effective_tools(config: RuntimeConfig, value: Any, *, use_history_tool: bool) -> None:
    expected = list(config.enabled_tools)
    if not use_history_tool:
        expected = [tool for tool in expected if tool != EXPOSED_TOOL_NAME]
    if not isinstance(value, list) or not all(isinstance(tool, str) for tool in value):
        raise ConfigurationError("effective tool set is not a string list")
    if len(value) != len(set(value)) or sorted(value) != sorted(expected):
        raise ConfigurationError(f"effective tool set mismatch: expected {sorted(expected)}, got {sorted(value)}")


def _verify_mcp_status(
    payload: dict[str, Any],
    definition: HistoryMcpDefinition | None,
    *,
    use_history_tool: bool,
) -> None:
    status = dict(payload)
    stderr_summary = status.pop("stderr_summary", None)
    if stderr_summary is not None and not isinstance(stderr_summary, str):
        raise ConfigurationError("history MCP stderr summary must be a string or null")
    if use_history_tool:
        if definition is None:
            raise ConfigurationError("required history MCP definition is missing")
        expected = {
            "server": "patchhound_history",
            "status": "connected",
            "error": None,
            "tool": EXPOSED_TOOL_NAME,
            "schema_sha256": definition.schema_sha256,
            "call_probe": "succeeded",
        }
    else:
        expected = {
            "server": "patchhound_history",
            "status": "disabled",
            "error": None,
            "tool": EXPOSED_TOOL_NAME,
            "schema_sha256": None,
            "call_probe": "disabled",
        }
    if status != expected:
        raise ConfigurationError(f"history MCP status mismatch: expected {expected}, got {payload}")


def _version_at_least(actual: str, minimum: str) -> bool:
    def parse(value: str) -> tuple[int, int, int] | None:
        match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", value.strip())
        return tuple(int(match.group(index)) for index in range(1, 4)) if match else None  # type: ignore[return-value]

    actual_version = parse(actual)
    minimum_version = parse(minimum)
    return actual_version is not None and minimum_version is not None and actual_version >= minimum_version


def _record_execution(artifacts: RunArtifacts, execution: TurnExecution) -> None:
    unexpected_permission = False
    for event in execution.events:
        if event.type.startswith("tool_"):
            artifacts.record(
                "tool",
                str(event.payload.get("tool", "unknown")),
                bool(event.payload.get("success", True)),
                json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
            )
        elif event.type == "permission_denied":
            artifacts.record("permission", "unexpected_request", False, json.dumps(event.payload, sort_keys=True))
            unexpected_permission = True
    if unexpected_permission:
        raise ConfigurationError("OMP emitted an unexpected permission denial in auto-approved restricted mode")
    artifacts.record(
        "omp_sdk",
        "turn_complete",
        True,
        f"name={execution.name} turn_id={execution.turn_id} finish={execution.finish_reason}",
    )


def _record_phoenix_health(artifacts: RunArtifacts, observer: PhoenixObserver, *, enabled: bool) -> None:
    artifacts.update_manifest(
        phoenix={
            "enabled": enabled,
            "project_name": observer.project_name,
            "status": "connected" if observer.available else "unavailable",
            "trace_id": observer.trace_id,
            "error": observer.last_error,
        }
    )


def _safe_diagnostic(value: str) -> str:
    value = re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s]+", r"\1[REDACTED]", value)
    return " ".join(value.split())[:4000]


def _record_phoenix_artifacts(phoenix: PhoenixObserver, paths: Any) -> None:
    """Record the same host-owned artifact inventory as experiments-codex-simple."""
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
        phoenix.checkpoint(
            f"artifact.{path.stem}",
            input_value={"artifact": path.name},
            output_value={
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            },
            attributes={"patchhound.artifact_name": path.name},
        )


def _classify_error(exc: BaseException, *, interrupted: bool) -> tuple[str, str]:
    if interrupted:
        return "INTERRUPTED", "interrupted"
    if isinstance(exc, AuthRequiredError):
        return "AUTH_REQUIRED", "auth_required"
    if isinstance(exc, TimeoutError):
        return "TIMEOUT", "timeout"
    if isinstance(exc, InvalidResponseError):
        return "RESPONSE_CONTRACT", "invalid_response"
    if isinstance(exc, SidecarError):
        return exc.code, "failed"
    if isinstance(exc, ConfigurationError):
        return "CONFIGURATION_ERROR", "failed"
    return exc.__class__.__name__, "failed"


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("the experiments-omp-simple run exceeded its total timeout")
    return remaining


def _bun_version() -> str | None:
    try:
        result = subprocess.run(["bun", "--version"], check=True, capture_output=True, text=True, timeout=5)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _project_revision() -> dict[str, Any]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, check=True, capture_output=True, text=True, timeout=5
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--", str(EXPERIMENT_ROOT.relative_to(PROJECT_ROOT))],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        )
        return {"git_commit": revision, "experiment_dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": None, "experiment_dirty": None}


def _sum_nullable(values: Any) -> int | None:
    concrete = [value for value in values if value is not None]
    return sum(concrete) if concrete else None
