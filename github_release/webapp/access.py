"""Account roles, navigation and HTTP permissions share one policy.

MIoT develops the software; the alliance operates it. A factory membership
never acquires alliance access merely by using an alliance role name.
Unknown memberships and newly added endpoints fail closed.
"""
import posixpath
from flask import abort, g, redirect, request, url_for

FACTORY_ROLES = {'planner', 'resource_manager', 'factory_admin'}
ALLIANCE_ROLES = {'coordinator', 'auditor', 'admin'}
COMMON = {'account.use', 'help.read'}
FACTORY_READ = {'workspace.enterprise', 'resources.read', 'demands.read',
                'plans.read', 'tasks.read', 'trace.read', 'valuation.read'}
FACTORY_WRITE = {'supply.write', 'task.confirm', 'task.execute', 'valuation.write'}
COORDINATOR = {'workspace.coordination', 'resources.read', 'demands.read',
               'plans.read', 'tasks.read', 'trace.read', 'trace.export', 'trace.verify',
               'coordination.write', 'settlement.write', 'algorithms.read',
               'algorithms.run', 'runs.read'}
AUDITOR = {'workspace.audit', 'plans.read', 'tasks.read', 'trace.read',
           'trace.export', 'trace.verify', 'runs.read'}


def valid_member(member):
    return bool(member and (
        (member['org_kind'] == 'factory' and member['role'] in FACTORY_ROLES) or
        (member['org_kind'] == 'alliance' and member['role'] in ALLIANCE_ROLES)))


def capabilities(member):
    if not valid_member(member):
        return frozenset()
    role = member['role']
    if member['org_kind'] == 'factory':
        result = COMMON | FACTORY_READ | FACTORY_WRITE
        if role in {'planner', 'factory_admin'}:
            result |= {'demand.write', 'task.accept', 'task.confirm_buyer'}
        return result
    if role == 'coordinator':
        return COMMON | COORDINATOR
    if role == 'auditor':
        return COMMON | AUDITOR
    return COMMON | COORDINATOR | AUDITOR | {'workspace.administration'}


def can(member, capability):
    return capability in capabilities(member)


def home_endpoint(member):
    if not valid_member(member):
        abort(403)
    if member['org_kind'] == 'factory':
        return 'platform.enterprise_workspace'
    return {'coordinator': 'platform.coordinator_workspace',
            'auditor': 'platform.auditor_workspace',
            'admin': 'platform.administration_workspace'}[member['role']]


def workspace_title(member):
    if not valid_member(member):
        return '账号登录'
    if member['org_kind'] == 'factory':
        return '企业工作台'
    return {'coordinator': '平台协调工作台', 'auditor': '联盟链验证工作台',
            'admin': '平台管理工作台'}[member['role']]


PUBLIC = {'landing', 'login', 'demo_login', 'register', 'set_language', 'healthz'}
# Values are GET/HEAD and POST capabilities. A missing method is denied.
POLICY = {
    'logout': (None, 'account.use'),
    'dashboard': ('account.use', None),
    'methodology': ('help.read', None), 'review_guide': ('help.read', None),
    'history': ('algorithms.read', None),
    'new_experiment': ('algorithms.read', 'algorithms.run'),
    'experiment_result': ('algorithms.read', None),
    'download_csv': ('algorithms.read', None),
    'experiment_chart': ('algorithms.read', None),
    'delete_experiment': (None, 'algorithms.run'),
    'platform.workspace': ('account.use', None),
    'platform.enterprise_workspace': ('workspace.enterprise', None),
    'platform.coordinator_workspace': ('workspace.coordination', None),
    'platform.auditor_workspace': ('workspace.audit', None),
    'platform.administration_workspace': ('workspace.administration', None),
    'platform.resources': ('resources.read', 'supply.write'),
    'platform.demands': ('demands.read', 'demand.write'),
    'platform.plans': ('plans.read', None),
    'platform.plan_detail': ('plans.read', None),
    'platform.create_plan': (None, 'coordination.write'),
    'platform.reserve': (None, 'coordination.write'),
    'platform.commitments': ('tasks.read', None),
    'platform.commitment_detail': ('tasks.read', None),
    'platform.confirm': (None, 'task.confirm'),
    'platform.execute': (None, 'task.execute'),
    'platform.accept': (None, 'task.accept'),
    'platform.settle': (None, 'settlement.write'),
    'platform.trace': ('trace.read', None),
    'platform.export_trace': ('trace.export', None),
    'platform.verify_trace': (None, 'trace.verify'),
    'platform.valuation': ('valuation.read', 'valuation.write'),
    'platform.algorithms': ('algorithms.read', None),
    'platform.compare_algorithms': (None, 'algorithms.run'),
    'platform.comparison_detail': ('algorithms.read', None),
    'platform.comparison_export': ('algorithms.read', None),
    'platform.comparison_csv': ('algorithms.read', None),
    'platform.coordination_trials': ('algorithms.read', 'algorithms.run'),
    'platform.coordination_detail': ('runs.read', None),
    'platform.coordination_export': ('runs.read', None),
    'platform.coordination_csv': ('runs.read', None),
    'platform.cda_demo': ('algorithms.read', None),
    'platform.cda_chart': ('algorithms.read', None),
}


def private_static():
    filename = posixpath.normpath((request.view_args or {}).get('filename', '')).lstrip('/')
    return filename == 'generated' or filename.startswith('generated/')


def enforce_request():
    endpoint = request.endpoint
    if endpoint is None or endpoint in PUBLIC:
        return None
    if endpoint == 'static':
        if not private_static():
            return None
        # Business plots must use the experiment-ID owner-checked route.
        if g.user is None:
            return redirect(url_for('login'))
        abort(404)
    if g.user is None:
        return redirect(url_for('login'))
    if not valid_member(g.membership):
        abort(403)
    policy = POLICY.get(endpoint)
    capability = policy[0 if request.method in {'GET', 'HEAD'} else 1] if policy else None
    if not capability or not can(g.membership, capability):
        abort(403)
    return None
