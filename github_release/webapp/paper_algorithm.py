"""Manuscript chapter 6: clearing (23), feedback (36), relaxation (37), transfers (29).

The broker receives local marginal responses, not agent coefficients. Clearing
uses x=y=u: for positive asks the original objective strictly decreases in y,
so this is an exact reduction, not an alternative allocation objective.
The previously agreed minimum patch adds bounded termination and settlement
rejection. It does not replace the manuscript update with a proximal method.
"""
from dataclasses import asdict, dataclass
import math
import time
import hashlib
import json
import platform
from pathlib import Path
import numpy as np
import scipy
from scipy.optimize import linprog, minimize

ALGORITHM_ID = 'paper-iterative-double-auction'
ALGORITHM_VERSION = 'paper-ida-1.0'


def provenance():
    source = Path(__file__).with_name('paper_source.json')
    return {'python':platform.python_version(),'numpy':np.__version__,'scipy':scipy.__version__,
            'engine_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'valuation_sha256':hashlib.sha256(Path(__file__).with_name('local_valuation.py').read_bytes()).hexdigest(),
            'manuscript':json.loads(source.read_text()) if source.exists() else None,
            'initialization':'local marginal response at u=1 on every admissible edge'}


@dataclass(frozen=True)
class Settings:
    delta: float = 0.1
    rho: float = 0.2
    tolerance: float = 1e-6
    max_iterations: int = 400
    settlement_tolerance: float = 2e-5
    time_limit: float = 30.0

    def __post_init__(self):
        values = [self.delta, self.rho, self.tolerance, self.settlement_tolerance, self.time_limit]
        if not all(math.isfinite(v) for v in values):
            raise ValueError('算法参数必须是有限数值。')
        if self.delta <= 0 or not 0 < self.rho <= 1 or not 1e-10 <= self.tolerance <= 0.1:
            raise ValueError('δ 必须为正，ρ 必须在 (0,1]，容差应在 1e-10 到 0.1。')
        if not isinstance(self.max_iterations, int) or not 1 <= self.max_iterations <= 2000:
            raise ValueError('最大轮数必须为 1–2000 的整数。')
        if not 0 <= self.settlement_tolerance <= 1e-3 or not 0 < self.time_limit <= 120:
            raise ValueError('结算容差或运行时限不合法。')


def constraints(demands, capacities, edges):
    d, c = np.asarray(demands, dtype=float), np.asarray(capacities, dtype=float)
    if d.ndim != 1 or c.ndim != 1 or not len(d) or not len(c):
        raise ValueError('至少需要一家需求方和一家供给方。')
    if not np.isfinite(d).all() or not np.isfinite(c).all() or (d <= 0).any() or (c < 0).any():
        raise ValueError('需求必须为有限正数，容量必须为有限非负数。')
    if len(edges) != len(set(map(tuple, edges))):
        raise ValueError('候选关系不能重复。')
    rows, cols = np.zeros((len(d), len(edges))), np.zeros((len(c), len(edges)))
    for e, (i, j) in enumerate(edges):
        if not 0 <= i < len(d) or not 0 <= j < len(c):
            raise ValueError('候选关系下标超出范围。')
        rows[i, e], cols[j, e] = 1, 1
    return d, c, rows, cols, np.vstack([-rows, cols]), np.r_[-d, c]


def feasible_point(demands, capacities, edges):
    d, c, rows, cols, a, b = constraints(demands, capacities, edges)
    if not edges:
        return None
    result = linprog(np.zeros(len(edges)), A_ub=a, b_ub=b, bounds=(0, None), method='highs')
    if result.status == 2:
        return None
    if not result.success:
        raise RuntimeError('物理可行性求解失败：' + result.message)
    return result.x


def clear(q, p, demands, capacities, edges, delta, start=None):
    """Strictly concave clearing with a separately checked first-order gap."""
    d, c, rows, cols, a, b = constraints(demands, capacities, edges)
    q, p = np.asarray(q, dtype=float), np.asarray(p, dtype=float)
    if len(q) != len(edges) or len(p) != len(edges) or not np.isfinite(q).all() or not np.isfinite(p).all() or (q <= 0).any() or (p <= 0).any():
        raise ValueError('买卖信号必须为有限正数并与候选关系对应。')
    if start is None:
        start = feasible_point(d, c, edges)
    if start is None:
        raise ValueError('PHYSICAL_INFEASIBLE')
    scale = max(1.0, float(max(q.max(), p.max())))

    def objective(u):
        s = u + delta
        return float((-q * np.log(s) + 0.5 * p * s*s).sum() / scale)

    def gradient(u):
        s = u + delta
        return (-q / s + p * s) / scale

    result = minimize(objective, start, jac=gradient, method='SLSQP',
                      bounds=[(0.0, float(c[j])) for _, j in edges],
                      constraints={'type': 'ineq', 'fun': lambda u: b - a @ u,
                                   'jac': lambda u: -a},
                      options={'ftol': 1e-13, 'maxiter': 350})
    u = np.maximum(np.asarray(result.x), 0.0)
    violation = max(0.0, float(np.max(a @ u - b)))
    grad = q / (u + delta) - p * (u + delta)
    certificate = linprog(-grad, A_ub=a, b_ub=b, bounds=(0, None), method='highs')
    gap = max(0.0, float(grad @ (certificate.x - u))) if certificate.success else math.inf
    # A flat objective can stop SLSQP before an accurate signal update. A dual
    # polish solves the same separable clearing, then rechecks the certificate.
    if gap > 1e-6 or violation > 1e-8:
        def dual(v):
            zeta, gamma = v[:len(d)], v[len(d):]
            diff = np.array([gamma[j] - zeta[i] for i, j in edges])
            disc = np.sqrt(diff*diff + 4*p*q)
            s = np.where(diff >= 0, 2*q / (disc + diff), (disc-diff)/(2*p))
            x = np.maximum(0, s-delta)
            f = (q*np.log(x+delta)-0.5*p*(x+delta)**2-diff*x).sum() - zeta@d + gamma@c
            jac = np.r_[rows@x-d, c-cols@x]
            return float(f/scale), jac/scale
        dual_result = minimize(dual, np.zeros(len(d)+len(c)), jac=True, method='L-BFGS-B',
                               bounds=[(0, None)]*(len(d)+len(c)),
                               options={'ftol': 1e-15, 'gtol': 1e-12, 'maxiter': 1000, 'maxls': 50})
        zeta, gamma = dual_result.x[:len(d)], dual_result.x[len(d):]
        diff = np.array([gamma[j]-zeta[i] for i, j in edges])
        disc = np.sqrt(diff*diff+4*p*q)
        u2 = np.maximum(0, np.where(diff >= 0, 2*q/(disc+diff), (disc-diff)/(2*p))-delta)
        violation2 = max(0.0, float(np.max(a@u2-b)))
        grad2 = q/(u2+delta)-p*(u2+delta)
        cert2 = linprog(-grad2, A_ub=a, b_ub=b, bounds=(0, None), method='highs')
        gap2 = max(0.0, float(grad2@(cert2.x-u2))) if cert2.success else math.inf
        if violation2 <= 1e-7 and gap2 < gap:
            u, violation, gap = u2, violation2, gap2
    if violation > 1e-7 or gap > 5e-5:
        raise RuntimeError(f'清算未达到精度要求，约束残差 {violation:.3g}，一阶差距 {gap:.3g}。')
    return u, {'constraint_violation': violation, 'clearing_gap': gap}


def signals(u, edges, buyers, providers, delta):
    q, p = np.zeros(len(edges)), np.zeros(len(edges))
    for i, agent in enumerate(buyers):
        positions = [e for e, edge in enumerate(edges) if edge[0] == i]
        q[positions] = (u[positions]+delta) * agent.marginal(u[positions])
    for j, agent in enumerate(providers):
        positions = [e for e, edge in enumerate(edges) if edge[1] == j]
        p[positions] = agent.marginal(u[positions])/(u[positions]+delta)
    return q, p


def transfers(u, q, p, edges, buyers, providers, settings):
    bid, ask = q/(u+settings.delta), p*(u+settings.delta)
    pay, reward = u*bid, u*ask
    buyer_payoffs = [agent.payoff(u[[e for e,(ii,_) in enumerate(edges) if ii==i]],
                                float(pay[[e for e,(ii,_) in enumerate(edges) if ii==i]].sum()))
                     for i,agent in enumerate(buyers)]
    provider_payoffs = [agent.payoff(u[[e for e,(_,jj) in enumerate(edges) if jj==j]],
                                    float(reward[[e for e,(_,jj) in enumerate(edges) if jj==j]].sum()))
                        for j,agent in enumerate(providers)]
    return {'bid':bid.tolist(), 'ask':ask.tolist(), 'payment':pay.tolist(), 'reward':reward.tolist(),
            'broker_surplus':float((pay-reward).sum()), 'buyer_payoffs':buyer_payoffs,
            'provider_payoffs':provider_payoffs,
            'welfare':float(sum(buyer_payoffs)+sum(provider_payoffs)+(pay-reward).sum())}


def run(demands, capacities, edges, buyers, providers, settings=None, initial_signals=None, state_valid=None):
    settings = settings or Settings()
    if len(buyers) != len(demands) or len(providers) != len(capacities):
        raise ValueError('本地估值代理数量与参与方数量不一致。')
    began = time.perf_counter()
    result = {'algorithm_id':ALGORITHM_ID, 'algorithm_version':ALGORITHM_VERSION,
              'settings':asdict(settings), 'provenance':provenance(), 'status':'NOT_CONVERGED', 'reason':'达到最大轮数。',
              'edges':[list(e) for e in edges], 'demands':list(map(float,demands)),
              'capacities':list(map(float,capacities)), 'trace':[], 'iterations':0}
    start = feasible_point(demands, capacities, edges)
    if start is None:
        result.update(status='PHYSICAL_INFEASIBLE', reason='当前候选网络无法满足全部最低需求。', elapsed=time.perf_counter()-began)
        return result
    # A strictly positive allocation at every edge makes zero marginal cost at
    # zero harmless for initialization, while the clearing still allows zeros.
    if initial_signals is None:
        q, p = signals(np.ones(len(edges)), edges, buyers, providers, settings.delta)
    else:
        q, p = map(lambda v: np.asarray(v, dtype=float), initial_signals)
        result['provenance']['initialization'] = 'caller-supplied signals; exact values in first trace row'
    if q.shape != (len(edges),) or p.shape != (len(edges),) or not np.isfinite(q).all() or not np.isfinite(p).all() or (q<=0).any() or (p<=0).any():
        result.update(status='INPUT_UNSUPPORTED',reason='初始买卖信号必须为有限正数，并覆盖全部候选关系。',elapsed=time.perf_counter()-began)
        return result
    for k in range(settings.max_iterations):
        if state_valid is not None and not state_valid():
            result.update(status='EPOCH_INVALIDATED', reason='计算期间业务快照已变化。')
            break
        if time.perf_counter()-began > settings.time_limit:
            result['reason'] = '达到运行时限，未确认收敛。'
            break
        try:
            u, diagnostics = clear(q,p,demands,capacities,edges,settings.delta,start)
        except RuntimeError as exc:
            result.update(status='SOLVER_FAILED', reason=str(exc))
            break
        target_q, target_p = signals(u,edges,buyers,providers,settings.delta)
        if not np.isfinite(target_q).all() or not np.isfinite(target_p).all() or (target_q<=0).any() or (target_p<=0).any():
            result.update(status='INPUT_UNSUPPORTED',reason='当前估值产生非正信号；请检查局部函数与零分配边的边际成本。')
            break
        residual = float(np.linalg.norm(target_q-q)+np.linalg.norm(target_p-p))
        settlement = transfers(u,q,p,edges,buyers,providers,settings)
        result['trace'].append({'iteration':k, 'q':q.tolist(), 'p':p.tolist(), 'x':u.tolist(), 'y':u.tolist(),
                                'fixed_point_residual':residual, 'signal_step':settings.rho*residual,
                                'welfare':settlement['welfare'], 'broker_surplus':settlement['broker_surplus'],
                                **diagnostics})
        result.update(iterations=k+1, allocation=u.tolist(), q=q.tolist(), p=p.tolist(), residual=residual,
                      settlement=settlement)
        if residual <= settings.tolerance:
            executed = u > 1e-9
            spread = np.asarray(settlement['bid'])-np.asarray(settlement['ask'])
            if (spread[executed] < -settings.settlement_tolerance).any() or settlement['broker_surplus'] < -settings.settlement_tolerance:
                result.update(status='SETTLEMENT_INFEASIBLE', reason='估值迭代已收敛，但边际支付未通过结算检查。')
            elif min(settlement['buyer_payoffs']+settlement['provider_payoffs']) < -settings.settlement_tolerance:
                result.update(status='SETTLEMENT_INFEASIBLE', reason='候选分配未通过参与方收益检查。')
            else:
                result.update(status='SUCCESS', reason='迭代达到数值容差，物理与结算检查通过。')
            break
        # Actual manuscript equation (37); do not replace with a centralized
        # welfare solve or with the separately proposed proximal variant.
        q = (1-settings.rho)*q + settings.rho*target_q
        p = (1-settings.rho)*p + settings.rho*target_p
        start = u
    if state_valid is not None and not state_valid():
        result.update(status='EPOCH_INVALIDATED', reason='结果产生时业务快照已变化。')
    result['elapsed'] = time.perf_counter()-began
    return result


def executable_allocation(result, buyers, providers):
    """Conservative 0.001-hour realization, followed by all original checks."""
    if result['status'] != 'SUCCESS':
        return None
    settings = Settings(**result['settings'])
    edges = result['edges']
    d,c,rows,cols,a,b = constraints(result['demands'],result['capacities'],edges)
    u = np.asarray(result['allocation'])
    low = np.floor(u*1000+1e-7)
    high = np.ceil(u*1000-1e-7)
    rounded = linprog(1-(u*1000-low)*1e-3, A_ub=a, b_ub=np.r_[-np.ceil(d*1000-1e-7),np.floor(c*1000+1e-7)],
                      bounds=list(zip(low,high)), integrality=np.ones(len(edges)), method='highs')
    if not rounded.success:
        result.update(status='QUANTIZATION_INFEASIBLE', reason='连续分配无法在当前记录精度下保持物理可行。')
        return None
    milli = np.rint(rounded.x).astype(int)
    uq = milli/1000.0
    if np.max(a@uq-b)>1e-9:
        result.update(status='QUANTIZATION_INFEASIBLE',reason='可记录数量未通过原始物理约束复核。')
        return None
    settlement = transfers(uq,np.asarray(result['q']),np.asarray(result['p']),edges,buyers,providers,settings)
    pay_cents = np.rint(np.asarray(settlement['payment'])*100).astype(int)
    reward_cents = np.rint(np.asarray(settlement['reward'])*100).astype(int)
    active = uq > 0
    spread = np.asarray(settlement['bid'])-np.asarray(settlement['ask'])
    row_payoffs = [agent.payoff(uq[[e for e,(ii,_) in enumerate(edges) if ii==i]],
                              float(pay_cents[[e for e,(ii,_) in enumerate(edges) if ii==i]].sum()/100))
                   for i,agent in enumerate(buyers)]
    col_payoffs = [agent.payoff(uq[[e for e,(_,jj) in enumerate(edges) if jj==j]],
                              float(reward_cents[[e for e,(_,jj) in enumerate(edges) if jj==j]].sum()/100))
                   for j,agent in enumerate(providers)]
    if (spread[active] < -settings.settlement_tolerance).any() or (pay_cents[active] < reward_cents[active]).any() or min(row_payoffs+col_payoffs) < -settings.settlement_tolerance:
        result.update(status='QUANTIZATION_INFEASIBLE', reason='数量或金额离散化后未通过结算/参与收益检查，未执行预留。')
        return None
    result['execution'] = {'quantity_milli':milli.tolist(), 'payment_cents':pay_cents.tolist(),
                           'reward_cents':reward_cents.tolist(), 'allocation_error':float(np.max(np.abs(uq-u))),
                           'broker_surplus_cents':int((pay_cents-reward_cents).sum()),
                           'buyer_payoffs':row_payoffs, 'provider_payoffs':col_payoffs}
    return result['execution']
