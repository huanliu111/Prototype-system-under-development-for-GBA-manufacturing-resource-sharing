"""Small server-side translation layer for the public entry page.

The operating prototype has a large Chinese domain vocabulary.  This layer is
deliberately explicit: untranslated domain content falls back to Chinese rather
than being machine-translated in place.
"""
from __future__ import annotations

SUPPORTED = {'zh': '中文', 'en': 'English'}

TEXTS = {
    'zh': {
        'brand.platform': '粤港澳大湾区制造资源协同服务平台',
        'brand.platform_short': '制造资源协同平台',
        'brand.team': '暨南大学 · 制造物联网团队',
        'login.title': '登录平台',
        'login.region': '粤港澳大湾区',
        'login.hero': '制造资源协同平台',
        'login.description': '办理区域内工厂的资源供需、共享任务与结算。',
        'login.heading': '账号登录',
        'login.subtitle': '使用本企业或联盟岗位账号登录。',
        'login.account': '账号',
        'login.password': '密码',
        'login.account_placeholder': '输入登录账号',
        'login.password_placeholder': '输入密码',
        'login.show': '显示',
        'login.hide': '隐藏',
        'login.submit': '登录',
        'login.invalid': '账号或密码不正确。',
        'login.test_accounts': '开发测试账号',
        'login.switch_hint': '点击“进入”切换岗位',
        'login.enter': '进入',
        'login.environment': '当前环境使用合成业务数据与记账式结算。',
        'login.no_accounts': '账号由平台运营方分配。账号开通或岗位调整请联系平台管理员。',
        'login.footer': 'MIoT · 暨南大学制造物联网团队',
        'login.language': 'English',
        'login.language_aria': '切换为英文',
        'login.columns.company_role': '企业 / 岗位',
        'login.columns.account': '账号',
        'login.columns.password': '密码',
        'login.role.demo': '广州精工制造有限公司 · 计划员',
        'login.role.beta': '东莞共享加工中心 · 设备主管',
        'login.role.gamma': '佛山智能制造有限公司 · 生产主管',
        'login.role.delta': '深圳精密制造有限公司 · 设备主管',
        'login.role.epsilon': '惠州智能装备有限公司 · 生产主管',
        'login.role.coordinator': '平台协调员',
        'login.role.reviewer': '联盟链验证员',
    },
    'en': {
        'brand.platform': 'Greater Bay Area Manufacturing Resource Coordination Platform',
        'brand.platform_short': 'Manufacturing Resource Platform',
        'brand.team': 'Jinan University · Manufacturing IoT Team',
        'login.title': 'Sign in',
        'login.region': 'Guangdong–Hong Kong–Macao Greater Bay Area',
        'login.hero': 'Manufacturing Resource Coordination Platform',
        'login.description': 'Coordinate factory capacity, shared tasks and settlement across the region.',
        'login.heading': 'Account sign-in',
        'login.subtitle': 'Sign in with a factory or alliance role.',
        'login.account': 'Account',
        'login.password': 'Password',
        'login.account_placeholder': 'Enter account',
        'login.password_placeholder': 'Enter password',
        'login.show': 'Show',
        'login.hide': 'Hide',
        'login.submit': 'Sign in',
        'login.invalid': 'Incorrect account or password.',
        'login.test_accounts': 'Development test accounts',
        'login.switch_hint': 'Click “Enter” to switch role',
        'login.enter': 'Enter',
        'login.environment': 'Synthetic business data and book-entry settlement are used in this environment.',
        'login.no_accounts': 'Accounts are issued by the platform operator. Contact the administrator for access or role changes.',
        'login.footer': 'MIoT · Jinan University Manufacturing IoT Team',
        'login.language': 'Chinese',
        'login.language_aria': 'Switch to Chinese',
        'login.columns.company_role': 'Company / role',
        'login.columns.account': 'Account',
        'login.columns.password': 'Password',
        'login.role.demo': 'Guangzhou Precision Manufacturing Co., Ltd. · Planner',
        'login.role.beta': 'Dongguan Shared Machining Center · Equipment Supervisor',
        'login.role.gamma': 'Foshan Intelligent Manufacturing Co., Ltd. · Production Supervisor',
        'login.role.delta': 'Shenzhen Precision Manufacturing Co., Ltd. · Equipment Supervisor',
        'login.role.epsilon': 'Huizhou Intelligent Equipment Co., Ltd. · Production Supervisor',
        'login.role.coordinator': 'Platform Coordinator',
        'login.role.reviewer': 'Consortium Ledger Verifier',
    },
}


def normalize(lang: str | None) -> str:
    return lang if lang in SUPPORTED else 'zh'


def translate(key: str, lang: str = 'zh', **kwargs) -> str:
    value = TEXTS.get(normalize(lang), TEXTS['zh']).get(key, key)
    return value.format(**kwargs) if kwargs else value
