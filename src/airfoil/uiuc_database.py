"""M03: UIUC 翼型数据库 — 读取 / 查询 / 管理。

Selig 格式 .dat 文件；OpenVSP 与 XFOIL 均可直接消费原始文件，
本模块负责：名称索引、坐标解析（规范化/面积计算）、存在性检查。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("uavdp.airfoil")


@dataclass
class AirfoilData:
    """归一化翼型数据（弦长=1）。"""
    name: str
    file_path: Path
    coords: list  # [(x, y)] 环绕序，起点任意
    upper: list   # [(x, y)] LE→TE
    lower: list   # [(x, y)] LE→TE

    @property
    def max_thickness(self) -> float:
        n = max(len(self.upper), len(self.lower))
        t = 0.0
        for i in range(n):
            yu = self.upper[min(i, len(self.upper) - 1)][1]
            yl = self.lower[min(i, len(self.lower) - 1)][1]
            t = max(t, yu - yl)
        return t

    @property
    def max_camber(self) -> float:
        n = max(len(self.upper), len(self.lower))
        cambers = []
        for i in range(n):
            yu = self.upper[min(i, len(self.upper) - 1)][1]
            yl = self.lower[min(i, len(self.lower) - 1)][1]
            cambers.append((yu + yl) / 2)
        return max(cambers, key=abs) if cambers else 0.0


class AirfoilDatabase:
    """UIUC 翼型库。索引 .dat 文件名（不含扩展名）。"""

    def __init__(self, db_dir: str | Path):
        self.db_dir = Path(db_dir)
        if not self.db_dir.exists():
            raise FileNotFoundError(f"UIUC 翼型库目录不存在: {self.db_dir}")
        self._index: dict[str, Path] = {}
        self._build_index()

    def _build_index(self) -> None:
        for p in sorted(self.db_dir.glob("*.dat")):
            key = p.stem.lower().strip()
            if key:
                self._index[key] = p
        logger.info("UIUC 翼型库索引完成: %d 个翼型 (%s)", len(self._index), self.db_dir)

    # ---------------- 查询 ----------------
    def list_names(self, pattern: str = "") -> list[str]:
        names = sorted(self._index.keys())
        if pattern:
            names = [n for n in names if pattern.lower() in n]
        return names

    def exists(self, name: str) -> bool:
        return name.lower().strip() in self._index

    def get_file(self, name: str) -> Path:
        key = name.lower().strip()
        if key not in self._index:
            raise KeyError(
                f"翼型 '{name}' 不在库中（共 {len(self._index)} 个）。"
                f"可用 list_names() 查询。"
            )
        return self._index[key]

    def find_or_default(self, name: str, default: str = "clarky") -> Path:
        """尾翼对称翼型等若缺失则回退默认翼型。"""
        try:
            return self.get_file(name)
        except KeyError:
            logger.warning("翼型 %s 不存在，回退 %s", name, default)
            return self.get_file(default)

    # ---------------- 读取 ----------------
    def load(self, name: str) -> AirfoilData:
        path = self.get_file(name)
        coords, header = _parse_selig(path)
        upper, lower = _split_surfaces(coords)
        # 归一化弦长
        xs = [p[0] for p in coords]
        chord = max(xs) - min(xs)
        x_le = min(xs)
        if abs(chord - 1.0) > 1e-6:
            coords = [((x - x_le) / chord, y / chord) for x, y in coords]
            upper = [((x - x_le) / chord, y / chord) for x, y in upper]
            lower = [((x - x_le) / chord, y / chord) for x, y in lower]
        return AirfoilData(
            name=path.stem, file_path=path, coords=coords, upper=upper, lower=lower
        )


# ---------------- 解析函数 ----------------
def _parse_selig(path: Path) -> tuple[list, str]:
    """解析 Selig 格式：首行名称 + 坐标行。容错：跳过无法解析的行。"""
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    header = lines[0].strip() if lines else path.stem
    pts = []
    for ln in lines[1:]:
        t = ln.split()
        if len(t) >= 2:
            try:
                pts.append((float(t[0]), float(t[1])))
            except ValueError:
                continue
    if len(pts) < 20:
        raise ValueError(f"翼型坐标点过少({len(pts)}): {path}")
    return pts, header


def _split_surfaces(pts: list) -> tuple[list, list]:
    """将环绕坐标切分为上/下表面（均为 LE→TE）。

    兼容两种起点（TE 起始或 LE 起始）与不严格闭合的情况。
    """
    n = len(pts)
    i_le = min(range(n), key=lambda i: pts[i][0])
    ring = pts[i_le:] + pts[:i_le]           # 以 LE 开头
    j_te = max(range(n), key=lambda i: ring[i][0])  # 环形序中首个 TE
    surf_a = ring[: j_te + 1]                # LE → TE 表面A
    rest = ring[j_te + 1:]                   # TE → ... → LE 前 表面B
    surf_b = [ring[0]] + rest[::-1] if rest else list(surf_a)
    if sum(y for _, y in surf_a) / len(surf_a) >= 0:
        return surf_a, surf_b
    return surf_b, surf_a
