"""M11: ReportGenerator — 分析结果 → Markdown 报告 (+图表)。"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
# 中文字体(Windows)
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False
import numpy as np
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from src.core.contracts import (AeroResult, Aircraft, AirfoilPolar,
                                MissionSpec, PerformanceResult)

logger = logging.getLogger("uavdp.report")

TEMPLATE_DIR = Path(__file__).resolve().parent


def _root_chord_local(tail) -> float:
    """梯形面根弦: c̄=(cr+ct)/2 → cr = 2c̄/(1+λ)。"""
    return 2.0 * tail.mean_chord_m / (1.0 + tail.taper_ratio)


class ReportGenerator:
    """输入: 各阶段结果 + 产物路径; 输出: Markdown 报告文件。"""

    def __init__(self):
        self._env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            undefined=StrictUndefined, trim_blocks=True,
            keep_trailing_newline=True,
        )

    def generate(self, mission: MissionSpec, aircraft: Aircraft,
                 aero: AeroResult, polar: AirfoilPolar | None,
                 perf: PerformanceResult, run_dir: Path,
                 run_id: str, artifacts: dict) -> Path:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)

        fig1 = self._plot_lift_curve(aero, run_dir)
        fig2 = self._plot_drag_polar(aero, run_dir)
        fig3 = self._plot_layout(aircraft, run_dir)

        template = self._env.get_template("report.md.j2")
        import datetime
        md = template.render(
            run_id=run_id,
            generated_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            mission=mission,
            ac=aircraft,
            aero=aero,
            polar=polar,
            perf=perf,
            artifacts=artifacts,
            fig_lift=fig1, fig_polar=fig2, fig_layout=fig3,
        )
        out = run_dir / "report.md"
        out.write_text(md, encoding="utf-8")
        # 附带机器可读结果
        (run_dir / "results.json").write_text(
            json.dumps({
                "run_id": run_id,
                "mission": mission.to_dict(),
                "aircraft": aircraft.to_dict(),
                "aero": aero.to_dict(),
                "airfoil_polar": None if polar is None else {
                    "airfoil_name": polar.airfoil_name, "reynolds": polar.reynolds,
                    "cl_max": polar.cl_max, "alpha_stall_deg": polar.alpha_stall_deg,
                    "converged": polar.converged},
                "performance": perf.to_dict(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("报告已生成: %s", out)
        return out

    # ---------------- 图表 ----------------
    def _plot_lift_curve(self, aero: AeroResult, run_dir: Path) -> str:
        alpha = np.array(aero.alpha_deg)
        cl = np.array(aero.CL)
        ld = np.array([c / d for c, d in zip(aero.CL, aero.CD) if d > 0])
        fig, ax = plt.subplots(1, 2, figsize=(10, 4))
        ax[0].plot(alpha, cl, "o-", color="#2563eb")
        ax[0].set_xlabel(r"$\alpha$ (deg)")
        ax[0].set_ylabel(r"$C_L$")
        ax[0].grid(alpha=0.3)
        ax[0].set_title("升力线 CL-α")
        ax[1].plot(alpha[: len(ld)], ld, "s-", color="#dc2626")
        ax[1].set_xlabel(r"$\alpha$ (deg)")
        ax[1].set_ylabel("L/D")
        ax[1].grid(alpha=0.3)
        ax[1].set_title("升阻比 L/D-α")
        fig.tight_layout()
        name = "fig_lift_curve.png"
        fig.savefig(run_dir / name, dpi=140)
        plt.close(fig)
        return name

    def _plot_drag_polar(self, aero: AeroResult, run_dir: Path) -> str:
        cl, cd = np.array(aero.CL), np.array(aero.CD)
        fig, ax = plt.subplots(figsize=(5.5, 4.5))
        ax.plot(cd, cl, "o-", color="#16a34a")
        cl_fit = np.linspace(min(cl), max(cl), 50)
        cd_fit = aero.CD0 + aero.k_induced * cl_fit ** 2
        ax.plot(cd_fit, cl_fit, "--", color="#9ca3af",
                label=f"fit: CD0={aero.CD0:.4f}, k={aero.k_induced:.4f}")
        ax.set_xlabel(r"$C_D$")
        ax.set_ylabel(r"$C_L$")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
        ax.set_title("阻力极曲线")
        fig.tight_layout()
        name = "fig_drag_polar.png"
        fig.savefig(run_dir / name, dpi=140)
        plt.close(fig)
        return name

    def _plot_layout(self, ac: Aircraft, run_dir: Path) -> str:
        """三视图简图(侧视+俯视)。"""
        w, h, v = ac.wing, ac.htail, ac.vtail
        fus_l, fus_d = ac.fuselage.length_m, ac.fuselage.max_diameter_m
        fig, ax = plt.subplots(1, 2, figsize=(10, 3.6))

        def trap(ax_, pts, **kw):
            ax_.add_patch(plt.Polygon(pts, fill=False, **kw))

        # ---- 侧视图 (x-z) ----
        ax[0].add_patch(plt.Rectangle((0, -fus_d / 2), fus_l, fus_d,
                                      fill=False, color="#374151"))
        # 垂尾(梯形): 根弦前缘在 vtail_x-0.25c̄, 上沿为梢弦
        v_le = ac.vtail_x_m - 0.25 * v.mean_chord_m
        vcr, vct, vb = _root_chord_local(v), v.taper_ratio, v.span_m
        v_te_off = (vcr - vct) * 0.5 + vb * float(np.tan(np.deg2rad(v.sweep_quarter_chord_deg)))
        trap(ax[0], [(v_le, fus_d / 2),
                     (v_le + vcr, fus_d / 2),
                     (v_le + vcr / 2 + v_te_off / 1.0, fus_d / 2 + vb),
                     (v_le + vcr / 2 + v_te_off - vct, fus_d / 2 + vb)],
             color="#374151")
        # 平尾侧视投影
        h_le = ac.htail_x_m - 0.25 * h.mean_chord_m
        hcr = _root_chord_local(h)
        ax[0].add_patch(plt.Rectangle((h_le, ac.htail_z_m), hcr,
                                      h.mean_chord_m * 0.12, fill=False,
                                      color="#6b7280"))
        ax[0].plot(ac.cg_x_m, 0, "r^", markersize=9, label="CG")
        ax[0].plot([ac.wing_x_m, ac.wing_x_m], [-fus_d, fus_d], "b-", alpha=0.5)
        ax[0].set_aspect("equal")
        ax[0].set_xlabel("x (m)")
        ax[0].legend(fontsize=8)
        ax[0].set_title("侧视图")

        # ---- 俯视图 (x-y) ----
        ax[1].add_patch(plt.Rectangle((0, -fus_d / 2), fus_l, fus_d,
                                      fill=False, color="#374151"))
        for sign, geom, x0, z_off in ((1, w, ac.wing_x_m, 0), (-1, w, ac.wing_x_m, 0),
                                      (1, h, h_le, ac.htail_z_m), (-1, h, h_le, ac.htail_z_m)):
            cr, ct = _root_chord_local(geom), geom.taper_ratio
            bhalf = geom.span_m / 2
            swp = bhalf * float(np.tan(np.deg2rad(geom.sweep_quarter_chord_deg)))
            trap(ax[1], [(x0, 0), (x0 + cr, 0),
                         (x0 + cr / 2 + swp, sign * bhalf),
                         (x0 + cr / 2 + swp - ct, sign * bhalf)],
                 color="#2563eb" if geom is w else "#6b7280")
        ax[1].plot(ac.cg_x_m, 0, "r^", markersize=9)
        ax[1].set_aspect("equal")
        ax[1].set_xlabel("x (m)")
        ax[1].set_ylabel("y (m)")
        ax[1].set_title("俯视图")
        fig.tight_layout()
        name = "fig_layout.png"
        fig.savefig(run_dir / name, dpi=140)
        plt.close(fig)
        return name
