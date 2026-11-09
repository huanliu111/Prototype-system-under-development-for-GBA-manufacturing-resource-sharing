"""Local synthetic valuation agents, separate from the clearing broker.

These are explicit simulation function families, not functions prescribed by
the manuscript. Keeping them local is an application boundary, not a formal
privacy or strategic-incentive guarantee.
"""
from dataclasses import dataclass
import numpy as np


@dataclass(frozen=True)
class LogBuyer:
    alpha: float = 480.0

    def __post_init__(self):
        if not np.isfinite(self.alpha) or self.alpha <= 0:
            raise ValueError('收益系数必须是有限正数。')

    def marginal(self, quantities):
        return self.alpha / (1.0 + np.asarray(quantities, dtype=float))

    def payoff(self, quantities, payment):
        return float(self.alpha * np.log1p(quantities).sum() - payment)


@dataclass(frozen=True)
class QuadraticProvider:
    linear: float = 5.0
    quadratic: float = 100.0

    def __post_init__(self):
        if not np.isfinite([self.linear, self.quadratic]).all() or self.linear < 0 or self.quadratic <= 0:
            raise ValueError('线性成本必须非负，二次成本系数必须为有限正数。')

    def marginal(self, quantities):
        return self.linear + self.quadratic * np.asarray(quantities, dtype=float)

    def payoff(self, quantities, reward):
        u = np.asarray(quantities, dtype=float)
        return float(reward - (self.linear * u + 0.5 * self.quadratic * u*u).sum())


@dataclass(frozen=True)
class ExponentialProvider:
    """Curvature diagnostic; used in isolated algorithm trials, not business defaults."""
    rate: float = 10.0
    center: float = 1.0

    def marginal(self, quantities):
        return np.exp(self.rate * (np.asarray(quantities, dtype=float) - self.center))

    def payoff(self, quantities, reward):
        u = np.asarray(quantities, dtype=float)
        cost = np.expm1(self.rate * u) / (self.rate * np.exp(self.rate * self.center))
        return float(reward - cost.sum())
