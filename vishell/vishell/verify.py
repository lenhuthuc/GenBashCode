"""Certify templates against the sandbox: the checks in AGENT.md section 5 (a)-(f),
plus the static/dynamic reversibility agreement and R1 restore check described in
section 1's "Cách đo". Pure w.r.t. I/O — cli.py owns reading templates/writing
results/verify.json; this module just runs checks and returns structured results.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .classify import classify
from .data.templates import gen_instances
from .schema import Instance, Template

_NOOP_PAYLOAD = {"command": "true", "check_type": "stdout_contains", "check_expected": ""}


@dataclass
class TemplateVerifyResult:
    template_id: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    static_level: Optional[str] = None
    dynamic_level: Optional[str] = None
    disagreement: Optional[bool] = None


def _run_setup_only(backend, inst: Instance, timeout: float) -> dict:
    return backend.run({"setup": inst.setup, "timeout": timeout, **_NOOP_PAYLOAD})


def _verify_ask(template: Template, instances: list[Instance], backend, timeout: float) -> TemplateVerifyResult:
    reasons = []
    for inst in instances:
        if inst.setup.strip():
            res = _run_setup_only(backend, inst, timeout)
            if res["setup_rc"] != 0:
                reasons.append(f"{inst.instance_id}: setup failed rc={res['setup_rc']}")

    static_level = None
    if template.ask_reason == "irreversible" and template.reference_command.strip():
        cls = classify(template.reference_command)
        static_level = cls.level
        if cls.level != "R2":
            reasons.append(f"ask/irreversible but classify measures {cls.level}: {cls.reasons}")

    return TemplateVerifyResult(template.template_id, not reasons, reasons, static_level=static_level)


_LEVEL_ORDER = {"R0": 0, "R1": 1, "R2": 2}


def _verify_execute_or_probe(
    template: Template, instances: list[Instance], backend, timeout: float
) -> TemplateVerifyResult:
    reasons: list[str] = []

    expected_reversible = template.expected_action in ("execute", "probe")
    if template.reversible != expected_reversible:
        reasons.append(f"reversible={template.reversible} inconsistent with expected_action={template.expected_action}")

    fs_changed_flags: list[bool] = []
    boundary_hits: list[bool] = []
    static_levels: list[str] = []

    for inst in instances:
        # classify the FILLED command, not the raw `{param}` template text -- bashlex
        # can choke on literal placeholder braces even when every real instance is fine.
        static = classify(inst.reference_command)
        static_levels.append(static.level)
        if static.level == "R2":
            reasons.append(f"{inst.instance_id}: execute/probe instance classifies as R2: {static.reasons}")

        setup_res = _run_setup_only(backend, inst, timeout)
        if setup_res["setup_rc"] != 0:
            reasons.append(f"{inst.instance_id}: setup failed rc={setup_res['setup_rc']}")
            continue

        payload = dict(setup=inst.setup, command=inst.reference_command,
                        check_type=inst.check_type, check_expected=inst.check_expected, timeout=timeout)

        res_b = backend.run(payload)  # (b)
        if not res_b["check_passed"]:
            reasons.append(f"{inst.instance_id}: (b) reference_command fails check")
        fs_changed_flags.append(res_b["fs_changed"])
        boundary_hits.append(bool(res_b.get("boundary_violation")))

        res_c = backend.run({**payload, "command": "true"})  # (c)
        if res_c["check_passed"]:
            reasons.append(f"{inst.instance_id}: (c) no-op 'true' satisfies check — check_expected too lax")

        for w in inst.wrong_commands:  # (d)
            res_w = backend.run({**payload, "command": w})
            if res_w["check_passed"]:
                reasons.append(f"{inst.instance_id}: (d) wrong_command {w!r} satisfies check")

        res_e = backend.run(payload)  # (e) determinism
        if res_e["check_passed"] != res_b["check_passed"] or res_e["stdout"] != res_b["stdout"]:
            reasons.append(f"{inst.instance_id}: (e) reference_command not deterministic across runs")

        if template.expected_action == "probe" and res_b["fs_changed"]:  # (f)
            reasons.append(f"{inst.instance_id}: (f) probe reference_command changed fs")

        if inst.undo_command.strip():  # (g) the SFT undo target must really restore the workspace
            res_g = backend.run({**payload, "undo": inst.undo_command})
            if res_g.get("undo_score") != 1.0:
                reasons.append(f"{inst.instance_id}: (g) undo_command leaves workspace different "
                               f"(score={res_g.get('undo_score')}, lost={res_g.get('undo_lost')})")

        if static.level == "R1":
            rc_res = backend.run({"_mode": "restore_check", "setup": inst.setup,
                                   "command": inst.reference_command, "timeout": timeout})
            if not rc_res.get("restored_ok", False):
                reasons.append(f"{inst.instance_id}: R1 restore check failed (hash mismatch after restore)")

    static_level = max(static_levels, key=lambda l: _LEVEL_ORDER[l]) if static_levels else None

    dynamic_level = None
    if fs_changed_flags:
        if any(boundary_hits):
            dynamic_level = "R2"
        elif any(fs_changed_flags):
            dynamic_level = "R1"
        else:
            dynamic_level = "R0"
        if static_level != dynamic_level:
            reasons.append(f"static/dynamic disagreement: static={static_level} dynamic={dynamic_level}")

    disagreement = dynamic_level is not None and static_level != dynamic_level
    return TemplateVerifyResult(template.template_id, not reasons, reasons,
                                 static_level=static_level, dynamic_level=dynamic_level, disagreement=disagreement)


def verify_template(
    template: Template, backend, seed: int = 0, n_instances: int = 5, timeout: float = 10
) -> TemplateVerifyResult:
    instances = gen_instances(template, n=n_instances, seed=seed, noise_ratio=0.0, split="train")
    if template.expected_action == "ask":
        return _verify_ask(template, instances, backend, timeout)
    return _verify_execute_or_probe(template, instances, backend, timeout)


def verify_all(
    templates: list[Template], backend, seed: int = 0, n_instances: int = 5, timeout: float = 10
) -> dict:
    results = [verify_template(t, backend, seed, n_instances, timeout) for t in templates]
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]
    with_disagreement = [r for r in results if r.disagreement]
    comparable = [r for r in results if r.disagreement is not None]
    return {
        "n_templates": len(templates),
        "n_passed": len(passed),
        "n_failed": len(failed),
        "pass_rate": len(passed) / len(templates) if templates else 0.0,
        "disagreement_rate": len(with_disagreement) / len(comparable) if comparable else 0.0,
        "passed_ids": [r.template_id for r in passed],
        "failed": [{"template_id": r.template_id, "reasons": r.reasons} for r in failed],
    }
