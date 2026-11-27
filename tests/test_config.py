"""M01: 配置加载与合并测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import DEFAULTS, _deep_merge, load_config
from src.core.contracts import MissionSpec


def test_deep_merge_nested():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    override = {"a": {"y": 9, "z": 4}}
    out = _deep_merge(base, override)
    assert out == {"a": {"x": 1, "y": 9, "z": 4}, "b": 3}
    # 原字典不被修改
    assert base["a"]["y"] == 2


def test_load_config_merge(tmp_path):
    user = tmp_path / "m.yaml"
    user.write_text(
        "mission:\n  payload_kg: 7.5\n  range_km: 300\n", encoding="utf-8")
    cfg = load_config(user)
    # 用户覆盖
    assert cfg["mission"]["payload_kg"] == 7.5
    assert cfg["mission"]["range_km"] == 300
    # 默认值保留
    assert cfg["mission"]["cruise_speed_ms"] == DEFAULTS["mission"]["cruise_speed_ms"]
    assert "analysis" in cfg and "tools" in cfg


def test_load_config_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("Z:/nonexistent/config.yaml")


def test_mission_spec_roundtrip():
    m = MissionSpec(range_km=150, payload_kg=3.0)
    d = m.to_dict()
    m2 = MissionSpec.from_dict(d)
    assert m2.range_km == 150
    assert m2.payload_kg == 3.0


def test_mission_spec_from_dict_ignores_unknown():
    m = MissionSpec.from_dict({"range_km": 10, "bogus_key": 1})
    assert m.range_km == 10
