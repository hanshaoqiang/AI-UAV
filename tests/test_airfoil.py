"""M03: UIUC 翼型库测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.airfoil.uiuc_database import (AirfoilDatabase, _parse_selig,
                                       _split_surfaces)


@pytest.fixture(scope="module")
def db(uiuc_dir):
    return AirfoilDatabase(uiuc_dir)


def test_index_built(db):
    assert len(db._index) > 1000  # 库应有 1600+ 翼型


def test_exists_and_get(db):
    assert db.exists("clarky")
    p = db.get_file("CLARKY")   # 大小写不敏感
    assert p.exists() and p.suffix == ".dat"


def test_get_missing_raises(db):
    with pytest.raises(KeyError):
        db.get_file("this_airfoil_does_not_exist_999")


def test_find_or_default(db):
    assert db.find_or_default("naca0008").exists()
    assert db.find_or_default("missing_xx", default="clarky").name == "clarky.dat"


def test_list_names_pattern(db):
    names = db.list_names("naca")
    assert len(names) > 10
    assert all("naca" in n for n in names)


def test_load_clarky(db):
    af = db.load("clarky")
    assert af.name == "clarky"
    # Clark Y: ~11.7% 厚, ~3.5% 弯度
    assert 0.10 < af.max_thickness < 0.13
    assert 0.02 < abs(af.max_camber) < 0.05
    # 归一化: 弦长 0..1
    xs = [x for x, _ in af.coords]
    assert min(xs) == pytest.approx(0.0, abs=1e-6)
    assert max(xs) == pytest.approx(1.0, abs=1e-6)
    # 上下表面符号正确
    assert all(y >= -1e-9 for _, y in af.upper)
    assert all(y <= 1e-9 for _, y in af.lower)


def test_split_surfaces_te_start():
    # TE 起始的环绕序(上表面 TE→LE, 下表面 LE→TE)
    pts = [(1.0, 0.01), (0.5, 0.05), (0.0, 0.0), (0.5, -0.05), (1.0, -0.01)]
    upper, lower = _split_surfaces(pts)
    assert upper[0][0] == pytest.approx(0.0)
    assert upper[-1][0] == pytest.approx(1.0)
    assert all(y >= -1e-12 for _, y in upper)
    assert all(y <= 1e-12 for _, y in lower)


def test_split_surfaces_le_start():
    # LE 起始(Selig 常规): 上 LE→TE, 下 TE→LE
    pts = [(0.0, 0.0), (0.5, 0.05), (1.0, 0.01), (0.5, -0.05), (0.0, 0.0)]
    upper, lower = _split_surfaces(pts)
    assert all(y >= 0 for _, y in upper)
    assert all(y <= 0 for _, y in lower)
    assert upper[0][0] == pytest.approx(0.0)
    assert upper[-1][0] == pytest.approx(1.0)


def test_parse_bad_file_raises(tmp_path):
    f = tmp_path / "bad.dat"
    f.write_text("name\n1.0 0.0\n2.0 0.0\n", encoding="utf-8")
    with pytest.raises(ValueError):
        _parse_selig(f)
