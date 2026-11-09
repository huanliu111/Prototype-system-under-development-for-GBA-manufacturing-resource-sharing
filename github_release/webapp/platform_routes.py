"""HTTP routes for the multi-factory manufacturing resource coordination slice."""
from __future__ import annotations

import json
import csv
import io
import math
from datetime import datetime, timedelta
from pathlib import Path

from flask import (
    Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request,
    send_from_directory, url_for,
)

from webapp import platform_store as store
from webapp.db import get_db
from webapp import paper_coordination as coordination
from webapp.paper_algorithm import Settings
from webapp import algorithm_comparison as comparison
from webapp import access
from webapp.scenarios import get_preset
from webapp.simulation import run_simulation
from webapp.charts import generate_charts

bp = Blueprint("platform", __name__, url_prefix="")


@bp.before_request
def require_login():
    if getattr(g, "user", None) is None:
        return redirect(url_for("login", next=request.path))


def _membership():
    membership = getattr(g, "membership", None)
    if membership is None:
        abort(403)
    return membership


def _coordinator():
    membership = _membership()
    if not access.can(membership, 'coordination.write'):
        abort(403)
    return membership


def _parse_window(form, prefix=""):
    start = (form.get(prefix + "window_start") or "").strip()
    end = (form.get(prefix + "window_end") or "").strip()
    if not start or not end:
        raise store.PlatformError("请填写共享开始和结束时间。")
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        raise store.PlatformError("时间格式不正确。")
    if end_dt <= start_dt:
        raise store.PlatformError("结束时间必须晚于开始时间。")
    return start_dt.isoformat(timespec="minutes"), end_dt.isoformat(timespec="minutes"), start_dt, end_dt


def _default_window():
    start = (datetime.now() + timedelta(days=2)).replace(hour=8, minute=0, second=0, microsecond=0)
    end = start.replace(hour=16)
    return start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")


def _hours(value, label="数量"):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise store.PlatformError("%s必须是数字。" % label)
    if not math.isfinite(number) or number <= 0:
        raise store.PlatformError("%s必须大于 0。" % label)
    return number


@bp.get("/workspace")
def workspace():
    return redirect(url_for(access.home_endpoint(_membership())))


@bp.get('/workspace/enterprise')
def enterprise_workspace():
    return _operating_workspace()


@bp.get('/workspace/coordination')
def coordinator_workspace():
    return _operating_workspace()


@bp.get('/workspace/administration')
def administration_workspace():
    return _operating_workspace()


@bp.get('/workspace/audit')
def auditor_workspace():
    membership = _membership()
    valid, bad_id = store.verify_ledger(get_db())
    return render_template('platform/audit_workspace.html',
        organization=store.get_org(membership['org_id']),
        commitments=store.list_commitments(membership, limit=8),
        events=store.ledger_events_for(membership, limit=8),
        valid=valid, bad_id=bad_id)


def _operating_workspace():
    membership = _membership()
    stats = store.platform_stats(membership)
    supplies = store.list_supplies(membership, limit=6)
    demands = store.list_demands(membership, limit=6)
    plans = store.list_plans(membership, limit=6)
    all_commitments = store.list_commitments(membership, limit=20)
    commitments = [item for item in all_commitments
                   if item["status"] in ("RESERVED", "CONFIRMED", "EXECUTED", "ACCEPTED")][:6]
    completed_commitments = [item for item in all_commitments
                             if item["status"] in ("ACCEPTED", "ACCOUNTED")][:4]
    events = store.ledger_summary(membership, limit=6)
    return render_template(
        "platform/workspace.html",
        stats=stats,
        supplies=supplies,
        demands=demands,
        plans=plans,
        commitments=commitments,
        completed_commitments=completed_commitments,
        events=events,
        organization=store.get_org(membership["org_id"]),
    )


@bp.route("/resources", methods=["GET", "POST"])
def resources():
    membership = _membership()
    if request.method == "POST":
        try:
            start_s, end_s, _, _ = _parse_window(request.form)
            supply_id = store.create_supply(
                membership,
                site=request.form.get("site", "").strip(),
                capability=request.form.get("capability", "").strip(),
                window_start=start_s,
                window_end=end_s,
                hours=_hours(request.form.get("hours"), "可共享能力"),
                unit=request.form.get("unit", "机时").strip() or "机时",
                note=request.form.get("note", "").strip(),
            )
            flash("资源供给已发布，并写入演示审计账本（#%s）。" % supply_id, "success")
            return redirect(url_for("platform.resources"))
        except store.PermissionDenied:
            abort(403)
        except store.PlatformError as exc:
            flash(str(exc), "error")
    selected_capability = (request.args.get("capability") or "").strip()
    supplies = store.list_supplies(membership, capability=selected_capability or None)
    catalog = get_db().execute(
        "SELECT code, name, unit FROM capability_catalog ORDER BY category, code"
    ).fetchall()
    default_start, default_end = _default_window()
    return render_template("platform/resources.html", supplies=supplies, catalog=catalog,
                           selected_capability=selected_capability,
                           organization=store.get_org(membership["org_id"]),
                           default_start=default_start, default_end=default_end)


@bp.route("/demands", methods=["GET", "POST"])
def demands():
    membership = _membership()
    if request.method == "POST":
        try:
            start_s, end_s, _, _ = _parse_window(request.form)
            demand_id = store.create_demand(
                membership,
                site=request.form.get("site", "").strip(),
                title=request.form.get("title", "").strip(),
                capability=request.form.get("capability", "").strip(),
                window_start=start_s,
                window_end=end_s,
                hours=_hours(request.form.get("hours"), "最低需求量"),
                unit=request.form.get("unit", "机时").strip() or "机时",
                quality=request.form.get("quality", "").strip(),
                note=request.form.get("note", "").strip(),
                reference_price_yuan=float(request.form.get("reference_price", "120") or 120),
            )
            flash("生产需求已登记，并写入演示审计账本（#%s）。" % demand_id, "success")
            return redirect(url_for("platform.demands"))
        except (store.PlatformError, ValueError) as exc:
            flash(str(exc), "error")
    demands = store.list_demands(membership)
    default_start, default_end = _default_window()
    return render_template("platform/demands.html", demands=demands, organization=store.get_org(membership["org_id"]),
                           default_start=default_start, default_end=default_end)


@bp.get("/plans")
def plans():
    membership = _membership()
    return render_template("platform/plans.html", plans=store.list_plans(membership))


@bp.post("/demands/<int:demand_id>/plan")
def create_plan(demand_id):
    membership = _coordinator()
    try:
        strategy = request.form.get('strategy', 'manual-s1')
        if strategy == 'paper-iterative-double-auction':
            plan_id = coordination.create_plan(demand_id, membership)
        elif strategy == 'manual-s1':
            plan_id = store.create_manual_plan(demand_id, membership)
        else:
            raise store.PlatformError('请选择已有的协调策略。')
        plan = store.get_plan(plan_id)
        if plan and plan["status"] == "READY":
            flash("协调方案已生成。请检查分配与结算建议后执行容量预留。", "success")
        else:
            flash(plan['failure_reason'] or '当前方案未通过检查。', "error")
        return redirect(url_for("platform.plan_detail", plan_id=plan_id))
    except store.PermissionDenied:
        abort(403)
    except store.PlatformError as exc:
        flash(str(exc), "error")
        return redirect(url_for("platform.demands"))


@bp.get("/plans/<int:plan_id>")
def plan_detail(plan_id):
    membership = _membership()
    plan = store.get_plan(plan_id)
    if plan is None or not store.can_view_plan(plan, membership):
        abort(404)
    plan = store.plan_view(plan, membership)
    allocations = json.loads(plan["allocation_json"] or "[]")
    supply_rows = {}
    for alloc in allocations:
        row = get_db().execute("SELECT * FROM resource_supplies WHERE id = ?", (alloc["supply_id"],)).fetchone()
        if row:
            supply_rows[alloc["supply_id"]] = row
    commitment = get_db().execute("SELECT * FROM commitments WHERE plan_id = ? ORDER BY id DESC LIMIT 1", (plan_id,)).fetchone()
    events = [e for e in store.ledger_events_for(membership, limit=50)
              if e["object_type"] == "plan" and e["object_id"] == str(plan_id)]
    return render_template(
        "platform/plan_detail.html", plan=plan, allocations=allocations,
        supply_rows=supply_rows, commitment=commitment, events=events,
        algorithm_run=coordination.get_run(plan['coordination_run_id']) if plan['coordination_run_id'] and store.is_alliance_member(membership) else None,
    )


@bp.post("/plans/<int:plan_id>/reserve")
def reserve(plan_id):
    membership = _coordinator()
    try:
        commitment_id = store.reserve_plan(plan_id, membership)
        flash("本轮容量已原子预留；相关需求的共享任务均可在任务列表中查看。请各参与企业分别确认。", "success")
        return redirect(url_for("platform.commitment_detail", commitment_id=commitment_id))
    except store.PermissionDenied:
        abort(403)
    except store.PlatformError as exc:
        flash(str(exc), "error")
        return redirect(url_for("platform.plan_detail", plan_id=plan_id))


@bp.get("/commitments")
def commitments():
    membership = _membership()
    return render_template("platform/commitments.html", commitments=store.list_commitments(membership))


@bp.get("/commitments/<int:commitment_id>")
def commitment_detail(commitment_id):
    membership = _membership()
    if not store.can_view_commitment(commitment_id, membership):
        abort(404)
    commitment = store.get_commitment(commitment_id)
    if commitment is None:
        abort(404)
    parties = store.commitment_parties(commitment_id)
    allocations = store.commitment_allocations(commitment_id)
    executions = get_db().execute(
        "SELECT * FROM execution_events WHERE commitment_id=? ORDER BY id", (commitment_id,)
    ).fetchall()
    settlements = get_db().execute(
        "SELECT * FROM settlements WHERE commitment_id=? ORDER BY id", (commitment_id,)
    ).fetchall()
    plan = store.get_plan(commitment["plan_id"])
    events = [e for e in store.ledger_events_for(membership, limit=100)
              if e["object_type"] == "commitment" and e["object_id"] == str(commitment_id)]
    my_party = next((p for p in parties if int(p["org_id"]) == int(membership["org_id"])), None)
    supplier_ids = {int(a["supplier_org_id"]) for a in allocations}
    own_total_milli=sum(a['qty_milli'] for a in allocations if a['supplier_org_id']==membership['org_id'])
    own_executed=any(e['event_type']=='EXECUTED' and e['actor_org_id']==membership['org_id'] for e in executions)
    if not store.is_alliance_member(membership):
        buyer = commitment['buyer_org_id'] == membership['org_id']
        if not buyer:
            allocations = [dict(a) for a in allocations if a['supplier_org_id']==membership['org_id']]
            parties = [p for p in parties if p['org_id'] in (membership['org_id'], commitment['buyer_org_id'])]
            executions = [dict(e) for e in executions if e['actor_org_id'] in (membership['org_id'],commitment['buyer_org_id'])]
            for e in executions:
                if e['event_type']=='ACCEPTED':
                    e['qty_milli']=sum(a['actual_milli'] for a in allocations)
            commitment = dict(commitment, total_milli=own_total_milli)
        allocations = [store.allocation_view(a, buyer, commitment=True) for a in allocations]
        plan = store.plan_view(plan, membership)
        # A supplier sees its own receivable, never the buyer's total payment.
        if not buyer:
            actual_reward=sum(round(a['proposed_reward_cents']*a['actual_milli']/a['qty_milli']) for a in allocations)
            settlements=[dict(s,amount_cents=actual_reward,detail_json='{}') for s in settlements]
        else:
            settlements=[dict(s,detail_json='{}') for s in settlements]
    return render_template(
        "platform/commitment_detail.html", commitment=commitment, plan=plan,
        parties=parties, allocations=allocations, executions=executions,
        settlements=settlements, events=events, my_party=my_party,
        is_buyer=int(commitment["buyer_org_id"]) == int(membership["org_id"]),
        is_supplier=int(membership["org_id"]) in supplier_ids,
        own_total_milli=own_total_milli, own_executed=own_executed,
        all_confirmed=commitment['status']!='RESERVED',
    )


@bp.post("/commitments/<int:commitment_id>/confirm")
def confirm(commitment_id):
    membership = _membership()
    try:
        result = store.confirm_commitment(commitment_id, membership)
        if result["all_confirmed"]:
            flash("本方确认成功，双方确认已完成，承诺生效。", "success")
        else:
            flash("本方已确认，等待其他参与方确认同一版本。", "success")
    except store.PermissionDenied:
        abort(403)
    except store.PlatformError as exc:
        flash(str(exc), "error")
    return redirect(url_for("platform.commitment_detail", commitment_id=commitment_id))


@bp.post("/commitments/<int:commitment_id>/execute")
def execute(commitment_id):
    membership = _membership()
    try:
        store.record_execution(
            commitment_id, membership,
            actual_hours=_hours(request.form.get("actual_hours"), "实际用量"),
            note=request.form.get("note", "").strip(),
        )
        flash("执行结果已登记；已消耗容量不会自动加回。", "success")
    except store.PermissionDenied:
        abort(403)
    except store.PlatformError as exc:
        flash(str(exc), "error")
    return redirect(url_for("platform.commitment_detail", commitment_id=commitment_id))


@bp.post("/commitments/<int:commitment_id>/accept")
def accept(commitment_id):
    membership = _membership()
    try:
        store.accept_commitment(commitment_id, membership, request.form.get("note", "").strip())
        flash("验收已登记。请由联盟协调员生成记账式结算。", "success")
    except store.PermissionDenied:
        abort(403)
    except store.PlatformError as exc:
        flash(str(exc), "error")
    return redirect(url_for("platform.commitment_detail", commitment_id=commitment_id))


@bp.post("/commitments/<int:commitment_id>/settle")
def settle(commitment_id):
    membership = _coordinator()
    try:
        store.settle_commitment(commitment_id, membership)
        flash("记账式模拟结算已登记；未发生真实付款。", "success")
    except store.PermissionDenied:
        abort(403)
    except store.PlatformError as exc:
        flash(str(exc), "error")
    return redirect(url_for("platform.commitment_detail", commitment_id=commitment_id))


@bp.get("/trace")
def trace():
    membership = _membership()
    events = store.ledger_events_for(membership)
    valid, bad_id = store.verify_ledger(get_db())
    organizations = {row["id"]: row["name"] for row in get_db().execute("SELECT id, name FROM organizations")}
    return render_template(
        "platform/trace.html", events=events, valid=valid, bad_id=bad_id,
        organizations=organizations, is_demo_ledger=True,
    )


@bp.route('/valuation', methods=['GET','POST'])
def valuation():
    membership=_membership()
    if membership['org_kind']!='factory':
        abort(403)
    if request.method=='POST':
        try:
            coordination.update_profile(membership,request.form.get('alpha'),request.form.get('linear'),request.form.get('quadratic'))
            flash('本企业估值参数已保存。依赖旧版本的未预留方案需要重新计算。','success')
            return redirect(url_for('platform.valuation'))
        except store.PermissionDenied:
            abort(403)
        except store.PlatformError as exc:
            flash(str(exc),'error')
    return render_template('platform/valuation.html',profile=coordination.profile(membership['org_id']))


@bp.get('/cda-demo')
def cda_demo():
    membership=_membership()
    if not access.can(membership,'algorithms.read'):
        abort(403)
    db=get_db()
    row=db.execute(
        "SELECT * FROM experiments WHERE status='completed' ORDER BY is_sample DESC, id LIMIT 1"
    ).fetchone()
    if row is not None:
        result=json.loads(row['result_json'])
    else:
        # A fresh test database has no persisted sample. Re-run the original
        # preset engine so the standalone comparison page still has real
        # contracts and summary values; chart assets remain the original
        # sample figures served by the protected chart route.
        config=dict(get_preset('equipment'))
        config['name']='连续双向拍卖示例 · 区域加工能力共享'
        try:
            result=run_simulation(config)
        except Exception as exc:
            flash('连续双向拍卖示例暂时不可用：%s' % exc, 'error')
            return redirect(url_for('platform.algorithms'))
    summary=result.get('summary',{})
    config=result.get('config',{})
    contracts=result.get('contracts',[])[:18]
    current = db.execute('''SELECT COUNT(DISTINCT org_id) AS providers,
        COALESCE(SUM(total_milli-reserved_milli-consumed_milli),0) AS available_milli
        FROM resource_supplies WHERE capability='CNC-3AXIS' AND status='ACTIVE' ''').fetchone()
    demand_count = db.execute("SELECT COUNT(*) AS n FROM demands WHERE capability='CNC-3AXIS'").fetchone()['n']
    completed = db.execute("SELECT COUNT(*) AS n FROM commitments WHERE status IN ('ACCEPTED','ACCOUNTED')").fetchone()['n']
    return render_template('platform/cda_demo.html', experiment=row, result=result,
        summary=summary, config=config, contracts=contracts,
        current=current, demand_count=demand_count, completed=completed,
        chart_prefix='/cda-demo/charts/')


@bp.get('/cda-demo/charts/<chart_name>')
def cda_chart(chart_name):
    membership=_membership()
    if not access.can(membership,'algorithms.read'):
        abort(403)
    # Prefer the chart generated by the seeded sample experiment.  A fresh
    # deployment does not ship the ignored static/generated directory, so
    # hard-coded local filenames would otherwise make this comparison page
    # fail after the first deploy.
    row=get_db().execute(
        "SELECT chart_json FROM experiments WHERE status='completed' ORDER BY is_sample DESC, id LIMIT 1"
    ).fetchone()
    filename=None
    if row is not None:
        try:
            charts=json.loads(row['chart_json'] or '{}')
            value=charts.get(chart_name)
            if value:
                filename=Path(value).name
        except (TypeError, ValueError):
            filename=None
    if not filename:
        allowed={
            'supply_demand':'sample_demo_9ac4bc_supply_demand.png',
            'prices':'sample_demo_9ac4bc_prices.png',
            'efficiency':'sample_demo_9ac4bc_efficiency.png',
        }
        filename=allowed.get(chart_name)
    directory=Path(current_app.config['GENERATED_DIR'])
    if not filename or not (directory / filename).exists():
        # Fresh deployments do not ship generated charts. Re-create the
        # deterministic CDA sample and serve the requested chart once.
        try:
            config=dict(get_preset('equipment'))
            config['name']='连续双向拍卖示例 · 区域加工能力共享'
            result=run_simulation(config)
            charts=generate_charts(result, 'cda_demo_%s' % chart_name,
                                   current_app.config['GENERATED_DIR'])
            filename=Path(charts[chart_name]).name
        except Exception:
            abort(404)
    if not (directory / filename).exists():
        directory = Path(current_app.root_path) / 'static' / 'generated'
    return send_from_directory(directory, filename, mimetype='image/png')


@bp.route('/coordination', methods=['GET','POST'])
def coordination_trials():
    membership=_membership()
    if request.method=='POST':
        try:
            case=request.form.get('case','standard')
            settings=Settings(delta=float(request.form.get('delta','.1')),
                rho=float(request.form.get('rho','.2')),tolerance=float(request.form.get('tolerance','1e-6')),
                max_iterations=int(request.form.get('max_iterations','400')))
            run_id=coordination.run_trial(case,membership,settings)
            return redirect(url_for('platform.coordination_detail',run_id=run_id))
        except (ValueError,store.PlatformError) as exc:
            flash(str(exc),'error')
    return render_template('platform/coordination.html',cases=coordination.TRIALS,runs=coordination.trial_runs(membership))


@bp.get('/algorithms')
def algorithms():
    membership=_membership()
    return render_template('platform/algorithms.html',cases=coordination.TRIALS,
        methods=comparison.METHODS,comparisons=comparison.history(membership))


@bp.post('/algorithms/compare')
def compare_algorithms():
    membership=_membership()
    try:
        settings=Settings(delta=float(request.form.get('delta','.1')),
            rho=float(request.form.get('rho','.2')),tolerance=1e-6,
            max_iterations=int(request.form.get('max_iterations','400')))
        comparison_id=comparison.save_comparison(request.form.get('case','standard'),membership,
            settings,request.form.get('reference_price','120'),request.form.getlist('methods'))
        return redirect(url_for('platform.comparison_detail',comparison_id=comparison_id))
    except (ValueError,store.PlatformError) as exc:
        flash(str(exc),'error')
        return redirect(url_for('platform.algorithms'))


def _authorized_comparison(comparison_id):
    result=comparison.get_comparison(comparison_id,_membership())
    if result is None:
        abort(404)
    return result


def _bars_3d(rows):
    """Small, dependency-free isometric bar chart for the method comparison."""
    usable=[row for row in rows if row.get('welfare_comparable') and row.get('welfare') is not None]
    if not usable:
        return None
    values=[float(row['welfare']) for row in usable]
    lo=min([0.]+values); hi=max([0.]+values)
    if hi-lo < 1e-9:
        hi=lo+1.0
    width=max(900, 90+len(usable)*116)
    base=300; max_height=176; depth=18; bar_w=58
    bars=[]
    for k,row in enumerate(usable):
        value=float(row['welfare'])
        h=max(6.0, (value-lo)/(hi-lo)*max_height) if hi>lo else 80.0
        x=72+k*116; top=base-h
        bars.append({
            'label':row['label'], 'method':row['method_id'], 'value':value,
            'front':f'{x},{top} {x+bar_w},{top} {x+bar_w},{base} {x},{base}',
            'top_face':f'{x},{top} {x+depth},{top-depth} {x+bar_w+depth},{top-depth} {x+bar_w},{top}',
            'side':f'{x+bar_w},{top} {x+bar_w+depth},{top-depth} {x+bar_w+depth},{base-depth} {x+bar_w},{base}',
            'label_x':x+bar_w/2, 'top':top, 'depth':depth,
        })
    return {'width':width,'height':410,'base':base,'bars':bars,'lo':lo,'hi':hi}


def _scatter_3d(rows):
    usable=[row for row in rows if row.get('welfare_comparable') and row.get('welfare') is not None]
    if not usable:
        return None
    values=[float(row['welfare']) for row in usable]
    hi=max([1.]+values); base=238; height=150; width=900
    max_iter=max([1]+[int(row.get('iterations') or 0) for row in usable])
    points=[]
    for k,row in enumerate(usable):
        value=float(row['welfare'])
        depth=22+34*min(1.0,int(row.get('iterations') or 0)/max_iter)
        x=74+k*104; y=base-(max(0.0,value)/hi)*height
        points.append({
            'x':x+depth, 'y':y-depth, 'r':6+10*min(1.0,(row.get('elapsed') or 0)/max(0.001,max(r.get('elapsed') or 0 for r in usable))),
            'label':row['label'], 'method':row['method_id'], 'value':value,
            'iterations':int(row.get('iterations') or 0), 'anchor_x':x, 'anchor_y':y,
        })
    return {'width':width,'height':300,'base':base,'hi':hi,'max_iter':max_iter,'points':points}


@bp.get('/comparisons/<int:comparison_id>')
def comparison_detail(comparison_id):
    run=_authorized_comparison(comparison_id)
    rows=[row for row in run['result']['outcomes'] if row.get('method_id')!='p2p']
    values=[row['welfare'] for row in rows if row['welfare_comparable'] and row['welfare'] is not None]
    lo=min([0.]+values)
    hi=max([0.]+values)
    if hi-lo<1e-12:
        hi=lo+1.
    zero=240+480*(-lo)/(hi-lo)
    bars=[]
    for k,row in enumerate(rows):
        if not row['welfare_comparable'] or row['welfare'] is None:
            continue
        x=240+480*(row['welfare']-lo)/(hi-lo)
        bars.append({'label':row['label'],'method':row['method_id'],'x':min(zero,x),
            'y':40+60*k,'width':abs(x-zero),'value':row['welfare']})
    chart_height=max(260, 40+60*len(bars)+24)
    return render_template('platform/comparison_detail.html',run=run,rows=rows,
        bars=bars,zero=zero,lower=lo,upper=hi,chart_height=chart_height,
        bars_3d=_bars_3d(rows),scatter_3d=_scatter_3d(rows))


@bp.get('/comparisons/<int:comparison_id>/export.json')
def comparison_export(comparison_id):
    run=_authorized_comparison(comparison_id)
    payload={k:run[k] for k in ('id','created_at','input_hash','snapshot','result','paper_run_id')}
    # Older saved comparisons may still contain the removed P2P baseline.
    # Do not expose it through new exports either.
    exported=dict(payload['result'])
    exported['outcomes']=[row for row in exported.get('outcomes',[]) if row.get('method_id')!='p2p']
    payload['result']=exported
    payload['paper_run']=coordination.get_run(run['paper_run_id'])['result'] if run['paper_run_id'] else None
    return Response(json.dumps(payload,ensure_ascii=False,indent=2),mimetype='application/json',
        headers={'Content-Disposition':f'attachment; filename=miot-comparison-{comparison_id}.json'})


@bp.get('/comparisons/<int:comparison_id>/summary.csv')
def comparison_csv(comparison_id):
    run=_authorized_comparison(comparison_id)
    output=io.StringIO()
    writer=csv.writer(output)
    fields=['method_id','label','version','group','input_hash','status','physical_feasible','welfare_comparable',
            'welfare','total_quantity','payment','reward','broker_surplus','elapsed','iterations',
            'state_verified','filter_violation','reason']
    writer.writerow(fields)
    for row in run['result']['outcomes']:
        if row.get('method_id')=='p2p':
            continue
        writer.writerow([row.get(field) for field in fields])
    return Response('\ufeff'+output.getvalue(),content_type='text/csv; charset=utf-8',
        headers={'Content-Disposition':f'attachment; filename=miot-comparison-{comparison_id}.csv'})


def _authorized_run(run_id):
    run=coordination.get_run(run_id)
    if not coordination.can_view_run(run,_membership()):
        abort(404)
    return run


@bp.get('/coordination/<int:run_id>')
def coordination_detail(run_id):
    run=_authorized_run(run_id)
    result=run['result']
    trace=result.get('trace',[])
    plot=None
    if trace:
        logs=[math.log10(max(t['fixed_point_residual'],1e-12)) for t in trace]
        lo=min(min(logs),math.log10(result['settings']['tolerance']))
        hi=max(max(logs),lo+1)
        points=' '.join(f'{50+680*k/max(1,len(logs)-1):.2f},{180-150*(v-lo)/(hi-lo):.2f}' for k,v in enumerate(logs))
        plot={'points':points,'upper':f'{10**hi:.1e}','lower':f'{10**lo:.1e}',
              'threshold':180-150*(math.log10(result['settings']['tolerance'])-lo)/(hi-lo)}
    plans=get_db().execute('SELECT id,demand_id,status FROM plans WHERE coordination_run_id=? ORDER BY id',(run_id,)).fetchall()
    return render_template('platform/coordination_detail.html',run=run,result=result,plot=plot,linked_plans=plans)


@bp.get('/coordination/<int:run_id>/export.json')
def coordination_export(run_id):
    run=_authorized_run(run_id)
    payload={k:run[k] for k in ('id','kind','algorithm_version','status','created_at','snapshot_hash','snapshot','result')}
    payload['ledger_mode']='DEMO_HASH_CHAIN'
    return Response(json.dumps(payload,ensure_ascii=False,indent=2),mimetype='application/json',
                    headers={'Content-Disposition':f'attachment; filename=miot-coordination-{run_id}.json'})


@bp.get('/coordination/<int:run_id>/iterations.csv')
def coordination_csv(run_id):
    run=_authorized_run(run_id)
    output=io.StringIO()
    writer=csv.writer(output)
    writer.writerow(['iteration','buyer_index','provider_index','q','p','x','y',
                     'fixed_point_residual','signal_step','welfare','broker_surplus','constraint_violation','clearing_gap'])
    for row in run['result'].get('trace',[]):
        for e,(i,j) in enumerate(run['result']['edges']):
            writer.writerow([row['iteration'],i,j,row['q'][e],row['p'][e],row['x'][e],row['y'][e],
                *[row[k] for k in ('fixed_point_residual','signal_step','welfare','broker_surplus','constraint_violation','clearing_gap')]])
    return Response('\ufeff'+output.getvalue(),content_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':f'attachment; filename=miot-iterations-{run_id}.csv'})


@bp.get("/trace/export.json")
def export_trace():
    membership = _membership()
    bundle = store.export_bundle(membership)
    payload = json.dumps(bundle, ensure_ascii=False, indent=2)
    return Response(
        payload, mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=multi_factory_s1_bundle.json"},
    )


@bp.post("/trace/verify")
def verify_trace():
    _membership()
    valid, bad_id = store.verify_ledger(get_db())
    if valid:
        flash("演示审计账本哈希链校验通过；这不等同于 Fabric 共识或真实企业数据。", "success")
    else:
        flash("哈希链校验失败，首个异常事件 ID：%s。" % bad_id, "error")
    return redirect(url_for("platform.trace"))
