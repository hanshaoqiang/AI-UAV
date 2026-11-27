"""M07: SQLite 数据管理 — Phase 1.5 多表结构。

表：
  missions       任务需求
  geometries     几何参数
  propulsions    推进系统参数
  aerodynamics   气动结果
  performances   性能结果
  run_history    运行历史（批次/状态/耗时/重试）

每张表均含 run_id 关联，供代理模型训练、多目标优化、AI Agent 调用。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("uavdp.database")

SCHEMA = """
CREATE TABLE IF NOT EXISTS run_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT UNIQUE NOT NULL,
    batch_id      TEXT,
    created_at    TEXT NOT NULL,
    status        TEXT DEFAULT 'running',   -- running|success|failed
    attempts      INTEGER DEFAULT 1,
    elapsed_s     REAL,
    error_msg     TEXT,
    phase         TEXT DEFAULT 'phase1.5'
);

CREATE TABLE IF NOT EXISTS missions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT UNIQUE NOT NULL,
    mission_name  TEXT,
    range_km      REAL,
    cruise_speed_ms REAL,
    payload_kg    REAL,
    mtow_kg       REAL,
    endurance_h   REAL,
    altitude_m    REAL,
    stall_speed_max_ms REAL,
    static_margin_min REAL,
    static_margin_max REAL,
    climb_rate_min_ms REAL,
    full_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS geometries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT UNIQUE NOT NULL,
    span_m        REAL,
    wing_area_m2  REAL,
    aspect_ratio  REAL,
    taper_ratio   REAL,
    sweep_deg     REAL,
    dihedral_deg  REAL,
    root_chord_m  REAL,
    tip_chord_m   REAL,
    htail_area_m2 REAL,
    vtail_area_m2 REAL,
    htail_arm_m   REAL,
    vtail_arm_m   REAL,
    fuselage_length_m REAL,
    fuselage_diameter_m REAL,
    mtow_kg       REAL,
    empty_mass_kg REAL,
    battery_mass_kg REAL,
    cg_x_m        REAL,
    wing_x_m      REAL,
    full_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS propulsions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT UNIQUE NOT NULL,
    n_props       INTEGER,
    prop_layout   TEXT,
    prop_diameter_m REAL,
    prop_pitch_m  REAL,
    prop_rpm      REAL,
    motor_power_kw REAL,
    motor_efficiency REAL,
    battery_voltage_v REAL,
    battery_capacity_ah REAL,
    battery_energy_wh REAL,
    battery_mass_kg REAL,
    full_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS aerodynamics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT UNIQUE NOT NULL,
    cl_alpha_per_rad REAL,
    cl_alpha_0deg REAL,
    cl_max        REAL,
    cd0_vlm       REAL,
    cd0_parasitic REAL,
    cd0_total     REAL,
    k_induced     REAL,
    ld_max        REAL,
    alpha_ldmax_deg REAL,
    cm_alpha_per_rad REAL,
    cm_alpha_pre_trim_per_rad REAL,
    neutral_point_x_pre_trim_m REAL,
    cg_x_pre_trim_m REAL,
    cg_shift_m REAL,
    static_margin_target REAL,
    full_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS performances (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT UNIQUE NOT NULL,
    v_stall_ms    REAL,
    v_cruise_ms   REAL,
    cl_cruise     REAL,
    cd_cruise     REAL,
    ld_cruise     REAL,
    ld_max_total  REAL,
    cd_induced_cruise REAL,
    cd_parasitic_cruise REAL,
    thrust_required_n REAL,
    thrust_available_n REAL,
    power_cruise_w REAL,
    power_available_w REAL,
    motor_efficiency REAL,
    propeller_efficiency REAL,
    prop_design_rpm REAL,
    prop_solve_rpm REAL,
    prop_operating_rpm REAL,
    tip_speed_design_ms REAL,
    tip_speed_operating_ms REAL,
    range_km      REAL,
    range_margin_km REAL,
    endurance_h   REAL,
    climb_rate_ms REAL,
    static_margin REAL,
    neutral_point_x_m REAL,
    static_margin_pre_trim REAL,
    neutral_point_x_pre_trim_m REAL,
    cg_x_pre_trim_m REAL,
    cg_shift_m REAL,
    static_margin_target REAL,
    constraints_pass INTEGER,
    full_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_metadata (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT UNIQUE NOT NULL,
    mission_id      TEXT,
    geometry_id     TEXT,
    dataset_id      TEXT,
    random_seed     INTEGER,
    sample_method   TEXT,
    platform_version TEXT,
    openvsp_version TEXT,
    xfoil_version   TEXT,
    vspaero_version TEXT,
    generated_at    TEXT NOT NULL,
    full_json       TEXT NOT NULL
);

-- Phase 2.5 C1: 可行性预筛拒绝记录（原始采样设计 + 失败原因 + 指标 + 阈值，供误杀验证）
CREATE TABLE IF NOT EXISTS prefilter_rejections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    design_id       TEXT UNIQUE NOT NULL,
    dataset_id      TEXT,
    generated_at    TEXT NOT NULL,
    design_vars     TEXT NOT NULL,   -- JSON: 原始设计变量
    failure_reasons TEXT NOT NULL,   -- JSON: list[{name,detail,value,limit}]
    warnings        TEXT,            -- JSON: 软约束告警
    metrics         TEXT,            -- JSON: 关键指标
    thresholds      TEXT             -- JSON: 预筛阈值快照
);

CREATE INDEX IF NOT EXISTS idx_geometries_span ON geometries(span_m);
CREATE INDEX IF NOT EXISTS idx_aero_ldmax ON aerodynamics(ld_max);
CREATE INDEX IF NOT EXISTS idx_perf_range ON performances(range_km);
CREATE INDEX IF NOT EXISTS idx_history_status ON run_history(status);
CREATE INDEX IF NOT EXISTS idx_meta_dataset ON dataset_metadata(dataset_id);
CREATE INDEX IF NOT EXISTS idx_meta_seed ON dataset_metadata(random_seed);
CREATE INDEX IF NOT EXISTS idx_prefilter_rej_dataset ON prefilter_rejections(dataset_id);
"""


class SQLiteManager:
    """设计数据持久化（多表 + 扁平列，便于筛选与训练）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Phase 2.5：WAL 模式 + 长 busy_timeout，消除热日志恢复阻塞与多连接锁竞争。
        #   - WAL：读写不互斥、提交仅 1 次 fsync（rollback 模式需 3 次），
        #          且无热日志（崩溃后无需回滚），避免 kill 后残留 journal 卡死读路径。
        #   - busy_timeout=30s：瞬时锁竞争时等待而非立即报 "database is locked"。
        self._conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()
        logger.info("数据库就绪: %s", self.db_path)

    # ---------------- 模式迁移（Phase 2.5 新增列，幂等） ----------------
    def _migrate(self) -> None:
        """为已有库补加 Phase 2.5 新增列（幂等，忽略重复列错误）。"""
        migrations = {
            "aerodynamics": [
                ("cm_alpha_pre_trim_per_rad", "REAL"),
                ("neutral_point_x_pre_trim_m", "REAL"),
                ("cg_x_pre_trim_m", "REAL"),
                ("cg_shift_m", "REAL"),
                ("static_margin_target", "REAL"),
            ],
            "performances": [
                ("range_margin_km", "REAL"),
                ("prop_design_rpm", "REAL"),
                ("prop_solve_rpm", "REAL"),
                ("prop_operating_rpm", "REAL"),
                ("static_margin_pre_trim", "REAL"),
                ("neutral_point_x_pre_trim_m", "REAL"),
                ("cg_x_pre_trim_m", "REAL"),
                ("cg_shift_m", "REAL"),
                ("static_margin_target", "REAL"),
                ("ld_max_total", "REAL"),
                ("tip_speed_design_ms", "REAL"),
                ("tip_speed_operating_ms", "REAL"),
            ],
        }
        for table, cols in migrations.items():
            existing = {r[1] for r in self._conn.execute(
                f"PRAGMA table_info({table})").fetchall()}
            for name, typ in cols:
                if name not in existing:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
        self._conn.commit()

    # ---------------- 运行历史 ----------------
    def start_run(self, run_id: str, batch_id: str | None = None) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO run_history
               (run_id, batch_id, created_at, status, attempts)
               VALUES (?,?,?,?,?)""",
            (run_id, batch_id, datetime.now().isoformat(timespec="seconds"),
             "running", 1))
        self._conn.commit()

    def finish_run(self, run_id: str, elapsed_s: float,
                   status: str = "success", error_msg: str | None = None) -> None:
        self._conn.execute(
            """UPDATE run_history SET status=?, elapsed_s=?, error_msg=?
               WHERE run_id=?""",
            (status, elapsed_s, error_msg, run_id))
        self._conn.commit()

    def increment_attempt(self, run_id: str) -> None:
        self._conn.execute(
            "UPDATE run_history SET attempts = attempts + 1 WHERE run_id=?",
            (run_id,))
        self._conn.commit()

    # ---------------- 核心入库 ----------------
    def save_run(self, run_id: str, mission: dict, geometry: dict,
                 propulsion: dict, aero: dict, perf: dict) -> None:
        """完整入库（Phase 1.5 六表）。propulsion 缺失时从 geometry 提取。"""
        if propulsion is None:
            propulsion = geometry.get("propulsion", {})
        self._save_mission(run_id, mission)
        self._save_geometry(run_id, geometry)
        self._save_propulsion(run_id, propulsion)
        self._save_aero(run_id, aero)
        self._save_perf(run_id, perf)

    def _save_mission(self, run_id: str, m: dict) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO missions
               (run_id, mission_name, range_km, cruise_speed_ms, payload_kg,
                mtow_kg, endurance_h, altitude_m, stall_speed_max_ms,
                static_margin_min, static_margin_max, climb_rate_min_ms, full_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, m.get("mission_name"), m.get("range_km"),
             m.get("cruise_speed_ms"), m.get("payload_kg"), m.get("mtow_kg"),
             m.get("endurance_h"), m.get("altitude_m"), m.get("stall_speed_max_ms"),
             m.get("static_margin_min"), m.get("static_margin_max"),
             m.get("climb_rate_min_ms"), json.dumps(m, ensure_ascii=False)))

    def _save_geometry(self, run_id: str, g: dict) -> None:
        wing = g.get("wing", {})
        htail = g.get("htail", {})
        vtail = g.get("vtail", {})
        fus = g.get("fuselage", {})
        self._conn.execute(
            """INSERT OR REPLACE INTO geometries
               (run_id, span_m, wing_area_m2, aspect_ratio, taper_ratio,
                sweep_deg, dihedral_deg, root_chord_m, tip_chord_m,
                htail_area_m2, vtail_area_m2, htail_arm_m, vtail_arm_m,
                fuselage_length_m, fuselage_diameter_m, mtow_kg, empty_mass_kg,
                battery_mass_kg, cg_x_m, wing_x_m, full_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, wing.get("span_m"), wing.get("area_m2"), wing.get("aspect_ratio"),
             wing.get("taper_ratio"), wing.get("sweep_quarter_chord_deg"),
             wing.get("dihedral_deg"), wing.get("root_chord_m"), wing.get("tip_chord_m"),
             htail.get("area_m2"), vtail.get("area_m2"), htail.get("arm_m"),
             vtail.get("arm_m"), fus.get("length_m"), fus.get("max_diameter_m"),
             g.get("mtow_kg"), g.get("empty_mass_kg"), g.get("battery_mass_kg"),
             g.get("cg_x_m"), g.get("wing_x_m"),
             json.dumps(g, ensure_ascii=False)))

    def _save_propulsion(self, run_id: str, p: dict) -> None:
        props = p.get("propellers", [])
        first = props[0] if props else {}
        motor = p.get("motor", {})
        battery = p.get("battery", {})
        self._conn.execute(
            """INSERT OR REPLACE INTO propulsions
               (run_id, n_props, prop_layout, prop_diameter_m, prop_pitch_m,
                prop_rpm, motor_power_kw, motor_efficiency, battery_voltage_v,
                battery_capacity_ah, battery_energy_wh, battery_mass_kg, full_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, p.get("n_props", len(props)), first.get("location"),
             first.get("diameter_m") or p.get("prop_diameter_m"),
             first.get("pitch_m"), first.get("rpm"),
             motor.get("max_power_kw") or p.get("motor_power_kw"),
             motor.get("efficiency"),
             battery.get("voltage_v"), battery.get("capacity_ah"),
             battery.get("energy_wh") or p.get("battery_energy_wh"),
             battery.get("mass_kg") or p.get("battery_mass_kg"),
             json.dumps(p, ensure_ascii=False)))

    def _save_aero(self, run_id: str, a: dict) -> None:
        cd0_total = (a.get("CD0_parasitic", 0.0) or 0.0) + (a.get("CD0", 0.0) or 0.0)
        self._conn.execute(
            """INSERT OR REPLACE INTO aerodynamics
               (run_id, cl_alpha_per_rad, cl_alpha_0deg, cl_max, cd0_vlm,
                cd0_parasitic, cd0_total, k_induced, ld_max, alpha_ldmax_deg,
                cm_alpha_per_rad, cm_alpha_pre_trim_per_rad,
                neutral_point_x_pre_trim_m, cg_x_pre_trim_m, cg_shift_m,
                static_margin_target, full_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, a.get("CL_alpha_per_rad"), a.get("CL_alpha_0deg"),
             a.get("CL_max"), a.get("CD0"), a.get("CD0_parasitic"), cd0_total,
             a.get("k_induced"), a.get("LD_max"), a.get("alpha_LDmax_deg"),
             a.get("Cm_alpha_per_rad"), a.get("Cm_alpha_pre_trim_per_rad"),
             a.get("neutral_point_x_pre_trim_m"), a.get("cg_x_pre_trim_m"),
             a.get("cg_shift_m"), a.get("static_margin_target"),
             json.dumps(a, ensure_ascii=False)))

    def _save_perf(self, run_id: str, p: dict) -> None:
        cp = p.get("constraints_pass", {})
        n_pass = sum(1 for v in cp.values() if isinstance(v, dict) and v.get("pass"))
        self._conn.execute(
            """INSERT OR REPLACE INTO performances
               (run_id, v_stall_ms, v_cruise_ms, cl_cruise, cd_cruise, ld_cruise,
                ld_max_total, cd_induced_cruise, cd_parasitic_cruise,
                thrust_required_n, thrust_available_n, power_cruise_w,
                power_available_w, motor_efficiency, propeller_efficiency,
                prop_design_rpm, prop_solve_rpm, prop_operating_rpm,
                tip_speed_design_ms, tip_speed_operating_ms,
                range_km, range_margin_km, endurance_h, climb_rate_ms,
                static_margin, neutral_point_x_m, static_margin_pre_trim,
                neutral_point_x_pre_trim_m, cg_x_pre_trim_m, cg_shift_m,
                static_margin_target, constraints_pass, full_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, p.get("v_stall_ms"), p.get("v_cruise_ms"), p.get("CL_cruise"),
             p.get("CD_cruise"), p.get("LD_cruise"), p.get("LD_max_total"),
             p.get("cd_induced_cruise"), p.get("cd_parasitic_cruise"),
             p.get("thrust_required_n"), p.get("thrust_available_n"),
             p.get("power_cruise_w"), p.get("power_available_w"),
             p.get("motor_efficiency"), p.get("propeller_efficiency"),
             p.get("prop_design_rpm"), p.get("prop_solve_rpm"),
             p.get("prop_operating_rpm"), p.get("tip_speed_design_ms"),
             p.get("tip_speed_operating_ms"), p.get("range_km"),
             p.get("range_margin_km"), p.get("endurance_h"),
             p.get("climb_rate_ms"), p.get("static_margin"),
             p.get("neutral_point_x_m"), p.get("static_margin_pre_trim"),
             p.get("neutral_point_x_pre_trim_m"), p.get("cg_x_pre_trim_m"),
             p.get("cg_shift_m"), p.get("static_margin_target"), n_pass,
             json.dumps(p, ensure_ascii=False)))

    # ---------------- 数据集元数据（Phase 2） ----------------
    def save_metadata(self, meta) -> None:
        """写入/更新一条数据集元数据。meta: DatasetMetadata 或 dict。"""
        d = meta.to_dict() if hasattr(meta, "to_dict") else dict(meta)
        self._conn.execute(
            """INSERT OR REPLACE INTO dataset_metadata
               (run_id, mission_id, geometry_id, dataset_id, random_seed,
                sample_method, platform_version, openvsp_version, xfoil_version,
                vspaero_version, generated_at, full_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (d.get("run_id"), d.get("mission_id"), d.get("geometry_id"),
             d.get("dataset_id"), d.get("random_seed"), d.get("sample_method"),
             d.get("platform_version"), d.get("openvsp_version"),
             d.get("xfoil_version"), d.get("vspaero_version"),
             d.get("generated_at"), json.dumps(d, ensure_ascii=False)))
        self._conn.commit()

    def get_metadata(self, run_id: str) -> dict | None:
        """按 run_id 查询元数据（无则返回 None）。"""
        cur = self._conn.execute(
            "SELECT * FROM dataset_metadata WHERE run_id=?", (run_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))

    # ---------------- 预筛拒绝记录（Phase 2.5 C1） ----------------
    def record_prefilter_rejection(self, design_id: str, dataset_id: str,
                                   generated_at: str, design_vars: dict,
                                   failure_reasons: list, metrics: dict,
                                   thresholds: dict, warnings: list | None = None,
                                   commit: bool = True) -> None:
        """写入一条预筛拒绝记录（INSERT OR REPLACE，断点恢复幂等）。

        commit=False 时仅写入不提交，供批量写入（单事务）减少 fsync 开销；
        调用方负责最后调用 commit()。
        """
        self._conn.execute(
            """INSERT OR REPLACE INTO prefilter_rejections
               (design_id, dataset_id, generated_at, design_vars,
                failure_reasons, warnings, metrics, thresholds)
               VALUES (?,?,?,?,?,?,?,?)""",
            (design_id, dataset_id, generated_at,
             json.dumps(design_vars, ensure_ascii=False, default=str),
             json.dumps(failure_reasons, ensure_ascii=False, default=str),
             json.dumps(warnings or [], ensure_ascii=False, default=str),
             json.dumps(metrics or {}, ensure_ascii=False, default=str),
             json.dumps(thresholds or {}, ensure_ascii=False, default=str)))
        if commit:
            self._conn.commit()

    def commit(self) -> None:
        """提交当前事务（批量写入场景的显式提交入口）。"""
        self._conn.commit()

    def fetch_prefilter_rejections(self, dataset_id: str | None = None,
                                   limit: int | None = None) -> list[dict]:
        """读回预筛拒绝记录（可按 dataset_id 过滤），供误杀验证与质量分析。"""
        if dataset_id:
            cur = self._conn.execute(
                "SELECT * FROM prefilter_rejections WHERE dataset_id=? ORDER BY id",
                (dataset_id,))
        else:
            cur = self._conn.execute(
                "SELECT * FROM prefilter_rejections ORDER BY id")
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        if limit is not None:
            rows = rows[:limit]
        for r in rows:
            for key in ("design_vars", "failure_reasons", "warnings", "metrics", "thresholds"):
                try:
                    r[key] = json.loads(r[key]) if r.get(key) else None
                except Exception:
                    pass
        return rows

    def count_prefilter_rejections(self, dataset_id: str | None = None) -> int:
        """统计预筛拒绝记录条数。"""
        if dataset_id:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM prefilter_rejections WHERE dataset_id=?",
                (dataset_id,))
        else:
            cur = self._conn.execute("SELECT COUNT(*) FROM prefilter_rejections")
        return cur.fetchone()[0]


    def list_metadata(self, dataset_id: str | None = None,
                      limit: int = 1000) -> list[dict]:
        """列出元数据（可按 dataset_id 过滤）。"""
        if dataset_id:
            cur = self._conn.execute(
                "SELECT * FROM dataset_metadata WHERE dataset_id=? ORDER BY id LIMIT ?",
                (dataset_id, limit))
        else:
            cur = self._conn.execute(
                "SELECT * FROM dataset_metadata ORDER BY id LIMIT ?", (limit,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def count_metadata(self, dataset_id: str | None = None) -> int:
        """统计元数据条数（可按 dataset_id 过滤）。"""
        if dataset_id:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM dataset_metadata WHERE dataset_id=?",
                (dataset_id,))
        else:
            cur = self._conn.execute("SELECT COUNT(*) FROM dataset_metadata")
        return cur.fetchone()[0]

    # ---------------- 查询 ----------------
    def count(self, table: str = "run_history") -> int:
        cur = self._conn.execute(f"SELECT COUNT(*) FROM {table}")
        return cur.fetchone()[0]

    def count_success(self) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM run_history WHERE status='success'")
        return cur.fetchone()[0]

    def count_failed(self) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM run_history WHERE status='failed'")
        return cur.fetchone()[0]

    def fetch_performance_rows(self, limit: int = 1000) -> list[dict]:
        """拉取性能表（供后续代理模型训练）。"""
        cur = self._conn.execute(
            "SELECT * FROM performances ORDER BY id LIMIT ?", (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ---------------- 数据集读回（Phase 2） ----------------
    def fetch_dataset_samples(self, dataset_id: str,
                              limit: int | None = None) -> list[dict]:
        """读取某数据集的所有样本（join metadata + 六表 full_json），供导出。

        返回 list[dict]，每项含 design_id / run_id / mission / aircraft /
        propulsion / aerodynamics / performance / metadata。
        断点恢复后已完成的样本也在库中，因此可完整读回。
        """
        cur = self._conn.execute(
            "SELECT run_id FROM dataset_metadata WHERE dataset_id=? ORDER BY id",
            (dataset_id,))
        run_ids = [r[0] for r in cur.fetchall()]
        if limit is not None:
            run_ids = run_ids[:limit]
        samples = [self._fetch_sample(run_id) for run_id in run_ids]
        return [s for s in samples if s]

    def fetch_dataset_elapsed(self, dataset_id: str) -> list[float]:
        """读取某数据集成功任务的耗时（elapsed_s），供 Benchmark 统计。

        join run_history + dataset_metadata，返回 list[float]（忽略 None）。
        """
        cur = self._conn.execute(
            "SELECT rh.elapsed_s FROM run_history rh "
            "JOIN dataset_metadata dm ON dm.run_id = rh.run_id "
            "WHERE dm.dataset_id=? AND rh.status='success'",
            (dataset_id,))
        return [r[0] for r in cur.fetchall() if r[0] is not None]

    def _fetch_sample(self, run_id: str) -> dict | None:
        """读回单个 run 的完整样本（dataset_metadata + 六表 full_json）。"""
        row = self._conn.execute(
            "SELECT run_id, dataset_id, random_seed, sample_method, "
            "platform_version, openvsp_version, xfoil_version, vspaero_version, "
            "generated_at, full_json FROM dataset_metadata WHERE run_id=?",
            (run_id,)).fetchone()
        if not row:
            return None
        keys = ("run_id", "dataset_id", "random_seed", "sample_method",
                "platform_version", "openvsp_version", "xfoil_version",
                "vspaero_version", "generated_at", "full_json")
        meta = dict(zip(keys, row))
        extra = {}
        try:
            extra = json.loads(meta.get("full_json") or "{}").get("extra", {})
        except Exception:
            pass

        def _load_full_json(table: str) -> dict:
            r = self._conn.execute(
                f"SELECT full_json FROM {table} WHERE run_id=?", (run_id,)).fetchone()
            if not r:
                return {}
            try:
                return json.loads(r[0])
            except Exception:
                return {}

        mission = _load_full_json("missions")
        performance = _load_full_json("performances")

        # 模型限制（MODEL_LIMITATIONS 注入 metadata.extra 的顶层键，非嵌套）
        from src.dataset.metadata import MODEL_LIMITATIONS
        model_limitations = {k: extra.get(k) for k in MODEL_LIMITATIONS if k in extra}

        # Phase 2.5：显式区分任务输入与预测输出，避免字段语义混淆
        return {
            "design_id": extra.get("design_id", run_id),
            "run_id": run_id,
            "dataset_id": meta.get("dataset_id"),
            "mission": mission,
            "aircraft": _load_full_json("geometries"),
            "propulsion": _load_full_json("propulsions"),
            "aerodynamics": _load_full_json("aerodynamics"),
            "performance": performance,
            # 语义别名（任务输入 / 模型预测 / 裕度）
            "mission_range_km": mission.get("range_km"),
            "predicted_range_km": performance.get("range_km"),
            "range_margin_km": performance.get("range_margin_km"),
            "metadata": {
                "generated_at": meta.get("generated_at"),
                "platform_version": meta.get("platform_version"),
                "openvsp_version": meta.get("openvsp_version"),
                "xfoil_version": meta.get("xfoil_version"),
                "vspaero_version": meta.get("vspaero_version"),
                "random_seed": meta.get("random_seed"),
                "sample_method": meta.get("sample_method"),
                "validation": extra.get("validation"),
                "model_limitations": model_limitations,
            },
        }

    def close(self) -> None:
        self._conn.close()
