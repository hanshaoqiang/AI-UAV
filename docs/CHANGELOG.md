# Changelog

> Git 风格，按时间倒序追加。

## 2026-11-26（Phase 2.5 阶段C3 Stage B：性能模型一致性修复 + Feature 冻结）

### Added

- `src/performance/evaluator.py` 新增 `tip_speed_design_ms` / `tip_speed_operating_ms` / `LD_max_total` 计算（区分设计/工作桨尖速度；含寄生阻力的最大升阻比）
- `scripts/backfill_prefilter_rejections.py` 新增预筛拒绝记录重放补录脚本（确定性重放，INSERT OR REPLACE 幂等）
- `docs/FEATURE_SCHEMA.md` 新增（schema_version 1.0.0，冻结 AI 训练特征，Phase 3 前不得修改）
- `tests/test_c3_repair.py` 新增 6 个修复项测试（D1-D4）

### Changed

- `src/propulsion/model.py` `_solve_propeller` 从 250rpm 线性扫描改为**二分求解**，并增加**轴功率约束**（`p_max_shaft`），消除风车区推力陡变导致的 overshoot 与电机超载（诊断 D1/D2）
- `src/core/contracts.py` `PerformanceResult` 新增 3 字段
- `src/database/sqlite_manager.py` `performances` 表新增 `ld_max_total` / `tip_speed_design_ms` / `tip_speed_operating_ms` 列（幂等迁移）
- `src/analysis/quality.py` 字段提取/统计口径/failure 口径修正（D5/D8）
- `src/dataset/generator.py` `_export` 新增 CSV 导出；`config/dataset.yaml` formats 加 csv（D6）
- `scripts/run_c2.py` `--n-valid` → `--n-tasks`（`--n-valid` 保留为别名），修正语义（D7）

### Fixed

- **巡航续航系统性偏低（达标率 0.3%）根因修复**：螺旋桨巡航工作点求解 overshoot（线性扫描在 BEMT 风车区推力陡变处 overshoot 数倍，样本 15220 达 9.4×）+ 无功率约束致电机超载（诊断 D1/D2）
- 预筛拒绝记录缺失 3466 条（3612 → 7078，重放补录，诊断 D5）
- `failure_record_complete` 误把校验拒绝计入失败的口径 bug（诊断 D8）

### Verified

- 完整测试套件 168/168 通过（原 162 + 新增 6）
- 修复后工作点 overshoot 从 9.4× → 1.00×（3 个 Stage A 样本复算）
- 预筛拒绝原因统计补全：7078 条记录 → 10923 个标签（推重比不足 6190 / 推力不足 3845 / 几何重量 888）
- 数据库已备份：`data/uavdp.sqlite.bak_c3_stageB_20261126_203008`

---

## 2026-11-26（Phase 2.5 阶段C2：万级生成完成 + 外部工具/数据库鲁棒性增强）

### Added

- `src/core/subprocess_util.py` 新增 `run_with_timeout()`：外部工具调用统一超时兜底——stdout/stderr 重定向到临时文件（避免孙进程继承管道句柄阻塞 `communicate()`），超时后 `taskkill /T /F`(Windows)/`killpg`(Unix) 杀整个进程树（含孙进程），抛 `ExternalToolTimeout`
- `scripts/finish_c2_report.py` 新增 C2 收尾脚本（用累计统计读回 + 质量分析 + 报告，跳过预筛/生成）
- `tests/test_subprocess_util.py` 新增 6 个超时兜底测试（含"超时杀进程树不残留孙进程 + 及时返回"核心验证）

### Changed

- `src/vspaero/adapter.py` / `src/openvsp/adapter.py` / `src/xfoil/adapter.py` 改用 `run_with_timeout()`（VSPAero/OpenVSP 超时抛带工具名 RuntimeError；XFOIL 超时降级 `converged=False`）
- `src/core/parallel_runner.py` 新增 worker 层「无进展看门狗」`stall_timeout_s`（生产路径默认 1800s），主进程超过阈值未收到结果且 worker 仍存活则判定死锁强制终止
- `src/database/sqlite_manager.py` 连接切 `PRAGMA journal_mode=WAL` + `busy_timeout=30000` + `synchronous=NORMAL`；`record_prefilter_rejection()` 新增 `commit=False` 参数
- `src/dataset/generator.py` `_record_rejections()` 改批量单事务（拒绝记录写入 10 分钟级 → 秒级）
- `scripts/run_c2.py` 顶部（导入 numpy 前）设 `OPENBLAS_NUM_THREADS=1` + `OMP_NUM_THREADS=1` + `faulthandler.enable()`

### Fixed

- VSPAero 卡死致整批死锁（孙进程继承管道句柄阻塞 `communicate()`）——进程树 kill + 文件重定向修复
- 预筛阶段偶发 SIGSEGV（scipy-openblas NO_AFFINITY 线程池竞争）——强制单线程 BLAS 修复
- SQLite 逐条 `commit()` 致写锁阻塞读路径（7078 条拒绝记录 10 分钟级 fsync）——WAL + 批量事务修复

### Verified

- 完整测试套件 162/162 通过（含 6 个超时兜底 + stall watchdog 测试）
- C2 万级生成完成：17242 采样 → 10164 预筛通过（59.0%）→ 7387 有效（72.7%）→ 2765 校验拒绝 → 12 失败
- 报告：`reports/c2/c2_10000_lhs_quality_report.md`

---

## 2026-11-25（Phase 2.5 阶段C2 前置：推进模型升级为 BEMT + 定尺寸校准）

### Added

- `src/propulsion/bemt.py` 新增 BEMT（叶素动量理论）求解器：`PolarTable`（XFOIL 极线查找表 5 Re × 36 α，二维线性插值）、`BladeGeometry`（弦长线性 taper + 扭转线性，0.75R 桨距角由几何螺距定义，暴露 `c_root`/`c_tip`）、`BEMTSolver.thrust_power`（Glauert BEMT + Prandtl 桨尖损失，风车状态返回 0）
- `scripts/build_prop_polar.py` 新增 XFOIL 极线查找表构建脚本 → `data/polars/clarky_polar.json`
- `scripts/verify_bemt_calibration.py` 新增 BEMT 校准验证脚本（预筛通过率 + 误杀/误放验证）
- `scripts/run_c2.py` 新增 C2 万级生成脚本（采样 17242 → 预筛 → ~10000 组完整分析 → 质量分析）
- `tests/test_bemt.py` 新增 12 个 BEMT 单元测试（极线插值/越界钳制/JSON 往返/桨叶几何/推力单调/风车状态/效率上界/动量理论对比）

### Changed

- `src/propulsion/model.py` / `src/validation/prefilter.py` 接入 BEMT：可用推力从 `p_max·η/v` 线性近似重构为「转速 ≤ 设计转速 ∪ 轴功率 ≤ 电机最大功率」双约束扫描（二分优化）
- `src/geometry/propeller.py` 桨距从 `pitch_ratio·D` 改为迭代匹配巡航工作点（`_match_cruise_pitch`：0.75R 截面巡航攻角 = 5°）；直径新增 `prop_diameter_factor` 缩放系数
- `src/geometry/sizing.py` 定尺寸接入 BEMT（`prop_diameter_factor` + `bemt_cfg` 透传）
- `config/design_space.yaml` `prop_diameter_m`（绝对直径 0.2~1.0）→ `prop_diameter_factor`（缩放系数 0.7~1.6）
- `src/core/config.py` 校准：`prop_efficiency` 0.7→0.6、`power_margin_factor` 3.5→3.0（有效裕度 5.0）
- 性能优化：`np.clip`→`min/max`；`_rpm_for_thrust`/`_available_thrust` 线性扫描→二分（单组预筛 121ms→36.8ms）

### Fixed

- BEMT 暴露定尺寸效率假设矛盾（`prop_efficiency=0.7` vs BEMT 实际 0.4~0.7）导致通过率暴跌 4.7%——通过 `prop_diameter_factor` + 效率校准修复
- 6 个测试因硬编码旧参数（pitch_ratio=0.6 / D=0.5）失败，更新为 BEMT 匹配值

### Verified

- BEMT 单元测试 12/12 通过；完整测试套件 155/155 通过
- 校准验证（300 组 LHS 扫描 + 100 组试点）：通过率 62%、误杀率 0%、误放率 10%

---

## 2026-11-25（Phase 2.5 阶段C1：预筛接入与 100 组试验数据质量验证）

### Added

- `src/database/sqlite_manager.py` 新增 `prefilter_rejections` 表 + `record_prefilter_rejection()` / `fetch_prefilter_rejections()` / `count_prefilter_rejections()`（预筛拒绝样本全量入库：design_id/dataset_id/generated_at/design_vars/failure_reasons/warnings/metrics/thresholds）
- `src/core/parallel_runner.py` 新增 `classify_failure_str()` 失败阶段分类（geometry_infeasible / aero_failed / external_tool_failed / other_failed）+ stats 对应计数
- `src/analysis/quality.py` 新增 `DataQualityAnalyzer`（采样/筛选/物理/标签/数据库五类质量分析）+ `build_quality_report()` Markdown 报告
- `scripts/run_c1.py` 新增 C1 执行脚本（100 组 LHS 生成 + 误杀验证 + 质量分析 + 报告输出）
- `tests/test_phase2_5_C1.py` 新增 9 个测试（预筛拆分与拒绝记录、预筛开关、配置控制、端到端记录、失败分类、稳定性标签往返、任务输入/预测输出、断点恢复幂等）

### Changed

- `src/dataset/generator.py` 接入 `FeasibilityPrefilter`：`generate()` 新增 `prefilter_enabled` 参数（显式 > config > 默认 True）；采样后预筛，拒绝样本记录不丢弃；summary 新增 `prefilter` 段；新增 `_design_to_mission_cfg` / `_prefilter_and_build_tasks` / `_record_rejections` / `_export_rejections`
- `config/dataset.yaml` 新增 `prefilter.enabled: true` + `prefilter.record_rejected: true`
- `config/prefilter.yaml` + `src/validation/prefilter.py`：桨尖速度从硬约束（`tip_speed_max_ms`）降级为软告警（`tip_speed_warn_ms`）；桨盘载荷从硬约束（`disk_loading_max_w_m2`）降级为软告警（`disk_loading_warn_w_m2`）；hard 仅保留 `thrust_margin_min` + `thrust_weight_ratio_min`
- `src/benchmark/benchmark.py` `run()` 新增 `prefilter_enabled` 透传
- 测试 mock 添加 `prefilter_enabled=False` 显式参数隔离预筛（`test_dataset_generator.py` / `test_benchmark.py` / `test_prefilter.py` 更新桨尖速度/桨盘载荷告警断言 + 新增推力不足拒绝测试）

### Fixed

- `_fetch_sample` 读回 `model_limitations` 为 0（`MODEL_LIMITATIONS` 顶层键被误作嵌套键 `extra.get("model_limitations")` 读取）——改为从顶层键提取，55/55 样本读回正确

### Verified

- 100 组 LHS 真实链路：100 采样 → 34 预筛拒绝 → 66 完整分析 → 55 成功 + 11 校验拒绝 + 0 失败；整体有效率 55.0%（预筛通过率 66.0% × 完整分析有效率 83.3%）
- 预筛误杀验证：抽样 10 组拒绝样本完整分析，误杀率 0.0%（两轮迭代将桨尖速度/桨盘载荷降级为软约束）
- 稳定性标签核查：`static_margin`（配平后恒=0.10，人为目标）与 `static_margin_pre_trim`（配平前实际计算值）字段真实存在、计算正确、正确入库
- 测试套件 133 → 143 全通过

---

## 2026-11-25（Phase 2.5 阶段B：数据生成质量优化）

### Added

- FeasibilityPrefilter（`src/validation/prefilter.py` + `config/prefilter.yaml`）：可行性预筛，硬约束（桨尖速度/桨盘载荷/推力不足/推重比不足）+ 软约束（航程/推重比/失速裕度）告警，纯 Python 毫秒级，记录过滤原因与指标
- 模型限制元数据 `MODEL_LIMITATIONS`（`src/dataset/metadata.py`，注入 metadata.extra）
- AeroResult / PerformanceResult 新增配平前稳定性原始信号字段（`Cm_alpha_pre_trim_per_rad` / `neutral_point_x_pre_trim_m` / `cg_x_pre_trim_m` / `cg_shift_m` / `static_margin_target` / `static_margin_pre_trim`）
- PerformanceResult 新增航程裕度 `range_margin_km` 与推进三转速 `prop_design_rpm` / `prop_solve_rpm` / `prop_operating_rpm`
- `sizing.py` 新增 `_cruise_drag_power()` 统一阻力/功率估算；`config.py` DEFAULTS 新增 `oswald_efficiency: 0.85`
- SQLite schema 幂等迁移（`_migrate` + ALTER TABLE ADD COLUMN）：aerodynamics 配平前字段、performances range_margin/prop_rpm/稳定性字段

### Changed

- **阻力分解对齐**：`evaluator.py` `_interp_polar` 改为返回诱导阻力 `k_induced·CL²`（不再返回含 CD0 截距的原始极线 CD）；定尺寸与完整分析复用 ParasiteDragModel
- **prop_rpm 真正接入推进模型**：设计转速作为求解上限，`thrust_available = min(转速受限推力, 功率受限推力)`，记录三转速
- **稳定性标签保留配平前信号**：`_trim_cg` 不覆盖原始 Cmα/NP/CG/调整量
- **range_km 字段语义区分**：任务输入 `mission_range_km` / 模型预测 `predicted_range_km` / 裕度 `range_margin_km`
- 采样器 `random_sample`/`lhs_sample`/`sobol_sample` 统一支持 `feasibility_check`/`max_attempts` 预筛回调
- 数据库 `_fetch_sample` 导出新字段（mission_range_km/predicted_range_km/range_margin_km/model_limitations）

### Fixed

- **VLM CD0 截距重复计数**（巡航阻力高估约一倍，剩余功率全负）——诱导阻力只用 `k_induced·CL²`
- **静止推力系数系统性偏低**（`ct_static = 0.10*p_d` → `0.10` 标准 UAV 桨经验值）
- `load_prefilter_config` 的 Path 对象传给 `yaml.safe_load` 报错（统一 `open()`）

---

## 2026-11-25（Phase 2）

### Added

- Design Space 扩展：25 维设计空间 YAML 驱动（21 连续 + 4 离散；机翼后掠/上反/扭转/安装位置/翼型白名单、尾翼锥比/后掠、螺旋桨直径/转速）
- 翼型白名单：30 个收敛稳定翼型（24 带弯度 + 6 对称，写入 `config/design_space.yaml`）
- Sobol 采样（scipy qmc，scramble 可复现）
- DataValidator（`src/validation/validator.py`：sanity/geometry/weight/stability/propulsion/aerodynamics/range 六类校验，error/warning 分级）
- DatasetMetadata（`src/dataset/metadata.py`：OpenVSP/XFOIL/VSPAero 版本检测 + 溯源元数据）
- ParallelBatchRunner（`src/core/parallel_runner.py`：worker 计算 + 主进程单写者多进程，失败重试/断点恢复/CPU 控制）
- DatasetGenerator（`src/dataset/generator.py`：采样→并行→校验→导出 YAML/JSON/SQLite 编排）
- BenchmarkRunner（`src/benchmark/benchmark.py`：成功率/平均耗时/CPU 利用率/库大小统计 + 参数/性能分布/相关性矩阵图 + Markdown 报告）

### Changed

- DesignSampler 扩展：`sobol_sample` / `DesignSpace.from_dict/to_dict` / `load_design_space`；`default_design_space()` 改为读 YAML
- SQLite 新增 `dataset_metadata` 表（七表）+ `fetch_dataset_samples` / `fetch_dataset_elapsed` 读回方法
- ParallelBatchRunner 新增 `validator` / `keep_invalid`（写库前校验，error 级样本不入库，stats 增 `rejected`）
- 几何管道贯通新设计变量（wing/tail/propeller/sizing 增加带默认值可选参数）
- `DesignPipeline` 新增 `enable_db` / `design_cfg` 可选参数（向后兼容，供单写者架构）

### Fixed

- `yaml.safe_load` 不接受 Path 对象（统一 `open()`）
- matplotlib 常量列直方图报错（配平后 static_margin 恒为常量）
- Windows spawn 局部 worker 函数无法 pickle（须模块级）

---

## 2026-11-25

### Added

- Propulsion 模块（PropulsionModel：螺旋桨/电机/电池模型，1~4 桨、nose/tail/wing/distributed 布局）
- ParasiteDragModel（组件叠加寄生阻力）
- DesignSampler（随机 + LHS 采样，9 连续 + 2 离散变量）
- BatchRunner（多任务、重试、断点续跑、自动入库）

### Changed

- SQLite 增加 Propulsion 表（六表结构：missions/geometries/propulsions/aerodynamics/performances/run_history）
- Performance 增加推进计算（需用/可用推力）+ 寄生阻力融合
- PropulsionParams 重构为含 propellers 列表 + motor/battery 子对象
- 采样空间校准（endurance [1,2.5]、aspect_ratio [6.5,14]、Vv [0.030,0.045]、range [50,300]）
- 重量迭代上限 60 → 300、容差 0.005 → 0.01

### Fixed

- 重量迭代发散（mtow = payload + a·mtow，a ≥ 1 时无固定点）
- 尾臂为负（机身长度增设 MAC 下限 L ≥ 2.25·c̄/0.58）
- 垂尾面积比超标（校验边界 0.20 → 0.35）
- 可行性预筛未覆盖完整校验链（补充 validate）

---

## 2026-11-25（Phase 1）

### Added

- 全链路闭环（Mission → Geometry → OpenVSP → XFOIL → VSPAero → CG 配平 → Performance → Report → SQLite）
- 双 OpenVSP 模型（展示模型 / 气动模型）
- XFOIL / VSPAero 适配器
- UIUC 翼型库索引（1665 翼型）

### Fixed

- POD 闭合体污染 VSPAero 求解（双模型方案）
- POD + 后掠 + 上反触发面元退化（主翼上反置 0）
- XFOIL 批处理丢行 / ASEQ 参数序 / 文件名过长
- VSPAero 俯仰力矩列选错（CMytot 才是真值）
