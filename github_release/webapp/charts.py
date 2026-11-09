"""Server-side charts for experiment results (no external JS/CDN required)."""
from __future__ import annotations

import os
import threading
from pathlib import Path

os.environ.setdefault("MPLBACKEND", "Agg")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if not os.environ.get("MPLCONFIGDIR"):
    os.environ["MPLCONFIGDIR"] = str(_PROJECT_ROOT / ".cache" / "matplotlib")
os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)

from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

INK = "#0f172a"
MUTED = "#64748b"
GRID = "#dbe4ef"
TEAL = "#0f766e"
TEAL_LIGHT = "#5eead4"
AMBER = "#f59e0b"
BLUE = "#2563eb"
PAPER = "#ffffff"
PANEL = "#f8fafc"
PERIOD_COLORS = ["#0f766e", "#2563eb", "#f59e0b", "#7c3aed", "#db2777", "#0891b2", "#65a30d", "#ea580c"]
CHART_LOCK = threading.Lock()


def _base_figure(width=8.2, height=4.4):
    fig = Figure(figsize=(width, height), dpi=160, facecolor=PAPER)
    ax = fig.add_subplot(111)
    ax.set_facecolor(PANEL)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.7, alpha=0.8)
    ax.set_axisbelow(True)
    return fig, ax


def _finish(fig, path):
    fig.tight_layout(pad=1.4)
    fig.savefig(path, dpi=160, facecolor=PAPER, bbox_inches="tight")
    path.chmod(0o644)
    fig.clear()


def _watermark(fig):
    fig.text(
        0.985, 0.015, "BidShare Lab · research prototype",
        ha="right", va="bottom", fontsize=7.5, color="#94a3b8",
    )


def _supply_demand_chart(result, path):
    curve = result["curve"]
    eq = result["equilibrium"]
    fig, ax = _base_figure()
    demand = curve["demand_values"]
    supply = curve["supply_values"]
    ax.step(range(len(demand)), demand, where="post", color="#dc2626", linewidth=2.1, label="Demand")
    ax.step(range(len(supply)), supply, where="post", color=TEAL, linewidth=2.1, label="Supply")

    low, high = eq["price_low"], eq["price_high"]
    if high > low:
        ax.axhspan(low, high, color=AMBER, alpha=0.16, label="Equilibrium band")
        ax.axhline(low, color=AMBER, linewidth=1.0, linestyle=(0, (2, 3)))
        ax.axhline(high, color=AMBER, linewidth=1.0, linestyle=(0, (2, 3)))
        ax.annotate(
            "%s–%s" % (low, high), xy=(0.01, (low + high) / 2),
            xycoords=("axes fraction", "data"), ha="left", va="center",
            fontsize=9, color="#92400e", fontweight="bold",
        )
    else:
        ax.axhline(high, color=AMBER, linewidth=1.3, linestyle="--", label="Equilibrium price")
        ax.annotate(
            str(high), xy=(0.01, high), xycoords=("axes fraction", "data"),
            ha="left", va="bottom", fontsize=9, color="#92400e", fontweight="bold",
        )

    ax.axvline(eq["quantity"], color=MUTED, linewidth=1.0, linestyle=":", alpha=0.8)
    ax.annotate(
        "Q* = %d" % eq["quantity"], xy=(eq["quantity"], 0.02),
        xycoords=("data", "axes fraction"), xytext=(8, 0), textcoords="offset points",
        fontsize=9, color=MUTED, va="bottom",
    )
    ax.set_title("Supply, demand and equilibrium", loc="left", fontsize=13, color=INK, pad=12, fontweight="bold")
    ax.set_xlabel("Tradable units", fontsize=9.5, color=MUTED)
    ax.set_ylabel("Value / cost", fontsize=9.5, color=MUTED)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.19), ncol=3,
              frameon=False, fontsize=9)
    _watermark(fig)
    _finish(fig, path)


def _price_chart(result, path):
    fig, ax = _base_figure()
    contracts = result["contracts"]
    eq = result["equilibrium"]
    if contracts:
        x = [item["no"] for item in contracts]
        y = [item["price"] for item in contracts]
        colors = [PERIOD_COLORS[(item["period"] - 1) % len(PERIOD_COLORS)] for item in contracts]
        ax.scatter(x, y, c=colors, s=28, zorder=3, linewidths=0.5, edgecolors="white")
        ax.plot(x, y, color="#94a3b8", linewidth=0.9, alpha=0.7, zorder=2)
        low, high = eq["price_low"], eq["price_high"]
        if high > low:
            ax.axhspan(low, high, color=AMBER, alpha=0.14)
            ax.axhline((low + high) / 2, color=AMBER, linestyle="--", linewidth=1.1, label="Equilibrium midpoint")
        else:
            ax.axhline(high, color=AMBER, linestyle="--", linewidth=1.1, label="Equilibrium price")

        cursor = 0
        for period in result["periods"]:
            cursor += period["contracts"]
            if cursor < len(contracts):
                ax.axvline(cursor + 0.5, color=GRID, linewidth=1.0, linestyle=":")
        ax.set_xlim(0.5, max(x) + 0.8)
        ax.set_title("Contract prices over the auction", loc="left", fontsize=13, color=INK, pad=12, fontweight="bold")
        ax.set_xlabel("Contract number", fontsize=9.5, color=MUTED)
        ax.set_ylabel("Transaction price", fontsize=9.5, color=MUTED)
        ax.legend(loc="upper right", frameon=False, fontsize=9)
    else:
        ax.text(0.5, 0.55, "No contracts in this run", transform=ax.transAxes, ha="center", va="center", color=MUTED, fontsize=13)
        ax.set_title("Contract prices over the auction", loc="left", fontsize=13, color=INK, pad=12, fontweight="bold")
    _watermark(fig)
    _finish(fig, path)


def _efficiency_chart(result, path):
    fig, ax = _base_figure()
    periods = result["periods"]
    labels = [str(period["period"]) for period in periods]
    efficiencies = [period["efficiency"] for period in periods]
    positions = list(range(len(periods)))
    bars = ax.bar(positions, efficiencies, color=TEAL, width=0.56, alpha=0.9, label="Allocative efficiency")
    for bar, value in zip(bars, efficiencies):
        ax.annotate(
            ("%.0f%%" % value), xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
            xytext=(0, 4), textcoords="offset points", ha="center", va="bottom",
            fontsize=8.5, color=TEAL, fontweight="bold",
        )
    ax.set_ylim(0, max(110, max(efficiencies or [0]) + 8))
    ax.set_xticks(positions)
    ax.set_xticklabels(labels)
    ax.set_title("Efficiency and trade ratio by period", loc="left", fontsize=13, color=INK, pad=12, fontweight="bold")
    ax.set_xlabel("Period", fontsize=9.5, color=MUTED)
    ax.set_ylabel("Efficiency (%)", fontsize=9.5, color=MUTED)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: "%.0f" % value))

    ax2 = ax.twinx()
    ax2.plot(positions, [period["trade_ratio"] * 100 for period in periods],
             color=BLUE, marker="o", markersize=4, linewidth=1.6, label="Trade ratio")
    ax2.set_ylim(0, max(110, max((p["trade_ratio"] * 100 for p in periods), default=0) + 10))
    ax2.set_ylabel("Trade ratio (%)", fontsize=9.5, color=BLUE)
    ax2.tick_params(axis="y", colors=BLUE, labelsize=9)
    ax2.spines["top"].set_visible(False)
    ax2.spines["right"].set_color("#bfdbfe")
    ax2.grid(False)

    handles, labels = ax.get_legend_handles_labels()
    handles2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(handles + handles2, labels + labels2, loc="lower right", frameon=False, fontsize=9)
    _watermark(fig)
    _finish(fig, path)


def generate_charts(result: dict, prefix: str, output_dir) -> dict:
    """Render the three result charts and return public URL paths."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "supply_demand": output_dir / ("%s_supply_demand.png" % prefix),
        "prices": output_dir / ("%s_prices.png" % prefix),
        "efficiency": output_dir / ("%s_efficiency.png" % prefix),
    }
    with CHART_LOCK:
        _supply_demand_chart(result, paths["supply_demand"])
        _price_chart(result, paths["prices"])
        _efficiency_chart(result, paths["efficiency"])
    return {key: "/static/generated/%s" % path.name for key, path in paths.items()}


def delete_charts(chart_data, output_dir):
    if not chart_data:
        return
    output_dir = Path(output_dir)
    for relative in chart_data.values():
        if isinstance(relative, str) and relative.startswith("/static/generated/"):
            target = output_dir / relative.split("/")[-1]
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
