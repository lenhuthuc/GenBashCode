"""Load templates/*.json, fill in parameters, generate instances, and split by
template_id so no template's instances leak across train/test. See AGENT.md section 5.
"""
from __future__ import annotations

import hashlib
import random
import re
from pathlib import Path

from ..classify import classify
from ..noise import noisy_for, selected_for_noise
from ..schema import ChoiceParam, Instance, IntParam, Template


def load_templates(directory: str | Path) -> tuple[list[Template], list[tuple[str, str]]]:
    """Returns (templates, errors); errors are (path, message) for files that fail to
    parse, so a batch-authoring loop can report and fix them instead of crashing."""
    templates, errors = [], []
    for p in sorted(Path(directory).glob("*.json")):
        try:
            templates.append(Template.model_validate_json(p.read_text(encoding="utf-8")))
        except Exception as e:
            errors.append((str(p), str(e)))
    return templates, errors


def sample_params(template: Template, rng: random.Random) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, spec in template.params.items():
        if isinstance(spec, IntParam):
            lo, hi = spec.int
            out[name] = str(rng.randint(lo, hi))
        elif isinstance(spec, ChoiceParam):
            out[name] = rng.choice(spec.choice)
        else:
            raise TypeError(f"unknown param spec for {name!r}: {spec!r}")
    return out


def fill_params(text: str, params: dict[str, str]) -> str:
    """Substitute only declared `{name}` placeholders — never `${x}` (shell var) or
    `{a,b}` (brace expansion), since those aren't in `params` and the lookbehind keeps
    us off `${name}` even when a param happens to share its name with a shell var."""
    if not params or not text:
        return text
    alt = "|".join(re.escape(k) for k in sorted(params, key=len, reverse=True))
    pattern = re.compile(r"(?<!\$)\{(" + alt + r")\}")
    return pattern.sub(lambda m: params[m.group(1)], text)


def _measure_level(template: Template, reference_command: str) -> str:
    if reference_command.strip():
        return classify(reference_command).level
    return "R2" if template.ask_reason == "irreversible" else "R1"


def gen_instances(
    template: Template, n: int, seed: int = 0, noise_ratio: float = 0.3, split: str = "train"
) -> list[Instance]:
    """n clean instances; each also gets a noisy variant when split == 'test' (test
    keeps clean+noisy pairs) or, in train, with probability noise_ratio (~30% of train)."""
    out: list[Instance] = []
    for i in range(n):
        rng = random.Random(f"{seed}:{template.template_id}:{i}")
        params = sample_params(template, rng)
        request = fill_params(rng.choice(template.requests_vi), params)
        iid = f"{template.template_id}-{i:04d}"

        setup = fill_params(template.setup, params)
        ref = fill_params(template.reference_command, params)
        undo = fill_params(template.undo_command, params)
        wrong = [fill_params(w, params) for w in template.wrong_commands]
        check_expected = fill_params(template.check_expected, params)
        level = _measure_level(template, ref)

        common = dict(
            template_id=template.template_id, setup=setup,
            expected_action=template.expected_action, reversible=template.reversible,
            reversibility_level=level, ask_reason=template.ask_reason,
            clarify_question_vi=template.clarify_question_vi, reference_command=ref, undo_command=undo,
            wrong_commands=wrong, check_type=template.check_type,
            check_expected=check_expected, split=split,
        )
        out.append(Instance(instance_id=iid, request_vi=request, is_noisy=False, noise_ops=[], **common))

        if split == "test" or selected_for_noise(iid, noise_ratio, seed):
            noisy_text, ops = noisy_for(iid, request, ref, seed)
            out.append(Instance(instance_id=iid + "-n", request_vi=noisy_text, is_noisy=True, noise_ops=ops, **common))
    return out


def split_templates(
    templates: list[Template], test_frac: float = 0.15, salt: str = ""
) -> tuple[list[Template], list[Template]]:
    """Deterministic split by sha1(salt + template_id) so it's stable across runs without
    storing any state, and so no template ever appears on both sides. A different salt
    gives a split independent of the train/test one (used for the SFT/GRPO split)."""
    train, test = [], []
    threshold = int(test_frac * 100)
    for t in templates:
        h = int(hashlib.sha1((salt + t.template_id).encode()).hexdigest(), 16)
        (test if (h % 100) < threshold else train).append(t)
    return train, test


def build_dataset(
    templates: list[Template], seed: int = 0, n_train_per_template: int = 20,
    n_test_per_template: int = 5, noise_ratio: float = 0.3, test_frac: float = 0.15,
) -> tuple[list[Instance], list[Instance]]:
    train_templates, test_templates = split_templates(templates, test_frac)
    train_instances = [
        inst for t in train_templates
        for inst in gen_instances(t, n_train_per_template, seed, noise_ratio, "train")
    ]
    test_instances = [
        inst for t in test_templates
        for inst in gen_instances(t, n_test_per_template, seed, noise_ratio, "test")
    ]
    return train_instances, test_instances
