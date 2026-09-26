import json

import pytest

from vishell.data.templates import (
    build_dataset, fill_params, gen_instances, load_templates, split_templates,
)
from vishell.schema import Template


def make_template(**overrides):
    base = dict(
        template_id="tpl-1", source="original",
        params={"n": {"int": [1, 3]}, "name": {"choice": ["a.txt", "b.txt"]}},
        setup="touch {name}", requests_vi=["đếm dòng của {name}"],
        expected_action="probe", reversible=True,
        reference_command="wc -l {name}", wrong_commands=["rm {name}"],
        check_type="stdout_contains", check_expected="0",
    )
    base.update(overrides)
    return Template(**base)


def test_fill_params_only_declared_names():
    assert fill_params("wc -l {name}", {"name": "a.txt"}) == "wc -l a.txt"


def test_fill_params_does_not_touch_dollar_brace():
    # ${name} must survive untouched even though "name" is a declared param
    out = fill_params("echo ${name} and {name}", {"name": "x"})
    assert out == "echo ${name} and x"


def test_fill_params_does_not_touch_brace_expansion():
    out = fill_params("echo {a,b}", {"a": "1"})  # "a,b" as a whole is not a declared name
    assert out == "echo {a,b}"


def test_gen_instances_fills_and_seeds_deterministically():
    t = make_template()
    a = gen_instances(t, n=5, seed=42, split="train")
    b = gen_instances(t, n=5, seed=42, split="train")
    assert [i.model_dump() for i in a] == [i.model_dump() for i in b]
    assert all("{" not in i.reference_command for i in a)


def test_gen_instances_fills_params_into_request_vi_too():
    # request_vi is what the model actually trains/evals on -- it must never leak
    # raw {param} placeholders (a real bug: this used to pick the phrasing unfilled).
    t = make_template()
    instances = gen_instances(t, n=5, seed=1, split="train")
    for inst in instances:
        assert "{" not in inst.request_vi, inst.request_vi
    assert any(v in i.request_vi for i in instances for v in ("a.txt", "b.txt"))


def test_gen_instances_test_split_always_has_noisy_pair():
    t = make_template()
    instances = gen_instances(t, n=3, seed=1, split="test")
    clean = [i for i in instances if not i.is_noisy]
    noisy = [i for i in instances if i.is_noisy]
    assert len(clean) == len(noisy) == 3


def test_split_templates_no_leakage():
    templates = [make_template(template_id=f"tpl-{i}") for i in range(200)]
    train, test = split_templates(templates, test_frac=0.15)
    train_ids = {t.template_id for t in train}
    test_ids = {t.template_id for t in test}
    assert not (train_ids & test_ids)
    assert train_ids | test_ids == {t.template_id for t in templates}
    assert 0.05 < len(test) / len(templates) < 0.25


def test_split_templates_deterministic():
    templates = [make_template(template_id=f"tpl-{i}") for i in range(50)]
    a = {t.template_id for t in split_templates(templates)[1]}
    b = {t.template_id for t in split_templates(templates)[1]}
    assert a == b


def test_build_dataset_end_to_end():
    templates = [make_template(template_id=f"tpl-{i}") for i in range(20)]
    train, test = build_dataset(templates, seed=0, n_train_per_template=2, n_test_per_template=1)
    assert train and test
    train_tpls = {i.template_id for i in train}
    test_tpls = {i.template_id for i in test}
    assert not (train_tpls & test_tpls)


def test_load_templates_reports_bad_files(tmp_path):
    good = make_template()
    (tmp_path / "good.json").write_text(good.model_dump_json(), encoding="utf-8")
    (tmp_path / "bad.json").write_text("{not valid json", encoding="utf-8")
    templates, errors = load_templates(tmp_path)
    assert len(templates) == 1
    assert len(errors) == 1
    assert "bad.json" in errors[0][0]
