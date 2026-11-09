"""Continuous-model demonstrations on identical inputs, separate from legacy CDA.

The planner has no payment mechanism. Fixed-price allocation has its own rule.
Only the paper method calls the manuscript feedback/clearing engine. None of
these demonstrations reserve business resources or imply a real payment.
"""
from dataclasses import asdict
from pathlib import Path
import hashlib
import json
import math
import time

import numpy as np
from scipy.optimize import linprog, minimize

from webapp.db import get_db
from webapp import paper_algorithm as paper
from webapp import paper_coordination as coordination
from webapp import platform_store as store
from webapp import access

VERSION='continuous-comparison-1.2'
METHODS={
    'paper':{'label':'迭代双向拍卖','version':paper.ALGORITHM_VERSION,
             'group':'proposed','description':'论文机制：边际反馈、清算与松弛更新，保留未收敛和结算失败。'},
    'fixed':{'label':'规则匹配 · 固定参考价','version':'fixed-price-greedy-1.0',
             'group':'baseline','description':'按需求顺序与剩余容量首适配；最低需求满足后，只在买方价值高于参考价且供给成本低于参考价时追加。'},
    'one_sided':{'label':'单边供给拍卖','version':'one-sided-ask-1.0',
                 'group':'baseline','description':'先满足最低需求；剩余容量只按供给边际报价与买方保留价值的本地剩余追加，不做全局清算。'},
    'planner':{'label':'集中式福利基准','version':'central-welfare-1.0',
               'group':'benchmark','description':'同一物理约束下直接求解社会福利上界；不定义支付。'},
    'paper_no_iteration':{'label':'消融 · 无迭代','version':'paper-single-shot-1.0',
                          'group':'ablation','description':'只用初始边际信号做一次清算，不进行估值反馈迭代。'},
    'paper_no_filter':{'label':'消融 · 无候选筛选','version':'paper-relaxed-edges-1.0',
                       'group':'ablation','description':'放宽候选关系，允许全部供给—需求对进入清算；检查是否使用了业务禁止的关系。'},
    'paper_no_state':{'label':'消融 · 无状态核验','version':'paper-no-state-check-1.0',
                      'group':'ablation','description':'跳过业务快照有效性检查；在过期/失配场景中观察错误放行。'},
}


def welfare(u,edges,buyers,providers):
    return float(sum(agent.payoff(u[[e for e,(ii,_) in enumerate(edges) if ii==i]],0)
                     for i,agent in enumerate(buyers))+
                 sum(agent.payoff(u[[e for e,(_,jj) in enumerate(edges) if jj==j]],0)
                     for j,agent in enumerate(providers)))


def welfare_gradient(u,edges,buyers,providers):
    gradient=np.zeros(len(edges))
    for i,agent in enumerate(buyers):
        indices=[e for e,(ii,_) in enumerate(edges) if ii==i]
        gradient[indices]+=agent.marginal(u[indices])
    for j,agent in enumerate(providers):
        indices=[e for e,(_,jj) in enumerate(edges) if jj==j]
        gradient[indices]-=agent.marginal(u[indices])
    return gradient


def summarize(method,status,reason,u,d,c,edges,buyers,providers,elapsed,iterations=0,**extra):
    result={'method_id':method,**METHODS[method],'status':status,'reason':reason,
            'elapsed':elapsed,'iterations':iterations,'allocation':None,
            'welfare':None,'total_quantity':None,'payment':None,'reward':None,
            'broker_surplus':None,'buyer_payoffs':None,'provider_payoffs':None,
            'physical_feasible':False,**extra}
    if u is not None:
        u=np.asarray(u,dtype=float)
        _,_,rows,cols,a,b=paper.constraints(d,c,edges)
        violation=max(0.,float(np.max(a@u-b)),float(-u.min()))
        result.update(allocation=u.tolist(),welfare=welfare(u,edges,buyers,providers),
            total_quantity=float(u.sum()),demand_totals=(rows@u).tolist(),
            supply_totals=(cols@u).tolist(),constraint_violation=violation,
            physical_feasible=violation<=1e-7)
    return result


def centralized(d,c,edges,buyers,providers):
    began=time.perf_counter()
    start=paper.feasible_point(d,c,edges)
    if start is None:
        return summarize('planner','PHYSICAL_INFEASIBLE','最低需求无法在候选网络内满足。',None,d,c,edges,buyers,providers,time.perf_counter()-began)
    _,_,_,_,a,b=paper.constraints(d,c,edges)
    scale=max(1.,float(np.max(np.abs(welfare_gradient(start,edges,buyers,providers)))))
    result=minimize(lambda u:-welfare(u,edges,buyers,providers)/scale,start,
        jac=lambda u:-welfare_gradient(u,edges,buyers,providers)/scale,
        method='SLSQP',bounds=[(0,float(c[j])) for _,j in edges],
        constraints={'type':'ineq','fun':lambda u:b-a@u,'jac':lambda u:-a},
        options={'maxiter':700,'ftol':1e-13})
    u=np.maximum(result.x,0)
    gradient=welfare_gradient(u,edges,buyers,providers)
    certificate=linprog(-gradient,A_ub=a,b_ub=b,bounds=(0,None),method='highs')
    gap=max(0.,float(gradient@(certificate.x-u))) if certificate.success else None
    iterations=int(result.nit)
    if gap is None or gap>5e-5:
        # Scaled SLSQP can stop on objective change while a small gradient
        # error still exceeds the absolute certificate. Refine the SAME
        # welfare problem without scaling, then keep only an improved,
        # independently checked feasible point.
        refined=minimize(lambda v:-welfare(v,edges,buyers,providers),u,
            jac=lambda v:-welfare_gradient(v,edges,buyers,providers),
            method='SLSQP',bounds=[(0,float(c[j])) for _,j in edges],
            constraints={'type':'ineq','fun':lambda v:b-a@v,'jac':lambda v:-a},
            options={'maxiter':200,'ftol':1e-13})
        v=np.maximum(refined.x,0)
        grad=welfare_gradient(v,edges,buyers,providers)
        check=linprog(-grad,A_ub=a,b_ub=b,bounds=(0,None),method='highs')
        new_gap=max(0.,float(grad@(check.x-v))) if check.success else None
        if np.max(a@v-b)<=1e-7 and new_gap is not None and (gap is None or new_gap<gap):
            u,gap=v,new_gap
        iterations+=int(refined.nit)
    valid=max(0.,float(np.max(a@u-b)))<=1e-7 and gap is not None and gap<=5e-5
    return summarize('planner','BENCHMARK_READY' if valid else 'SOLVER_FAILED',
        '同一物理约束下直接求解社会福利；不定义付款或奖励。' if valid else '集中式基准未达到求解精度。',
        u,d,c,edges,buyers,providers,time.perf_counter()-began,iterations,clearing_gap=gap,
        payment_rule='none')


def _fill_local_extra(u, remaining, edges, buyers, providers, mode, price=0.0, step=0.05):
    """Let a baseline use spare capacity when its own local rule says it pays.

    This avoids comparing the proposed method against a baseline that is
    artificially frozen at the minimum demand. It is still a myopic rule: the
    method chooses one edge at a time from current local marginals.
    """
    while True:
        best=None; best_margin=1e-9
        for e,(i,j) in enumerate(edges):
            if remaining[j] <= 1e-9:
                continue
            q=u[e]
            b=float(buyers[i].marginal(np.array([q]))[0])
            a=float(providers[j].marginal(np.array([q]))[0])
            if mode == 'fixed':
                if b <= price or a >= price:
                    continue
                margin=min(b-price, price-a)
            else:
                if b <= a:
                    continue
                margin=b-a
            if margin > best_margin:
                best_margin=margin; best=(e,j)
        if best is None:
            break
        e,j=best
        qty=min(step,remaining[j])
        u[e]+=qty; remaining[j]-=qty


def fixed_price(d,c,edges,buyers,providers,price):
    began=time.perf_counter()
    if paper.feasible_point(d,c,edges) is None:
        return summarize('fixed','PHYSICAL_INFEASIBLE','最低需求无法在候选网络内满足。',None,d,c,edges,buyers,providers,time.perf_counter()-began)
    u=np.zeros(len(edges))
    remaining=np.asarray(c,dtype=float).copy()
    for i,required in enumerate(d):
        needed=required
        candidates=sorted([e for e,(ii,_) in enumerate(edges) if ii==i],key=lambda e:(-remaining[edges[e][1]],edges[e][1]))
        for e in candidates:
            j=edges[e][1]
            qty=min(needed,remaining[j])
            u[e]=qty
            needed-=qty
            remaining[j]-=qty
            if needed<=1e-9:
                break
        if needed>1e-7:
            return summarize('fixed','ALLOCATION_FAILED','顺序分配未能满足全部最低需求。',u,d,c,edges,buyers,providers,time.perf_counter()-began)
    _fill_local_extra(u,remaining,edges,buyers,providers,'fixed',price)
    pay=u*price
    buyer_payoffs=[agent.payoff(u[[e for e,(ii,_) in enumerate(edges) if ii==i]],
        float(pay[[e for e,(ii,_) in enumerate(edges) if ii==i]].sum())) for i,agent in enumerate(buyers)]
    provider_payoffs=[agent.payoff(u[[e for e,(_,jj) in enumerate(edges) if jj==j]],
        float(pay[[e for e,(_,jj) in enumerate(edges) if jj==j]].sum())) for j,agent in enumerate(providers)]
    valid=min(buyer_payoffs+provider_payoffs)>=-2e-5
    return summarize('fixed','SUCCESS' if valid else 'SETTLEMENT_INFEASIBLE',
        '按需求顺序和剩余容量分配最低量；买卖双方按同一参考价记账。' if valid else '固定参考价方案有参与方收益为负。',
        u,d,c,edges,buyers,providers,time.perf_counter()-began,1,
        payment=float(pay.sum()),reward=float(pay.sum()),broker_surplus=0.,
        buyer_payoffs=buyer_payoffs,provider_payoffs=provider_payoffs,
        payment_rule='fixed-price',reference_price=price)




def one_sided(d,c,edges,buyers,providers):
    """Supply-side one-sided auction: cheapest asks fill minimum demand only."""
    began=time.perf_counter()
    if paper.feasible_point(d,c,edges) is None:
        return summarize('one_sided','PHYSICAL_INFEASIBLE','最低需求无法在候选网络内满足。',None,d,c,edges,buyers,providers,time.perf_counter()-began)
    u=np.zeros(len(edges)); remaining=np.asarray(c,dtype=float).copy()
    # Ask at zero is the supplier's minimum marginal cost. The auction does
    # not use buyer values, so it can be welfare-poor; that is the baseline.
    asks=np.array([providers[j].marginal(np.array([0.0]))[0] for _,j in edges])
    for i,required in enumerate(d):
        needed=required
        candidates=sorted([e for e,(ii,_) in enumerate(edges) if ii==i],
                          key=lambda e:(asks[e],edges[e][1]))
        for e in candidates:
            j=edges[e][1]
            qty=min(needed,remaining[j])
            u[e]=qty; needed-=qty; remaining[j]-=qty
            if needed<=1e-9: break
        if needed>1e-7:
            return summarize('one_sided','ALLOCATION_FAILED','单边拍卖未能满足全部最低需求。',u,d,c,edges,buyers,providers,time.perf_counter()-began)
    _fill_local_extra(u,remaining,edges,buyers,providers,'one_sided')
    ask_edges=np.array([providers[j].marginal(np.array([u[e]]))[0] for e,(_,j) in enumerate(edges)])
    pay=u*ask_edges
    buyer_payoffs=[agent.payoff(u[[e for e,(ii,_) in enumerate(edges) if ii==i]],
        float(pay[[e for e,(ii,_) in enumerate(edges) if ii==i]].sum())) for i,agent in enumerate(buyers)]
    provider_payoffs=[agent.payoff(u[[e for e,(_,jj) in enumerate(edges) if jj==j]],
        float(pay[[e for e,(_,jj) in enumerate(edges) if jj==j]].sum())) for j,agent in enumerate(providers)]
    valid=min(buyer_payoffs+provider_payoffs)>=-2e-5
    return summarize('one_sided','SUCCESS' if valid else 'SETTLEMENT_INFEASIBLE',
        '只按供给边际报价最低优先分配最低需求。' if valid else '单边报价导致参与方收益为负。',
        u,d,c,edges,buyers,providers,time.perf_counter()-began,1,
        payment=float(pay.sum()),reward=float(pay.sum()),broker_surplus=0.,
        buyer_payoffs=buyer_payoffs,provider_payoffs=provider_payoffs,
        payment_rule='one-sided-ask',reference_price=None)


def paper_single_shot(d,c,edges,buyers,providers,settings,state_valid=None):
    """Ablation: one clearing at the initial marginal signals, no feedback."""
    began=time.perf_counter()
    if paper.feasible_point(d,c,edges) is None:
        return summarize('paper_no_iteration','PHYSICAL_INFEASIBLE','最低需求无法在候选网络内满足。',None,d,c,edges,buyers,providers,time.perf_counter()-began)
    q,p=paper.signals(np.ones(len(edges)),edges,buyers,providers,settings.delta)
    try:
        u,diagnostics=paper.clear(q,p,d,c,edges,settings.delta)
    except RuntimeError as exc:
        return summarize('paper_no_iteration','SOLVER_FAILED',str(exc),None,d,c,edges,buyers,providers,time.perf_counter()-began)
    settlement=paper.transfers(u,q,p,edges,buyers,providers,settings)
    status='SINGLE_SHOT'; reason='只使用初始信号做一次清算，未执行估值反馈迭代。'
    executed=u>1e-9; spread=np.asarray(settlement['bid'])-np.asarray(settlement['ask'])
    if (spread[executed] < -settings.settlement_tolerance).any() or settlement['broker_surplus'] < -settings.settlement_tolerance:
        status='SETTLEMENT_INFEASIBLE'; reason='单轮清算的边际支付未通过结算检查。'
    elif min(settlement['buyer_payoffs']+settlement['provider_payoffs']) < -settings.settlement_tolerance:
        status='SETTLEMENT_INFEASIBLE'; reason='单轮清算未通过参与方收益检查。'
    if state_valid is not None and not state_valid():
        status='EPOCH_INVALIDATED'; reason='结果产生时业务快照已变化。'
    return summarize('paper_no_iteration',status,reason,u,d,c,edges,buyers,providers,
        time.perf_counter()-began,1,payment=sum(settlement['payment']),reward=sum(settlement['reward']),
        broker_surplus=settlement['broker_surplus'],buyer_payoffs=settlement['buyer_payoffs'],
        provider_payoffs=settlement['provider_payoffs'],residual=None,
        payment_rule='paper-single-shot',clearing_gap=diagnostics.get('clearing_gap'),
        constraint_violation=diagnostics.get('constraint_violation'),state_verified=True)


def compare(case,settings=None,price=120.,methods=None):
    settings=settings or paper.Settings()
    methods=list(methods) if methods is not None else list(METHODS)
    if not methods or len(set(methods))!=len(methods) or any(m not in METHODS for m in methods):
        raise store.PlatformError('请选择已有的对比方法。')
    try:
        price=float(price)
    except (TypeError,ValueError):
        raise store.PlatformError('请输入数值参考价。')
    if not math.isfinite(price) or not 0<=price<=1e6:
        raise store.PlatformError('参考价应在 0 到 1,000,000 之间。')
    d,c,edges,buyers,providers,snapshot=coordination.trial_case(case)
    all_edges=[tuple(edge) for edge in snapshot.get('all_edges', edges)]
    valid_edges={tuple(edge) for edge in edges}
    state_valid=(lambda: bool(snapshot.get('state_valid', True)))
    input_hash=coordination._digest(snapshot)
    outcomes=[]
    raw_paper=None
    for method in methods:
        if method=='paper':
            raw_paper=paper.run(d,c,edges,buyers,providers,settings,state_valid=state_valid)
            settlement=raw_paper.get('settlement',{})
            row=summarize('paper',raw_paper['status'],raw_paper['reason'],raw_paper.get('allocation'),
                d,c,edges,buyers,providers,raw_paper['elapsed'],raw_paper['iterations'],
                payment=sum(settlement['payment']) if settlement else None,
                reward=sum(settlement['reward']) if settlement else None,
                broker_surplus=settlement.get('broker_surplus'),
                buyer_payoffs=settlement.get('buyer_payoffs'),provider_payoffs=settlement.get('provider_payoffs'),
                residual=raw_paper.get('residual'),payment_rule='paper-original',
                state_verified=bool(snapshot.get('state_valid', True)))
        elif method=='paper_no_iteration':
            row=paper_single_shot(d,c,edges,buyers,providers,settings,state_valid=state_valid)
            row['state_verified']=bool(snapshot.get('state_valid', True))
        elif method=='paper_no_filter':
            raw=paper.run(d,c,all_edges,buyers,providers,settings,state_valid=state_valid)
            settlement=raw.get('settlement',{})
            row=summarize('paper_no_filter',raw['status'],raw['reason'],raw.get('allocation'),
                d,c,all_edges,buyers,providers,raw['elapsed'],raw['iterations'],
                payment=sum(settlement['payment']) if settlement else None,
                reward=sum(settlement['reward']) if settlement else None,
                broker_surplus=settlement.get('broker_surplus'),
                buyer_payoffs=settlement.get('buyer_payoffs'),provider_payoffs=settlement.get('provider_payoffs'),
                residual=raw.get('residual'),payment_rule='paper-relaxed-edges',
                state_verified=bool(snapshot.get('state_valid', True)),filter_violation=0.0)
            if row.get('allocation'):
                used=sum(row['allocation'][e] for e,edge in enumerate(all_edges) if edge not in valid_edges and row['allocation'][e]>1e-9)
                row['filter_violation']=float(used)
                if used>1e-9:
                    row['status']='FILTER_VIOLATION'
                    row['reason']='放宽候选集合后使用了业务禁止的关系（%.6f 机时）。'%used
            if not snapshot.get('invalid_edges'):
                row['applicability']='本场景没有被候选筛选排除的关系，消融与论文方法同解。'
        elif method=='paper_no_state':
            raw=paper.run(d,c,edges,buyers,providers,settings,state_valid=None)
            settlement=raw.get('settlement',{})
            row=summarize('paper_no_state',raw['status'],raw['reason'],raw.get('allocation'),
                d,c,edges,buyers,providers,raw['elapsed'],raw['iterations'],
                payment=sum(settlement['payment']) if settlement else None,
                reward=sum(settlement['reward']) if settlement else None,
                broker_surplus=settlement.get('broker_surplus'),
                buyer_payoffs=settlement.get('buyer_payoffs'),provider_payoffs=settlement.get('provider_payoffs'),
                residual=raw.get('residual'),payment_rule='paper-no-state-check',
                state_verified=False)
            if not snapshot.get('state_valid', True) and row['physical_feasible']:
                row['status']='UNVERIFIED_STATE'
                row['reason']='跳过状态核验；输入快照已过期/失配，结果不能作为可执行候选。'
            elif snapshot.get('state_valid', True):
                row['applicability']='本场景状态有效，消融结果与论文方法同解。'
        elif method=='fixed':
            row=fixed_price(d,c,edges,buyers,providers,price)
        elif method=='one_sided':
            row=one_sided(d,c,edges,buyers,providers)
        else:
            row=centralized(d,c,edges,buyers,providers)
        row['input_hash']=input_hash
        row.setdefault('state_verified', None)
        outcomes.append(row)
    # Welfare is displayed only for physically feasible candidates that did
    # not fail a filter/state/solver check. Failed or stale results remain in
    # the table with their explicit status.
    blocked={'SOLVER_FAILED','ALLOCATION_FAILED','PHYSICAL_INFEASIBLE','EPOCH_INVALIDATED',
             'FILTER_VIOLATION','UNVERIFIED_STATE'}
    for row in outcomes:
        row['welfare_comparable']=row['physical_feasible'] and row['status'] not in blocked
    result={'version':VERSION,'input_hash':input_hash,'outcomes':outcomes,
            'settings':asdict(settings),'reference_price':price,'stage':'continuous-only',
            'provenance':{**paper.provenance(),
                'comparison_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}}
    return snapshot,result,raw_paper


def save_comparison(case,membership,settings=None,price=120.,methods=None):
    if not access.can(membership, 'algorithms.run'):
        raise store.PermissionDenied('当前岗位不能运行联盟算法工具。')
    snapshot,result,raw_paper=compare(case,settings,price,methods)
    db=get_db()
    try:
        paper_run_id=None
        if raw_paper is not None:
            paper_snapshot={**snapshot,'settings':result['settings'],'purpose':'continuous-comparison'}
            paper_run_id=coordination._save_run('trial',paper_snapshot,raw_paper,membership)
        cur=db.execute('''INSERT INTO algorithm_comparisons
            (created_by,input_hash,snapshot_json,result_json,paper_run_id,created_at) VALUES (?,?,?,?,?,?)''',
            (membership['user_id'],result['input_hash'],store._canonical(snapshot),store._canonical(result),paper_run_id,store.now_iso()))
        db.commit()
        return cur.lastrowid
    except Exception:
        db.rollback()
        raise


def get_comparison(comparison_id,membership):
    row=get_db().execute('SELECT * FROM algorithm_comparisons WHERE id=? AND created_by=?',
                         (comparison_id,membership['user_id'])).fetchone()
    if row is None:
        return None
    return {**dict(row),'snapshot':json.loads(row['snapshot_json']),'result':json.loads(row['result_json'])}


def history(membership):
    return get_db().execute('SELECT * FROM algorithm_comparisons WHERE created_by=? ORDER BY id DESC LIMIT 30',
                            (membership['user_id'],)).fetchall()
