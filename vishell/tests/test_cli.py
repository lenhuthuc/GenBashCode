from vishell.cli import _deep_update, _set_path, load_config


def test_deep_update_merges_nested_dicts():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    _deep_update(base, {"a": {"y": 20}, "c": 4})
    assert base == {"a": {"x": 1, "y": 20}, "b": 3, "c": 4}


def test_set_path_creates_nested_keys_and_parses_types():
    cfg = {}
    _set_path(cfg, "train.lr", "0.001")
    _set_path(cfg, "train.enabled", "false")
    _set_path(cfg, "run_name", "myrun")
    assert cfg == {"train": {"lr": 0.001, "enabled": False}, "run_name": "myrun"}


def test_load_config_default_has_expected_shape():
    cfg = load_config(None, [])
    assert cfg["run_name"] == "default"
    assert "sandbox" in cfg and "templates" in cfg and "reward" in cfg


def test_load_config_applies_smoke_overrides():
    from vishell.paths import PROJECT_ROOT
    cfg = load_config(str(PROJECT_ROOT / "configs" / "smoke.yaml"), [])
    assert cfg["run_name"] == "smoke"
    assert cfg["templates"]["n_train_per_template"] == 4
    assert cfg["train"]["enabled"] is False
    # untouched default.yaml keys must survive the merge
    assert cfg["sandbox"]["timeout"] == 10


def test_load_config_set_override_wins_over_file():
    from vishell.paths import PROJECT_ROOT
    cfg = load_config(str(PROJECT_ROOT / "configs" / "smoke.yaml"), ["templates.n_train_per_template=99"])
    assert cfg["templates"]["n_train_per_template"] == 99
