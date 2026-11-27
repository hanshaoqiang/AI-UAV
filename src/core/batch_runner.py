"""Phase 1.5: BatchRunner — 批量运行框架。

功能：
  - 多任务顺序/并行运行
  - 失败自动重试
  - 日志记录
  - 中断恢复（断点续跑，跳过已成功的 run）
  - 结果自动入库

不追求 GPU 并行，优先稳定性。
"""
from __future__ import annotations

import json
import logging
import time
import traceback
from pathlib import Path

logger = logging.getLogger("uavdp.batch")


class BatchRunner:
    """批量执行设计分析任务。

    每个任务 = (design_id, mission_spec, design_cfg)。
    状态通过 SQLite run_history 表持久化，支持中断恢复。
    """

    def __init__(self, pipeline, db, max_retries: int = 2,
                 state_file: str | Path | None = None):
        self.pipeline = pipeline      # DesignPipeline 实例
        self.db = db                  # SQLiteManager 实例
        self.max_retries = max_retries
        self.state_file = Path(state_file) if state_file else None
        self.results: list[dict] = []
        self.stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0}

    def run(self, tasks: list[dict], resume: bool = True,
            batch_id: str | None = None) -> dict:
        """执行任务列表。

        tasks: list[dict]，每项含
            design_id (str), mission (MissionSpec), design_cfg (dict)
        resume: 断点续跑（跳过 run_history 中已 success 的 design_id）
        返回统计 dict。
        """
        import time as _t
        batch_id = batch_id or f"batch_{_t.strftime('%Y%m%d_%H%M%S')}"
        self.stats = {"total": len(tasks), "success": 0, "failed": 0, "skipped": 0}
        t_start = _t.time()

        for i, task in enumerate(tasks):
            design_id = task.get("design_id", f"design_{i:04d}")
            mission = task["mission"]
            design_cfg = task.get("design_cfg")

            # 中断恢复：跳过已成功的
            if resume and self._is_done(design_id):
                self.stats["skipped"] += 1
                logger.info("[%d/%d] 跳过已完成: %s", i + 1, len(tasks), design_id)
                continue

            ok = self._run_with_retry(design_id, mission, design_cfg, batch_id)
            if ok:
                self.stats["success"] += 1
            else:
                self.stats["failed"] += 1

            self._save_state(batch_id)
            logger.info("[%d/%d] 完成: %s (累计 成功%d/失败%d/跳过%d)",
                        i + 1, len(tasks), design_id, self.stats["success"],
                        self.stats["failed"], self.stats["skipped"])

        self.stats["elapsed_s"] = _t.time() - t_start
        self.stats["batch_id"] = batch_id
        logger.info("批量完成: %s", self.stats)
        return self.stats

    def _run_with_retry(self, design_id: str, mission, design_cfg,
                        batch_id: str) -> bool:
        """单任务重试执行。"""
        for attempt in range(1, self.max_retries + 2):
            try:
                t0 = time.time()
                # 临时覆盖 pipeline 的设计配置（若有）
                if design_cfg:
                    saved_design = dict(self.pipeline.cfg.get("design", {}))
                    self.pipeline.cfg["design"].update(design_cfg)
                else:
                    saved_design = None

                result = self.pipeline.run(mission)
                dt = time.time() - t0

                # 回滚设计配置
                if saved_design is not None:
                    self.pipeline.cfg["design"] = saved_design

                self.results.append({
                    "design_id": design_id, "run_id": result["run_id"],
                    "status": "success", "elapsed_s": dt,
                })
                return True

            except Exception as e:
                if saved_design is not None:
                    self.pipeline.cfg["design"] = saved_design
                logger.warning("任务 %s 第 %d 次失败: %s", design_id, attempt, e)
                if self.db:
                    try:
                        self.db.increment_attempt(design_id)
                    except Exception:
                        pass
                if attempt > self.max_retries:
                    self.results.append({
                        "design_id": design_id, "status": "failed",
                        "error": str(e),
                        "traceback": traceback.format_exc(),
                    })
                    logger.error("任务 %s 重试 %d 次后放弃", design_id, self.max_retries)
                    return False
        return False

    def _is_done(self, design_id: str) -> bool:
        """检查 design_id 是否已成功完成（断点恢复）。"""
        if not self.db:
            return False
        try:
            cur = self.db._conn.execute(
                "SELECT status FROM run_history WHERE run_id LIKE ? AND status='success'",
                (f"%{design_id}%",))
            return cur.fetchone() is not None
        except Exception:
            return False

    def _save_state(self, batch_id: str) -> None:
        """保存批次状态（供中断恢复）。"""
        if not self.state_file:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "batch_id": batch_id,
            "stats": self.stats,
            "completed_design_ids": [
                r["design_id"] for r in self.results if r["status"] == "success"],
        }
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                   encoding="utf-8")

    def summary(self) -> dict:
        return dict(self.stats)
