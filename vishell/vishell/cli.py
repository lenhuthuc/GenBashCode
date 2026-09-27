"""`python -m vishell <step> --config configs/x.yaml --set a.b=c [--force]`.

Every step reads its knobs from the merged config (configs/default.yaml <- --config file
<- --set overrides) and writes its outputs under Paths(root, run_name). `pipeline` runs
every step in order, skipping ones with a Paths.is_done() marker unless --force.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from .paths import PROJECT_ROOT, Paths

_DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "default.yaml"

STEPS = [
    "data-nl2bash", "verify", "build-dataset", "sft-nl2bash", "merge-nl2bash",
    "sft-scenarios", "merge-scenarios", "grpo", "merge-grpo", "export",
    "evaluate", "report", "demo",
]
# GPU/heavy-dependency steps: skipped in `pipeline` when train.enabled=false (used by
# the laptop smoke run, which only exercises data/verify/eval-harness/report).
GPU_STEPS = {"sft-nl2bash", "merge-nl2bash", "sft-scenarios", "merge-scenarios",
             "grpo", "merge-grpo", "export"}


def _deep_update(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def _set_path(cfg: dict, dotted_key: str, raw_value: str) -> None:
    value = yaml.safe_load(raw_value)
    keys = dotted_key.split(".")
    node = cfg
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value


def load_config(config_path: str | None, overrides: list[str]) -> dict:
    cfg = yaml.safe_load(_DEFAULT_CONFIG.read_text(encoding="utf-8"))
    if config_path:
        override_cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        _deep_update(cfg, override_cfg or {})
    for item in overrides:
        if "=" not in item:
            raise ValueError(f"--set expects key=value, got {item!r}")
        key, value = item.split("=", 1)
        _set_path(cfg, key, value)
    return cfg


def make_paths(cfg: dict) -> Paths:
    return Paths(root=cfg["paths"]["root"], run_name=cfg["run_name"])


def make_backend(cfg: dict):
    from .sandbox.backends import DockerBackend, LocalBackend, auto_backend
    kind = cfg["sandbox"]["backend"]
    timeout = cfg["sandbox"]["timeout"]
    if kind == "docker":
        return DockerBackend(timeout=timeout)
    if kind == "local":
        return LocalBackend(timeout=timeout)
    return auto_backend(timeout=timeout)


# ------------------------------------------------------------------------- steps
def _resolve_data_path(rel: str) -> Path:
    """`data/nl2bash_vi` etc. live at the outer repo root per AGENT.md section 2; test
    fixtures (for the laptop smoke config) live inside the vishell project itself."""
    for base in (PROJECT_ROOT.parent, PROJECT_ROOT):
        p = base / rel
        if p.exists():
            return p
    return PROJECT_ROOT.parent / rel


def _translate_from_scratch(cfg: dict, paths: Paths) -> tuple[list[dict], dict]:
    """NL2Bash (GitHub) -> clean -> translate EN->VI through a vLLM server (started and
    stopped here if not already running). Resumable per chunk."""
    from .data import nl2bash as nb

    tc = cfg["data"]["translator"]
    nl_path, cm_path = nb.download_nl2bash(paths.data / "nl2bash_raw")
    pairs, clean_stats = nb.clean_and_dedupe(nb.load_raw_pairs(nl_path, cm_path))
    if cfg["data"]["max_rows"]:
        pairs = pairs[: cfg["data"]["max_rows"]]
    print(f"[data-nl2bash] {len(pairs)} pairs to translate (cleaning dropped {clean_stats})")

    with nb.translator_server(
        tc["server_url"], tc["api_key"], tc["model"], tc["autostart"],
        PROJECT_ROOT / "scripts" / "start_vllm.sh", tc["startup_timeout"], paths.logs / "vllm.log",
    ):
        chat = nb._openai_chat(tc["server_url"], tc["api_key"], tc["model"])
        translated = nb.translate_rows(pairs, chat, paths.data / "translate_chunks",
                                       tc["chunk_size"], tc["max_workers"])

    from collections import Counter
    reasons = Counter(r["reason"] for r in translated)
    rows = [{"id": r["id"], "nl_vi": r["nl_vi"], "bash": r["bash"]} for r in translated if r["nl_vi"]]
    return rows, {"translator_model": tc["model"], "clean_dropped": clean_stats,
                  "translation_reasons": dict(reasons), "n_translated_ok": len(rows)}


def step_data_nl2bash(cfg: dict, paths: Paths) -> None:
    from .data import nl2bash as nb

    vi_path = _resolve_data_path(cfg["data"]["nl2bash_dir"])
    has_data = vi_path.is_file() or (vi_path.is_dir() and any(vi_path.iterdir()))
    extra: dict = {}
    if has_data:
        rows = nb.load_existing_vi(vi_path)
        source = f"existing translated data at {vi_path}"
    else:
        sft_v1 = _resolve_data_path(cfg["models"]["sft_v1_dir"])
        if (sft_v1 / "adapter_config.json").exists():
            # spec 6.1: a ready sft_v1 means sft_nl2bash is skipped, so translating is wasted work
            print(f"[data-nl2bash] {sft_v1} exists and no translated data given: skipping translation")
            _write_json(paths.results / "data_stats.json", {"source": "skipped (sft_v1 already trained)"})
            return
        rows, extra = _translate_from_scratch(cfg, paths)
        source = f"NL2Bash (TellinaTool) translated with {cfg['data']['translator']['model']}"

    # split BY COMMAND so nothing in val/test leaks into train
    train, val, test = nb.split_by_command_group(rows, seed=cfg["seed"])
    noise, drop_r2 = cfg["data"]["noise_ratio"], cfg["data"]["drop_r2_in_sft_nl2bash"]
    to_pairs = lambda sft: [{"request_vi": r["nl_vi"], "target": r["target_json"]} for r in sft]
    sft_train = nb.build_sft_nl2bash_dataset(train, seed=cfg["seed"], noise_ratio=noise, drop_r2=drop_r2)
    sft_val = nb.build_sft_nl2bash_dataset(val, seed=cfg["seed"], noise_ratio=0.0, drop_r2=drop_r2)
    _write_jsonl(paths.data / "sft_nl2bash.jsonl", to_pairs(sft_train))
    _write_jsonl(paths.data / "sft_nl2bash_val.jsonl", to_pairs(sft_val))
    _write_jsonl(paths.data / "nl2bash_test.jsonl", test)  # for the NL2Bash parse/EM/utility eval (not wired yet)
    _write_json(paths.results / "data_stats.json", {
        "source": source, "n_raw": len(rows), "n_train_rows": len(train), "n_val_rows": len(val),
        "n_test_rows": len(test), "n_sft": len(sft_train), "n_sft_val": len(sft_val), **extra})


def step_verify(cfg: dict, paths: Paths) -> None:
    from .data.templates import load_templates
    from .verify import verify_all

    templates, load_errors = load_templates(PROJECT_ROOT / cfg["templates"]["dir"])
    if load_errors:
        print(f"[verify] {len(load_errors)} template file(s) failed to parse:")
        for path, err in load_errors:
            print(f"  {path}: {err}")

    backend = make_backend(cfg)
    report = verify_all(templates, backend, seed=cfg["seed"], n_instances=cfg["verify"]["n_instances"],
                        timeout=cfg["sandbox"]["timeout"])

    out_path = paths.results / "verify.json"
    history = json.loads(out_path.read_text(encoding="utf-8"))["history"] if out_path.exists() else []
    history.append(report)
    _write_json(out_path, {**report, "history": history})
    print(f"[verify] {report['n_passed']}/{report['n_templates']} passed "
          f"(disagreement rate {report['disagreement_rate']:.2%})")


def step_build_dataset(cfg: dict, paths: Paths) -> None:
    from .data.templates import build_dataset, load_templates

    templates, _ = load_templates(PROJECT_ROOT / cfg["templates"]["dir"])
    verify_path = paths.results / "verify.json"
    if verify_path.exists():
        passed_ids = set(json.loads(verify_path.read_text(encoding="utf-8")).get("passed_ids", []))
        templates = [t for t in templates if t.template_id in passed_ids] or templates

    train, test = build_dataset(
        templates, seed=cfg["seed"], n_train_per_template=cfg["templates"]["n_train_per_template"],
        n_test_per_template=cfg["templates"]["n_test_per_template"], noise_ratio=cfg["data"]["noise_ratio"],
        test_frac=cfg["templates"]["test_frac"],
    )
    _write_jsonl(paths.data / "instances_train.jsonl", [i.model_dump() for i in train])
    _write_jsonl(paths.data / "instances_test.jsonl", [i.model_dump() for i in test])

    scenarios_sft = [
        {"request_vi": i.request_vi,
         "target": _target_json(i)}
        for i in train
    ]
    _write_jsonl(paths.data / "sft_scenarios.jsonl", scenarios_sft)
    print(f"[build-dataset] {len(train)} train, {len(test)} test instances from {len(templates)} templates")


def _target_json(instance) -> str:
    from .schema import ModelOutput
    if instance.expected_action == "ask":
        return ModelOutput(action="ask", command="", question=instance.clarify_question_vi or "").model_dump_json()
    return ModelOutput(action=instance.expected_action, command=instance.reference_command, question="").model_dump_json()


def step_sft(stage: str, cfg: dict, paths: Paths, force: bool) -> None:
    from .prompts import SYSTEM_PROMPT
    from .train.sft import train_sft

    tc = cfg["train"]
    if stage == "nl2bash":
        sft_v1 = _resolve_data_path(cfg["models"]["sft_v1_dir"])
        if (sft_v1 / "adapter_config.json").exists() and not force:
            print(f"[sft-nl2bash] reusing existing adapter at {sft_v1}")
            return
        data_path = paths.data / "sft_nl2bash.jsonl"
        out_dir = paths.checkpoints / "sft_nl2bash"
        base_model, from_adapter = cfg["models"]["base_model"], None
    else:
        data_path = paths.data / "sft_scenarios.jsonl"
        out_dir = paths.checkpoints / "sft_scenarios"
        base_model = str(paths.models / "merged_nl2bash")
        from_adapter = None

    val_path = paths.data / f"sft_{stage}_val.jsonl"
    if not val_path.exists():
        _write_jsonl(val_path, [])

    result = train_sft(
        train_path=str(data_path), val_path=str(val_path), base_model=base_model, out_dir=str(out_dir),
        system_prompt=SYSTEM_PROMPT, lora_r=tc["lora_r"], lora_alpha=tc["lora_alpha"],
        lora_dropout=tc["lora_dropout"], epochs=tc["scenarios_epochs"] if stage == "scenarios" else tc["epochs"], lr=tc["lr"], max_len=tc["max_len"],
        per_device_bs=tc["per_device_bs"], grad_accum=tc["grad_accum"], max_steps=tc["max_steps"],
        save_steps=tc["save_steps"], eval_steps=tc["eval_steps"], seed=cfg["seed"], dtype=tc["dtype"],
        force=force, from_adapter=from_adapter,
    )
    print(f"[sft-{stage}] {result}")


def step_merge(stage: str, cfg: dict, paths: Paths, force: bool) -> None:
    from .train.merge import merge_adapter

    base_model = cfg["models"]["base_model"] if stage == "nl2bash" else str(paths.models / f"merged_{_prev_stage(stage)}")
    adapter_dir = str(paths.checkpoints / f"sft_{stage}" / "final") if stage != "grpo" else str(paths.checkpoints / "grpo" / "final")
    if stage == "nl2bash":
        # step_sft skips training when a pre-trained sft_v1 exists, so merge that adapter
        sft_v1 = _resolve_data_path(cfg["models"]["sft_v1_dir"])
        if (sft_v1 / "adapter_config.json").exists():
            adapter_dir = str(sft_v1)
    out_dir = paths.models / f"merged_{stage}"
    result = merge_adapter(base_model, adapter_dir, str(out_dir), dtype=cfg["train"]["dtype"], force=force)
    print(f"[merge-{stage}] {result}")


def _prev_stage(stage: str) -> str:
    return {"scenarios": "nl2bash", "grpo": "scenarios"}[stage]


def step_grpo(cfg: dict, paths: Paths, force: bool) -> None:
    from .train.grpo import train_grpo

    backend = make_backend(cfg)
    gc = cfg["grpo"]
    result = train_grpo(
        train_path=str(paths.data / "instances_train.jsonl"), base_model=str(paths.models / "merged_scenarios"),
        out_dir=str(paths.checkpoints / "grpo"), backend=backend, lora_r=cfg["train"]["lora_r"],
        lora_alpha=cfg["train"]["lora_alpha"], lora_dropout=cfg["train"]["lora_dropout"], lr=gc["lr"],
        max_steps=gc["max_steps"], save_steps=gc["save_steps"], num_generations=gc["num_generations"], temperature=gc["temperature"],
        max_prompt_length=gc["max_prompt_length"], max_completion_length=gc["max_completion_length"],
        decision_coef=cfg["reward"]["decision_coef"], seed=cfg["seed"], dtype=gc["dtype"],
        use_vllm=gc["use_vllm"], force=force,
    )
    print(f"[grpo] {result}")


def step_export(cfg: dict, paths: Paths, force: bool) -> None:
    from .train.export import export_gguf
    result = export_gguf(str(paths.models / "merged_grpo"), str(paths.models / "gguf"), force=force)
    print(f"[export] {result}")


def step_evaluate(cfg: dict, paths: Paths) -> None:
    from .evaluate import (
        evaluate_instance, evaluate_system_on_templates, mock_generate, oracle_generate,
        summarize_template_eval,
    )
    from .schema import Instance

    test_path = paths.data / "instances_test.jsonl"
    instances = [Instance.model_validate(r) for r in _read_jsonl(test_path)] if test_path.exists() else []
    if not instances:
        print("[evaluate] no test instances (run build-dataset first)")
        return

    backend = make_backend(cfg)
    timeout = cfg["sandbox"]["timeout"]
    all_rows = []
    for name in cfg["evaluate"]["systems"]:
        if name == "oracle":
            # oracle needs the matching instance's own expected output, not just its
            # request text, so it can't go through the text-only GenerateFn contract.
            for inst in instances:
                metrics = evaluate_instance(inst, oracle_generate(inst)(inst.request_vi), backend, timeout)
                all_rows.append({"system": name, "variant": "noisy" if inst.is_noisy else "clean",
                                  "instance_id": inst.instance_id, **metrics})
            continue
        generate_fn = _resolve_system(name, cfg, paths)
        if generate_fn is None:
            continue
        all_rows += evaluate_system_on_templates(name, generate_fn, instances, backend, timeout)
    _write_jsonl(paths.results / "predictions_templates.jsonl", all_rows)
    summary = summarize_template_eval(all_rows)
    _write_json(paths.results / "eval_templates_summary.json", summary)
    print(f"[evaluate] {len(summary)} (system, variant) rows written")


_SYSTEM_MODEL_DIR_CFG_KEY = {
    "sft_nl2bash": "merged_nl2bash", "sft_scenarios": "merged_scenarios", "grpo": "merged_grpo",
}


def _resolve_system(name: str, cfg: dict, paths: Paths):
    """mock: dependency-free stand-in. api_large: OpenAI-compatible endpoint (only if
    its env var is set). base/sft_*/grpo: real HF generation — needs [train] extras and,
    for the non-base systems, that stage's merged model to already exist on disk (GPU
    training steps, run on Colab/Kaggle per AGENT.md section 2)."""
    from .evaluate import api_generate_fn, hf_generate_fn, mock_generate
    from .prompts import SYSTEM_PROMPT

    if name == "mock":
        return mock_generate
    if name == "api_large":
        import os
        base_url = os.environ.get(cfg["evaluate"]["api_base_env"])
        if not base_url:
            print(f"[evaluate] api_large: {cfg['evaluate']['api_base_env']} not set, skipping")
            return None
        return api_generate_fn(base_url, os.environ.get("VISHELL_API_KEY", "EMPTY"), "default", SYSTEM_PROMPT)

    if name == "base":
        model_dir = cfg["models"]["base_model"]
    elif name in _SYSTEM_MODEL_DIR_CFG_KEY:
        model_dir = str(paths.models / _SYSTEM_MODEL_DIR_CFG_KEY[name])
        if not Path(model_dir).exists():
            print(f"[evaluate] {name}: {model_dir} not found yet (train+merge that stage first), skipping")
            return None
    else:
        print(f"[evaluate] unknown system {name!r}, skipping")
        return None

    try:
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError:
        print(f"[evaluate] system {name!r} needs the [train] extras (transformers/torch) — skipping here")
        return None
    tok = AutoTokenizer.from_pretrained(model_dir)
    model = AutoModelForCausalLM.from_pretrained(model_dir, device_map="auto")
    return hf_generate_fn(model, tok, SYSTEM_PROMPT, max_new_tokens=cfg["evaluate"]["max_new_tokens"])


def step_report(cfg: dict, paths: Paths) -> None:
    from .report import write_report
    out = write_report(paths, PROJECT_ROOT / "REPORT.md")
    print(f"[report] wrote {out}")


def step_demo(cfg: dict, paths: Paths) -> None:
    from .demo import build_app, mock_generate
    build_app(mock_generate, share=cfg["demo"]["share"])


# ------------------------------------------------------------------------- I/O helpers
def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ------------------------------------------------------------------------- dispatch
_STEP_FUNCS = {
    "data-nl2bash": lambda cfg, paths, force: step_data_nl2bash(cfg, paths),
    "verify": lambda cfg, paths, force: step_verify(cfg, paths),
    "build-dataset": lambda cfg, paths, force: step_build_dataset(cfg, paths),
    "sft-nl2bash": lambda cfg, paths, force: step_sft("nl2bash", cfg, paths, force),
    "merge-nl2bash": lambda cfg, paths, force: step_merge("nl2bash", cfg, paths, force),
    "sft-scenarios": lambda cfg, paths, force: step_sft("scenarios", cfg, paths, force),
    "merge-scenarios": lambda cfg, paths, force: step_merge("scenarios", cfg, paths, force),
    "grpo": lambda cfg, paths, force: step_grpo(cfg, paths, force),
    "merge-grpo": lambda cfg, paths, force: step_merge("grpo", cfg, paths, force),
    "export": lambda cfg, paths, force: step_export(cfg, paths, force),
    "evaluate": lambda cfg, paths, force: step_evaluate(cfg, paths),
    "report": lambda cfg, paths, force: step_report(cfg, paths),
    "demo": lambda cfg, paths, force: step_demo(cfg, paths),
}


def run_step(name: str, cfg: dict, paths: Paths, force: bool = False) -> None:
    # GPU steps skip themselves by checking their real outputs; a stale .done marker would
    # skip them even after those outputs were deleted.
    if not force and name not in GPU_STEPS and paths.is_done(name):
        print(f"[{name}] already done, skipping (--force to rerun)")
        return
    _STEP_FUNCS[name](cfg, paths, force)
    paths.mark_done(name)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m vishell")
    parser.add_argument("step", choices=STEPS + ["pipeline"])
    parser.add_argument("--config", default=None)
    parser.add_argument("--set", action="append", default=[], dest="overrides")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(args.config, args.overrides)
    paths = make_paths(cfg)

    if args.step == "pipeline":
        for name in STEPS:
            if name == "demo":
                continue  # demo is interactive, not part of an unattended pipeline run
            if name in GPU_STEPS and not cfg["train"]["enabled"]:
                print(f"[{name}] train.enabled=false, skipping")
                continue
            run_step(name, cfg, paths, args.force)
    else:
        run_step(args.step, cfg, paths, args.force)


if __name__ == "__main__":
    main(sys.argv[1:])
