# -*- coding: utf-8 -*-
"""Картинка с итогом недели."""
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e4e3df"
BEFORE = "#85847f"   # нейтральный: где была
AFTER = "#2a78d6"    # акцент: где теперь

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 13,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK,
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
})


def _clean(ax):
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(length=0)


def _empty(ax, title):
    ax.set_title(title, loc="left", color=INK, fontsize=15, fontweight="bold", pad=12)
    ax.text(0.5, 0.5, "Пока нет данных", ha="center", va="center", color=INK2, transform=ax.transAxes)
    ax.set_xticks([]); ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)


def make_summary(scales, belief, emotions):
    """scales: [(название, до, после)], belief: {день: 0-10}, emotions: {эмоция: сколько дней}."""
    fig, (a1, a2, a3) = plt.subplots(
        3, 1, figsize=(9, 17), dpi=120, gridspec_kw={"height_ratios": [max(9, len(scales)), 4, 5], "hspace": 0.38})

    # 1. Было и стало
    rows = [s for s in scales if s[1] is not None or s[2] is not None]
    if rows:
        _clean(a1)
        a1.set_title("Было и стало", loc="left", color=INK, fontsize=15, fontweight="bold", pad=30)
        ys = list(range(len(rows)))[::-1]
        for y, (name, b, a) in zip(ys, rows):
            if b is not None and a is not None:
                a1.plot([b, a], [y, y], color=GRID, lw=3, zorder=1, solid_capstyle="round")
            if b is not None:
                a1.scatter([b], [y], s=150, color=BEFORE, zorder=2, edgecolor=SURFACE, linewidth=2)
            if a is not None:
                a1.scatter([a], [y], s=150, color=AFTER, zorder=3, edgecolor=SURFACE, linewidth=2)
                side = 1 if (b is None or a >= b) else -1
                a1.text(a + 0.38 * side, y, str(a), va="center", ha="left" if side > 0 else "right",
                        color=INK, fontsize=13, fontweight="bold")
        a1.set_yticks(ys)
        a1.set_yticklabels([r[0] for r in rows])
        a1.set_xlim(-0.9, 10.9)
        a1.set_ylim(-0.6, len(rows) - 0.4)
        a1.set_xticks(range(0, 11, 2))
        a1.grid(axis="x", color=GRID, lw=1)
        a1.set_axisbelow(True)
        a1.scatter([], [], s=110, color=BEFORE, label="в начале")
        a1.scatter([], [], s=110, color=AFTER, label="на седьмой день")
        a1.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False,
                  labelcolor=INK2, handletextpad=0.3, columnspacing=1.6, borderaxespad=0.2)
    else:
        _empty(a1, "Было и стало")

    # 2. Вера в себя по дням
    if belief:
        _clean(a2)
        a2.set_title("Вера в себя по дням", loc="left", color=INK, fontsize=15, fontweight="bold", pad=12)
        days = sorted(belief)
        vals = [belief[d] for d in days]
        a2.plot(days, vals, color=AFTER, lw=2, marker="o", markersize=9,
                markeredgecolor=SURFACE, markeredgewidth=2)
        for i in sorted({0, len(days) - 1}):
            a2.annotate(str(vals[i]), (days[i], vals[i]), textcoords="offset points", xytext=(0, 12),
                        ha="center", color=INK, fontweight="bold")
        a2.set_xlim(0.6, 7.4)
        a2.set_ylim(0, 11.5)
        a2.set_xticks(range(1, 8))
        a2.set_xticklabels(["день %d" % d for d in range(1, 8)])
        a2.set_yticks([0, 5, 10])
        a2.grid(axis="y", color=GRID, lw=1)
        a2.set_axisbelow(True)
    else:
        _empty(a2, "Вера в себя по дням")

    # 3. Эмоции недели
    if emotions:
        _clean(a3)
        a3.spines["bottom"].set_visible(False)
        a3.set_title("Эмоции недели: в скольких днях они были", loc="left", color=INK,
                     fontsize=15, fontweight="bold", pad=12)
        top = sorted(emotions.items(), key=lambda kv: (-kv[1], kv[0]))[:8][::-1]
        names, counts = [t[0] for t in top], [t[1] for t in top]
        a3.barh(names, counts, color=AFTER, height=0.5)
        for y, c in enumerate(counts):
            a3.text(c + 0.1, y, str(c), va="center", color=INK, fontweight="bold")
        a3.set_xlim(0, max(counts) + 0.8)
        a3.set_xticks([])
    else:
        _empty(a3, "Эмоции недели")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.4)
    plt.close(fig)
    return buf.getvalue()
