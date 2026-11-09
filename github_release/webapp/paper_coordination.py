"""Business adapter for manuscript clearing; additive persistence and local agents.

An epoch contains all pending demands for one capability/unit/exact window.
Factories have one role per epoch. The first adapter accepts one pool/one
demand per factory; the numerical engine itself supports arbitrary fixed Ω.
Only successful, revalidated, executable epochs can reserve capacity.
"""
import hashlib
import json
import math
from dataclasses import asdict

from webapp.db import get_db
from webapp import access
from webapp import platform_store as store
from webapp.local_valuation import LogBuyer, QuadraticProvider, ExponentialProvider
from webapp import paper_algorithm as algorithm

DEFAULT_PROFILE = {'alpha': 480.0, 'linear': 5.0, 'quadratic': 100.0}
MODEL_VERSION = 'synthetic-separable-log-quadratic-1'


def init_schema(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS valuation_profiles (
          org_id INTEGER PRIMARY KEY REFERENCES organizations(id),
          alpha REAL NOT NULL, linear REAL NOT NULL, quadratic REAL NOT NULL,
          version INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL,
          updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS coordination_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
          algorithm_version TEXT NOT NULL, status TEXT NOT NULL,
          snapshot_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL,
          result_json TEXT NOT NULL, created_by INTEGER, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS algorithm_comparisons (
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_by INTEGER NOT NULL,
          input_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL,
          result_json TEXT NOT NULL, paper_run_id INTEGER REFERENCES coordination_runs(id),
          created_at TEXT NOT NULL);
    ''')
    columns = {row['name'] for row in db.execute('PRAGMA table_info(plans)')}
    if 'coordination_run_id' not in columns:
        db.execute('ALTER TABLE plans ADD COLUMN coordination_run_id INTEGER REFERENCES coordination_runs(id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_plans_run ON plans(coordination_run_id)')
    db.commit()


def profile(org_id, materialize=False):
    db = get_db()
    row = db.execute('SELECT * FROM valuation_profiles WHERE org_id=?', (org_id,)).fetchone()
    if row is None and materialize:
        db.execute('''INSERT INTO valuation_profiles
            (org_id,alpha,linear,quadratic,version,source,updated_at)
            VALUES (?,?,?,?,1,'synthetic-default',?)''',
            (org_id, *DEFAULT_PROFILE.values(), store.now_iso()))
        row = db.execute('SELECT * FROM valuation_profiles WHERE org_id=?', (org_id,)).fetchone()
    return dict(row) if row else {**DEFAULT_PROFILE, 'org_id':org_id, 'version':0, 'source':'synthetic-default'}


def update_profile(membership, alpha, linear, quadratic):
    if not access.can(membership, 'valuation.write'):
        raise store.PermissionDenied('只有本企业业务人员可以修改本地估值参数。')
    try:
        alpha, linear, quadratic = float(alpha), float(linear), float(quadratic)
        LogBuyer(alpha)
        QuadraticProvider(linear,quadratic)
        if alpha > 1e6 or linear > 1e6 or quadratic > 1e6 or linear < 0.001:
            raise ValueError('参数上限为 1,000,000；本业务适配器要求线性成本至少 0.001。')
    except (TypeError,ValueError) as exc:
        raise store.PlatformError(str(exc))
    db = get_db()
    profile(membership['org_id'], True)
    db.execute('''UPDATE valuation_profiles SET alpha=?,linear=?,quadratic=?,version=version+1,
        source='synthetic-configured',updated_at=? WHERE org_id=?''',
        (alpha,linear,quadratic,store.now_iso(),membership['org_id']))
    # Coefficients are deliberately excluded from the shared audit payload.
    version = profile(membership['org_id'])['version']
    store.append_ledger(db,'VALUATION_UPDATED','organization',membership['org_id'],membership['org_id'],
                        {'org_id':membership['org_id'],'profile_version':version,'model_version':MODEL_VERSION})
    db.commit()


def _digest(value):
    return hashlib.sha256(store._canonical(value).encode()).hexdigest()


def collect(anchor_id, materialize=False):
    anchor = store.get_demand(anchor_id)
    if anchor is None or anchor['status'] not in ('OPEN','PLAN_READY'):
        raise store.ConflictError('需求已预留或关闭，不能加入本次协调。')
    db = get_db()
    demands = db.execute('''SELECT * FROM demands WHERE status IN ('OPEN','PLAN_READY')
        AND capability=? AND unit=? AND window_start=? AND window_end=? ORDER BY id''',
        (anchor['capability'],anchor['unit'],anchor['window_start'],anchor['window_end'])).fetchall()
    buyers = {d['org_id'] for d in demands}
    supplies = [s for s in db.execute('''SELECT * FROM resource_supplies WHERE status='ACTIVE'
        AND capability=? AND unit=? AND window_start<=? AND window_end>=? ORDER BY id''',
        (anchor['capability'],anchor['unit'],anchor['window_start'],anchor['window_end']))
        if s['org_id'] not in buyers and s['total_milli']-s['reserved_milli']-s['consumed_milli']>0]
    orgs = sorted(buyers | {s['org_id'] for s in supplies})
    profiles = {org_id:profile(org_id,materialize) for org_id in orgs}
    snapshot = {'anchor_id':anchor_id, 'data_source':'synthetic', 'model_version':MODEL_VERSION,
        'capability':anchor['capability'],'unit':anchor['unit'],
        'window_start':anchor['window_start'],'window_end':anchor['window_end'],
        'demands':[{k:d[k] for k in ('id','org_id','required_milli','site','quality','title')} for d in demands],
        'supplies':[{**{k:s[k] for k in ('id','org_id','site','version','window_start','window_end')},
                     'available_milli':s['total_milli']-s['reserved_milli']-s['consumed_milli']} for s in supplies],
        'profiles':[{'org_id':o,'version':profiles[o]['version'],'source':profiles[o]['source']} for o in orgs]}
    return snapshot,demands,supplies,profiles


def snapshot_valid(snapshot):
    try:
        return _digest(collect(snapshot['anchor_id'])[0]) == _digest(snapshot)
    except store.PlatformError:
        return False


def _save_run(kind,snapshot,result,membership):
    db = get_db()
    cur = db.execute('''INSERT INTO coordination_runs
        (kind,algorithm_version,status,snapshot_hash,snapshot_json,result_json,created_by,created_at)
        VALUES (?,?,?,?,?,?,?,?)''', (kind,algorithm.ALGORITHM_VERSION,result['status'],_digest(snapshot),
        store._canonical(snapshot),store._canonical(result),membership['user_id'],store.now_iso()))
    return cur.lastrowid


def _failure(status,reason):
    return {'algorithm_id':algorithm.ALGORITHM_ID,'algorithm_version':algorithm.ALGORITHM_VERSION,
            'status':status,'reason':reason,'iterations':0,'trace':[],'settings':asdict(algorithm.Settings()),
            'provenance':algorithm.provenance()}


def create_plan(anchor_id,membership):
    if not access.can(membership, 'coordination.write'):
        raise store.PermissionDenied('只有联盟协调员可以生成协调方案。')
    snapshot,demands,supplies,profiles = collect(anchor_id,True)
    db = get_db()
    db.commit()  # Release profile writes; live-state checks must see other writers.
    buyer_orgs = [d['org_id'] for d in demands]
    provider_orgs = [s['org_id'] for s in supplies]
    if len(set(buyer_orgs)) != len(buyer_orgs) or len(set(provider_orgs)) != len(provider_orgs):
        result = _failure('INPUT_UNSUPPORTED','同一共享窗口内，每家企业请合并为一条需求或一个供给池，避免重复计算企业收益与成本。')
    elif len(demands)>12 or len(supplies)>12 or len(demands)*len(supplies)>100:
        result = _failure('INPUT_UNSUPPORTED','当前交互式协调最多支持 12 家需求方、12 家供给方和 100 条候选关系。')
    elif not supplies:
        result = _failure('PHYSICAL_INFEASIBLE','当前没有符合能力、单位、时间和企业角色要求的可用供给。')
    else:
        buyers = [LogBuyer(profiles[o]['alpha']) for o in buyer_orgs]
        providers = [QuadraticProvider(profiles[o]['linear'],profiles[o]['quadratic']) for o in provider_orgs]
        edges = [(i,j) for i in range(len(demands)) for j in range(len(supplies))]
        result = algorithm.run([d['required_milli']/1000 for d in demands],
            [s['available_milli']/1000 for s in snapshot['supplies']],edges,buyers,providers,
            state_valid=lambda:snapshot_valid(snapshot))
        algorithm.executable_allocation(result,buyers,providers)
    try:
        db.execute('BEGIN IMMEDIATE')
        if not snapshot_valid(snapshot):
            result.update(status='EPOCH_INVALIDATED',reason='保存结果前，业务数据或本地估值版本发生变化。')
        run_id = _save_run('business',snapshot,result,membership)
        success = result['status']=='SUCCESS'
        # A failure has no effect on existing executable plans. A successful
        # replacement invalidates overlapping old epochs, including siblings.
        if success:
            ids = [d['id'] for d in demands]
            marks = ','.join('?' for _ in ids)
            old_runs = [r[0] for r in db.execute(f'''SELECT DISTINCT coordination_run_id FROM plans
                WHERE demand_id IN ({marks}) AND status='READY' AND coordination_run_id IS NOT NULL''',ids)]
            for old in old_runs:
                db.execute("UPDATE plans SET status='SUPERSEDED' WHERE coordination_run_id=? AND status='READY'",(old,))
                db.execute("UPDATE coordination_runs SET status='SUPERSEDED' WHERE id=?",(old,))
            db.execute(f"UPDATE plans SET status='SUPERSEDED' WHERE demand_id IN ({marks}) AND status='READY'",ids)
        created = {}
        for i,demand in enumerate(demands):
            if not success and demand['id'] != anchor_id:
                continue
            allocations = []
            if success:
                for e,(ii,j) in enumerate(result['edges']):
                    if ii != i or result['execution']['quantity_milli'][e]<=0:
                        continue
                    s = supplies[j]
                    allocations.append({'supply_id':s['id'],'supply_version':s['version'],
                        'supplier_org_id':s['org_id'],'supplier_site':s['site'],
                        'qty_milli':result['execution']['quantity_milli'][e],
                        'continuous_quantity':result['allocation'][e],
                        'unit_price_cents':round(result['execution']['payment_cents'][e]*1000/result['execution']['quantity_milli'][e]),
                        'payment_cents':result['execution']['payment_cents'][e],
                        'reward_cents':result['execution']['reward_cents'][e]})
            pay=sum(a['payment_cents'] for a in allocations)
            reward=sum(a['reward_cents'] for a in allocations)
            cur=db.execute('''INSERT INTO plans
                (demand_id,strategy,algorithm_version,status,snapshot_hash,allocation_json,
                 proposed_payment_cents,proposed_reward_cents,broker_surplus_cents,settlement_status,
                 failure_reason,created_by,created_at,coordination_run_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (demand['id'],algorithm.ALGORITHM_ID,algorithm.ALGORITHM_VERSION,'READY' if success else result['status'],
                 _digest(snapshot),store._canonical(allocations),pay,reward,pay-reward,
                 'CHECKED' if success else 'REJECTED',None if success else result['reason'],
                 membership['user_id'],store.now_iso(),run_id))
            created[demand['id']]=cur.lastrowid
            if success:
                db.execute("UPDATE demands SET status='PLAN_READY' WHERE id=?",(demand['id'],))
            store.append_ledger(db,'PLAN_GENERATED' if success else 'PLAN_FAILED','plan',cur.lastrowid,
                membership['org_id'],{'demand_id':demand['id'],'coordination_run_id':run_id,
                'algorithm_version':algorithm.ALGORITHM_VERSION,'status':result['status'],
                'snapshot_hash':_digest(snapshot),'result_hash':_digest(result),
                'participant_org_ids':sorted(set([demand['org_id']]+provider_orgs))})
        db.commit()
        return created[anchor_id]
    except Exception:
        db.rollback()
        raise


def get_run(run_id):
    row = get_db().execute('SELECT * FROM coordination_runs WHERE id=?',(run_id,)).fetchone()
    if row is None:
        return None
    return {**dict(row),'snapshot':json.loads(row['snapshot_json']),'result':json.loads(row['result_json'])}


def reserve_epoch(plan_id,membership):
    db=get_db()
    plan=store.get_plan(plan_id)
    run_id=plan['coordination_run_id']
    try:
        db.execute('BEGIN IMMEDIATE')
        run=get_run(run_id)
        plans=db.execute('SELECT id FROM plans WHERE coordination_run_id=? ORDER BY id',(run_id,)).fetchall()
        linked=[store.get_plan(p['id']) for p in plans]
        if run['status']!='SUCCESS' or not linked or any(p['status']!='READY' for p in linked):
            raise store.ConflictError('同轮方案已被预留或替换。')
        if not snapshot_valid(run['snapshot']):
            raise store.ConflictError('供需或本地估值版本已变化，请重新生成整轮方案。')
        # All snapshot checks precede mutation; a supplier shared by multiple
        # buyers is updated once by the aggregate epoch reservation.
        totals={}
        for p in linked:
            for a in store._plan_allocations(p):
                totals[a['supply_id']]=totals.get(a['supply_id'],0)+a['qty_milli']
        for sid,qty in totals.items():
            s=db.execute('SELECT * FROM resource_supplies WHERE id=?',(sid,)).fetchone()
            if qty>s['total_milli']-s['reserved_milli']-s['consumed_milli']:
                raise store.ConflictError('同轮总预留量超过可用容量。')
        for sid,qty in totals.items():
            db.execute('UPDATE resource_supplies SET reserved_milli=reserved_milli+?,version=version+1 WHERE id=?',(qty,sid))
        ids={}
        for p in linked:
            ids[p['id']]=store._insert_commitment(db,p,store._plan_allocations(p),membership)
        db.execute("UPDATE coordination_runs SET status='RESERVED' WHERE id=?",(run_id,))
        db.commit()
        return ids[plan_id]
    except store.PlatformError:
        db.rollback()
        db.execute("UPDATE plans SET status='EPOCH_INVALIDATED' WHERE coordination_run_id=? AND status='READY'",(run_id,))
        db.execute("UPDATE coordination_runs SET status='EPOCH_INVALIDATED' WHERE id=? AND status='SUCCESS'",(run_id,))
        db.commit()
        raise
    except Exception:
        db.rollback()
        raise


TRIALS = {
    'standard':{'label':'多企业共享','description':'两家需求企业共同竞争两家供给企业的可用容量。'},
    'scarce':{'label':'容量紧张','description':'供给容量接近最低需求，观察多家需求方的分配与收支。'},
    'minimum':{'label':'高最低需求','description':'保留最低需求约束，检查收支不足时能否正确拒绝结算。'},
    'curvature':{'label':'成本快速变化','description':'使用指数成本，比较不同更新步长下的迭代行为。'},
    'filtered':{'label':'候选筛选对比','description':'两条业务允许的候选关系与两条被禁止的交叉关系；用于比较是否筛选候选。'},
    'stale':{'label':'过期状态','description':'输入快照已被标记为过期；用于比较是否执行状态核验。'},
}


def trial_case(case):
    """One source of synthetic inputs for standalone and comparison runs."""
    if case not in TRIALS:
        raise store.PlatformError('请选择已有的模拟场景。')
    if case in ('standard','scarce'):
        d,c,edges=[1.,1.],[5.,5.] if case=='standard' else [1.2,1.2],[(0,0),(0,1),(1,0),(1,1)]
        buyers,providers=[LogBuyer(480),LogBuyer(520)],[QuadraticProvider(5,100),QuadraticProvider(8,110)]
    elif case=='minimum':
        d,c,edges=[2.],[10.],[(0,0)]
        buyers,providers=[LogBuyer(4)],[QuadraticProvider(0,1)]
    elif case=='filtered':
        # Two business-allowed relationships; the other two are cross-plant
        # relations that the capability/site filter must exclude.
        d,c,edges=[1.,1.],[1.,1.],[(0,0),(1,1)]
        buyers,providers=[LogBuyer(480),LogBuyer(520)],[QuadraticProvider(5,100),QuadraticProvider(8,110)]
    elif case=='stale':
        d,c,edges=[1.],[5.],[(0,0)]
        buyers,providers=[LogBuyer(480)],[QuadraticProvider(5,100)]
    else:
        d,c,edges=[.01],[3.],[(0,0)]
        buyers,providers=[LogBuyer(2.2)],[ExponentialProvider()]
    all_edges=[(0,0),(0,1),(1,0),(1,1)] if case=='filtered' else list(edges)
    invalid_edges=[edge for edge in all_edges if edge not in set(edges)]
    snapshot={'case':case,'label':TRIALS[case]['label'],'data_source':'synthetic-controlled',
              'model_version':MODEL_VERSION,'demands':d,'capacities':c,'edges':edges,
              'all_edges':all_edges,'invalid_edges':invalid_edges,
              'state_valid': case!='stale',
              'buyers':[asdict(b) for b in buyers],'providers':[asdict(p) for p in providers],
              'provider_model':'exponential' if case=='curvature' else 'quadratic','unit':'机时'}
    return d,c,edges,buyers,providers,snapshot


def run_trial(case,membership,settings=None):
    if not access.can(membership, 'algorithms.run'):
        raise store.PermissionDenied('当前岗位不能运行联盟算法工具。')
    d,c,edges,buyers,providers,snapshot=trial_case(case)
    settings=settings or algorithm.Settings(rho=1.0 if case=='curvature' else .2,max_iterations=120 if case=='curvature' else 400)
    state_valid = (lambda: bool(snapshot.get('state_valid', True))) if 'state_valid' in snapshot else None
    result=algorithm.run(d,c,edges,buyers,providers,settings,state_valid=state_valid)
    algorithm.executable_allocation(result,buyers,providers)
    snapshot['settings']=asdict(settings)
    run_id=_save_run('trial',snapshot,result,membership)
    get_db().commit()
    return run_id


def trial_runs(membership):
    return get_db().execute("SELECT * FROM coordination_runs WHERE kind='trial' AND created_by=? ORDER BY id DESC LIMIT 30",
                            (membership['user_id'],)).fetchall()


def can_view_run(run,membership):
    if run is None:
        return False
    # Business trace contains all buyers' marginal messages; only alliance
    # roles see it. Factories retain their scoped plan and own profiles.
    return (run['kind']=='trial' and access.can(membership,'algorithms.read') and run['created_by']==membership['user_id']) or (run['kind']=='business' and store.is_alliance_member(membership))
