# DESIGN_DECISIONS

> 重大设计决策记录（ADR）。记录架构和技术路线为什么这么设计，以及对后续开发的影响。**永远追加，永远不要删除旧记录。**

---

## Decision 001 — 双 OpenVSP 模型（展示模型 / 气动模型）

日期：2026-11-25（Phase 1）

原因：VSPAero 3.55 薄面元模式不能稳定处理闭合 Body（伪尾涡污染整场求解）。

影响：增加一个 OpenVSPAdapter 维护两套模型；机身阻力不在 VLM 计算，改由寄生阻力法在 Performance 层计入。

---

## Decision 002 — Propeller 作为一级设计变量

日期：2026-11-25（Phase 1.5）

原因：未来支持分布式推进（1~4 桨、机头/机尾/翼上/分布式安装）。

影响：PropulsionParams 含 propellers 列表（数量 + 布局 + 直径 + 螺距 + 转速），推进模型独立于 Geometry。

---

## Decision 003 — Performance 独立于 Aerodynamics

日期：2026-11-25（Phase 1）

原因：未来支持 CFD 替换 VLM。

影响：Performance 只消费 AeroResult 接口，不感知 OpenVSP/VSPAero 存在；M13 CFD 预留同构接口。

---

## Decision 004 — OpenVSP 集成走 AngelScript 批处理（vspscript.exe）

日期：2026-11-25（Phase 0/1）

原因：OpenVSP 3.55 官方 Python 绑定对 Python 版本严格匹配，本机 Python 3.13 大概率不兼容；AngelScript 零依赖、完全可复现。

影响：几何 API 调用封装在 M04 内部脚本模板，更换集成路线只改 M04。

---

## Decision 005 — 寄生阻力采用组件叠加法

日期：2026-11-25（Phase 1.5）

原因：机身等闭合体不在 VLM 中计算，需独立补足零升阻力；组件叠加法（形状因子 × 湿面积 × 摩阻系数）适合概念设计阶段。

影响：新增 ParasiteDragModel，融合方式 CD_total = CD_induced + CD_parasitic + CD_wave。

---

## Decision 006 — SQLite 作为唯一数据库，六表结构

日期：2026-11-25（Phase 1.5）

原因：支撑代理模型训练与多目标优化，需链路完整、扁平化可查询。

影响：missions / geometries / propulsions / aerodynamics / performances / run_history 六表，均含常用标量列 + full_json。

---

## Decision 007 — 推进采用快速动量理论模型

日期：2026-11-25（Phase 1.5）

原因：概念设计阶段优先速度与稳定性，不做高精度 CFD/叶素法。

影响：PropulsionModel 用 CT/CP 经验系数法；Phase 2 可升级 BEMT（接口不变）。

---

## Decision 008 — 设计空间 YAML 驱动，禁止硬编码

日期：2026-11-25（Phase 2）

原因：Phase 2 需 25 维设计变量（机翼后掠/上反/扭转/翼型、尾翼锥比/后掠、螺旋桨直径/转速），硬编码无法满足「所有变量均可配置」的要求。

影响：`config/design_space.yaml` 为唯一设计空间定义源；`DesignSpace.from_dict/to_dict` + `load_design_space` 支撑；内置空间仅作兜底。

---

## Decision 009 — 根弦/梢弦/前后缘后掠为派生量

日期：2026-11-25（Phase 2）

原因：当前架构是约束驱动定尺寸（任务→翼载→面积），再结合展弦比 + 锥比 + 后掠唯一确定根/梢弦；独立采样根/梢弦会破坏可行性保证。

影响：采样变量 = 展弦比/锥比/后掠/翼型/安装位置；根弦/梢弦由几何唯一确定并完整入库，不独立采样。

---

## Decision 010 — 翼型采样用精选白名单

日期：2026-11-25（Phase 2）

原因：UIUC 1665 翼型含大量低雷诺/厚翼/反射/滑翔翼型，全库随机致 XFOIL/VSPAero 收敛率显著下降。

影响：精选 30 个收敛稳定翼型（根用带弯度、梢/尾用对称）写入 YAML，白名单本身可扩展。

---

## Decision 011 — 多进程采用「worker 计算 + 主进程单写者」

日期：2026-11-25（Phase 2）

原因：Python multiprocessing + SQLite 并发写存在锁竞争与 `database is locked` 风险；且 Windows 需 spawn 而非 fork。

影响：`ParallelBatchRunner` 中 worker 进程仅计算（`DesignPipeline(enable_db=False)`），结果经队列回主进程，由主进程独占 SQLite 写六表 + 元数据；worker 函数模块级可导入。

---

## Decision 012 — 数据质量控制 = 写库前校验过滤

日期：2026-11-25（Phase 2）

原因：为保证代理模型训练数据质量，物理异常样本（NaN/几何不可行/推进不足/静不稳定）不得入库。

影响：`DataValidator` 在 `ParallelBatchRunner` 主进程消费结果时、写库前执行；error 级样本不入库（stats.rejected），warning 级（未达任务规格但物理有效）标记不过滤。

---

## Decision 013 — 阻力分解 = 诱导(k·CL²) + 寄生(组件叠加)，不重复计数

日期：2026-11-25（Phase 2.5 阶段B）

原因：VLM 极线拟合的 CD0 截距是「数值伪零升阻力」，与组件叠加法的寄生阻力重复计数会导致巡航阻力高估约一倍、剩余功率系统性为负。

影响：`_interp_polar` 只返回 `k_induced·CL²`；总阻力 = 诱导 + 寄生（组件叠加法）；波阻亚声速恒 0。定尺寸与完整分析复用 ParasiteDragModel 保证自洽。

---

## Decision 014 — prop_rpm 作为一级设计变量并真正接入推进模型

日期：2026-11-25（Phase 2.5 阶段B）

原因：prop_rpm 若不接入模型，数据集中该变量与输出无因果关联，误导代理模型。

影响：设计转速作为推进求解的**上限约束**；`thrust_available = min(转速受限推力, 功率受限推力)`；记录 design/solve/operating 三转速；`ct_static = 0.10`（标准 UAV 桨经验值）。

---

## Decision 015 — 可行性预筛在外部工具分析前运行

日期：2026-11-25（Phase 2.5 阶段B）

原因：25 维设计空间边缘产生大量明显不可行设计（桨尖超速/桨盘超载/推力不足），浪费 OpenVSP/XFOIL/VSPAero 计算资源。

影响：新增 `FeasibilityPrefilter`（纯 Python 毫秒级），硬约束过滤 + 软约束告警，记录原因与指标；阈值与 `validation.yaml` 联动；预筛模型复用 ParasiteDragModel 与 sizing/evaluator 一致。

---

## Decision 016 — 稳定性标签保留配平前原始信号

日期：2026-11-25（Phase 2.5 阶段B）

原因：`_trim_cg` 重心配平会覆盖原始 Cmα/中性点/重心，若只存配平后值，static_margin 恒等于目标 0.10，代理模型无法学习真实静稳定裕度分布。

影响：配平前信号另存 `*_pre_trim` 字段（Cmα_pre_trim/NP_pre/CG_pre/cg_shift/static_margin_target），区分「实际计算值」与「人为设定目标裕度」；数据库新增对应列。

---

## Decision 017 — 预筛硬约束仅保留物理硬性约束（桨尖速度/桨盘载荷降级为软约束）

日期：2026-11-25（Phase 2.5 阶段C1）

原因：误杀验证（C1.2）发现桨尖速度硬约束误杀率 42.9%（设计转速是推进求解上限，实际工作转速被节流下调，预筛高估实际桨尖速度）；桨盘载荷按电机最大功率计（实际巡航功率远低），且完整分析 DataValidator 不检查盘载荷——即预筛用了一个下游不验证、且快速估算与真实求解不一致的经验阈值。经验阈值不是绝对物理定律。

影响：`config/prefilter.yaml` hard 仅保留 `thrust_margin_min` + `thrust_weight_ratio_min`（两项物理硬性约束）；桨尖速度/桨盘载荷降级为 `soft` 告警（`tip_speed_warn_ms` / `disk_loading_warn_w_m2`），仍记录告警与指标但不拒绝。最终误杀率 0%。后续若要对这两项做硬过滤，需先让完整分析真正计算并验证它们。

---

## Decision 018 — 预筛拒绝样本不丢弃，全量入库

日期：2026-11-25（Phase 2.5 阶段C1）

原因：预筛拒绝样本承载「为什么该设计不可行」的关键信息（失败原因 + 具体约束指标 + 阈值），对后续预筛调参、设计空间校准、代理模型负样本都有价值；直接丢弃会丢失这部分信号。

影响：新增 `prefilter_rejections` 表（design_id/dataset_id/generated_at/design_vars/failure_reasons/warnings/metrics/thresholds）；`DatasetGenerator` 预筛拒绝后调用 `record_prefilter_rejection` 入库并导出 JSON；拒绝样本不计入 stats.success/rejected/failed，而是独立的 `summary.prefilter.n_rejected`。保证「不伪装失败样本、不凑数」。

---

## Decision 019 — 失败阶段分类与任务输入/预测输出语义分离

日期：2026-11-25（Phase 2.5 阶段C1）

原因：数据集需区分「几何不可行」「气动分析失败」「外部软件失败」等不同失败阶段，以及「任务输入」与「模型预测输出」，避免字段语义混淆误导代理模型训练。

影响：`parallel_runner` 新增 `classify_failure_str()`（geometry_infeasible / aero_failed / external_tool_failed / other_failed），stats 对应计数；`_fetch_sample` 显式区分 `mission_range_km`（任务输入）/ `predicted_range_km`（模型预测）/ `range_margin_km`（裕度）；稳定性标签区分 `static_margin`（配平后人为目标）与 `static_margin_pre_trim`（配平前实际计算值），不允许把目标静稳定裕度当成实际标签。

---

## Decision 020 — 推进模型升级为 BEMT（叶素动量理论）

日期：2026-11-25（Phase 2.5 阶段C2 前置）

原因：Phase 1.5 的快速动量理论（CT/CP 经验系数）无法捕捉桨叶几何（弦长/扭转/桨距）与巡航工作点之间的物理关系，且经验系数对设计空间边缘系统性失准。升级为经典 Glauert BEMT 提供真实的桨尺寸 vs 巡航需求匹配物理。

影响：`src/propulsion/bemt.py` 实现 `PolarTable`（XFOIL 离线查找表 5 Re × 36 α，二维线性插值）+ `BladeGeometry`（弦长线性 taper + 扭转线性）+ `BEMTSolver.thrust_power`（含 Prandtl 桨尖损失）。`PropulsionModel` / `FeasibilityPrefilter` 优先 BEMT，加载失败回退 legacy 动量理论。可用推力从 `p_max·η/v` 线性近似重构为「转速 ≤ 设计转速 ∪ 轴功率 ≤ 电机最大功率」双约束扫描。桨距改为迭代匹配巡航工作点（0.75R 截面巡航攻角 = 5°），否则巡航节流降转速使截面进入负攻角、效率崩塌。

---

## Decision 021 — 直径缩放系数 prop_diameter_factor + BEMT 定尺寸校准

日期：2026-11-25（Phase 2.5 阶段C2 前置）

原因：BEMT 暴露了原设计空间「绝对直径 0.2~1.0m 与任务推力需求脱耦」的系统性矛盾——采样直径中位数 ≈ 功率反推推荐直径的 2 倍，D>0.6 的桨巡航效率崩塌（<0.38）。同时定尺寸假设 `prop_efficiency=0.7` 高于 BEMT 实际巡航效率（0.4~0.7），导致电机功率系统性不足（通过率暴跌到 4.7%）。

影响：设计空间 `prop_diameter_m` → `prop_diameter_factor`（D = factor × 0.17·√(P_max/n/100)，factor ∈ [0.7, 1.6]），使桨盘直径始终与任务功率匹配、保留 ±40% 探索自由度。定尺寸校准选定 `prop_efficiency=0.6` + `power_margin_factor=3.0`（有效裕度 3.0/0.6=5.0），预筛通过率 62%、误杀率 0%、误放率 10%。

---

## Decision 022 — 外部工具超时兜底：文件重定向 + 进程树 kill

日期：2026-11-26（Phase 2.5 阶段C2）

原因：C2 生产事故——VSPAero（vspscript 启动的孙进程 vspaero.exe）卡死后整批 10164 任务死锁 8+ 小时。根因是 `subprocess.run(capture_output=True, timeout=)` 的两个缺陷：超时只 kill 直接子进程（孙进程成孤儿残留），且孙进程继承管道写端句柄导致超时后 `communicate()` 无限阻塞。

影响：新增 `src/core/subprocess_util.py::run_with_timeout(cmd, cwd, timeout_s, env, stdin_file)`：stdout/stderr 重定向到临时文件（孙进程继承文件句柄不阻塞），超时后 `taskkill /T /F`(Windows)/`killpg`(Unix) 杀整个进程树。三个 adapter 统一改用：VSPAero/OpenVSP 超时抛带工具名 RuntimeError（供失败分类），XFOIL 超时降级 `converged=False`。新增 `tests/test_subprocess_util.py` 验证「超时杀树不残留孙进程 + 及时返回」。

---

## Decision 023 — 螺旋桨工作点求解用二分 + 轴功率约束（替代 250rpm 线性扫描）

日期：2026-11-26（Phase 2.5 阶段C3 Stage B）

原因：C3 Stage A 诊断发现，`_solve_propeller` 用 250rpm 步进线性扫描求「产生所需推力的最小转速」，但 BEMT 推力在脱离风车区（前进比过高）时陡变（0 → 几十/几百 N），250rpm 步进跨过陡变区导致推力/功率 overshoot 数倍（样本 15220 达 9.4×），功率虚高、续航系统性缩水至达标率 0.3%。且 `_solve_propeller` 只约束转速、不约束轴功率，导致电机超载（样本 15220 超载 2.75×）。

影响：`_solve_propeller` 改为二分求解（BEMT 推力脱离风车区后随转速单调非减，二分正确收敛），并新增 `p_max_shaft` 轴功率约束（超限时降到功率允许最大转速）。修复仅改求解器离散化与约束逻辑，**不改任何 BEMT 物理公式、经验系数、任务约束、预筛阈值**；修正的是数值求解误差，不是"为提高通过率反向调模型"。`_rpm_scan` 保留但标记废弃。

---

## Decision 024 — 桨尖速度区分设计/工作转速 + 升阻比统一含寄生阻力口径

日期：2026-11-26（Phase 2.5 阶段C3 Stage B）

原因：C3 诊断发现两个标签语义问题：(1) `tip_speed_ms` 用设计转速计算（703 m/s 马赫 2.07），而实际工作转速被节流（137 m/s 马赫 0.40），标签高估实际桨尖速度约 5 倍；(2) `LD_max` 来自 VLM 仅含诱导阻力（不含机身寄生阻力），与 `LD_cruise`（含寄生阻力）口径不一致，AR→LD_max 相关 0.956 因此被结构放大。

影响：`PerformanceResult` 新增 `tip_speed_design_ms`（设计转速）、`tip_speed_operating_ms`（工作转速）、`LD_max_total`（含寄生阻力的最大升阻比）；数据库 `performances` 表幂等加列；质量分析改用工作桨尖速度。Feature 冻结于 `docs/FEATURE_SCHEMA.md`（schema_version 1.0.0），Phase 3 前不得修改。

---

## Decision 025 — Feature Schema 冻结（AI 训练特征契约）

日期：2026-11-26（Phase 2.5 阶段C3 Stage B）

原因：代理模型训练需要稳定、明确、无歧义的特征/标签定义。C3 诊断暴露了字段语义混乱（tip_speed 口径、LD_max 口径、`--n-valid` 语义、任务输入 vs 预测输出混淆），需在进入 Phase 3 前冻结。

影响：`docs/FEATURE_SCHEMA.md` 定义全部 AI 训练 Feature（6 类 + 数据集级语义 `analysis_valid`/`mission_feasible`/`analysis_failed`），每个字段明确名称/单位/类型/输入/输出/可训练/参与优化。任何字段变更须升级 `schema_version`（语义化版本）。训练严格区分「任务不可行 ≠ 计算失败」：`analysis_valid=True` 且 `mission_feasible=False` 的样本仍可作回归训练样本。

