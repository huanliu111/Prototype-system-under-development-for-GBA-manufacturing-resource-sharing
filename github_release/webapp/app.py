"""Flask application for the MIoT manufacturing resource coordination platform."""
from __future__ import annotations

import csv
import hmac
import io
import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

# Make the headless matplotlib backend and a writable cache effective before
# the original engine (which imports pyplot) is loaded.
os.environ.setdefault("MPLBACKEND", "Agg")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if not os.environ.get("MPLCONFIGDIR"):
    os.environ["MPLCONFIGDIR"] = str(PROJECT_ROOT / ".cache" / "matplotlib")
os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)

from flask import (
    Flask, abort, flash, g, redirect, render_template, request,
    send_file, send_from_directory, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from webapp.charts import delete_charts, generate_charts
from webapp.db import (
    DEMO_ACCOUNTS, get_db, get_user_by_id, get_user_by_username,
    init_app as init_db_app, init_db, insert_experiment, seed_demo_users,
    update_experiment_result,
)
from webapp.scenarios import (
    PRESETS, STRATEGY_OPTIONS, form_from_preset, get_preset, preset_cards,
)
from webapp.simulation import SimulationError, run_simulation
from webapp.platform_routes import bp as platform_bp
from webapp import platform_store
from webapp import access
from webapp import i18n
from webapp.english import translate_html


APP_VERSION = "0.7.0"


def _read_or_create_secret(instance_dir: Path) -> str:
    path = instance_dir / "secret_key"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_hex(32)
    path.write_text(value + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


def _safe_next(target: str | None) -> str | None:
    if not target:
        return None
    target = target.strip()
    if not target.startswith("/") or target.startswith("//"):
        return None
    if urlsplit(target).netloc:
        return None
    return target


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
        return view(*args, **kwargs)

    return wrapped


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=False)
    instance_dir = Path(app.root_path) / "instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    generated_dir = Path(app.root_path) / "static" / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)

    app.config.from_mapping(
        SECRET_KEY=os.environ.get("WEB_SECRET_KEY") or _read_or_create_secret(instance_dir),
        DATABASE=os.environ.get("WEB_DATABASE", str(instance_dir / "app.db")),
        GENERATED_DIR=str(generated_dir),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("WEB_COOKIE_SECURE", "0") == "1",
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        DEMO_MODE=os.environ.get("WEB_DEMO_MODE", "1") != "0",
        SHOW_DEMO_CREDENTIALS=os.environ.get('WEB_SHOW_DEMO_CREDENTIALS','1')!='0',
        SEED_SAMPLE=os.environ.get("WEB_SEED_SAMPLE", "1") != "0",
        APP_VERSION=APP_VERSION,
    )
    if test_config:
        app.config.update(test_config)
    if os.environ.get("WEB_BEHIND_PROXY", "0") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["GENERATED_DIR"]).mkdir(parents=True, exist_ok=True)

    init_db_app(app)

    with app.app_context():
        init_db()
        seed_demo_users()
        platform_store.init_platform_schema()
        platform_store.seed_platform()
        if app.config.get("SEED_SAMPLE") and not app.config.get("TESTING"):
            _ensure_sample_experiment(app)

    app.register_blueprint(platform_bp)

    @app.before_request
    def load_user_and_csrf():
        g.lang = i18n.normalize(request.args.get("lang") or session.get("lang"))
        g.user = None
        user_id = session.get("user_id")
        if user_id is not None:
            user_row = get_user_by_id(user_id)
            g.user = dict(user_row) if user_row is not None else None
            if g.user is None:
                session.clear()

        g.membership = None
        g.organization = None
        if g.user is not None:
            membership = platform_store.membership_for(g.user["id"])
            if membership is not None:
                g.membership = dict(membership)
                g.organization = platform_store.get_org(g.membership["org_id"])

        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        g.csrf_token = token

        denied = access.enforce_request()
        if denied is not None:
            return denied

        if request.method == "POST":
            sent = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
            if not sent or not hmac.compare_digest(str(token), str(sent)):
                abort(400, description="表单已过期，请刷新页面后重试。")

    @app.after_request
    def add_security_headers(response):
        if request.endpoint != 'static' or access.private_static():
            response.headers['Cache-Control'] = 'no-store, private'
            response.vary.add('Cookie')
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; font-src 'self'; connect-src 'self'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        if getattr(g, 'lang', 'zh') == 'en' and response.mimetype == 'text/html':
            response.set_data(translate_html(response.get_data(as_text=True)))
        return response

    @app.context_processor
    def inject_globals():
        display_user = getattr(g, "user", None)
        if display_user and display_user.get("display_name") == "论文评审专家":
            display_user = dict(display_user, display_name="联盟审计员")
        return {
            "current_user": display_user,
            "current_membership": getattr(g, "membership", None),
            "current_org": getattr(g, "organization", None),
            "is_alliance_user": bool(g.membership and platform_store.is_alliance_member(g.membership)),
            "can": lambda capability: access.can(getattr(g, 'membership', None), capability),
            "workspace_title": access.workspace_title(getattr(g, 'membership', None)),
            "csrf_token": getattr(g, "csrf_token", ""),
            "preset_cards": preset_cards(),
            "strategy_options": STRATEGY_OPTIONS,
            "demo_mode": app.config["DEMO_MODE"],
            "app_version": app.config["APP_VERSION"],
            "lang": getattr(g, "lang", "zh"),
            "t": lambda key, **kw: i18n.translate(key, getattr(g, "lang", "zh"), **kw),
        }

    @app.template_filter("money")
    def money_filter(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "—"
        if number.is_integer():
            return "{:,.0f}".format(number)
        return "{:,.1f}".format(number)

    @app.template_filter("pct")
    def pct_filter(value, digits=1):
        try:
            return ("{:." + str(digits) + "f}%").format(float(value))
        except (TypeError, ValueError):
            return "—"

    @app.template_filter("datetime_cn")
    def datetime_cn_filter(value, fmt="%Y-%m-%d %H:%M"):
        if not value:
            return "—"
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.astimezone().strftime(fmt)
        except (TypeError, ValueError):
            return str(value)

    @app.template_filter("initials")
    def initials_filter(value):
        text = str(value or "访客").strip()
        if not text:
            return "访"
        return text[0]

    @app.template_filter("initials_en")
    def initials_en_filter(value):
        text = str(value or "User").strip()
        mapping = {'平': 'PC', '计': 'PL', '设': 'ES', '生': 'PS', '联': 'LV',
                   '企': 'EA', '系': 'SA'}
        return mapping.get(text[:1], text[:2].upper() if text.isascii() else 'MI')

    @app.template_filter("hours")
    def hours_filter(value, unit="机时"):
        try:
            return platform_store.fmt_milli(int(value), unit or "机时")
        except (TypeError, ValueError):
            return "—"

    @app.template_filter("money_cents")
    def money_cents_filter(value):
        try:
            return platform_store.fmt_money(int(value))
        except (TypeError, ValueError):
            return "—"

    @app.template_filter("from_json")
    def from_json_filter(value):
        try:
            return json.loads(value or "[]")
        except (TypeError, ValueError):
            return []

    # ------------------------------------------------------------------ public
    @app.get("/")
    def landing():
        return redirect(url_for('login'))

    @app.get('/language/<lang>')
    def set_language(lang):
        session['lang'] = i18n.normalize(lang)
        target = request.args.get('next', '')
        if not target.startswith('/') or target.startswith('//'):
            target = url_for('login')
        return redirect(target)

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        # Accounts are provisioned by the operator, never self-assigned roles.
        abort(404)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        next_url = request.values.get("next", "")
        if request.method == "POST":
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            user = get_user_by_username(username)
            if user is not None and check_password_hash(user["password_hash"], password):
                membership = platform_store.membership_for(user['id'])
                if access.valid_member(membership):
                    _log_in(user)
                    return redirect(url_for(access.home_endpoint(membership)))
                error = ('This account has no valid membership. Contact the platform operator.' if getattr(g,'lang','zh')=='en' else '当前账号未配置有效岗位，请联系平台管理员。')
            else:
                error = i18n.translate("login.invalid", getattr(g, "lang", "zh"))
        return render_template(
            "login.html",
            error=error,
            next_url=next_url,
            demo_accounts=_development_accounts(app),
            prefill_username=request.args.get("username", ""),
        )

    @app.get("/demo-login")
    def demo_login():
        # Compatibility URL only; it never creates or switches a session.
        return redirect(url_for('login'))

    @app.post("/logout")
    def logout():
        lang = session.get('lang')
        session.clear()
        if lang:
            session['lang'] = lang
        flash("你已安全退出。", "info")
        return redirect(url_for("login"))

    # ------------------------------------------------------------------- app
    @app.get("/dashboard")
    @login_required
    def dashboard():
        return redirect(url_for("platform.workspace"))

    @app.get("/experiments")
    @login_required
    def history():
        rows = get_db().execute(
            "SELECT * FROM experiments WHERE user_id = ? ORDER BY created_at DESC, id DESC",
            (g.user["id"],),
        ).fetchall()
        experiments = [_experiment_row(row) for row in rows]
        return render_template("history.html", experiments=experiments, stats=_dashboard_stats(experiments))

    @app.route("/experiments/new", methods=["GET", "POST"])
    @login_required
    def new_experiment():
        if request.method == "GET":
            slug = request.args.get("preset", "equipment")
            preset = get_preset(slug)
            form = form_from_preset(preset)
            if not form.get("name"):
                form["name"] = "%s · 实验 %s" % (
                    preset["title"], datetime.now().strftime("%m%d-%H%M")
                )
            return render_template(
                "experiment_form.html", form=form, selected_preset=preset["slug"],
                preset_title=preset["title"], preset_description=preset["description"],
                preset_expected=preset["expected"], form_error=None,
            )

        form = _form_from_request(request.form)
        config = _config_from_form(request.form)
        try:
            result = run_simulation(config)
            prefix = "exp_%s" % uuid.uuid4().hex[:12]
            charts = generate_charts(result, prefix, app.config["GENERATED_DIR"])
            experiment_id = insert_experiment(
                user_id=g.user["id"], config=result["config"], result=result, charts=charts,
            )
        except SimulationError as exc:
            flash(str(exc), "error")
            return render_template(
                "experiment_form.html", form=form, selected_preset=None,
                preset_title="自定义实验", preset_description="请修正参数后重新运行。",
                preset_expected=None, form_error=str(exc),
            ), 400
        except Exception:
            app.logger.exception("Simulation failed")
            message = "实验运行失败，请稍后重试；如果问题持续出现，请联系系统管理员。"
            flash(message, "error")
            return render_template(
                "experiment_form.html", form=form, selected_preset=None,
                preset_title="自定义实验", preset_description="请检查参数后重新运行。",
                preset_expected=None, form_error=message,
            ), 500

        flash("实验运行完成，已生成 %d 笔成交。" % result["summary"]["total_contracts"], "success")
        return redirect(url_for("experiment_result", experiment_id=experiment_id))

    @app.get("/experiments/<int:experiment_id>")
    @login_required
    def experiment_result(experiment_id):
        row = _owned_experiment(experiment_id)
        experiment = _experiment_row(row)
        if experiment["status"] != "completed" or not experiment["result"]:
            return render_template("result.html", experiment=experiment, missing_result=True)
        if not _charts_exist(experiment["charts"], app.config["GENERATED_DIR"]):
            try:
                prefix = "regen_%d_%s" % (experiment_id, uuid.uuid4().hex[:6])
                charts = generate_charts(experiment["result"], prefix, app.config["GENERATED_DIR"])
                update_experiment_result(experiment_id, experiment["result"], charts)
                experiment["charts"] = charts
            except Exception:
                app.logger.exception("Could not regenerate charts for experiment %s", experiment_id)
        return render_template("result.html", experiment=experiment, missing_result=False)

    @app.get("/experiments/<int:experiment_id>/download.csv")
    @login_required
    def download_csv(experiment_id):
        row = _owned_experiment(experiment_id)
        experiment = _experiment_row(row)
        result = experiment["result"] or {}
        contracts = result.get("contracts", [])
        buffer = io.StringIO()
        buffer.write("\ufeff")
        writer = csv.writer(buffer)
        writer.writerow([
            "实验", "周期", "成交编号", "需求企业", "供给企业", "成交价",
            "需求估值", "供给成本", "单位社会剩余", "买方利润", "卖方利润",
        ])
        for item in contracts:
            writer.writerow([
                experiment["name"], item.get("period"), item.get("no"),
                item.get("buyer"), item.get("seller"), item.get("price"),
                item.get("value"), item.get("cost"), item.get("surplus"),
                item.get("buyer_profit"), item.get("seller_profit"),
            ])
        writer.writerow([])
        summary = experiment["summary"]
        writer.writerow(["算法模型", "continuous-double-auction-discrete-units"])
        writer.writerow(["配置效率", summary.get("overall_efficiency", 0)])
        writer.writerow(["成交数量", summary.get("total_contracts", 0)])
        writer.writerow(["加权均价", summary.get("avg_price", 0)])
        writer.writerow(["均衡数量", summary.get("eq_quantity", 0)])
        writer.writerow(["均衡价下限", summary.get("eq_price_low", 0)])
        writer.writerow(["均衡价上限", summary.get("eq_price_high", 0)])
        data = io.BytesIO(buffer.getvalue().encode("utf-8"))
        return send_file(
            data, mimetype="text/csv", as_attachment=True,
            download_name="miot_cda_experiment_%d.csv" % experiment_id,
        )

    @app.get('/experiments/<int:experiment_id>/charts/<chart_name>')
    def experiment_chart(experiment_id, chart_name):
        experiment = _owned_experiment(experiment_id)
        value = _json_load(experiment['chart_json'], {}).get(chart_name)
        if not value:
            abort(404)
        filename = Path(urlsplit(value).path).name
        if not filename.endswith('.png'):
            abort(404)
        return send_from_directory(app.config['GENERATED_DIR'], filename, mimetype='image/png')

    @app.post("/experiments/<int:experiment_id>/delete")
    @login_required
    def delete_experiment(experiment_id):
        row = _owned_experiment(experiment_id)
        delete_charts(_json_load(row["chart_json"], {}), app.config["GENERATED_DIR"])
        get_db().execute("DELETE FROM experiments WHERE id = ? AND user_id = ?", (experiment_id, g.user["id"]))
        get_db().commit()
        flash("实验已删除。", "info")
        return redirect(url_for("history"))

    # ------------------------------------------------------------- knowledge
    @app.get("/methodology")
    @app.get("/about")
    def methodology():
        return render_template("methodology.html")

    @app.get("/review-guide")
    @app.get("/guide")
    def review_guide():
        return render_template("review_guide.html")

    @app.get("/healthz")
    def healthz():
        db = get_db()
        db.execute("SELECT 1").fetchone()
        return {"status": "ok", "version": APP_VERSION, "database": "ok"}

    # ---------------------------------------------------------------- errors
    @app.errorhandler(400)
    def bad_request(error):
        message = getattr(error, "description", "请求无效。")
        return render_template("error.html", code=400, title="请求无效", message=message), 400

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template("error.html", code=403, title="没有访问权限", message="当前岗位无权访问此功能，请返回本岗位工作台。"), 403

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, title="页面不存在", message="这个页面可能已被移动或删除。"), 404

    @app.errorhandler(500)
    def server_error(_error):
        return render_template("error.html", code=500, title="系统暂时不可用", message="服务器遇到错误，请稍后重试。"), 500

    @app.cli.command("seed-sample")
    def seed_sample_command():
        """Create the reviewer sample experiment if it is missing."""
        _ensure_sample_experiment(app, force=True)
        print("sample experiment ready")

    return app


# --------------------------------------------------------------------- helpers
def _log_in(user):
    lang = session.get('lang')
    session.clear()
    if lang:
        session['lang'] = lang
    session["user_id"] = user["id"]
    session["_csrf_token"] = secrets.token_urlsafe(32)


def _development_accounts(app):
    """Only the operator's known demo identities, never arbitrary user secrets.

    Candidates come from development settings, not password recovery. Check
    against the current hash so a customized password is never falsely shown
    as the working credential. Production mode does not return credentials.
    """
    if not app.config['DEMO_MODE'] or not app.config['SHOW_DEMO_CREDENTIALS']:
        return []
    default = os.environ.get('WEB_DEMO_PASSWORD', 'Demo2026!')
    specs = [('demo', default, '广州精工制造有限公司 · 计划员'),
             ('beta', default, '东莞共享加工中心 · 设备主管'),
             ('gamma', default, '佛山智能制造有限公司 · 生产主管'),
             ('delta', default, '深圳精密制造有限公司 · 设备主管'),
             ('epsilon', default, '惠州智能装备有限公司 · 生产主管'),
             ('coordinator', default, '平台协调员'),
             ('reviewer', os.environ.get('WEB_REVIEWER_PASSWORD','Review2026!'), '联盟链验证员')]
    accounts = []
    for username, candidate, label in specs:
        user = get_user_by_username(username)
        if user is None:
            continue
        membership = platform_store.membership_for(user['id'])
        if not access.valid_member(membership):
            continue
        matches = check_password_hash(user['password_hash'], candidate)
        accounts.append({'username': username, 'password': candidate if matches else None,
                         'label': label})
    return accounts


def _owned_experiment(experiment_id):
    row = get_db().execute(
        "SELECT * FROM experiments WHERE id = ? AND user_id = ?",
        (experiment_id, g.user["id"]),
    ).fetchone()
    if row is None:
        abort(404)
    return row


def _json_load(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _experiment_row(row):
    summary = _json_load(row["summary_json"], {})
    result = _json_load(row["result_json"], {})
    charts = _json_load(row["chart_json"], {})
    return {
        "id": row["id"],
        "name": row["name"].replace("评审示例｜", "预置场景｜", 1) if row["is_sample"] else row["name"],
        "resource_type": row["resource_type"],
        "unit_label": row["unit_label"],
        "status": row["status"],
        "is_sample": bool(row["is_sample"]),
        "created_at": row["created_at"],
        "summary": summary,
        "result": result,
        "charts": charts,
        "config": _json_load(row["config_json"], {}),
        "error_message": row["error_message"],
    }


def _dashboard_stats(experiments):
    completed = [item for item in experiments if item["status"] == "completed" and item["summary"]]
    avg_eff = (
        sum(float(item["summary"].get("overall_efficiency", 0)) for item in completed) / len(completed)
        if completed else 0
    )
    total_contracts = sum(int(item["summary"].get("total_contracts", 0)) for item in completed)
    return {
        "count": len(experiments),
        "completed": len(completed),
        "avg_efficiency": round(avg_eff, 1),
        "total_contracts": total_contracts,
    }


def _form_from_request(form):
    buyer_names = form.getlist("buyer_name")
    buyer_values = form.getlist("buyer_values")
    seller_names = form.getlist("seller_name")
    seller_values = form.getlist("seller_values")

    def rows(names, values):
        result = []
        for name, value in zip(names, values):
            result.append({"name": name.strip(), "values": value.strip()})
        return result

    return {
        "name": form.get("name", "").strip(),
        "resource_type": form.get("resource_type", "").strip(),
        "unit_label": form.get("unit_label", "单位").strip(),
        "notes": form.get("notes", "").strip(),
        "price_floor": form.get("price_floor", "0"),
        "price_ceiling": form.get("price_ceiling", "500"),
        "periods": form.get("periods", "1"),
        "rounds": form.get("rounds", "20"),
        "seed": form.get("seed", "2026"),
        "buyer_strategy": form.get("buyer_strategy", "Trader_AA"),
        "seller_strategy": form.get("seller_strategy", "Trader_AA"),
        "buyer_rows": rows(buyer_names, buyer_values),
        "seller_rows": rows(seller_names, seller_values),
    }


def _config_from_form(form):
    return {
        "name": form.get("name", "").strip(),
        "resource_type": form.get("resource_type", "").strip(),
        "unit_label": form.get("unit_label", "单位").strip(),
        "notes": form.get("notes", "").strip(),
        "price_floor": form.get("price_floor", 0),
        "price_ceiling": form.get("price_ceiling", 0),
        "periods": form.get("periods", 1),
        "rounds": form.get("rounds", 20),
        "seed": form.get("seed", 2026),
        "buyer_strategy": form.get("buyer_strategy", "Trader_AA"),
        "seller_strategy": form.get("seller_strategy", "Trader_AA"),
        "buyers": [
            {"name": name.strip(), "values": values.strip()}
            for name, values in zip(form.getlist("buyer_name"), form.getlist("buyer_values"))
        ],
        "sellers": [
            {"name": name.strip(), "costs": values.strip()}
            for name, values in zip(form.getlist("seller_name"), form.getlist("seller_values"))
        ],
    }


def _charts_exist(charts, generated_dir):
    if not charts or len(charts) < 3:
        return False
    generated_dir = Path(generated_dir)
    for relative in charts.values():
        if not isinstance(relative, str) or "/" not in relative:
            return False
        if not (generated_dir / relative.rsplit("/", 1)[-1]).exists():
            return False
    return True


def _ensure_sample_experiment(app, force=False):
    """Seed one finished experiment per demo account for an instant review."""
    if not app.config.get("SEED_SAMPLE") and not force:
        return
    db = get_db()
    targets = (
        ("reviewer", "预置场景｜高端装备共享（6×6×3）"),
        ("demo", "演示示例｜高端装备共享（6×6×3）"),
    )
    preset = get_preset("equipment")
    for username, experiment_name in targets:
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user is None:
            continue
        if not force:
            existing = db.execute(
                "SELECT id FROM experiments WHERE user_id = ? AND is_sample = 1 LIMIT 1",
                (user["id"],),
            ).fetchone()
            if existing is not None:
                continue
        config = {
            "name": experiment_name,
            "resource_type": preset["resource_type"],
            "unit_label": preset["unit_label"],
            "notes": "系统预置的演示场景，用于快速了解从参数到结果的完整流程。",
            "price_floor": preset["price_floor"],
            "price_ceiling": preset["price_ceiling"],
            "periods": preset["periods"],
            "rounds": preset["rounds"],
            "seed": preset["seed"],
            "buyer_strategy": preset["buyer_strategy"],
            "seller_strategy": preset["seller_strategy"],
            "buyers": preset["buyers"],
            "sellers": preset["sellers"],
        }
        try:
            result = run_simulation(config)
            prefix = "sample_%s_%s" % (username, uuid.uuid4().hex[:6])
            charts = generate_charts(result, prefix, app.config["GENERATED_DIR"])
            insert_experiment(
                user_id=user["id"], config=result["config"], result=result,
                charts=charts, is_sample=True,
            )
            app.logger.info("Seeded sample experiment for %s.", username)
        except Exception:
            app.logger.exception("Could not seed sample experiment for %s", username)
