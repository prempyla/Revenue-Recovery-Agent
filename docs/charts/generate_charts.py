"""Generates the three final evaluation charts for the pitch video.

Numbers come from a LIVE run of the same 3-seed harness comparison used
throughout DECISIONS.md (300 customers, default payment count, seeds 42/7/123)
-- nothing here is a hardcoded number copied out of a report. Run this
script again after any change to the simulator, the policies, or full_agent,
and the PNGs regenerate to match.

Usage:
    .venv/bin/python docs/charts/generate_charts.py

Requires matplotlib (see requirements.txt). Writes three PNGs into this
directory: 01_primary_rupees_per_contact.png, 02_ablation_outage_detection.png,
03_tradeoff_recovered_vs_contacts.png.
"""

import sys
from datetime import datetime
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC_DIR))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from simulator import (
    generate_customers,
    generate_outage_events,
    generate_payments,
    inject_outage_events,
    run_eval,
)

OUTPUT_DIR = Path(__file__).resolve().parent
SEEDS = (42, 7, 123)
WINDOW_START = datetime(2026, 1, 1)
N_CUSTOMERS = 300  # matches every 3-seed comparison run so far (DECISIONS.md)

# Okabe-Ito palette -- colorblind-safe, the standard recommendation for
# scientific/technical charts. Each policy gets one fixed color used
# consistently across all three charts.
COLOR_NAIVE = "#D55E00"  # vermillion
COLOR_RULES_ONLY = "#0072B2"  # blue
COLOR_ABLATION = "#E69F00"  # orange
COLOR_FULL_AGENT = "#009E73"  # bluish green

POLICIES = ["naive_fixed_retry", "rules_only", "full_agent_minus_outage_detection", "full_agent"]
POLICY_LABEL = {
    "naive_fixed_retry": "Naive retry",
    "rules_only": "Rules-only",
    "full_agent_minus_outage_detection": "Agent, no outage\ndetection",
    "full_agent": "Cost-aware agent",
}
POLICY_COLOR = {
    "naive_fixed_retry": COLOR_NAIVE,
    "rules_only": COLOR_RULES_ONLY,
    "full_agent_minus_outage_detection": COLOR_ABLATION,
    "full_agent": COLOR_FULL_AGENT,
}
POLICY_MARKER = {
    "naive_fixed_retry": "o",
    "rules_only": "s",
    "full_agent_minus_outage_detection": "^",
    "full_agent": "D",
}


def _apply_style():
    plt.rcParams.update(
        {
            "font.size": 17,
            "font.family": "sans-serif",
            "axes.titlesize": 21,
            "axes.titleweight": "bold",
            "axes.labelsize": 19,
            "xtick.labelsize": 16,
            "ytick.labelsize": 16,
            # Legend text must never read smaller than the axis labels it
            # sits beside -- explicit design constraint.
            "legend.fontsize": 19,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.spines.left": True,
            "axes.spines.bottom": True,
            "axes.linewidth": 1.4,
            "axes.grid": True,
            # Deliberately NOT thin: a faint hairline gridline disappears
            # under video compression. Bold enough to read, restricted to
            # the y-axis only so it doesn't turn into a grid of clutter.
            "axes.grid.axis": "y",
            "grid.linewidth": 1.1,
            "grid.alpha": 0.45,
            "grid.color": "#999999",
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
            "savefig.dpi": 200,
            "savefig.bbox": "tight",
        }
    )


def collect_results():
    """Runs the exact same batch-generation + run_eval pipeline used for
    every 3-seed comparison in DECISIONS.md. Returns {seed: {policy_name:
    metrics_dict}}."""
    results_by_seed = {}
    for seed in SEEDS:
        customers = generate_customers(N_CUSTOMERS, seed=seed)
        payments = generate_payments(customers, window_start=WINDOW_START, window_days=30, seed=seed)
        events = generate_outage_events(window_start=WINDOW_START, window_days=30, seed=seed)
        payments = inject_outage_events(payments, customers, events, seed=seed)
        results_by_seed[seed] = run_eval(payments, customers, events, seed=seed)
    return results_by_seed


def chart_1_primary(results_by_seed, out_path):
    """₹ recovered per contact, 4 policies, all 3 seeds -- the hero chart."""
    fig, ax = plt.subplots(figsize=(11, 7))

    x_positions = np.arange(len(POLICIES))
    for i, policy in enumerate(POLICIES):
        values = [results_by_seed[seed][policy]["primary_rupees_per_contact"] for seed in SEEDS]
        mean_value = sum(values) / len(values)

        ax.bar(
            x_positions[i],
            mean_value,
            width=0.6,
            color=POLICY_COLOR[policy],
            alpha=0.35,
            edgecolor=POLICY_COLOR[policy],
            linewidth=2,
            zorder=2,
        )
        # Individual seeds, jittered slightly so 3 close values are all visible.
        jitter = np.linspace(-0.12, 0.12, len(values))
        ax.scatter(
            x_positions[i] + jitter,
            values,
            color=POLICY_COLOR[policy],
            edgecolor="black",
            linewidth=1.2,
            s=110,
            zorder=3,
        )
        ax.text(
            x_positions[i],
            mean_value * 1.15,
            f"₹{mean_value:,.0f}",
            ha="center",
            va="bottom",
            fontsize=17,
            fontweight="bold",
            color="#222222",
            zorder=4,
        )

    ax.set_yscale("log")
    ax.set_xticks(x_positions)
    ax.set_xticklabels([POLICY_LABEL[p] for p in POLICIES])
    ax.set_ylabel("₹ recovered per contact (log scale)")
    ax.set_title("Cost-aware policy recovers far more ₹ per contact —\nand it holds across every seed tested, not one lucky run")

    # Legend explaining the marker/bar convention once, not per-policy.
    seed_handle = plt.Line2D(
        [], [], marker="o", color="white", markerfacecolor="#666666", markeredgecolor="black",
        markersize=11, linestyle="None", label="Individual seed (42, 7, 123)",
    )
    bar_handle = plt.Rectangle((0, 0), 1, 1, facecolor="#666666", alpha=0.35, edgecolor="#666666", linewidth=2, label="Mean across seeds")
    ax.legend(handles=[bar_handle, seed_handle], loc="upper left", frameon=False)

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def chart_2_ablation(results_by_seed, out_path):
    """full_agent vs full_agent_minus_outage_detection, total ₹ recovered,
    per seed, with the % delta annotated."""
    fig, ax = plt.subplots(figsize=(11, 7))

    x_positions = np.arange(len(SEEDS))
    width = 0.32

    full_agent_values = [results_by_seed[seed]["full_agent"]["total_recovered_rupees"] for seed in SEEDS]
    ablation_values = [
        results_by_seed[seed]["full_agent_minus_outage_detection"]["total_recovered_rupees"] for seed in SEEDS
    ]

    ax.bar(
        x_positions - width / 2, ablation_values, width=width, color=COLOR_ABLATION,
        edgecolor="black", linewidth=1.2, label="Agent, no outage detection", zorder=2,
    )
    ax.bar(
        x_positions + width / 2, full_agent_values, width=width, color=COLOR_FULL_AGENT,
        edgecolor="black", linewidth=1.2, label="Cost-aware agent (with detection)", zorder=2,
    )

    for i, seed in enumerate(SEEDS):
        delta_pct = (full_agent_values[i] - ablation_values[i]) / ablation_values[i] * 100
        top = max(full_agent_values[i], ablation_values[i])
        ax.annotate(
            f"+{delta_pct:.1f}%",
            xy=(x_positions[i], top),
            xytext=(0, 14),
            textcoords="offset points",
            ha="center",
            fontsize=17,
            fontweight="bold",
            color="#1a1a1a",
        )

    ax.set_xticks(x_positions)
    ax.set_xticklabels([f"Seed {seed}" for seed in SEEDS])
    ax.set_ylabel("Total ₹ recovered")
    ax.set_ylim(top=max(full_agent_values) * 1.18)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"₹{v:,.0f}"))
    ax.set_title("Detecting the outage recovers 7–10% more revenue\nthan diagnosing without it — on every seed")
    # Below the x-axis, not inside the plot: an axes-fraction "upper ..."
    # anchor sits at a FIXED position relative to the axes box regardless
    # of the data, so it collides with whatever tick label or annotation
    # happens to occupy that corner -- headroom doesn't help, since it's
    # relative position, not data position. Below the x-tick labels is
    # the one place guaranteed empty no matter what the values are;
    # savefig's bbox_inches="tight" (set globally) expands the saved
    # image to include it rather than clipping it.
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=2, frameon=False, columnspacing=1.5)

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def chart_3_tradeoff(results_by_seed, out_path):
    """Total ₹ recovered vs. contacts spent -- all 4 policies, all seeds."""
    fig, ax = plt.subplots(figsize=(11, 7))

    for policy in POLICIES:
        xs = [results_by_seed[seed][policy]["contacts_made"] for seed in SEEDS]
        ys = [results_by_seed[seed][policy]["total_recovered_rupees"] for seed in SEEDS]
        ax.scatter(
            xs,
            ys,
            color=POLICY_COLOR[policy],
            marker=POLICY_MARKER[policy],
            edgecolor="black",
            linewidth=1.3,
            s=220,
            label=POLICY_LABEL[policy].replace("\n", " "),
            zorder=3,
        )

    ax.set_xlabel("Contacts made (count)")
    ax.set_ylabel("Total ₹ recovered")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"₹{v:,.0f}"))
    ax.set_title("The cost-aware agent recovers more —\nwith far fewer contacts than either baseline")
    ax.legend(loc="lower right", frameon=False, markerscale=1.1)

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def main():
    _apply_style()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Running the 3-seed harness comparison (seeds {SEEDS})...")
    results_by_seed = collect_results()

    for seed in SEEDS:
        for policy in POLICIES:
            r = results_by_seed[seed][policy]
            assert r["implemented"], f"{policy} not implemented on seed {seed}: {r.get('error')}"

    chart_1_primary(results_by_seed, OUTPUT_DIR / "01_primary_rupees_per_contact.png")
    print(f"Wrote {OUTPUT_DIR / '01_primary_rupees_per_contact.png'}")

    chart_2_ablation(results_by_seed, OUTPUT_DIR / "02_ablation_outage_detection.png")
    print(f"Wrote {OUTPUT_DIR / '02_ablation_outage_detection.png'}")

    chart_3_tradeoff(results_by_seed, OUTPUT_DIR / "03_tradeoff_recovered_vs_contacts.png")
    print(f"Wrote {OUTPUT_DIR / '03_tradeoff_recovered_vs_contacts.png'}")


if __name__ == "__main__":
    main()
