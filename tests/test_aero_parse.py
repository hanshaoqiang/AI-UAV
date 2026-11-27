"""M05: 极线解析测试(纯解析逻辑, 不依赖外部工具)。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.contracts import AeroResult, AirfoilPolar
from src.vspaero.adapter import _fit, parse_polar
from src.xfoil.adapter import parse_polar as parse_xfoil_polar

# VSPAero .polar 样例(列结构与真实输出一致)
VSP_POLAR_SAMPLE = """  Title
  Beta Mach AoA Re/1e6 CLo CLi CLtot CDo CDi CDtot CSo CSi CStot L/D E CMox CMoy CMoz CMix CMiy CMiz CMxtot CMytot CMztot
  0.0 0.10 -2.0 10.0 0.0 -0.25 -0.25 0.012 0.003 0.015 0.0 0.0 0.0 -16.7 0.9 0.0 0.0 0.0 0.0 0.0 0.0 0.0 -0.20 0.0
  0.0 0.10 2.0 10.0 0.0 0.25 0.25 0.012 0.003 0.015 0.0 0.0 0.0 16.7 0.9 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.10 0.0
  0.0 0.10 6.0 10.0 0.0 0.75 0.75 0.012 0.027 0.039 0.0 0.0 0.0 19.2 0.9 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.40 0.0
  0.0 0.10 10.0 10.0 0.0 1.25 1.25 0.012 0.075 0.087 0.0 0.0 0.0 14.4 0.9 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.70 0.0
"""


def test_vspaero_parse(tmp_path):
    f = tmp_path / "aero.polar"
    f.write_text(VSP_POLAR_SAMPLE, encoding="utf-8")
    res = parse_polar(f, cg_x_m=0.5, ar=8.0)
    assert len(res.alpha_deg) == 4
    assert res.CL == pytest.approx([-0.25, 0.25, 0.75, 1.25])
    assert res.CD == pytest.approx([0.015, 0.015, 0.039, 0.087])
    # CMytot 列(俯仰力矩)
    assert res.Cm == pytest.approx([-0.20, 0.10, 0.40, 0.70])
    # 抛物线拟合: CD = 0.012 + 0.048·CL²
    assert res.CD0 == pytest.approx(0.012, abs=2e-3)
    assert res.k_induced == pytest.approx(0.048, abs=2e-3)
    # CLα = 0.125/deg → 7.16/rad (样例斜率)
    assert res.CL_alpha_per_rad == pytest.approx(0.125 * 180 / 3.14159, rel=1e-2)
    # Cmα: 0.15/deg(线性) → 符号用于 SM
    assert res.Cm_alpha_per_rad > 0


# XFOIL 极线样例
XFOIL_POLAR_SAMPLE = """
       XFOIL         Version 6.99

 Calculated polar for: CLARK Y

 1 1 Reynolds number fixed          Mach number fixed

 Mach =   0.000     Re =     0.500 e 6     Ncrit =   9.000

   alpha    CL        CD       CDp       CM     Top_Xtr  Bot_Xtr
  ------ -------- --------- --------- -------- -------- --------
  -2.000   0.1775   0.00887   0.00238  -0.0851   0.7875   0.1347
   0.000   0.3760   0.00652   0.00219  -0.0794   0.7115   0.8794
   4.000   0.8316   0.00848   0.00299  -0.0809   0.4690   1.0000
   8.000   1.2096   0.01293   0.00672  -0.0695   0.2509   1.0000
"""


def test_xfoil_parse(tmp_path):
    f = tmp_path / "xfoil_polar.dat"
    f.write_text(XFOIL_POLAR_SAMPLE, encoding="utf-8")
    p = parse_xfoil_polar(f, "clarky", 5e5)
    assert len(p.CL) == 4
    assert p.CL == pytest.approx([0.1775, 0.3760, 0.8316, 1.2096])
    assert p.cl_max == pytest.approx(1.2096)
    assert p.alpha_stall_deg == pytest.approx(8.0)
    assert p.converged


def test_vspaero_fit_ldmax():
    import numpy as np
    alpha = np.array([-2.0, 0.0, 2.0, 4.0, 6.0, 8.0])
    cl = 0.1 * (alpha + 2.0)
    cd = 0.02 + 0.05 * cl ** 2
    ld = np.where(cd > 0, cl / cd, 0)
    res = AeroResult()
    _fit(res, alpha, cl, cd, ld, np.zeros_like(alpha), ar=8.0)
    assert res.CL_alpha_per_rad == pytest.approx(0.1 * 180 / np.pi, rel=1e-3)
    assert res.CD0 == pytest.approx(0.02, abs=1e-6)
    assert res.k_induced == pytest.approx(0.05, abs=1e-6)
    # 最大 L/D 在 CL* = sqrt(CD0/k) = 0.632, CD* = 0.02+0.05·0.4 = 0.04
    assert 3.9 <= res.alpha_LDmax_deg <= 6.1
    assert res.LD_max == pytest.approx(0.632 / 0.04, rel=1e-3)
