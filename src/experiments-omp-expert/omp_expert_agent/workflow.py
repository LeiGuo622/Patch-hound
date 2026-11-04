"""Serial staged expert workflow backed by the OpenCode JSONL sidecar."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .artifacts import RunArtifacts, bounded_text, sha256_file, sha256_text, utc_now
from .case_io import CaseData, RunPaths, allocate_run_paths, load_case
from .config import (
    CODING_PLAN_API_ENV,
    CODING_PLAN_PROVIDER,
    DEEPSEEK_API_ENV,
    DEEPSEEK_PROVIDER,
    DEFAULT_CASES_DIR,
    DEFAULT_GUIDANCE_ROOT,
    DEFAULT_HISTORY_SERVER,
    DEFAULT_METADATA_DIR,
    DEFAULT_PROMPT_TEMPLATE,
    DEFAULT_RUNTIME_CONFIG,
    DGX_API_ENV,
    DGX_PROVIDER,
    PROJECT_ROOT,
    PhoenixConfig,
    RuntimeConfig,
    provider_base_url,
    resolve_linux_repo,
    resolve_provider_environment,
)
from .events import TurnExecution
from .guidance import GuidanceCorpus, GuidanceDocument, format_documents, load_corpus
from .history_mcp import direct_probe
from .isolation import build_bwrap_command, create_layout, run_isolation_probe, run_network_connectivity_probe
from .phoenix import PhoenixObserver
from .prompt import load_template, render_template
from .runtime import SidecarRuntime
from .stage_output import OPTIONAL_STAGE_IDS, StageResult, parse_selection, parse_stage_plan, parse_stage_result

STAGE_ORDER = ("execution_flow", *OPTIONAL_STAGE_IDS)
STAGES = {
    "execution_flow": ("Execution flow verification", "stage_2_execution_flow.md"),
    "resource_management": ("Resource management", "stage_3_resource_management.md"),
    "locking_synchronization": ("Locking and synchronization", "stage_4_locking_synchronization.md"),
    "security_audit": ("Security audit", "stage_5_security_audit.md"),
    "hardware_driver_review": ("Hardware and driver review", "stage_6_hardware_driver_review.md"),
}
ASSESSMENT = re.compile(
    r"^-\s+(?:\*\*)?Final Assessment:(?:\*\*)?\s*(?:\*\*|`)?"
    r"(NO_NEW_ISSUES|NEW_ISSUES_FOUND)(?:\*\*|`)?\s*$",
    re.MULTILINE,
)
CONFIDENCE = re.compile(
    r"^-\s+(?:\*\*)?Confidence:(?:\*\*)?\s*(?:\*\*|`)?(HIGH|MEDIUM|LOW)(?:\*\*|`)?\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class AnalysisResult:
    final_response: str
    assessment: str | None
    confidence: str | None
    thread_ids: dict[str, str]
    turn_ids: tuple[str, ...]
    run_dir: Path
    output_path: Path
    phoenix_trace_id: str | None

    @property
    def session_id(self) -> str:
        """Compatibility summary for the simple CLI; stages each have their own session."""
        return self.thread_ids.get("final_synthesis") or next(iter(self.thread_ids.values()), "")


@dataclass(frozen=True)
class CallResult:
    name: str
    response: str
    execution: TurnExecution
    thread_id: str


@dataclass(frozen=True)
class PromptSet:
    task_context: str
    selection: str
    planning: str
    analysis: str
    final: str
    stage_tasks: dict[str, str]
    hashes: dict[str, str]


def run_case(
    case_id: str,
    *,
    cases_dir: Path = DEFAULT_CASES_DIR,
    prompt_template: Path | None = None,
    runtime_config: Path = DEFAULT_RUNTIME_CONFIG,
    guidance_root: Path = DEFAULT_GUIDANCE_ROOT,
    guidance_dir: Path | None = None,
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
    manual_stages: Iterable[str] | None = None,
    **_ignored: Any,
) -> AnalysisResult:
    config = RuntimeConfig.load(runtime_config)
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_contract_repairs not in (None, 0):
        raise ValueError("expert workflow does not use contract repairs")
    case = load_case(case_id, cases_dir)
    corpus = load_corpus(guidance_dir or guidance_root)
    prompts = load_prompts()
    manual = normalize_manual_stages(manual_stages)
    paths = allocate_run_paths(
        case.case_dir,
        output_dir_name=config.output_dir_name,
        output_root=output_root,
        run_id=run_id,
        force_run=force_run,
    )
    artifacts = RunArtifacts(paths, save_conversation=save_conversation)
    linux_repo = resolve_linux_repo(history_linux_repo, env_path)
    timeout = config.timeout_seconds if timeout_seconds is None else timeout_seconds
    prompt_hash, runtime_hash = sha256_text(json.dumps(prompts.hashes, sort_keys=True)), sha256_file(config.path)
    limitations: list[str] = []
    states: dict[str, dict[str, Any]] = {}
    threads: dict[str, str] = {}
    turns: list[str] = []
    usage: dict[tuple[str, str], GuidanceDocument] = {}
    artifacts.initialize_manifest(
        {
            "agent": "experiments-omp-expert",
            "agent_backend": "oh-my-pi",
            "case_id": case.case_id,
            "run_id": paths.run_id,
            "status": "running",
            "logical_model": config.logical_model,
            "model_provider": config.provider,
            "reasoning_effort": config.reasoning_effort,
            "prompt_files": prompts.hashes,
            "prompt_bundle_sha256": prompt_hash,
            "runtime_config_sha256": runtime_hash,
            "commit_message_sha256": sha256_text(case.commit_message),
            "diff_sha256": sha256_text(case.diff),
            "guidance_revision": corpus.revision,
            "guidance_selected": [],
            "guidance_injected": [],
            "stage_plan": [],
            "stages": {},
            "thread_ids": {},
            "turn_ids": [],
            "workflow_limitations": [],
            "source_access": "read",
            "scratch_access": "write-private-per-call",
            "network_enabled": config.network_enabled,
            "history_tool_enabled": use_history_tool,
            "phoenix_enabled": False,
            "phoenix_project": None,
            "phoenix_trace_id": None,
            "started_at": utc_now(),
            "completed_at": None,
            "final_assessment": None,
            "confidence": None,
            "error": None,
        }
    )
    artifacts.write_model_config(
        {
            "Agent backend": "OHMYPI SDK",
            "Protocol": "jsonl-v1",
            "Logical model": config.logical_model,
            "SDK model selector": config.model_selector,
            "Provider": config.provider,
            "Reasoning effort": config.reasoning_effort,
            "Source access": "read",
            "Scratch access": "write-private-per-call",
            "Agent tool network": "enabled" if config.network_enabled else "disabled",
            "Control-plane network": "enabled",
            "History tool": "enabled" if use_history_tool else "disabled",
            "Phoenix": "configured per runtime",
            "Prompt bundle SHA-256": prompt_hash,
            "Runtime config SHA-256": runtime_hash,
            "Guidance revision": corpus.revision,
        }
    )

    def status(message: str, success: bool = True, component: str = "runtime") -> None:
        artifacts.record(component, "status", success, message)
        if live_output:
            print(f"[patchhound-omp-expert] {message}", flush=True)

    phoenix: PhoenixObserver | None = None
    try:
        auth = _resolve_auth_data_dir(auth_data_dir)
        if config.auth_required:
            _require_auth(auth, config.provider)
        try:
            pc = PhoenixConfig.load(
                runtime_path=config.path,
                env_path=env_path,
                enabled=phoenix_enabled,
                endpoint=phoenix_endpoint,
                project_name=phoenix_project,
            )
            phoenix = PhoenixObserver.create(
                config=pc,
                case_id=case.case_id,
                run_id=paths.run_id,
                model=config.logical_model,
                prompt_sha256=prompt_hash,
                input_hashes={"runtime_config_sha256": runtime_hash},
                warn=lambda m: status(m, False, "phoenix"),
                commit_message=case.commit_message,
                diff=case.diff,
                model_provider=config.provider,
                reasoning_effort=config.reasoning_effort,
                history_tool_enabled=use_history_tool,
                source_access="read",
                # Phoenix uses the canonical shared value used by the Codex
                # trace; the manifest retains the more precise per-call scope.
                scratch_access="write",
                network_enabled=config.network_enabled,
            )
            artifacts.update_manifest(
                phoenix_enabled=bool(phoenix),
                phoenix_project=pc.project_name,
                phoenix_trace_id=phoenix.trace_id if phoenix else None,
            )
            if phoenix is not None:
                phoenix.record_checkpoint(
                    "runtime.configuration",
                    input_value={"case_id": case.case_id, "run_id": paths.run_id},
                    output_value={
                        "provider": config.provider,
                        "model": config.logical_model,
                        "reasoning_effort": config.reasoning_effort,
                        "approval_policy": "never",
                        "sdk_approval_mode": "deny_all",
                        "source_access": "read",
                        "scratch_access": "write",
                        "network_enabled": config.network_enabled,
                        "history_tool_enabled": use_history_tool,
                    },
                )
        except Exception as exc:
            limitations.append(f"Phoenix unavailable: {exc.__class__.__name__}: {exc}")
            status(limitations[-1], False, "phoenix")
        deadline = time.monotonic() + timeout
        usage[("knowledge_selection", corpus.index.relative_path)] = corpus.index
        selected_call = _invoke_call(
            "knowledge_selection",
            render_selection_prompt(prompts, corpus, case),
            (corpus.index,),
            case,
            config,
            paths,
            artifacts,
            phoenix,
            auth,
            linux_repo,
            history_metadata_dir,
            deadline,
            use_history_tool,
            live_output,
        )
        _track(selected_call, artifacts, threads, turns, states)
        selected = corpus.validate_selection(
            parse_selection(selected_call.response, allow_wrapped=True)
        )
        artifacts.update_manifest(guidance_selected=list(selected))
        if manual is None:
            docs = corpus.documents_for("stage_planning", selected)
            _record_guidance(usage, "stage_planning", docs)
            try:
                planned = _invoke_call(
                    "stage_planning",
                    render_planning_prompt(prompts, corpus, case, selected, docs),
                    docs,
                    case,
                    config,
                    paths,
                    artifacts,
                    phoenix,
                    auth,
                    linux_repo,
                    history_metadata_dir,
                    deadline,
                    use_history_tool,
                    live_output,
                )
                _track(planned, artifacts, threads, turns, states)
                optional = tuple(
                    item.stage_id
                    for item in parse_stage_plan(
                        planned.response,
                        allow_wrapped=True,
                    ).stages
                )
            except Exception as exc:
                optional = OPTIONAL_STAGE_IDS
                limitations.append(
                    f"Stage planning failed ({exc.__class__.__name__}); all conditional stages were run."
                )
                _failed("stage_planning", exc, artifacts, states)
        else:
            optional = manual
        stage_ids = ("execution_flow", *optional)
        artifacts.update_manifest(stage_plan=list(stage_ids))
        results: list[StageResult] = []
        for stage_id in stage_ids:
            docs = corpus.documents_for(stage_id, selected)
            _record_guidance(usage, stage_id, docs)
            try:
                call = _invoke_call(
                    stage_id,
                    render_analysis_prompt(prompts, corpus, case, stage_id, docs),
                    docs,
                    case,
                    config,
                    paths,
                    artifacts,
                    phoenix,
                    auth,
                    linux_repo,
                    history_metadata_dir,
                    deadline,
                    use_history_tool,
                    live_output,
                )
                _track(call, artifacts, threads, turns, states)
                results.append(parse_stage_result(call.response, stage_id=stage_id))
            except Exception as exc:
                limitations.append(f"Analysis stage {stage_id} failed ({exc.__class__.__name__}: {exc}).")
                _failed(stage_id, exc, artifacts, states)
        final_docs = corpus.documents_for("final_synthesis", selected)
        _record_guidance(usage, "final_synthesis", final_docs)
        final = _invoke_call(
            "final_synthesis",
            render_final_prompt(prompts, corpus, case, final_docs, results, limitations),
            final_docs,
            case,
            config,
            paths,
            artifacts,
            phoenix,
            auth,
            linux_repo,
            history_metadata_dir,
            deadline,
            use_history_tool,
            live_output,
        )
        _track(final, artifacts, threads, turns, states)
        if not final.response.strip():
            raise RuntimeError("final synthesis returned an empty response")
        artifacts.write_analysis(final.response)
        artifacts.ensure_history_log(enabled=use_history_tool)
        assessment, confidence = _extract(ASSESSMENT, final.response), _extract(CONFIDENCE, final.response)
        artifacts.update_manifest(
            status="completed",
            completed_at=utc_now(),
            final_assessment=assessment,
            confidence=confidence,
            stages=states,
            thread_ids=threads,
            turn_ids=turns,
            workflow_limitations=limitations,
            guidance_injected=_guidance_payload(usage),
            phoenix_trace_id=phoenix.trace_id if phoenix else None,
        )
        status(f"completed assessment={assessment or 'unparsed'}")
        artifacts.write_logs()
        if phoenix:
            _record_phoenix_artifacts(phoenix, paths)
            phoenix.complete(
                status="completed",
                output=final.response,
                thread_ids=threads,
                turn_ids=turns,
                attributes={"patchhound.case_id": case.case_id},
            )
        return AnalysisResult(
            final.response,
            assessment,
            confidence,
            dict(threads),
            tuple(turns),
            paths.run_dir,
            paths.analysis,
            phoenix.trace_id if phoenix else None,
        )
    except BaseException as exc:
        _failed("runtime", exc, artifacts, states)
        artifacts.update_manifest(
            status="failed", completed_at=utc_now(), error={"type": exc.__class__.__name__, "message": str(exc)}
        )
        artifacts.write_logs()
        if phoenix:
            phoenix.fail(stage="runtime", exc=exc)
        raise


def load_prompts(root: Path | None = None) -> PromptSet:
    root = (root or Path(__file__).resolve().parents[1] / "prompts").resolve()
    paths = {
        "task_context": root / "task_context.md",
        "selection": root / "knowledge_selection.md",
        "planning": root / "stage_planning.md",
        "analysis": root / "analysis_stage.md",
        "final": root / "final_synthesis.md",
        **{s: root / "stages" / f for s, (_n, f) in STAGES.items()},
    }
    task = load_template(paths["task_context"], required={"SOURCE_TREE"})
    selection = load_template(paths["selection"], required={"SUBSYSTEM_INDEX", "COMMIT_MESSAGE", "DIFF", "SOURCE_TREE"})
    planning = load_template(
        paths["planning"], required={"SELECTED_GUIDE_NAMES_JSON", "SELECTED_REVIEW_GUIDANCE", "COMMIT_MESSAGE", "DIFF"}
    )
    analysis = load_template(
        paths["analysis"],
        required={
            "STAGE_ID",
            "STAGE_NAME",
            "GUIDANCE_REVISION",
            "REVIEW_GUIDANCE",
            "STAGE_TASK",
            "COMMIT_MESSAGE",
            "DIFF",
            "SOURCE_TREE",
        },
    )
    final = load_template(
        paths["final"],
        required={
            "GUIDANCE_REVISION",
            "REVIEW_GUIDANCE",
            "COMMIT_MESSAGE",
            "DIFF",
            "SOURCE_TREE",
            "STAGE_RESULTS_JSON",
            "WORKFLOW_LIMITATIONS_JSON",
        },
    )
    return PromptSet(
        task,
        selection,
        planning,
        analysis,
        final,
        {s: paths[s].read_text(encoding="utf-8") for s in STAGES},
        {n: sha256_file(p) for n, p in paths.items()},
    )


def render_selection_prompt(prompts: PromptSet, corpus: GuidanceCorpus, case: CaseData) -> str:
    return render_template(
        prompts.selection,
        {
            "SUBSYSTEM_INDEX": corpus.index.content.rstrip(),
            "COMMIT_MESSAGE": case.commit_message.rstrip(),
            "DIFF": case.diff.rstrip(),
            "SOURCE_TREE": ".",
        },
    )


def render_planning_prompt(
    prompts: PromptSet,
    corpus: GuidanceCorpus,
    case: CaseData,
    selected: Iterable[str],
    docs: Iterable[GuidanceDocument],
) -> str:
    return (
        render_template(prompts.task_context, {"SOURCE_TREE": "."}).rstrip()
        + "\n\n"
        + render_template(
            prompts.planning,
            {
                "SELECTED_GUIDE_NAMES_JSON": json.dumps(list(selected), ensure_ascii=False),
                "SELECTED_REVIEW_GUIDANCE": format_documents(docs),
                "COMMIT_MESSAGE": case.commit_message.rstrip(),
                "DIFF": case.diff.rstrip(),
            },
        ).rstrip()
        + "\n"
    )


def render_analysis_prompt(
    prompts: PromptSet, corpus: GuidanceCorpus, case: CaseData, stage_id: str, docs: Iterable[GuidanceDocument]
) -> str:
    values = {
        "STAGE_ID": stage_id,
        "STAGE_NAME": STAGES[stage_id][0],
        "GUIDANCE_REVISION": corpus.revision,
        "REVIEW_GUIDANCE": format_documents(docs),
        "STAGE_TASK": prompts.stage_tasks[stage_id].rstrip(),
        "COMMIT_MESSAGE": case.commit_message.rstrip(),
        "DIFF": case.diff.rstrip(),
        "SOURCE_TREE": ".",
    }
    return (
        render_template(prompts.task_context, {"SOURCE_TREE": "."}).rstrip()
        + "\n\n"
        + render_template(prompts.analysis, values).rstrip()
        + "\n"
    )


def render_final_prompt(
    prompts: PromptSet,
    corpus: GuidanceCorpus,
    case: CaseData,
    docs: Iterable[GuidanceDocument],
    results: Iterable[StageResult],
    limitations: Iterable[str],
) -> str:
    values = {
        "GUIDANCE_REVISION": corpus.revision,
        "REVIEW_GUIDANCE": format_documents(docs),
        "COMMIT_MESSAGE": case.commit_message.rstrip(),
        "DIFF": case.diff.rstrip(),
        "SOURCE_TREE": ".",
        "STAGE_RESULTS_JSON": json.dumps([r.as_dict() for r in results], ensure_ascii=False, indent=2),
        "WORKFLOW_LIMITATIONS_JSON": json.dumps(list(limitations), ensure_ascii=False, indent=2),
    }
    return (
        render_template(prompts.task_context, {"SOURCE_TREE": "."}).rstrip()
        + "\n\n"
        + render_template(prompts.final, values).rstrip()
        + "\n"
    )


def preview_case(
    case_id: str,
    *,
    cases_dir: Path = DEFAULT_CASES_DIR,
    runtime_config: Path = DEFAULT_RUNTIME_CONFIG,
    guidance_root: Path = DEFAULT_GUIDANCE_ROOT,
    manual_stages: Iterable[str] | None = None,
    **_ignored: Any,
) -> dict[str, Any]:
    case = load_case(case_id, cases_dir)
    config = RuntimeConfig.load(runtime_config)
    corpus = load_corpus(guidance_root)
    prompts = load_prompts()
    manual = normalize_manual_stages(manual_stages)
    stages = ("execution_flow", *(manual or OPTIONAL_STAGE_IDS))
    rendered = {"knowledge_selection": render_selection_prompt(prompts, corpus, case)}
    rendered.update({s: render_analysis_prompt(prompts, corpus, case, s, corpus.documents_for(s, ())) for s in stages})
    rendered["final_synthesis"] = render_final_prompt(prompts, corpus, case, (), (), ())
    return {
        "dry_run": True,
        "case_id": case.case_id,
        "model_provider": config.provider,
        "model": config.logical_model,
        "reasoning_effort": config.reasoning_effort,
        "simulated_stages": list(stages),
        "prompts": {n: {"bytes": len(v.encode()), "sha256": sha256_text(v)} for n, v in rendered.items()},
    }


def normalize_manual_stages(values: Iterable[str] | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    chosen = set(values)
    unknown = chosen - set(OPTIONAL_STAGE_IDS)
    if unknown:
        raise ValueError(f"unknown stage(s): {', '.join(sorted(unknown))}")
    return tuple(s for s in OPTIONAL_STAGE_IDS if s in chosen)


def _invoke_call(
    name: str,
    prompt: str,
    docs: Iterable[GuidanceDocument],
    case: CaseData,
    config: RuntimeConfig,
    paths: RunPaths,
    artifacts: RunArtifacts,
    phoenix: PhoenixObserver | None,
    auth: Path | None,
    linux_repo: Path,
    metadata_dir: Path,
    deadline: float,
    use_history: bool,
    live_output: bool = True,
) -> CallResult:
    root = Path(tempfile.mkdtemp(prefix=f"patchhound-omp-{name}-"))
    layout = create_layout(root)
    runtime = None
    operation = (
        phoenix.operation_start(
            _operation_name(name),
            input_value={
                "name": name,
                "prompt_sha256": sha256_text(prompt),
                "prompt_bytes": len(prompt.encode("utf-8")),
                "timeout_seconds": max(1, int(deadline - time.monotonic())),
                "guidance_files": [doc.relative_path for doc in docs],
            },
            attributes={"patchhound.stage_id": name},
        )
        if phoenix
        else None
    )
    environment_span = (
        phoenix.operation_start(
            "runtime.omp_environment",
            input_value={"stage_id": name, "source_dir": str(case.source_dir)},
            parent=operation,
        )
        if phoenix
        else None
    )
    try:
        provider_environment = resolve_provider_environment(config)
        provider_env_file: Path | None = None
        if provider_environment:
            provider_env_file = layout.root / "provider.env"
            provider_env_file.write_text(
                "".join(f"{key}={value}\n" for key, value in provider_environment.items()),
                encoding="utf-8",
            )
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
            agent_model_config.chmod(0o600)
        probe = run_isolation_probe(case.source_dir, layout)
        artifacts.record("isolation", "probe", probe.ok, json.dumps(probe.as_dict(), sort_keys=True))
        artifacts.update_manifest(agent_network_disabled=True, control_plane_network_enabled=True)
        network = run_network_connectivity_probe(
            case.source_dir,
            layout,
            target_url=PhoenixConfig.load(runtime_path=config.path).collector_endpoint,
            agent_shell_backend=probe.agent_shell_backend,
        )
        artifacts.record(
            "isolation", "network_denial_probe", not network.reachable, json.dumps(network.as_dict(), sort_keys=True)
        )
        if network.reachable:
            raise RuntimeError("Agent data-plane network isolation probe unexpectedly connected")
        definition = None
        probe_args = None
        history_span = None
        if use_history:
            touched = _first_touched_path(case.diff)
            args = {
                "kind": "file_history",
                "file_path": touched,
                "max_events": 1,
                "analysis_stage": name,
                "reason": f"{name} context",
            }
            probe_args = args
            history_span = (
                phoenix.operation_start(
                    "history.mcp.status",
                    input_value={
                        "stage_id": name,
                        "server": "patchhound_history",
                        "tool": "query_pre_patch_history",
                    },
                    parent=operation,
                )
                if phoenix
                else None
            )
            try:
                definition = direct_probe(
                    case_id=case.case_id, linux_repo=linux_repo, metadata_dir=metadata_dir, call_arguments=args
                ).definition
            except BaseException as exc:
                if phoenix is not None:
                    phoenix.operation_error(history_span, exc)
                raise
            if phoenix is not None:
                phoenix.operation_end(history_span, output_value={"available": True})
        if phoenix is not None:
            phoenix.operation_end(
                environment_span,
                output_value={
                    "source_access": "read",
                    "scratch_access": "write",
                    "network_enabled": config.network_enabled,
                    "sandbox_backend": probe.agent_shell_backend,
                    "history_mcp_enabled": use_history,
                },
            )
            environment_span = None
        command = build_bwrap_command(
            layout=layout,
            source_dir=case.source_dir,
            auth_data_dir=auth,
            history_server=DEFAULT_HISTORY_SERVER if use_history else None,
            history_linux_repo=linux_repo if use_history else None,
            history_metadata_dir=metadata_dir if use_history else None,
            history_project_root=PROJECT_ROOT / "tools" if use_history else None,
            agent_shell_backend=probe.agent_shell_backend,
            agent_model_config=agent_model_config,
            provider_env_file=provider_env_file,
            command=["/app/node_modules/.bin/bun", "/app/dist/sidecar/index.js"],
        )
        runtime = SidecarRuntime(
            command,
            message_bytes=config.limits.message_bytes,
            stderr_bytes=config.limits.stderr_bytes,
            event_output_bytes=config.limits.event_output_bytes,
            total_event_bytes=config.limits.total_event_bytes,
            dispose_grace_seconds=config.dispose_grace_seconds,
            host_cwd=PROJECT_ROOT,
        )
        runtime.start()
        remaining = max(1, deadline - time.monotonic())
        runtime.initialize(
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
                "enabled_tools": list(config.enabled_tools),
                "disabled_tools": list(config.disabled_tools),
                "history_enabled": use_history,
                "mcp_command": ["/usr/bin/python3", "-B", "/runtime/history-tools/history_mcp_server.py"],
                "mcp_schema_sha256": definition.schema_sha256 if definition else None,
                "mcp_timeout_ms": config.mcp_timeout_ms,
                "mcp_probe_arguments": probe_args,
            },
            timeout=min(remaining, config.server_ready_timeout_seconds),
        )
        started, _mcp = runtime.start_session({}, timeout=max(1, deadline - time.monotonic()))
        thread_id = str(started.session_id)
        if phoenix is not None:
            phoenix.record_thread_start(thread_id, stage_id=name, parent=operation)
            phoenix.set_thread_id(thread_id)
        span = (
            phoenix.turn_start(
                name=name,
                prompt=prompt,
                timeout_seconds=max(1, int(deadline - time.monotonic())),
                parent=operation,
            )
            if phoenix
            else None
        )
        try:
            execution = runtime.prompt(name, prompt, timeout=max(1, deadline - time.monotonic()))
        except BaseException as exc:
            if phoenix:
                phoenix.turn_error(span, exc)
            raise
        if phoenix:
            phoenix.turn_created(span, execution.turn_id)
            for event in execution.events:
                phoenix.record_event(span, name, event)
                if event.type == "assistant_delta" and event.payload.get("text"):
                    if live_output:
                        print(event.payload["text"], end="", flush=True)
            if live_output and any(event.type == "assistant_delta" for event in execution.events):
                print(flush=True)
            phoenix.turn_end(span, execution)
        _record_execution_events(artifacts, execution)
        artifacts.record_tokens(name, execution.usage)
        artifacts.record_conversation(
            name, prompt, execution.final_response, tuple(e.as_dict() for e in execution.events)
        )
        artifacts.record(
            "runtime",
            "turn_complete",
            True,
            f"stage={name} turn_id={execution.turn_id} response_bytes={len(execution.final_response.encode())}",
        )
        artifacts.write_logs()
        _append_history(paths.history_queries, layout.history_log)
        if phoenix is not None:
            bounded = _bounded_response(execution.final_response, 2048)
            phoenix.operation_end(
                operation,
                output_value={
                    "thread_id": thread_id,
                    "turn_id": execution.turn_id,
                    "response_sha256": bounded[2],
                    "response_bytes": bounded[0],
                    "response_truncated": bounded[1],
                    "response": bounded[3],
                },
            )
            operation = None
        return CallResult(name, execution.final_response, execution, thread_id)
    except BaseException as exc:
        if phoenix is not None and environment_span is not None:
            phoenix.operation_error(environment_span, exc)
            environment_span = None
        if phoenix is not None and operation is not None:
            phoenix.operation_error(operation, exc)
            operation = None
        raise
    finally:
        if runtime:
            try:
                runtime.dispose()
            except Exception as exc:
                artifacts.record("runtime", "dispose", False, str(exc))
        shutil.rmtree(root, ignore_errors=True)


def _resolve_auth_data_dir(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit.expanduser().resolve()
    value = os.environ.get("PATCHHOUND_OMP_DATA_DIR", "").strip()
    return Path(value).expanduser().resolve() if value else None


def _require_auth(path: Path | None, provider: str) -> None:
    if path is None:
        raise RuntimeError("AUTH_REQUIRED: set PATCHHOUND_OMP_DATA_DIR to the isolated OMP data directory after login")
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"AUTH_REQUIRED: OMP agent directory is missing: {path}")
    database = path / "agent.db"
    if database.is_symlink() or not database.is_file():
        raise RuntimeError(f"AUTH_REQUIRED: OMP agent database is missing: {database}")
    if not database.stat().st_size:
        raise RuntimeError(f"AUTH_REQUIRED: OMP agent database is empty: {database}")


def _first_touched_path(diff: str) -> str:
    match = re.search(r"^diff --git a/(.+?) b/(.+?)$", diff, re.MULTILINE)
    if match:
        return match.group(2)
    match = re.search(r"^\+\+\+ b/(.+?)$", diff, re.MULTILINE)
    if match:
        return match.group(1)
    for label in ("After", "File", "Before"):
        match = re.search(rf"^{label}:\s*(\S.*?)\s*$", diff, re.MULTILINE)
        if match:
            return match.group(1).strip()
    return "README"


def _operation_name(name: str) -> str:
    if name == "knowledge_selection":
        return "guidance.selection"
    if name == "stage_planning":
        return "stage.planning"
    if name == "final_synthesis":
        return "final.synthesis"
    return f"analysis.stage.{name}"


def _bounded_response(response: str, max_bytes: int) -> tuple[int, bool, str, str]:
    bounded = bounded_text(response, max_bytes)
    return bounded.raw_bytes, bounded.truncated, bounded.sha256, bounded.text


def _record_phoenix_artifacts(phoenix: PhoenixObserver, paths: RunPaths) -> None:
    for path in (
        paths.analysis,
        paths.manifest,
        paths.model_config,
        paths.process_log,
        paths.token_usage,
        paths.conversation,
        paths.history_queries,
    ):
        if path.is_file():
            phoenix.record_checkpoint(
                f"artifact.{path.stem}",
                input_value={"artifact": path.name},
                output_value={"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)},
            )


def _track(
    call: CallResult,
    artifacts: RunArtifacts,
    threads: dict[str, str],
    turns: list[str],
    states: dict[str, dict[str, Any]],
) -> None:
    threads[call.name] = call.thread_id
    turns.append(call.execution.turn_id)
    states[call.name] = {
        "status": "completed",
        "thread_id": call.thread_id,
        "turn_id": call.execution.turn_id,
        "response_bytes": len(call.response.encode()),
        "response_sha256": sha256_text(call.response),
    }
    artifacts.update_manifest(thread_ids=threads, turn_ids=turns, stages=states)


def _failed(name: str, exc: BaseException, artifacts: RunArtifacts, states: dict[str, dict[str, Any]]) -> None:
    states[name] = {"status": "failed", "error": {"type": exc.__class__.__name__, "message": str(exc)}}
    artifacts.record("runtime", f"{name}.failed", False, str(exc))
    artifacts.update_manifest(stages=states)


def _record_guidance(
    usage: dict[tuple[str, str], GuidanceDocument], consumer: str, docs: Iterable[GuidanceDocument]
) -> None:
    for doc in docs:
        usage[(consumer, doc.relative_path)] = doc


def _guidance_payload(usage: dict[tuple[str, str], GuidanceDocument]) -> list[dict[str, Any]]:
    return [
        {"consumer": c, "path": p, "sha256": d.sha256, "bytes": d.byte_count} for (c, p), d in sorted(usage.items())
    ]


def _extract(pattern: re.Pattern[str], value: str) -> str | None:
    match = pattern.search(value)
    return match.group(1) if match else None


def _append_history(destination: Path, source: Path) -> None:
    if not source.is_file():
        return
    text = source.read_text(encoding="utf-8")
    with destination.open("a", encoding="utf-8") as handle:
        if destination.stat().st_size:
            handle.write("\n\n")
        handle.write(text)


def _record_execution_events(artifacts: RunArtifacts, execution: TurnExecution) -> None:
    """Persist bounded event metadata without storing hidden reasoning text."""
    for event in execution.events:
        if event.type in {"assistant_delta", "reasoning_delta"}:
            continue
        payload = dict(event.payload)
        artifacts.record("sidecar_event", event.type, True, json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def render_case_prompt(
    case_id: str, *, cases_dir: Path = DEFAULT_CASES_DIR, prompt_template: Path = DEFAULT_PROMPT_TEMPLATE
) -> str:
    case = load_case(case_id, cases_dir)
    corpus = load_corpus(DEFAULT_GUIDANCE_ROOT)
    return render_selection_prompt(load_prompts(), corpus, case)
