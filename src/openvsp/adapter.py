"""M04: OpenVSPAdapter — Aircraft 对象 → vsp3 模型 + STL。

Python 为唯一主控：参数计算/流程调度/结果解析全在 Python；
AngelScript 模板仅做 OpenVSP API 调用（D1 决策）。
未来更换建模工具时仅需替换本适配器，上层 Workflow 不变。
"""
from __future__ import annotations

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from src.airfoil.uiuc_database import AirfoilDatabase
from src.core.contracts import Aircraft
from src.core.subprocess_util import ExternalToolTimeout, run_with_timeout

logger = logging.getLogger("uavdp.openvsp")

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "external" / "scripts"


class OpenVSPAdapter:
    """输入: Aircraft + 翼型库; 输出: vsp3 文件路径 + STL 路径。"""

    def __init__(self, vspscript_exe: str, airfoil_db: AirfoilDatabase,
                 timeout_s: int = 300):
        self.vspscript_exe = str(vspscript_exe)
        self.airfoil_db = airfoil_db
        self.timeout_s = timeout_s
        self._env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            undefined=StrictUndefined,
            trim_blocks=True,
            keep_trailing_newline=True,
        )

    # ---------------- 主入口 ----------------
    def build(self, aircraft: Aircraft, run_dir: Path,
              vsp3_name: str = "aircraft.vsp3",
              aero_vsp3_name: str = "aero.vsp3",
              stl_name: str | None = "aircraft.stl") -> tuple[Path, Path, Path | None]:
        """生成两个模型: 完整模型(含机身, 展示/导出) + 气动模型(仅升力面, VSPAero 用)。

        返回 (full_vsp3, aero_vsp3, stl_path|None)。
        原因: VSPAero 3.55 薄面元下闭合 POD 体产生伪尾涡污染求解(GMRES 发散/负诱导阻力),
        故气动模型不含机身; 机身阻力由 Performance 模块寄生阻力法计入(概念设计标准做法)。
        """
        run_dir.mkdir(parents=True, exist_ok=True)
        full_vsp3 = (run_dir / vsp3_name).resolve()
        aero_vsp3 = (run_dir / aero_vsp3_name).resolve()
        stl_path = (run_dir / stl_name).resolve() if stl_name else None

        params = self._render_params(aircraft)

        # 完整模型(含机身+整流罩, 导出STL)
        p_full = dict(params)
        p_full["include_body"] = True
        p_full["vsp3_path"] = _fwd(str(full_vsp3))
        p_full["stl_path"] = _fwd(str(stl_path)) if stl_path else None
        self._render_and_run("build_model.vspscript.j2", p_full,
                             run_dir / "build_model.vspscript", run_dir)
        if not full_vsp3.exists():
            raise RuntimeError(f"vsp3 未生成: {full_vsp3}")
        if stl_path and not stl_path.exists():
            logger.warning("STL 未生成(非致命): %s", stl_path)
            stl_path = None

        # 气动模型(仅升力面)
        p_aero = dict(params)
        p_aero["include_body"] = False
        p_aero["vsp3_path"] = _fwd(str(aero_vsp3))
        p_aero["stl_path"] = None
        # 气动模型主翼上反置 0：上反对纵向气动可忽略，且规避「后掠+上反」面元退化
        # （DESIGN_DECISIONS/LESSONS 已记录；Phase 2 将上反升为设计变量后必须保留此处理）
        p_aero["wing_dihedral"] = 0.0
        self._render_and_run("build_model.vspscript.j2", p_aero,
                             run_dir / "build_aero_model.vspscript", run_dir)
        if not aero_vsp3.exists():
            raise RuntimeError(f"气动 vsp3 未生成: {aero_vsp3}")
        logger.info("模型已生成: %s + %s(气动)%s", full_vsp3.name, aero_vsp3.name,
                    f" + {stl_path.name}" if stl_path else "")
        return full_vsp3, aero_vsp3, stl_path

    def _render_and_run(self, template_name: str, params: dict,
                        script_path: Path, run_dir: Path) -> None:
        template = self._env.get_template(template_name)
        script_path.write_text(template.render(**params), encoding="utf-8")
        logger.info("OpenVSP 建模: %s", script_path.name)
        self._run_vspscript(script_path, run_dir)

    # ---------------- 参数计算(业务逻辑, Python侧) ----------------
    def _render_params(self, ac: Aircraft) -> dict:
        w, h, v, f, p = ac.wing, ac.htail, ac.vtail, ac.fuselage, ac.propulsion

        af_root = _fwd(str(self.airfoil_db.find_or_default(w.airfoil_root)))
        af_tip = _fwd(str(self.airfoil_db.find_or_default(w.airfoil_tip)))
        af_tail = _fwd(str(self.airfoil_db.find_or_default(h.airfoil, default=w.airfoil_root)))

        return {
            # 主翼(半模型镜像: Area=全面积/2)
            "wing_root_chord": w.root_chord_m,
            "wing_tip_chord": w.tip_chord_m,
            "wing_half_area": w.area_m2 / 2.0,
            "wing_sweep": w.sweep_quarter_chord_deg,
            "wing_dihedral": w.dihedral_deg,
            "wing_twist": w.twist_tip_deg,
            "wing_x": ac.wing_x_m,
            "wing_af_root": af_root,
            "wing_af_tip": af_tip,
            # 平尾(镜像: Area=全面积/2)
            "h_root_chord": _root_chord(h),
            "h_tip_chord": _root_chord(h) * h.taper_ratio,
            "h_half_area": h.area_m2 / 2.0,
            "h_sweep": h.sweep_quarter_chord_deg,
            "h_x": ac.htail_x_m - 0.25 * h.mean_chord_m,   # XForm 定位在根弦前缘
            "h_z": ac.htail_z_m,
            # 垂尾(单片: Area=全面积)
            "v_root_chord": _root_chord(v),
            "v_tip_chord": _root_chord(v) * v.taper_ratio,
            "v_area": v.area_m2,
            "v_sweep": v.sweep_quarter_chord_deg,
            "v_x": ac.vtail_x_m - 0.25 * v.mean_chord_m,
            "v_z_root": f.max_diameter_m * 0.4,           # 垂尾根弦距机身轴线高度
            "tail_af": af_tail,
            # 机身
            "fuselage": f,
            # 螺旋桨整流罩(拉进式布置在机头)
            "spinner_len": round(0.5 * p.prop_diameter_m, 3),
            "spinner_x": -0.5 * p.prop_diameter_m,         # 位于机头前
        }

    # ---------------- 子进程 ----------------
    def _run_vspscript(self, script_path: Path, cwd: Path) -> str:
        cmd = [self.vspscript_exe, "-script", str(script_path)]
        try:
            _, out, err = run_with_timeout(cmd, cwd, self.timeout_s)
        except ExternalToolTimeout as e:
            raise RuntimeError(f"vspscript 超时(>{self.timeout_s}s): {script_path}") from e
        log = (out or "") + (err or "")
        (cwd / "build_model.stdout.log").write_text(log, encoding="utf-8")
        # 关键错误检查
        errors = [ln for ln in log.splitlines() if "Error Code" in ln]
        if errors:
            logger.warning("OpenVSP API 错误 %d 条(详见日志):\n  %s",
                           len(errors), "\n  ".join(errors[:5]))
        if "UAVDP_BUILD_DONE" not in log:
            raise RuntimeError(f"vspscript 未正常完成(缺 UAVDP_BUILD_DONE 标记): {script_path}")
        return log


def _root_chord(tail) -> float:
    """梯形面根弦: c̄=(cr+ct)/2 → cr = 2c̄/(1+λ)。"""
    return 2.0 * tail.mean_chord_m / (1.0 + tail.taper_ratio)


def _fwd(path: str) -> str:
    """OpenVSP/AngelScript 字符串内路径统一正斜杠。"""
    return path.replace("\\", "/")
