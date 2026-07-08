"""Configuration file: path resolution, defaults, env overrides, and creation
of the commented default file. Resolution is pure (a parsed mapping plus an
environ dict in, resolved values out), so it tests without touching real files
except the explicit ensure_config_file filesystem tests.
"""

import os

import yaml

from skreenshot import config


# -- config_path -----------------------------------------------------------


def test_config_path_respects_xdg_config_home():
    p = config.config_path({"XDG_CONFIG_HOME": "/x/cfg"})
    assert p == "/x/cfg/skreenshot/config.yaml"


def test_config_path_defaults_to_dot_config():
    p = config.config_path({"HOME": "/home/u"})
    assert p == "/home/u/.config/skreenshot/config.yaml"


# -- dim -------------------------------------------------------------------


def test_dim_defaults_to_140_when_absent():
    assert config.resolve({}, {}).dim == 140


def test_dim_read_from_config():
    assert config.resolve({"dim": 90}, {}).dim == 90


def test_dim_env_overrides_config():
    assert config.resolve({"dim": 90}, {"SKREENSHOT_DIM": "200"}).dim == 200


def test_dim_is_clamped_to_255():
    assert config.resolve({"dim": 999}, {}).dim == 255


def test_dim_invalid_falls_back_to_default():
    assert config.resolve({"dim": "dark"}, {}).dim == 140


# -- log_file --------------------------------------------------------------


def test_log_file_none_by_default():
    assert config.resolve({}, {}).log_file is None


def test_log_file_from_config_expands_tilde():
    cfg = config.resolve({"log_file": "~/x.log"}, {"HOME": "/home/u"})
    assert cfg.log_file == "/home/u/x.log"


def test_log_file_env_overrides_config():
    cfg = config.resolve(
        {"log_file": "~/x.log"},
        {"HOME": "/home/u", "SKREENSHOT_LOG": "/tmp/y.log"},
    )
    assert cfg.log_file == "/tmp/y.log"


# -- save_dir --------------------------------------------------------------


def test_save_dir_defaults_to_pictures():
    assert config.resolve({}, {"HOME": "/home/u"}).save_dir == "/home/u/Pictures"


def test_save_dir_from_config_expands_tilde():
    cfg = config.resolve({"save_dir": "~/shots"}, {"HOME": "/home/u"})
    assert cfg.save_dir == "/home/u/shots"


# -- robustness ------------------------------------------------------------


def test_empty_mapping_uses_all_defaults():
    cfg = config.resolve({}, {"HOME": "/home/u"})
    assert cfg.dim == 140
    assert cfg.log_file is None
    assert cfg.save_dir == "/home/u/Pictures"


def test_dim_infinity_falls_back_to_default():
    # YAML `.inf` / `-.inf` parse to float('inf'); int(inf) raises OverflowError.
    assert config.resolve({"dim": float("inf")}, {}).dim == 140
    assert config.resolve({"dim": float("-inf")}, {}).dim == 140


def test_resolve_tolerates_non_dict_data():
    # A YAML file that is a bare scalar / list / string must not crash resolve.
    for junk in (42, None, [1, 2], "hello"):
        cfg = config.resolve(junk, {"HOME": "/home/u"})
        assert cfg == config.Config(
            save_dir="/home/u/Pictures", dim=140, log_file=None
        )


def test_config_path_ignores_relative_xdg_config_home():
    p = config.config_path({"XDG_CONFIG_HOME": "relative/cfg", "HOME": "/home/u"})
    assert p == "/home/u/.config/skreenshot/config.yaml"


def test_load_with_infinity_dim_does_not_crash(tmp_path):
    cfg_dir = tmp_path / "skreenshot"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("dim: .inf\n")
    cfg = config.load({"XDG_CONFIG_HOME": str(tmp_path), "HOME": str(tmp_path)})
    assert cfg.dim == 140


def test_default_config_text_is_valid_yaml_with_expected_defaults():
    data = yaml.safe_load(config.DEFAULT_CONFIG_TEXT)
    assert data["save_dir"] == "~/Pictures"
    assert data["dim"] == 140
    assert data["log_file"] is None
    # And it round-trips through resolve to the built-in defaults.
    cfg = config.resolve(data, {"HOME": "/home/u"})
    assert cfg == config.Config(save_dir="/home/u/Pictures", dim=140, log_file=None)


# -- ensure_config_file (real filesystem, tmp) -----------------------------


def test_ensure_creates_commented_default(tmp_path):
    p = str(tmp_path / "skreenshot" / "config.yaml")
    config.ensure_config_file(p)
    assert os.path.exists(p)
    text = open(p).read()
    assert text.lstrip().startswith("#")  # has explanatory comments
    for key in ("save_dir", "dim", "log_file"):
        assert key in text


def test_ensure_does_not_overwrite_existing(tmp_path):
    p = str(tmp_path / "config.yaml")
    with open(p, "w") as fh:
        fh.write("save_dir: /custom")
    config.ensure_config_file(p)
    assert open(p).read() == "save_dir: /custom"
