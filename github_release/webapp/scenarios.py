"""Pre-filled manufacturing resource-sharing scenarios for the review demo.

The numbers are illustrative, not field data.  A buyer's values are its
willingness to pay for successive units of a shared resource; a seller's costs
are the reservation costs of the units it can release.
"""
from copy import deepcopy

STRATEGY_OPTIONS = [
    {
        "value": "Trader_AA",
        "label": "自适应进取（AA）",
        "short": "AA",
        "description": "根据市场状态调整报价，适合作为默认基准策略。",
    },
    {
        "value": "Trader_ZIC",
        "label": "零智能约束（ZIC）",
        "short": "ZIC",
        "description": "在估值/成本约束内随机报价，常用于对照实验。",
    },
    {
        "value": "Trader_ZIP",
        "label": "ZIP 学习型",
        "short": "ZIP",
        "description": "根据成交反馈调整利润 margin 的学习型报价策略。",
    },
    {
        "value": "Trader_GD",
        "label": "GD 双向改进",
        "short": "GD",
        "description": "沿当前最优买卖价逐步改进报价。",
    },
    {
        "value": "Trader_PS",
        "label": "PS 均衡逼近",
        "short": "PS",
        "description": "以均衡价格附近为目标的报价策略。",
    },
    {
        "value": "Trader_Kaplan",
        "label": "Kaplan 狙击",
        "short": "Kaplan",
        "description": "带狙击行为的报价策略，用于压力测试。",
    },
    {
        "value": "Trader_Shaver",
        "label": "Shaver 收敛",
        "short": "Shaver",
        "description": "逐步收窄价差的简单收敛策略。",
    },
]

STRATEGY_VALUES = {item["value"] for item in STRATEGY_OPTIONS}


def _buyers(rows):
    return [{"name": name, "values": values} for name, values in rows]


def _sellers(rows):
    return [{"name": name, "costs": costs} for name, costs in rows]


PRESETS = {
    "equipment": {
        "slug": "equipment",
        "title": "高端装备共享",
        "subtitle": "6 家需求企业 × 6 家供给企业 × 3 个机时",
        "badge": "推荐演示",
        "description": "五轴加工中心在多个制造企业之间共享，需求方按机时估值，供给方按可用机时报价。",
        "resource_type": "五轴数控加工中心机时",
        "unit_label": "机时",
        "notes": "示例数据：6 家需求企业、6 家资源供给企业，每家企业最多交易 3 个机时。",
        "price_floor": 0,
        "price_ceiling": 400,
        "periods": 3,
        "rounds": 20,
        "seed": 2026,
        "buyer_strategy": "Trader_AA",
        "seller_strategy": "Trader_AA",
        "buyers": _buyers([
            ("华航精密", [360, 340, 320]),
            ("恒远装备", [350, 330, 300]),
            ("东辰制造", [340, 310, 290]),
            ("联合机电", [320, 300, 280]),
            ("启明工业", [300, 280, 260]),
            ("远大重工", [280, 260, 240]),
        ]),
        "sellers": _sellers([
            ("精工机床", [120, 140, 160]),
            ("国盛智能", [130, 150, 170]),
            ("海川装备", [150, 170, 190]),
            ("中科数控", [170, 190, 210]),
            ("锐虎机械", [190, 210, 230]),
            ("金鼎制造", [210, 230, 250]),
        ]),
        "expected": "示例均衡约 17 个单位，均衡价区间约 240–250。",
    },
    "emergency": {
        "slug": "emergency",
        "title": "紧急产能撮合",
        "subtitle": "5 家需求企业 × 5 家供给企业 × 2 个班次",
        "badge": "冲击场景",
        "description": "订单突然增加时，区域共享工厂用剩余班次响应紧急需求，适合观察价格与效率变化。",
        "resource_type": "应急精密加工产能",
        "unit_label": "班次",
        "notes": "示例数据：订单激增场景，5 家需求方与 5 家供给方，每家最多 2 个班次。",
        "price_floor": 0,
        "price_ceiling": 600,
        "periods": 5,
        "rounds": 15,
        "seed": 2027,
        "buyer_strategy": "Trader_AA",
        "seller_strategy": "Trader_ZIC",
        "buyers": _buyers([
            ("启明汽车零部件", [520, 490]),
            ("宏达医疗设备", [500, 460]),
            ("新锐新能源", [470, 430]),
            ("北方重工", [450, 410]),
            ("智造电子", [420, 380]),
        ]),
        "sellers": _sellers([
            ("长三角共享工厂", [180, 210]),
            ("中原精工", [200, 230]),
            ("西南智造", [220, 250]),
            ("环渤海机加", [240, 270]),
            ("珠三角模具", [360, 390]),
        ]),
        "expected": "示例均衡约 9 个单位，均衡价区间约 380–390。",
    },
    "mold": {
        "slug": "mold",
        "title": "区域模具协同",
        "subtitle": "8 家需求企业 × 7 家供给企业 × 1 套模具",
        "badge": "多主体市场",
        "description": "区域模具资源以整套方式共享，用于观察更多参与者下的价格收敛与成交效率。",
        "resource_type": "注塑模具共享",
        "unit_label": "套",
        "notes": "示例数据：8 家需求企业与 7 家模具供给企业，每家交易 1 套。",
        "price_floor": 50,
        "price_ceiling": 500,
        "periods": 4,
        "rounds": 25,
        "seed": 2028,
        "buyer_strategy": "Trader_AA",
        "seller_strategy": "Trader_AA",
        "buyers": _buyers([
            ("瑞康医疗", [480]),
            ("安拓电气", [455]),
            ("云帆装备", [430]),
            ("华宇精工", [405]),
            ("德胜制造", [380]),
            ("中联模塑", [355]),
            ("天工智造", [330]),
            ("海岳工业", [305]),
        ]),
        "sellers": _sellers([
            ("南方模具", [160]),
            ("精诚模架", [190]),
            ("恒达塑模", [220]),
            ("东盛模具", [250]),
            ("宏源精模", [280]),
            ("三江模塑", [310]),
            ("北辰模具", [340]),
        ]),
        "expected": "示例均衡约 6 个单位，均衡价区间约 330–340。",
    },
    "blank": {
        "slug": "blank",
        "title": "从空白开始",
        "subtitle": "3 家需求企业 × 3 家供给企业 × 2 个单位",
        "badge": "自定义",
        "description": "用自己的企业、估值和成本数据构建实验。",
        "resource_type": "",
        "unit_label": "单位",
        "notes": "",
        "price_floor": 0,
        "price_ceiling": 500,
        "periods": 1,
        "rounds": 20,
        "seed": 2026,
        "buyer_strategy": "Trader_AA",
        "seller_strategy": "Trader_AA",
        "buyers": _buyers([("需求企业 A", []), ("需求企业 B", []), ("需求企业 C", [])]),
        "sellers": _sellers([("供给企业 A", []), ("供给企业 B", []), ("供给企业 C", [])]),
        "expected": "填写每家企业的逐单位估值/成本后运行。",
    },
}


def get_preset(slug):
    """Return a deep copy so request handlers cannot mutate module state."""
    return deepcopy(PRESETS.get(slug, PRESETS["equipment"]))


def preset_cards():
    keys = ("equipment", "emergency", "mold")
    return [
        {
            "slug": PRESETS[key]["slug"],
            "title": PRESETS[key]["title"],
            "subtitle": PRESETS[key]["subtitle"],
            "description": PRESETS[key]["description"],
            "badge": PRESETS[key]["badge"],
        }
        for key in keys
    ]


def form_from_preset(preset):
    """Convert a scenario into the form structure used by the template."""
    return {
        "name": "",
        "resource_type": preset["resource_type"],
        "unit_label": preset["unit_label"],
        "notes": preset["notes"],
        "price_floor": preset["price_floor"],
        "price_ceiling": preset["price_ceiling"],
        "periods": preset["periods"],
        "rounds": preset["rounds"],
        "seed": preset["seed"],
        "buyer_strategy": preset["buyer_strategy"],
        "seller_strategy": preset["seller_strategy"],
        "buyer_rows": [
            {"name": row["name"], "values": ", ".join(str(v) for v in row["values"])}
            for row in preset["buyers"]
        ],
        "seller_rows": [
            {"name": row["name"], "values": ", ".join(str(v) for v in row["costs"])}
            for row in preset["sellers"]
        ],
    }
