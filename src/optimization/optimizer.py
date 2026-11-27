"""M08: 优化模块 — Phase 3 才实现。Phase 1 仅保留接口定义。"""
from __future__ import annotations

from abc import ABC, abstractmethod


class CandidateDesign:
    """优化产出的候选设计（Phase 3）。"""
    def __init__(self, geometry: dict, objectives: dict, rank: int):
        self.geometry = geometry
        self.objectives = objectives
        self.rank = rank


class Optimizer(ABC):
    """优化器接口: 输入 PerformanceResult 流, 输出 Top-K CandidateDesign。

    Phase 3 实现: NSGA-II / 贝叶斯优化 + 代理模型(M09)。
    """

    @abstractmethod
    def sample(self) -> list:
        """参数采样(LHS/随机)。"""

    @abstractmethod
    def optimize(self, evaluate_fn, n_generations: int) -> list:
        """执行多目标优化。evaluate_fn: 参数向量 → PerformanceResult。"""

    @abstractmethod
    def select_topk(self, k: int) -> list:
        """Top-K 设计推荐。"""


class NotImplementedOptimizer(Optimizer):
    """Phase 1 占位实现。"""

    def sample(self) -> list:
        raise NotImplementedError("M08 Optimization 在 Phase 3 实现")

    def optimize(self, evaluate_fn, n_generations: int) -> list:
        raise NotImplementedError("M08 Optimization 在 Phase 3 实现")

    def select_topk(self, k: int) -> list:
        raise NotImplementedError("M08 Optimization 在 Phase 3 实现")
