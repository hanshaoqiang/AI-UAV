# Phase 2 大规模数据生成阶段 — 完成报告

> 阶段目标：构建高质量无人机设计数据库（设计空间 → 数据集生成 → 并行批处理 → 数据质量控制 → 数据库增强 → 基准），为 AI 代理模型训练与多目标优化提供可靠数据。

---

## 1. 阶段概览

| 项 | 结果 |
|---|---|
| 设计空间 | 25 维（21 连续 + 4 离散），YAML 驱动 |
| 采样方法 | Random / LHS / Sobol（scipy qmc） |
| 并行能力 | multiprocessing 多进程（worker 计算 + 主进程单写者） |
| 数据质量控制 | DataValidator 写库前校验，error 级样本过滤 |
| 数据库 | 七表（六表 + dataset_metadata） |
| 工具版本 | OpenVSP 3.55.0 / VSPAero 3.55.0 / XFOIL 6.99 / 平台 0.2.0 |
| 测试 | **118/118 通过**（Phase 1.5 的 49 + Phase 2 新增 69） |

---

## 2. 模块完成情况

### 2.1 Design Space（设计空间）

**文件**：`config/design_space.yaml`、`src/sampling/design_sampler.py`

- 设计空间由 YAML 驱动，禁止硬编码；`DesignSpace.from_dict/to_dict` + `load_design_space`。
- 新增 `sobol_sample`（scipy qmc.Sobol，scramble 可复现）。
- 扩展设计变量（Phase 1.5 的 11 维 → 25 维）：

| 类别 | 新增变量 | 范围 |
|---|---|---|
| 机翼 | 后掠 `wing_sweep_deg` | 0~20° |
| 机翼 | 上反 `wing_dihedral_deg`、扭转 `wing_twist_deg` | 0~5° / -3~0° |
| 机翼 | 安装位置 `wing_x_fraction` | 0.25~0.40 |
| 机翼 | 翼型 `airfoil_root` / `airfoil_tip`（离散白名单） | 24 带弯度 / 6 对称 |
| 平尾 | 锥比 `htail_taper_ratio`、后掠 `htail_sweep_deg` | 0.4~0.8 / 0~15° |
| 垂尾 | 锥比 `vtail_taper_ratio`、后掠 `vtail_sweep_deg` | 0.4~0.8 / 0~25° |
| 螺旋桨 | 直径 `prop_diameter_m`、转速 `prop_rpm` | 0.20~1.00 m / 4000~9000 |

- 保留 Phase 1.5 的 9 连续 + 2 离散变量（range/cruise/payload/endurance/altitude + AR/taper/Vh/Vv + n_props/prop_layout）。
- **决策落实**：根弦/梢弦/前后缘后掠为派生量（面积+展弦比+锥比+后掠唯一确定，完整入库）；翼型采样用 30 个精选白名单。

### 2.2 DataValidator（数据质量控制）

**文件**：`src/validation/validator.py`、`config/validation.yaml`

- `CheckResult` / `ValidationResult` / `DataValidator` 三结构。
- 六类校验（阈值 YAML 化）：sanity（NaN/inf 预扫描）、geometry、weight、stability、propulsion、aerodynamics、range。
- 分级：`error` = 物理异常（过滤）；`warning` = 未达任务规格（标记不过滤）。

### 2.3 数据库增强（Dataset Metadata）

**文件**：`src/dataset/metadata.py`

- `DatasetMetadata`：生成时间、平台版本、OpenVSP/XFOIL/VSPAero 版本、随机种子、Mission/Geometry/Run ID、dataset_id、sample_method。
- 版本检测（带缓存）：OpenVSP 从目录名/CHANGELOG 解析（不运行程序）、XFOIL 运行 banner 解析（失败回退 unknown 不阻塞）。
- SQLite 新增 `dataset_metadata` 表（七表）+ `fetch_dataset_samples` / `fetch_dataset_elapsed` 读回方法。

### 2.4 Batch Parallel（ParallelBatchRunner）

**文件**：`src/core/parallel_runner.py`

- 架构：**worker 计算 + 主进程单写者**（规避 SQLite 并发写竞争）。
- 特性：动态任务分配、失败重试、断点恢复（state_file）、CPU 资源控制（max_workers）、Windows spawn 安全。
- `DesignPipeline` 增加 `enable_db` / `design_cfg` 可选参数（向后兼容）。
- 验证：4 任务 / 2 worker 全部成功，wall 18.1s（vs 串行约 34s，加速 ~1.9×）。

### 2.5 DatasetGenerator（数据集生成编排）

**文件**：`src/dataset/generator.py`、`config/dataset.yaml`

- 整合：采样（random/lhs/sobol）→ 并行批分析 → 校验过滤 → 导出 YAML/JSON/SQLite。
- 扩展 `ParallelBatchRunner` 新增 `validator` / `keep_invalid`：写库前校验，error 级样本不入库（stats.rejected）。
- 导出从 SQLite 读回（`fetch_dataset_samples`），断点恢复后也能完整导出。

### 2.6 Benchmark（基准）

**文件**：`src/benchmark/benchmark.py`、`config/benchmark.yaml`

- 统计：成功率、平均计算时间、总 CPU 时间、墙钟时间、CPU 利用率、并行加速比、数据库大小。
- CPU 利用率定义（无 psutil 依赖）：`total_cpu_time / (wall_time × max_workers)`。
- 绘图（matplotlib Agg）：参数分布、性能分布、Pearson 相关性矩阵（过滤常量列）。
- 报告：`benchmark_report.md`（统计摘要 + 附图 + 说明）。

---

## 3. 数据库结构（七表）

| 表 | 关键字段 |
|---|---|
| missions | range_km, cruise_speed_ms, payload_kg, endurance_h, altitude_m ... |
| geometries | span_m, wing_area_m2, aspect_ratio, taper_ratio, sweep, tail, fuselage ... |
| propulsions | n_props, prop_layout, diameter, pitch, rpm, motor, battery ... |
| aerodynamics | cl_alpha, cd0_vlm, cd0_parasitic, cd0_total, k, ld_max ... |
| performances | v_stall, cl/cd_cruise, thrust_req/avail, power, range, endurance, SM ... |
| run_history | status, attempts, elapsed_s, error_msg, batch_id ... |
| dataset_metadata | dataset_id, random_seed, sample_method, 版本×4, generated_at ... |

---

## 4. 测试结果

- 完整测试套件：**118/118 通过**，无回归。
- Phase 2 新增 69 个测试：
  - `test_design_space.py`（11）：采样器新变量、YAML 加载、sobol、几何贯通
  - `test_validator.py`（18）：正常/异常样本、NaN、航程未达标、filter
  - `test_metadata.py`（13）：版本检测、元数据往返、SQLite 读写
  - `test_parallel_runner.py`（9）：资源控制、断点恢复、单写者、spawn 端到端
  - `test_dataset_generator.py`（11）：采样、任务组装、导出、校验过滤、读回
  - `test_benchmark.py`（7）：统计、DataFrame、绘图、报告、端到端

---

## 5. 验证结果

### 5.1 真实 pipeline 端到端（各模块）

| 模块 | 验证 | 结果 |
|---|---|---|
| 模块 1 | 20 组采样→Aircraft 贯通 | 20/20 通过 |
| 模块 2 | 真实结果校验 | 31 项检查，0 error，2 warning |
| 模块 3 | 版本检测 | OpenVSP 3.55.0 / XFOIL 6.99 |
| 模块 4 | 4 任务 / 2 worker 并行 | 100% 成功，加速 ~1.9× |
| 模块 5 | 3 组生成 | 1 success + 1 rejected（推进不足）+ 1 failed（几何不可行） |
| 模块 6 | 8 组基准 | 见下表 |

### 5.2 Benchmark（8 组，真实 OpenVSP/XFOIL）

| 指标 | 值 |
|---|---|
| 请求样本数 | 8 |
| 有效样本数（入库） | 3 |
| 成功率 | 37.5%（3 success / 1 failed / 4 rejected） |
| 平均计算时间 | 7.99 s/组 |
| 墙钟时间 | 32.0 s |
| CPU 利用率（2 worker） | 37.4% |
| 并行加速比 | 0.75× |
| 数据库大小 | 0.13 MB |

> 说明：成功率反映设计空间扩展（25 维）后的边缘组合情况——推进不足/几何不可行设计被 DataValidator 正确过滤（rejected）或几何校验拒绝（failed），属数据质量控制的预期行为。大规模生成时可通过可行性预筛 + 采样范围校准提升成功率。

---

## 6. 关键设计决策（新增 ADR 008~012）

| 编号 | 决策 | 要点 |
|---|---|---|
| 008 | 设计空间 YAML 驱动 | `design_space.yaml` 为唯一定义源，禁止硬编码 |
| 009 | 根弦/梢弦为派生量 | 约束驱动定尺寸，根/梢弦由面积+AR+锥比唯一确定 |
| 010 | 翼型精选白名单 | 30 个收敛稳定翼型，全库随机致收敛率下降 |
| 011 | worker 计算 + 单写者 | 规避 SQLite 并发写竞争，Windows spawn 安全 |
| 012 | 写库前校验过滤 | error 级样本不入库，warning 级标记不过滤 |

完整记录见 `docs/DESIGN_DECISIONS.md`。

---

## 7. 已知局限与 Phase 3 建议

1. **成功率优化**：设计空间边缘不可行设计较多，建议在 DatasetGenerator 采样阶段加入 `feasibility_check` 预筛（复用 DataValidator），提升大规模生成成功率。
2. **推进模型**：仍为动量理论快速模型，Phase 3 若需更高精度可升级 BEMT（接口不变）。
3. **寄生阻力**：形状因子为经验值，可对标实验标定。
4. **波阻**：亚声速恒为 0，跨声速未覆盖。
5. **大规模生成**：Phase 3 建议按 `parallel.max_workers`（本机 32 核）配置并行，精简模式（跳过 STL/报告）已由 DatasetGenerator 支撑。
6. **数据规模**：Phase 2 已具备 1~2 万组生成能力（DatasetGenerator + ParallelBatchRunner + DataValidator + 七表入库），正式大规模生成留待 Phase 3 前执行。
