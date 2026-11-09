"""Clean service wrapper around the original double-auction research engine.

The original GUI is stateful and prints heavily.  This module turns a validated
manufacturing scenario into one or more independent market periods and returns a
JSON-serialisable result object for the web layer.
"""
from __future__ import annotations

import contextlib
import io
import math
import random
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np

from Environment.spot_environment_model import SpotEnvironmentModel
from Institution.double_auction_institution import Auction
from Institution.spot_system import SpotSystem
from webapp.scenarios import STRATEGY_VALUES

SIM_LOCK = threading.Lock()

MAX_BUYERS = 30
MAX_SELLERS = 30
MAX_UNITS = 8
MAX_TRADERS = 40
MAX_PERIODS = 10
MAX_ROUNDS = 100


class SimulationError(ValueError):
    """A user-correctable error in the market configuration."""


def parse_number_list(value: Any) -> list[int]:
    """Parse a comma/space separated list of integers from a form value."""
    if isinstance(value, (list, tuple)):
        parts = list(value)
    elif value is None:
        parts = []
    else:
        text = str(value).strip()
        if not text:
            return []
        # Accept commas, Chinese commas, semicolons and whitespace.
        parts = [part for part in re.split(r"[,，;；\s]+", text) if part.strip()]

    numbers: list[int] = []
    for part in parts:
        if isinstance(part, str):
            part = part.strip()
            if not part:
                continue
        try:
            number = int(part)
        except (TypeError, ValueError):
            raise SimulationError("估值/成本必须是整数，请检查：%s" % part)
        if number < 0:
            raise SimulationError("估值/成本不能为负数：%s" % part)
        numbers.append(number)
    return numbers


def _normalise_side(rows: Any, value_key: str, label: str) -> list[dict]:
    if not isinstance(rows, list):
        raise SimulationError("%s数据格式不正确" % label)

    cleaned = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise SimulationError("%s第 %d 行格式不正确" % (label, index))
        name = str(row.get("name", "")).strip()
        values = parse_number_list(row.get(value_key, []))
        if not name and not values:
            continue
        if not name:
            raise SimulationError("%s第 %d 行缺少企业名称" % (label, index))
        if len(name) > 40:
            raise SimulationError("%s第 %d 行名称过长（最多 40 个字符）" % (label, index))
        if not values:
            raise SimulationError("“%s”尚未填写逐单位%s" % (name, "估值" if value_key == "values" else "成本"))
        cleaned.append({"name": name, value_key: values})
    return cleaned


def validate_config(config: dict) -> dict:
    """Validate and normalise a scenario dict.

    Expected shape::

        {
          "name": "...", "resource_type": "...", "unit_label": "...",
          "buyers": [{"name": "...", "values": [100, 90]}],
          "sellers": [{"name": "...", "costs": [40, 50]}],
          "price_floor": 0, "price_ceiling": 400,
          "periods": 3, "rounds": 20, "seed": 2026,
          "buyer_strategy": "Trader_AA", "seller_strategy": "Trader_AA"
        }
    """
    if not isinstance(config, dict):
        raise SimulationError("实验参数格式不正确")

    buyers = _normalise_side(config.get("buyers"), "values", "需求企业")
    sellers = _normalise_side(config.get("sellers"), "costs", "供给企业")
    if not buyers:
        raise SimulationError("至少需要 1 家需求企业")
    if not sellers:
        raise SimulationError("至少需要 1 家资源供给企业")
    if len(buyers) > MAX_BUYERS or len(sellers) > MAX_SELLERS:
        raise SimulationError("需求/供给企业数量不能超过 %d 家" % MAX_BUYERS)
    if len(buyers) + len(sellers) > MAX_TRADERS:
        raise SimulationError("交易者总数不能超过 %d 个" % MAX_TRADERS)

    unit_counts = {len(row["values"]) for row in buyers} | {len(row["costs"]) for row in sellers}
    if len(unit_counts) != 1:
        raise SimulationError("每家企业的逐单位估值/成本数量必须一致，当前为 %s" % sorted(unit_counts))
    units = unit_counts.pop()
    if units < 1 or units > MAX_UNITS:
        raise SimulationError("每家企业可交易单位数必须在 1–%d 之间" % MAX_UNITS)

    try:
        price_floor = int(config.get("price_floor", 0))
        price_ceiling = int(config.get("price_ceiling", 0))
        periods = int(config.get("periods", 1))
        rounds = int(config.get("rounds", 20))
        seed = int(config.get("seed", 2026))
    except (TypeError, ValueError):
        raise SimulationError("价格上下限、周期、轮次和随机种子必须是整数")

    if price_floor < 0:
        raise SimulationError("价格下限不能为负数")
    if price_ceiling <= price_floor:
        raise SimulationError("价格上限必须大于价格下限")
    if not (1 <= periods <= MAX_PERIODS):
        raise SimulationError("交易周期必须在 1–%d 之间" % MAX_PERIODS)
    if not (1 <= rounds <= MAX_ROUNDS):
        raise SimulationError("每周期轮次必须在 1–%d 之间" % MAX_ROUNDS)
    if seed < 0:
        raise SimulationError("随机种子必须为非负整数")

    buyer_strategy = str(config.get("buyer_strategy", "Trader_AA"))
    seller_strategy = str(config.get("seller_strategy", "Trader_AA"))
    if buyer_strategy not in STRATEGY_VALUES:
        raise SimulationError("不支持的需求方报价策略")
    if seller_strategy not in STRATEGY_VALUES:
        raise SimulationError("不支持的供给方报价策略")

    name = str(config.get("name", "")).strip() or "未命名实验"
    resource_type = str(config.get("resource_type", "")).strip() or "制造资源共享"
    unit_label = str(config.get("unit_label", "")).strip() or "单位"
    notes = str(config.get("notes", "")).strip()

    return {
        "name": name[:80],
        "resource_type": resource_type[:80],
        "unit_label": unit_label[:20],
        "notes": notes[:500],
        "buyers": buyers,
        "sellers": sellers,
        "units": units,
        "price_floor": price_floor,
        "price_ceiling": price_ceiling,
        "periods": periods,
        "rounds": rounds,
        "seed": seed,
        "buyer_strategy": buyer_strategy,
        "seller_strategy": seller_strategy,
    }


def _market_from_config(config: dict) -> tuple[SpotEnvironmentModel, dict]:
    buyers = config["buyers"]
    sellers = config["sellers"]
    units = config["units"]
    mkt = SpotEnvironmentModel()
    mkt.set_market_parms(["web", len(buyers), len(sellers), units])
    mkt.make_market({
        "buyers": {index: row["values"] for index, row in enumerate(buyers)},
        "sellers": {index: row["costs"] for index, row in enumerate(sellers)},
    })
    mkt.make_supply()
    mkt.make_demand()
    equilibrium = mkt.get_equilibrium()
    quantity, price_low, price_high, max_surplus = equilibrium
    if quantity <= 0 or max_surplus <= 0:
        raise SimulationError("当前供需曲线没有形成可行成交区间：请提高需求估值或降低供给成本。")
    return mkt, {
        "quantity": int(quantity),
        "price_low": int(price_low),
        "price_high": int(price_high),
        "max_surplus": int(max_surplus),
    }


def _round_id(trader_id: str) -> int:
    try:
        return int(trader_id[1:])
    except (TypeError, ValueError):
        return -1


def _entity_index(entity_id: str) -> int:
    """Extract the numeric suffix from ids such as buyer3 / seller2 / t7."""
    digits = "".join(character for character in str(entity_id) if character.isdigit())
    try:
        return int(digits)
    except (TypeError, ValueError):
        return -1


def run_simulation(config: dict) -> dict:
    """Run all periods and return contract-, period-, and trader-level results."""
    config = validate_config(config)
    started = time.perf_counter()

    with SIM_LOCK:
        mkt, equilibrium = _market_from_config(config)
        buyers = config["buyers"]
        sellers = config["sellers"]
        num_buyers = len(buyers)
        num_sellers = len(sellers)
        limits = (config["price_ceiling"], config["price_floor"])
        strategy_list = [config["buyer_strategy"]] * num_buyers + [config["seller_strategy"]] * num_sellers

        periods_result: list[dict] = []
        contracts_result: list[dict] = []
        trader_aggregate: dict[str, dict] = {}
        total_orders = 0
        contract_no = 0

        for period in range(config["periods"]):
            random.seed(config["seed"] + period * 10007)
            np.random.seed(config["seed"] + period * 10007)

            system = SpotSystem()
            system.mkt = mkt
            system.limits = limits
            system.num_market_rounds = config["rounds"]
            system.da = Auction("da", limits[0], limits[1])

            captured = io.StringIO()
            try:
                with contextlib.redirect_stdout(captured):
                    system.init_traders(strategy_list, period)
                    system.run_system(0, "", "", num_buyers, num_sellers)
                    evaluation = system.eval()
            except Exception as exc:  # keep a useful UI error rather than a raw traceback
                raise SimulationError("拍卖运行失败（第 %d 个周期）：%s" % (period + 1, exc)) from exc

            contracts = list(system.da.report_contracts())
            orders = list(system.da.report_orders())
            total_orders += len(orders)
            period_actual = int(evaluation[7])
            period_max = int(evaluation[6])
            period_eff = float(evaluation[8])
            prices = [float(contract[0]) for contract in contracts]
            avg_price = sum(prices) / len(prices) if prices else 0.0

            periods_result.append({
                "period": period + 1,
                "contracts": len(contracts),
                "avg_price": round(avg_price, 2),
                "actual_surplus": period_actual,
                "max_surplus": period_max,
                "efficiency": round(period_eff, 2),
                "alpha": round(float(system.alphas[-1]), 3) if system.alphas else 0.0,
                "trade_ratio": round(float(system.trade_ratio_list[-1]), 4) if system.trade_ratio_list else 0.0,
                "orders": len(orders),
            })

            buyer_units: dict[int, int] = {index: 0 for index in range(num_buyers)}
            seller_units: dict[int, int] = {index: 0 for index in range(num_sellers)}
            for contract in contracts:
                contract_no += 1
                price = float(contract[0])
                buyer_index = _round_id(contract[1])
                seller_index = _round_id(contract[2]) - num_buyers
                if not (0 <= buyer_index < num_buyers and 0 <= seller_index < num_sellers):
                    continue
                buyer = buyers[buyer_index]
                seller = sellers[seller_index]
                buyer_unit = min(buyer_units[buyer_index], len(buyer["values"]) - 1)
                seller_unit = min(seller_units[seller_index], len(seller["costs"]) - 1)
                value = int(buyer["values"][buyer_unit])
                cost = int(seller["costs"][seller_unit])
                buyer_units[buyer_index] += 1
                seller_units[seller_index] += 1
                contracts_result.append({
                    "no": contract_no,
                    "period": period + 1,
                    "price": round(price, 2),
                    "buyer_id": "B%02d" % (buyer_index + 1),
                    "buyer": buyer["name"],
                    "seller_id": "S%02d" % (seller_index + 1),
                    "seller": seller["name"],
                    "value": value,
                    "cost": cost,
                    "surplus": round(value - cost, 2),
                    "buyer_profit": round(value - price, 2),
                    "seller_profit": round(price - cost, 2),
                })

            for trader_id, info in system.trader_info.items():
                index = _round_id(trader_id)
                if index < 0:
                    continue
                if index < num_buyers:
                    name = buyers[index]["name"]
                    role = "buyer"
                    role_label = "需求企业"
                    quantities = buyers[index]["values"]
                    trader_key = "B%02d" % (index + 1)
                else:
                    seller_index = index - num_buyers
                    name = sellers[seller_index]["name"]
                    role = "seller"
                    role_label = "供给企业"
                    quantities = sellers[seller_index]["costs"]
                    trader_key = "S%02d" % (seller_index + 1)

                aggregate = trader_aggregate.setdefault(trader_key, {
                    "id": trader_key,
                    "name": name,
                    "role": role,
                    "role_label": role_label,
                    "strategy": info.get("strat", ""),
                    "quantities": quantities,
                    "units": 0,
                    "earnings": 0.0,
                })
                aggregate["units"] += int(info.get("units", 0))
                aggregate["earnings"] += float(info.get("earn", 0.0))

        total_contracts = len(contracts_result)
        total_surplus = sum(period["actual_surplus"] for period in periods_result)
        total_max = equilibrium["max_surplus"] * config["periods"]
        overall_efficiency = (total_surplus / total_max * 100.0) if total_max else 0.0
        avg_efficiency = (
            sum(period["efficiency"] for period in periods_result) / len(periods_result)
            if periods_result else 0.0
        )
        all_prices = [contract["price"] for contract in contracts_result]
        avg_price = sum(all_prices) / len(all_prices) if all_prices else 0.0
        avg_alpha = (
            sum(period["alpha"] for period in periods_result) / len(periods_result)
            if periods_result else 0.0
        )
        avg_trade_ratio = (
            sum(period["trade_ratio"] for period in periods_result) / len(periods_result)
            if periods_result else 0.0
        )

        for aggregate in trader_aggregate.values():
            aggregate["earnings"] = round(aggregate["earnings"], 2)
            aggregate["efficiency"] = round(
                aggregate["earnings"] / total_max * 100.0 if total_max else 0.0, 2
            )

        duration_ms = int((time.perf_counter() - started) * 1000)
        curve = {
            "demand_values": [int(value) for value in mkt.env["dem"]],
            "supply_values": [int(value) for value in mkt.env["sup"]],
            "demand_points": [
                {"name": buyers[_entity_index(trader_id)]["name"], "value": int(value)}
                for trader_id, value in mkt.env["demand"]
            ],
            "supply_points": [
                {"name": sellers[_entity_index(trader_id)]["name"], "value": int(value)}
                for trader_id, value in mkt.env["supply"]
            ],
        }

        return {
            "config": config,
            "equilibrium": equilibrium,
            "curve": curve,
            "periods": periods_result,
            "contracts": contracts_result,
            "traders": sorted(trader_aggregate.values(), key=lambda row: (row["role"] != "buyer", row["id"])),
            "summary": {
                "periods": config["periods"],
                "rounds": config["rounds"],
                "num_buyers": num_buyers,
                "num_sellers": num_sellers,
                "num_traders": num_buyers + num_sellers,
                "units": config["units"],
                "total_contracts": total_contracts,
                "total_orders": total_orders,
                "avg_price": round(avg_price, 2),
                "total_surplus": int(total_surplus),
                "max_surplus": int(total_max),
                "overall_efficiency": round(overall_efficiency, 2),
                "avg_efficiency": round(avg_efficiency, 2),
                "avg_alpha": round(avg_alpha, 3),
                "avg_trade_ratio": round(avg_trade_ratio, 4),
                "duration_ms": duration_ms,
                "eq_quantity": equilibrium["quantity"],
                "eq_price_low": equilibrium["price_low"],
                "eq_price_high": equilibrium["price_high"],
            },
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
