"""共享 fixture: 项目根路径与工具可用性检查。"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VSPSCRIPT = Path("C:/OpenVSP-3.55.0-win64/vspscript.exe")
XFOIL = Path("C:/Xfoil/xfoil.exe")
UIUC_DIR = ROOT / "uiuc" / "coord_seligFmt"

TOOLS_AVAILABLE = VSPSCRIPT.exists() and XFOIL.exists() and UIUC_DIR.exists()

requires_tools = pytest.mark.skipif(
    not TOOLS_AVAILABLE, reason="本机未安装 OpenVSP/XFOIL 或翼型库")


@pytest.fixture(scope="session")
def uiuc_dir() -> Path:
    assert UIUC_DIR.exists(), f"UIUC 翼型库不存在: {UIUC_DIR}"
    return UIUC_DIR


@pytest.fixture(scope="session")
def demo_config() -> dict:
    from src.core.config import load_config
    return load_config(ROOT / "config" / "mission.yaml")
