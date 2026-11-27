# AI-UAV 无人机快速概念设计平台 — 总体设计（V1.1）

> 状态：待确认。确认后进入 Phase 1 模块开发。
> 环境已验证：Python 3.13.14 / OpenVSP 3.55.0（vspscript.exe、vspaero.exe）/ XFOIL（C:\Xfoil\xfoil.exe）/ UIUC 翼型库 1665 个 .dat 文件。

---

## 1. 总体架构

五层插件化架构，模块间只通过统一数据契约（dataclass + JSON 序列化）通信，禁止直接调用其他模块内部实现。

```
┌─────────────────────────────────────────────────────┐
│ L1 UI        main.py CLI（argparse 子命令）          │
├─────────────────────────────────────────────────────┤
│ L2 Workflow  Pipeline 编排（run/configure/sweep/     │
│              optimize/report 六个子命令）             │
├─────────────────────────────────────────────────────┤
│ L3 Core      M01 Core │ M02 Geometry │ M03 Airfoil  │
│              M04 OpenVSP │ M05 Aero │ M06 Perf      │
│              M08 Optimization │ M09 AI │ M10 Agent  │
│              M11 Report │ M13 CFD(预留)             │
├─────────────────────────────────────────────────────┤
│ L4 Data      M07 Database（SQLite）+ data/ 文件缓存  │
├─────────────────────────────────────────────────────┤
│ L5 Tools     OpenVSP(vspscript) / VSPAero / XFOIL   │
└─────────────────────────────────────────────────────┘
```

**关键决策 D1 — OpenVSP 集成路线：AngelScript 批处理（vspscript.exe）**
- Python 由模板渲染生成 `.vspscript`（AngelScript），调用 `vspscript.exe -script xxx.vspscript` 产出 `.vsp3`；
- 理由：OpenVSP 3.55 官方 Python 绑定对 Python 版本有严格匹配要求（本机 Python 3.13 大概率不兼容），AngelScript 路线零依赖、完全可复现；
- 后果：所有几何 API 调用封装在 M04 内部的脚本模板中，更换集成路线只改 M04。

**关键决策 D2 — 技术栈**
- Python 3.13 + venv（`E:\workbuddy\AI-UAV\.venv`）
- 依赖：numpy / scipy / pyyaml / pandas / matplotlib / jinja2（模板渲染 vspscript）/ pytest
- M07 用标准库 sqlite3（无 ORM），M08 优化器用 scipy + 自实现 NSGA-II（初版不引入 pymoo，减少依赖风险；接口预留可切换）
- XFOIL 采用批处理文件（`.xfoil` 命令脚本）驱动，规避交互式 stdin 问题

---

## 2. 模块依赖关系

```
M10 Agent ──► (解析) ──► M01 Core
M01 Core ──► MissionSpec
MissionSpec ──► M02 Geometry ──► GeometryConfig
M03 Airfoil ──(翼型坐标/特性)──► M02 / M04 / M05
GeometryConfig + Airfoil ──► M04 OpenVSP ──► model.vsp3
model.vsp3 ──► M05 Aero(VSPAero) ──► AeroResult
翼型坐标 ──► M05 Aero(XFOIL) ──► AirfoilPolar
AeroResult + AirfoilPolar + MissionSpec ──► M06 Performance ──► PerformanceResult
M02/M05/M06 ──► M07 Database
M07 ──► M08 Optimization ──► M09 AI(代理模型) ──► TopK Designs
TopK + 全部结果 ──► M11 Report
M13 CFD：仅接口占位
```

依赖方向单向：L2 → L3 → L4/L5。M06 Performance 只消费 AeroResult 接口，不感知 OpenVSP 存在（规约要求）。

---

## 3. 项目目录结构

```
E:\workbuddy\AI-UAV\
├── main.py                     # CLI 入口
├── requirements.txt
├── configs/
│   ├── default.yaml            # 默认任务模板
│   └── example_mission.yaml    # 示例任务
├── uavdp/                      # 包（UAV Design Platform）
│   ├── __init__.py
│   ├── contracts.py            # ★ 统一数据契约（全部 dataclass）
│   ├── core/                   # M01：config.py / logging.py / cli.py / pipeline.py
│   ├── geometry/               # M02：sizing.py（初步尺寸估算）/ layout.py（参数化布局）
│   ├── airfoil/                # M03：loader.py / query.py / normalize.py
│   ├── openvsp/                # M04：script_template.j2 / vspmodel.py / export.py / runner.py
│   ├── aero/                   # M05：xfoil_adapter.py / vspaero_adapter.py / aero_service.py
│   ├── performance/            # M06：aero_perf.py / flight_perf.py / stability.py
│   ├── database/               # M07：schema.sql / db.py / dao_*.py
│   ├── optimization/           # M08：sampler.py / nsga2.py / objectives.py / topk.py
│   ├── ai/                     # M09：surrogate.py（XGBoost/MLP）/ interfaces.py
│   ├── agent/                  # M10：parser.py
│   ├── report/                 # M11：markdown.py / exporter.py
│   └── cfd/                    # M13：base.py（接口占位，不实现）
├── uiuc/coord_seligFmt/        # 已有，1665 个翼型
├── data/                       # 运行产物：vsp3/脚本/气动原始输出（git 忽略）
│   └── runs/<run_id>/…
├── db/uavdp.sqlite             # M07 数据库文件
├── reports/                    # 生成的报告
├── docs/                       # 本文档及后续设计文档
└── tests/                      # 每模块独立测试
```

---

## 4. 数据流设计

单次设计分析（Phase 1 闭环）：

```
config.yaml
  → MissionSpec（航程/速度/载荷/MTOW/续航/高度/约束）
  → M02 初步尺寸估算（重量分解迭代：W_wing/W_prop/W_batt/W_pay/W_struct → MTOW 收敛）
  → GeometryConfig（机翼/平尾/垂尾/机身/螺旋桨几何参数 + 选定翼型）
  → M04 渲染 AngelScript → vspscript.exe → model.vsp3 → DegenGeom 导出
  → M05：VSPAero 批量运行（CL/CD/Cm/L-D vs α 扫描）→ AeroResult
        XFOIL 运行（剖面极线 + 失速角）→ AirfoilPolar
  → M06 Performance（失速速度/巡航功率/推重比/航程/续航/爬升/静稳定裕度）
  → 控制台结果摘要 + JSON 落盘 + （M07）入库
```

每次运行分配 `run_id`（时间戳+哈希），所有中间产物归档于 `data/runs/<run_id>/`，可追溯、可复现。

---

## 5. 模块输入输出接口（数据契约摘要）

全部定义在 `uavdp/contracts.py`，模块间只传这些对象：

| 契约 | 字段（核心） | 生产者 → 消费者 |
|---|---|---|
| `MissionSpec` | range_km, cruise_speed_ms, payload_kg, mtow_kg, endurance_h, altitude_m, constraints{} | M01/M10 → M02/M06/M08 |
| `GeometryConfig` | wing{span,area,AR,sweep,taper,twist,root/tip airfoil}, htail{…,arm}, vtail{…}, fuselage{len,max_d}, prop{n,dia,kv},cg_x | M02 → M04/M06 |
| `AirfoilData` | name, coords[(x,y)], reynolds_ref, cl_max, alpha_stall | M03 → M04/M05 |
| `AirfoilPolar` | name, reynolds, points[(alpha,cl,cd,cm)] | M05 → M06 |
| `AeroResult` | run_id, alpha[], CL[], CD[], Cm[], L/D_max, CL_max, 稳定性导数{CLa,Cma,CYb…} | M05 → M06/M07 |
| `PerformanceResult` | v_stall, v_cruise, p_cruise, thrust_weight, range_km, endurance_h, climb_rate, static_margin, 尾容量系数 | M06 → M07/M08/M11 |
| `DesignRecord` | mission+geometry+aero+performance 扁平化（M07 入库、M08 优化变量+目标向量） | M02/M05/M06 → M07/M08 |

M05 内部 `AeroService` 接口：`analyze_geometry(geo, mission) -> AeroResult`；XFOIL/VSPAero 为其内部实现细节（Strategy 可替换，为 M13 CFD 预留同构接口）。

---

## 6. MVP 开发路线（Phase 1，逐模块交付）

| 步骤 | 内容 | 验证方式 |
|---|---|---|
| 0 | venv + 依赖 + 目录骨架 + contracts.py | pytest 空跑 |
| 1 | M01 Core：YAML 加载、日志、CLI 骨架 | `python main.py configure --show` |
| 2 | M03 Airfoil：读取/查询 UIUC 库（先于 M02，因布局需选翼型） | 单测：读 3 个典型翼型 |
| 3 | M02 Geometry：重量估算迭代 + 参数化布局 | 给定 MissionSpec 输出合理 GeometryConfig |
| 4 | M04 OpenVSP：模板 + 渲染 + 运行 + vsp3 校验 | vspviewer 可打开模型 |
| 5 | M05-A XFOIL 适配器 | 批处理跑出 NACA2412 极线，与参考值比对 |
| 6 | M05-B VSPAero 适配器 | 跑出 CL-α 曲线，量级合理 |
| 7 | M06 Performance | 手算交叉验证 2~3 个指标 |
| 8 | M01 Pipeline 串联 + M11 简版报告(Markdown) | **`python main.py run configs/example_mission.yaml` 全链路跑通** |

每步完成后暂停汇报，通过后进入下一步。Phase 2~4 按项目规约另行细化。

---

## 7. 风险分析

| # | 风险 | 影响 | 缓解措施 |
|---|---|---|---|
| R1 | VSPAero 批处理调用方式（DegenGeom→输入卡）在 3.55 上的具体格式未验证 | Phase 1 关键路径 | 步骤 6 开始前先做独立 spike 试验，用最小机翼模型打通；失败则降级为 vortex-lattice 自研初版，VSPAero 后补 |
| R2 | XFOIL 低雷诺数/特定翼型不收敛 | 部分设计点无极线 | 限制迭代次数；失败时标记并降级用经验公式（Flat-plate+形状因子估算）；不阻塞流程 |
| R3 | 重量迭代不收敛（结构系数经验公式偏差） | 无法得到几何参数 | 松弛因子 + 最大迭代次数 + 失败报告；初版用 Raymer/Gunduz 经络公式并在报告标注置信度 |
| R4 | AngelScript 模板维护成本（OpenVSP API 版本绑定 3.55） | 升级 OpenVSP 需改模板 | API 调用集中封装在少数模板文件；版本固定 3.55.0 |
| R5 | Windows 编码/路径问题（XFOIL、子进程调用） | 间歇性崩溃 | 全流程 UTF-8、绝对路径、子进程加超时与重试 |
| R6 | Python 3.13 与部分库（如 XGBoost）兼容性 | Phase 3 受阻 | Phase 3 前再评估；必要时用 MLP/scipy 替代，代理模型接口不变 |
| R7 | 大批量运行耗时（Phase 2 万级样本） | 数据生成慢 | 先测单次 VSPAero 耗时，估算总时长；必要时降网格精度/并行 |

---

## 待确认事项

1. **D1 OpenVSP 集成路线**（AngelScript 批处理，推荐）是否认可；
2. **技术栈与依赖清单**（D2）是否认可；
3. 目录结构、模块接口契约是否需要调整；
4. 确认后从步骤 0 开始逐模块开发。
