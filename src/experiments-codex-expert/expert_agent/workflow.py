"""Serial, independently isolated staged patch-security workflow."""

from __future__ import annotations

import json
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .artifacts import RunArtifacts, bounded_text, sha256_file, sha256_text, utc_now
from .case_io import CaseData, RunPaths, allocate_run_paths, load_case
from .codex_runtime import (
    build_codex_config,
    build_session_config,
    check_history_tool,
    external_sandbox_read_paths,
    import_codex_sdk,
    load_runtime_config,
    prepare_isolated_environment,
    resolve_codex_binary,
    resolve_linux_repo,
    runtime_selection,
    select_codex_sandbox_backend,
)
from .config import DEFAULT_GUIDANCE_ROOT, DEFAULT_METADATA_DIR, PROMPTS_ROOT, PhoenixConfig
from .events import TurnExecution, run_turn_with_timeout
from .guidance import GuidanceCorpus, GuidanceDocument, format_documents, load_corpus
from .phoenix import PhoenixObserver
from .prompt import load_template, render_template
from .stage_output import OPTIONAL_STAGE_IDS, StageResult, parse_selection, parse_stage_plan, parse_stage_result

STAGE_ORDER = ("execution_flow", *OPTIONAL_STAGE_IDS)
STAGES: dict[str, tuple[str, str]] = {
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
    cases_dir: Path,
    runtime_config: Path,
    guidance_root: Path = DEFAULT_GUIDANCE_ROOT,
    run_id: str | None = None,
    force_run: bool = False,
    output_root: Path | None = None,
    model: str | None = None,
    codex_bin: str | None = None,
    timeout_seconds: int = 7200,
    use_history_tool: bool = True,
    history_linux_repo: Path | None = None,
    env_path: Path | None = None,
    phoenix_enabled: bool | None = None,
    phoenix_endpoint: str | None = None,
    phoenix_project: str | None = None,
    save_conversation: bool = False,
    live_output: bool = True,
    manual_stages: Iterable[str] | None = None,
) -> AnalysisResult:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    case = load_case(case_id, cases_dir)
    runtime_config = runtime_config.expanduser().resolve()
    load_runtime_config(runtime_config)
    corpus = load_corpus(guidance_root)
    prompts = load_prompts()
    selected_manual = normalize_manual_stages(manual_stages)
    run_paths = allocate_run_paths(
        case.case_dir,
        run_id=run_id,
        force_run=force_run,
        output_root=output_root,
    )
    artifacts = RunArtifacts(run_paths, save_conversation=save_conversation)
    provider, effective_model, reasoning = runtime_selection(runtime_config, model)
    runtime_hash = sha256_file(runtime_config)
    prompt_bundle_hash = sha256_text(json.dumps(prompts.hashes, sort_keys=True))
    started_at = utc_now()
    deadline = time.monotonic() + timeout_seconds
    phoenix_warning: str | None = None
    try:
        phoenix_config = PhoenixConfig.load(
            runtime_path=runtime_config,
            env_path=env_path,
            enabled=phoenix_enabled,
            endpoint=phoenix_endpoint,
            project_name=phoenix_project,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry configuration is fail-open.
        phoenix_config = PhoenixConfig(enabled=False)
        phoenix_warning = f"Phoenix configuration disabled: {exc.__class__.__name__}: {exc}"
    phoenix_project_name = phoenix_config.effective_project_name(effective_model)
    linux_repo = resolve_linux_repo(history_linux_repo, env_path)
    thread_ids: dict[str, str] = {}
    turn_ids: list[str] = []
    stage_states: dict[str, dict[str, Any]] = {}
    workflow_limitations: list[str] = []
    guidance_usage: dict[tuple[str, str], GuidanceDocument] = {}
    selection_names: tuple[str, ...] = ()
    selection_source = "manual" if selected_manual is not None else "planner"

    artifacts.initialize_manifest(
        {
            "agent": "experiments-codex-expert",
            "case_id": case.case_id,
            "run_id": run_paths.run_id,
            "status": "running",
            "model_provider": provider,
            "model": effective_model,
            "reasoning_effort": reasoning,
            "prompt_files": prompts.hashes,
            "prompt_bundle_sha256": prompt_bundle_hash,
            "commit_message_sha256": sha256_text(case.commit_message),
            "diff_sha256": sha256_text(case.diff),
            "runtime_config_sha256": runtime_hash,
            "guidance_revision": corpus.revision,
            "guidance_selected": [],
            "guidance_injected": [],
            "stage_selection_source": selection_source,
            "stage_plan": [],
            "stages": {},
            "thread_ids": {},
            "turn_ids": [],
            "workflow_limitations": [],
            "source_access": "read",
            "scratch_access": "write",
            "network_enabled": False,
            "native_web_search_enabled": False,
            "control_plane_network_enabled": True,
            "sandbox_backend": "auto",
            "history_tool_enabled": use_history_tool,
            "phoenix_enabled": phoenix_config.enabled,
            "phoenix_project": phoenix_project_name if phoenix_config.enabled else None,
            "phoenix_trace_id": None,
            "started_at": started_at,
            "completed_at": None,
            "final_assessment": None,
            "confidence": None,
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
            "Scratch access": "write (private per call)",
            "Agent tool network": "disabled",
            "Native web search": "disabled",
            "Control-plane network": "enabled",
            "Sandbox backend": "auto: bubblewrap when available, external Landlock fallback otherwise",
            "History tool": "enabled" if use_history_tool else "disabled",
            "Phoenix": "enabled" if phoenix_config.enabled else "disabled",
            "Phoenix project": phoenix_project_name if phoenix_config.enabled else "disabled",
            "Prompt bundle SHA-256": prompt_bundle_hash,
            "Guidance revision": corpus.revision,
            "Runtime config SHA-256": runtime_hash,
        }
    )

    def status(message: str, *, success: bool = True) -> None:
        artifacts.record("runtime", "status", success, message)
        if live_output:
            print(f"[patchhound-expert] {message}", flush=True)

    def warn_phoenix(message: str) -> None:
        artifacts.record("phoenix", "warning", False, message)
        if live_output:
            print(f"[patchhound-expert] {message}", flush=True)

    phoenix: PhoenixObserver | None = None
    active_stage = "initialization"
    try:
        if phoenix_warning:
            warn_phoenix(phoenix_warning)
        try:
            phoenix = PhoenixObserver.create(
                config=phoenix_config,
                case_id=case.case_id,
                run_id=run_paths.run_id,
                model=effective_model,
                commit_message=case.commit_message,
                diff=case.diff,
                prompt_sha256=prompt_bundle_hash,
                runtime_config_sha256=runtime_hash,
                history_tool_enabled=use_history_tool,
                warn=warn_phoenix,
                model_provider=provider,
                reasoning_effort=reasoning,
            )
        except Exception as exc:  # noqa: BLE001 - optional telemetry is fail-open.
            warn_phoenix(f"Phoenix setup disabled: {exc.__class__.__name__}: {exc}")
            phoenix = None
        status(f"case={case.case_id}")
        status(f"source={case.source_dir}")
        status(f"run_output_dir={run_paths.run_dir}")
        status(f"model_provider={provider} model={effective_model} reasoning={reasoning}")
        status(f"guidance_revision={corpus.revision}")
        if phoenix is not None:
            status(f"phoenix=enabled trace_id={phoenix.trace_id} project={phoenix_project_name}")
            phoenix.record_checkpoint(
                "runtime.configuration",
                input_value={"case_id": case.case_id, "run_id": run_paths.run_id},
                output_value={
                    "provider": provider,
                    "model": effective_model,
                    "reasoning_effort": reasoning,
                    "approval_policy": "never",
                    "sdk_approval_mode": "deny_all",
                    "source_access": "read",
                    "scratch_access": "write_private_per_call",
                    "network_enabled": False,
                    "control_plane_network_enabled": True,
                    "history_tool_enabled": use_history_tool,
                },
            )

        with tempfile.TemporaryDirectory(prefix="patchhound-expert-") as temporary:
            host_temp_root = Path(temporary)
            active_stage = "knowledge_selection"
            selection_prompt = render_selection_prompt(prompts, corpus, case)
            guidance_usage[(active_stage, corpus.index.relative_path)] = corpus.index
            selection_call = _invoke_call(
                name=active_stage,
                prompt=selection_prompt,
                guidance_paths=(corpus.index.relative_path,),
                call_root=host_temp_root / "01-knowledge-selection",
                case=case,
                runtime_config=runtime_config,
                run_paths=run_paths,
                artifacts=artifacts,
                phoenix=phoenix,
                codex_bin=codex_bin,
                model=model,
                use_history_tool=use_history_tool,
                history_linux_repo=linux_repo,
                deadline=deadline,
                live_output=live_output,
            )
            _track_call(selection_call, artifacts, thread_ids, turn_ids, stage_states)
            selection_names = corpus.validate_selection(
                parse_selection(selection_call.response, allow_wrapped=True)
            )
            stage_states[active_stage]["parse_status"] = "valid"
            artifacts.record("guidance", "selection", True, f"selected={list(selection_names)}")
            artifacts.update_manifest(guidance_selected=list(selection_names))
            if phoenix is not None:
                phoenix.record_checkpoint(
                    "guidance.selection.result",
                    input_value={"response_sha256": sha256_text(selection_call.response)},
                    output_value={"selected": list(selection_names)},
                )

            if selected_manual is None:
                active_stage = "stage_planning"
                planning_docs = corpus.documents_for("stage_planning", selection_names)
                _record_guidance_use(guidance_usage, active_stage, planning_docs)
                planning_prompt = render_planning_prompt(prompts, corpus, case, selection_names, planning_docs)
                try:
                    planning_call = _invoke_call(
                        name=active_stage,
                        prompt=planning_prompt,
                        guidance_paths=tuple(doc.relative_path for doc in planning_docs),
                        call_root=host_temp_root / "02-stage-planning",
                        case=case,
                        runtime_config=runtime_config,
                        run_paths=run_paths,
                        artifacts=artifacts,
                        phoenix=phoenix,
                        codex_bin=codex_bin,
                        model=model,
                        use_history_tool=use_history_tool,
                        history_linux_repo=linux_repo,
                        deadline=deadline,
                        live_output=live_output,
                    )
                    _track_call(planning_call, artifacts, thread_ids, turn_ids, stage_states)
                    plan = parse_stage_plan(planning_call.response, allow_wrapped=True)
                    optional = tuple(stage.stage_id for stage in plan.stages)
                    stage_states[active_stage]["parse_status"] = "valid"
                    stage_states[active_stage]["decisions"] = [
                        {"stage_id": stage.stage_id, "reason": stage.reason} for stage in plan.stages
                    ]
                    for warning in plan.warnings:
                        artifacts.record("planning", "warning", False, warning)
                    artifacts.record(
                        "planning",
                        "selected_stages",
                        True,
                        "; ".join(f"{stage.stage_id}: {stage.reason}" for stage in plan.stages) or "none",
                    )
                except Exception as exc:  # noqa: BLE001 - planner has an explicit conservative fallback.
                    optional = OPTIONAL_STAGE_IDS
                    limitation = (
                        f"Stage planning failed ({exc.__class__.__name__}: {exc}); all conditional stages were run."
                    )
                    workflow_limitations.append(limitation)
                    _record_failed_stage(active_stage, exc, artifacts, stage_states)
                    artifacts.record("planning", "fallback_all", False, limitation)
                    if phoenix is not None:
                        phoenix.record_checkpoint(
                            "stage.planning.fallback",
                            output_value={"stages": list(optional), "error": limitation},
                        )
            else:
                optional = selected_manual
                artifacts.record("planning", "manual_selection", True, f"stages={list(optional)}")

            analysis_stage_ids = ("execution_flow", *optional)
            artifacts.update_manifest(
                stage_plan=list(analysis_stage_ids),
                stage_selection_source=selection_source,
                workflow_limitations=workflow_limitations,
            )
            stage_results: list[StageResult] = []
            call_number = 3 if selected_manual is None else 2
            for stage_id in analysis_stage_ids:
                active_stage = stage_id
                docs = corpus.documents_for(stage_id, selection_names)
                _record_guidance_use(guidance_usage, stage_id, docs)
                stage_prompt = render_analysis_prompt(prompts, corpus, case, stage_id, docs)
                try:
                    execution = _invoke_call(
                        name=stage_id,
                        prompt=stage_prompt,
                        guidance_paths=tuple(doc.relative_path for doc in docs),
                        call_root=host_temp_root / f"{call_number:02d}-{stage_id.replace('_', '-')}",
                        case=case,
                        runtime_config=runtime_config,
                        run_paths=run_paths,
                        artifacts=artifacts,
                        phoenix=phoenix,
                        codex_bin=codex_bin,
                        model=model,
                        use_history_tool=use_history_tool,
                        history_linux_repo=linux_repo,
                        deadline=deadline,
                        live_output=live_output,
                    )
                    _track_call(execution, artifacts, thread_ids, turn_ids, stage_states)
                    parsed = parse_stage_result(execution.response, stage_id=stage_id)
                    stage_results.append(parsed)
                    stage_states[stage_id]["parse_status"] = "valid"
                    stage_states[stage_id]["concern_count"] = len(parsed.concerns)
                    artifacts.record(
                        "analysis",
                        stage_id,
                        True,
                        f"concerns={len(parsed.concerns)} dismissed={len(parsed.dismissed_concerns)}",
                    )
                except Exception as exc:  # noqa: BLE001 - individual analysis stages are fail-soft.
                    limitation = f"Analysis stage {stage_id} failed ({exc.__class__.__name__}: {exc})."
                    workflow_limitations.append(limitation)
                    _record_failed_stage(stage_id, exc, artifacts, stage_states)
                    artifacts.record("analysis", stage_id, False, limitation)
                    if phoenix is not None:
                        phoenix.record_checkpoint(
                            f"analysis.stage.{stage_id}.failure",
                            output_value={"error": limitation},
                        )
                call_number += 1
                _update_runtime_manifest(
                    artifacts,
                    stage_states,
                    thread_ids,
                    turn_ids,
                    workflow_limitations,
                    guidance_usage,
                )

            active_stage = "final_synthesis"
            final_docs = corpus.documents_for(active_stage, selection_names)
            _record_guidance_use(guidance_usage, active_stage, final_docs)
            final_prompt = render_final_prompt(
                prompts,
                corpus,
                case,
                final_docs,
                stage_results,
                workflow_limitations,
            )
            final_call = _invoke_call(
                name=active_stage,
                prompt=final_prompt,
                guidance_paths=tuple(doc.relative_path for doc in final_docs),
                call_root=host_temp_root / f"{call_number:02d}-final-synthesis",
                case=case,
                runtime_config=runtime_config,
                run_paths=run_paths,
                artifacts=artifacts,
                phoenix=phoenix,
                codex_bin=codex_bin,
                model=model,
                use_history_tool=use_history_tool,
                history_linux_repo=linux_repo,
                deadline=deadline,
                live_output=live_output,
            )
            _track_call(final_call, artifacts, thread_ids, turn_ids, stage_states)
            final_response = final_call.response
            if not final_response.strip():
                raise RuntimeError("final synthesis returned an empty response")

        artifacts.write_analysis(final_response)
        artifacts.ensure_history_log(enabled=use_history_tool)
        artifacts.record("filesystem", f"write {run_paths.analysis}", True)
        assessment = _extract(ASSESSMENT, final_response)
        confidence = _extract(CONFIDENCE, final_response)
        trace_id = phoenix.trace_id if phoenix is not None else None
        _update_runtime_manifest(
            artifacts,
            stage_states,
            thread_ids,
            turn_ids,
            workflow_limitations,
            guidance_usage,
            status="completed",
            completed_at=utc_now(),
            final_assessment=assessment,
            confidence=confidence,
            phoenix_trace_id=trace_id,
            error=None,
        )
        artifacts.record("runtime", "complete", True, f"assessment={assessment or 'unparsed'}")
        artifacts.write_logs()
        if phoenix is not None:
            _record_phoenix_artifacts(phoenix, run_paths)
            phoenix.complete(output=final_response, thread_ids=thread_ids, turn_ids=turn_ids)
            artifacts.write_logs()
        return AnalysisResult(
            final_response=final_response,
            assessment=assessment,
            confidence=confidence,
            thread_ids=dict(thread_ids),
            turn_ids=tuple(turn_ids),
            run_dir=run_paths.run_dir,
            output_path=run_paths.analysis,
            phoenix_trace_id=trace_id,
        )
    except BaseException as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        _record_failed_stage(active_stage, exc, artifacts, stage_states)
        if phoenix is not None:
            phoenix.fail(stage=active_stage, exc=exc)
        artifacts.record(
            "runtime", "interrupted" if interrupted else "failed", False, f"{exc.__class__.__name__}: {exc}"
        )
        _update_runtime_manifest(
            artifacts,
            stage_states,
            thread_ids,
            turn_ids,
            workflow_limitations,
            guidance_usage,
            status="interrupted" if interrupted else "failed",
            completed_at=utc_now(),
            phoenix_trace_id=phoenix.trace_id if phoenix is not None else None,
            error={"type": exc.__class__.__name__, "message": str(exc), "stage": active_stage},
        )
        artifacts.write_logs()
        raise


def load_prompts(root: Path = PROMPTS_ROOT) -> PromptSet:
    paths = {
        "task_context": root / "task_context.md",
        "knowledge_selection": root / "knowledge_selection.md",
        "stage_planning": root / "stage_planning.md",
        "analysis_stage": root / "analysis_stage.md",
        "final_synthesis": root / "final_synthesis.md",
        **{stage_id: root / "stages" / filename for stage_id, (_name, filename) in STAGES.items()},
    }
    task = load_template(paths["task_context"], required={"SOURCE_TREE"})
    selection = load_template(
        paths["knowledge_selection"], required={"SUBSYSTEM_INDEX", "COMMIT_MESSAGE", "DIFF", "SOURCE_TREE"}
    )
    planning = load_template(
        paths["stage_planning"],
        required={"SELECTED_GUIDE_NAMES_JSON", "SELECTED_REVIEW_GUIDANCE", "COMMIT_MESSAGE", "DIFF"},
    )
    analysis = load_template(
        paths["analysis_stage"],
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
        paths["final_synthesis"],
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
    stage_tasks = {stage_id: paths[stage_id].read_text(encoding="utf-8") for stage_id in STAGES}
    return PromptSet(
        task_context=task,
        selection=selection,
        planning=planning,
        analysis=analysis,
        final=final,
        stage_tasks=stage_tasks,
        hashes={name: sha256_file(path) for name, path in paths.items()},
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
    documents: Iterable[GuidanceDocument],
) -> str:
    common = render_template(prompts.task_context, {"SOURCE_TREE": "."})
    stage = render_template(
        prompts.planning,
        {
            "SELECTED_GUIDE_NAMES_JSON": json.dumps(list(selected), ensure_ascii=False),
            "SELECTED_REVIEW_GUIDANCE": format_documents(documents),
            "COMMIT_MESSAGE": case.commit_message.rstrip(),
            "DIFF": case.diff.rstrip(),
        },
    )
    return f"{common.rstrip()}\n\n{stage.rstrip()}\n"


def render_analysis_prompt(
    prompts: PromptSet,
    corpus: GuidanceCorpus,
    case: CaseData,
    stage_id: str,
    documents: Iterable[GuidanceDocument],
) -> str:
    display_name, _filename = STAGES[stage_id]
    common = render_template(prompts.task_context, {"SOURCE_TREE": "."})
    stage = render_template(
        prompts.analysis,
        {
            "STAGE_ID": stage_id,
            "STAGE_NAME": display_name,
            "GUIDANCE_REVISION": corpus.revision,
            "REVIEW_GUIDANCE": format_documents(documents),
            "STAGE_TASK": prompts.stage_tasks[stage_id].rstrip(),
            "COMMIT_MESSAGE": case.commit_message.rstrip(),
            "DIFF": case.diff.rstrip(),
            "SOURCE_TREE": ".",
        },
    )
    return f"{common.rstrip()}\n\n{stage.rstrip()}\n"


def render_final_prompt(
    prompts: PromptSet,
    corpus: GuidanceCorpus,
    case: CaseData,
    documents: Iterable[GuidanceDocument],
    stage_results: Iterable[StageResult],
    workflow_limitations: Iterable[str],
) -> str:
    common = render_template(prompts.task_context, {"SOURCE_TREE": "."})
    final = render_template(
        prompts.final,
        {
            "GUIDANCE_REVISION": corpus.revision,
            "REVIEW_GUIDANCE": format_documents(documents),
            "COMMIT_MESSAGE": case.commit_message.rstrip(),
            "DIFF": case.diff.rstrip(),
            "SOURCE_TREE": ".",
            "STAGE_RESULTS_JSON": json.dumps(
                [result.as_dict() for result in stage_results], ensure_ascii=False, indent=2
            ),
            "WORKFLOW_LIMITATIONS_JSON": json.dumps(list(workflow_limitations), ensure_ascii=False, indent=2),
        },
    )
    return f"{common.rstrip()}\n\n{final.rstrip()}\n"


def preview_case(
    case_id: str,
    *,
    cases_dir: Path,
    runtime_config: Path,
    guidance_root: Path = DEFAULT_GUIDANCE_ROOT,
    model: str | None = None,
    manual_stages: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate and render all possible prompts without allocating a run or starting services."""

    case = load_case(case_id, cases_dir)
    load_runtime_config(runtime_config)
    provider, effective_model, reasoning = runtime_selection(runtime_config, model)
    corpus = load_corpus(guidance_root)
    prompts = load_prompts()
    manual = normalize_manual_stages(manual_stages)
    selection = render_selection_prompt(prompts, corpus, case)
    planning_docs = corpus.documents_for("stage_planning", ())
    rendered: dict[str, str] = {"knowledge_selection": selection}
    if manual is None:
        rendered["stage_planning"] = render_planning_prompt(prompts, corpus, case, (), planning_docs)
        stages = STAGE_ORDER
    else:
        stages = ("execution_flow", *manual)
    for stage_id in stages:
        rendered[stage_id] = render_analysis_prompt(prompts, corpus, case, stage_id, corpus.documents_for(stage_id, ()))
    empty_results = [parse_stage_result("{}", stage_id=stage_id) for stage_id in stages]
    rendered["final_synthesis"] = render_final_prompt(
        prompts,
        corpus,
        case,
        corpus.documents_for("final_synthesis", ()),
        empty_results,
        (),
    )
    return {
        "dry_run": True,
        "case_id": case.case_id,
        "model_provider": provider,
        "model": effective_model,
        "reasoning_effort": reasoning,
        "guidance_revision": corpus.revision,
        "simulated_selection": [],
        "simulated_stages": list(stages),
        "prompts": {
            name: {"bytes": len(value.encode("utf-8")), "sha256": sha256_text(value)}
            for name, value in rendered.items()
        },
    }


def normalize_manual_stages(values: Iterable[str] | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    chosen = list(values)
    unknown = sorted(set(chosen) - set(OPTIONAL_STAGE_IDS))
    if unknown:
        raise ValueError(f"unknown --stage value(s): {', '.join(unknown)}")
    selected = set(chosen)
    return tuple(stage_id for stage_id in OPTIONAL_STAGE_IDS if stage_id in selected)


def _invoke_call(
    *,
    name: str,
    prompt: str,
    guidance_paths: tuple[str, ...],
    call_root: Path,
    case: CaseData,
    runtime_config: Path,
    run_paths: RunPaths,
    artifacts: RunArtifacts,
    phoenix: PhoenixObserver | None,
    codex_bin: str | None,
    model: str | None,
    use_history_tool: bool,
    history_linux_repo: Path,
    deadline: float,
    live_output: bool,
) -> CallResult:
    ApprovalMode, Codex, Sandbox = import_codex_sdk()
    operation = (
        phoenix.operation_start(
            _operation_name(name),
            input_value={
                "name": name,
                "prompt_sha256": sha256_text(prompt),
                "prompt_bytes": len(prompt.encode("utf-8")),
                "timeout_seconds": _remaining_seconds(deadline),
                "guidance_files": list(guidance_paths),
            },
            attributes={"patchhound.stage_id": name},
        )
        if phoenix is not None
        else None
    )
    try:
        environment_span = (
            phoenix.operation_start(
                "runtime.codex_environment",
                input_value={"stage_id": name, "source_dir": str(case.source_dir)},
                parent=operation,
            )
            if phoenix is not None
            else None
        )
        isolated = prepare_isolated_environment(call_root, runtime_config)
        sandbox_backend = select_codex_sandbox_backend()
        if sandbox_backend == "external-landlock":
            # Keep the app-server's own HOME separate from its auth cache.  The
            # shell environment policy below omits CODEX_HOME, so model-created
            # commands cannot discover the copied credential file.
            shell_home = isolated.scratch_dir / "home"
            shell_home.mkdir(parents=True, exist_ok=True)
            isolated_env = dict(isolated.env)
            isolated_env["HOME"] = str(shell_home)
            real_codex_bin = resolve_codex_binary(codex_bin)
            read_paths = external_sandbox_read_paths(
                source_dir=case.source_dir,
                codex_bin=real_codex_bin,
                history_linux_repo=history_linux_repo,
                history_metadata_dir=DEFAULT_METADATA_DIR,
            )
            writable_paths = (
                isolated.codex_home,
                isolated.sqlite_home,
                isolated.scratch_dir,
                run_paths.run_dir,
                Path("/dev"),
            )
            config = build_codex_config(
                codex_bin=codex_bin,
                cwd=case.source_dir,
                env=isolated_env,
                sandbox_backend=sandbox_backend,
                source_dir=case.source_dir,
                read_paths=read_paths,
                writable_paths=writable_paths,
            )
            session_codex_bin = str(real_codex_bin)
        else:
            config = build_codex_config(codex_bin=codex_bin, cwd=case.source_dir, env=isolated.env)
            session_codex_bin = getattr(config, "codex_bin", None)
        session = build_session_config(
            runtime_config,
            scratch_dir=isolated.scratch_dir,
            codex_bin=session_codex_bin,
            use_history_tool=use_history_tool,
            case_id=case.case_id,
            history_linux_repo=history_linux_repo,
            history_query_log=run_paths.history_queries,
            sandbox_backend=sandbox_backend,
        )
        artifacts.record(
            "runtime",
            "isolated_environment",
            True,
            f"stage={name} source=read scratch=write-private agent_network=disabled "
            f"control_plane_network=enabled sandbox_backend={sandbox_backend}",
        )
        artifacts.update_manifest(
            sandbox_backend=sandbox_backend,
            network_enabled=False,
            control_plane_network_enabled=True,
        )
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
                    "history_mcp_enabled": use_history_tool,
                },
            )
            environment_span = None
        with Codex(config=config) as codex:
            thread = codex.thread_start(
                approval_mode=ApprovalMode.deny_all,
                cwd=str(case.source_dir),
                ephemeral=True,
                config=session,
                model=model,
                sandbox=Sandbox.workspace_write if sandbox_backend == "external-landlock" else None,
            )
            thread_id = str(thread.id)
            artifacts.record("codex_sdk", "thread_start", True, f"stage={name} thread_id={thread_id}")
            if phoenix is not None:
                phoenix.record_thread_start(thread_id, stage_id=name, parent=operation)
            if use_history_tool:
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
                    phoenix.operation_end(history_span, output_value={"available": checked})
                artifacts.record(
                    "codex_sdk",
                    "history_tool_status",
                    True,
                    f"stage={name} {'available' if checked else 'status check unsupported by SDK'}",
                )
            execution = run_turn_with_timeout(
                codex=codex,
                thread=thread,
                prompt=prompt,
                source_dir=case.source_dir,
                approval_mode=ApprovalMode,
                model=model,
                timeout_seconds=_remaining_seconds(deadline),
                turn_name=name,
                live_output=live_output,
                artifacts=artifacts,
                phoenix=phoenix,
                turn_parent=operation,
            )
        response = execution.final_response
        bounded = bounded_text(response, 2048)
        artifacts.record(
            "codex_sdk",
            "turn_complete",
            True,
            f"stage={name} thread_id={thread_id} turn_id={execution.turn_id} response_bytes={bounded.raw_bytes} "
            f"truncated={bounded.truncated} response_sha256={bounded.sha256} response={bounded.text}",
        )
        artifacts.record_tokens(name, execution.result)
        artifacts.record_conversation(name, prompt, execution.result, execution.events)
        artifacts.write_logs()
        if phoenix is not None:
            phoenix.operation_end(
                operation,
                output_value={
                    "thread_id": thread_id,
                    "turn_id": execution.turn_id,
                    "response_sha256": bounded.sha256,
                    "response_bytes": bounded.raw_bytes,
                    "response_truncated": bounded.truncated,
                    "response": bounded.text,
                },
            )
        return CallResult(name, response, execution, thread_id)
    except BaseException as exc:
        if phoenix is not None and environment_span is not None:
            phoenix.operation_error(environment_span, exc)
        if phoenix is not None:
            phoenix.operation_error(operation, exc)
        raise


def _track_call(
    call: CallResult,
    artifacts: RunArtifacts,
    thread_ids: dict[str, str],
    turn_ids: list[str],
    stage_states: dict[str, dict[str, Any]],
) -> None:
    thread_ids[call.name] = call.thread_id
    turn_ids.append(call.execution.turn_id)
    stage_states[call.name] = {
        "status": "completed",
        "thread_id": call.thread_id,
        "turn_id": call.execution.turn_id,
        "response_sha256": sha256_text(call.response),
        "response_bytes": len(call.response.encode("utf-8")),
    }
    artifacts.update_manifest(thread_ids=thread_ids, turn_ids=turn_ids, stages=stage_states)


def _record_failed_stage(
    stage_id: str,
    exc: BaseException,
    artifacts: RunArtifacts,
    stage_states: dict[str, dict[str, Any]],
) -> None:
    existing = stage_states.setdefault(stage_id, {})
    existing.update({"status": "failed", "error": {"type": exc.__class__.__name__, "message": str(exc)}})
    artifacts.update_manifest(stages=stage_states)


def _record_guidance_use(
    usage: dict[tuple[str, str], GuidanceDocument], consumer: str, documents: Iterable[GuidanceDocument]
) -> None:
    for document in documents:
        usage[(consumer, document.relative_path)] = document


def _update_runtime_manifest(
    artifacts: RunArtifacts,
    stage_states: dict[str, dict[str, Any]],
    thread_ids: dict[str, str],
    turn_ids: list[str],
    workflow_limitations: list[str],
    usage: dict[tuple[str, str], GuidanceDocument],
    **extra: Any,
) -> None:
    injected = [
        {
            "consumer": consumer,
            "path": path,
            "sha256": document.sha256,
            "bytes": document.byte_count,
        }
        for (consumer, path), document in sorted(usage.items())
    ]
    artifacts.update_manifest(
        stages=stage_states,
        thread_ids=thread_ids,
        turn_ids=turn_ids,
        workflow_limitations=workflow_limitations,
        guidance_injected=injected,
        **extra,
    )


def _remaining_seconds(deadline: float) -> int:
    remaining = int(deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("the experiments-codex-expert run exceeded its total timeout")
    return remaining


def _operation_name(name: str) -> str:
    if name == "knowledge_selection":
        return "guidance.selection"
    if name == "stage_planning":
        return "stage.planning"
    if name == "final_synthesis":
        return "final.synthesis"
    return f"analysis.stage.{name}"


def _extract(pattern: re.Pattern[str], response: str) -> str | None:
    match = pattern.search(response)
    return match.group(1) if match else None


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
