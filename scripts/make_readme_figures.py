"""Regenerate the README figures in assets/ from the live agent's logs and the measured results in docs/progress.md.

    ~/.venv-plots/bin/python scripts/make_readme_figures.py      # needs matplotlib, seaborn, pandas (not agent deps)

Real-platform results come from logs/agent-main.out* (Testing matches against other contestants; self-play excluded);
the opponent of each seat is the "Opponents:" line of its assignment, paired through the seat id. The development
gains and Werewolf numbers are copied from docs/progress.md and docs/werewolf_benchmark.md (paired measurements).
"""
from __future__ import annotations

import collections
import glob
import json
import math
import os
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
import seaborn as sns  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"
OUT = ROOT / "assets"
OUT.mkdir(exist_ok=True)
sns.set_theme(style="whitegrid", context="notebook", font_scale=0.95)
PAL = {"won": "#2a9d8f", "lost": "#e76f51"}
GAME = {"pokemon_vgc_doubles_draft": "Pokémon VGC draft", "red_alert": "Red Alert", "werewolf": "Werewolf"}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - h) / d, (c + h) / d


def real_results() -> pd.DataFrame:
    lines: list[str] = []
    for f in sorted(glob.glob(str(LOGS / "agent-main.out*")), key=os.path.getmtime):
        lines += open(f, errors="replace").read().splitlines()
    pending: list[tuple[str, str]] = []      # (game, opponent) of assignments not yet started
    seat_info: dict[str, tuple[str, str]] = {}
    rows = []
    for ln in lines:
        m = re.search(r"Match assigned: (\w+)", ln)
        if m:
            pending.append((m.group(1), "SELF" if "self-play" in ln else ""))
            continue
        m = re.search(r"Opponents: (.+)", ln)
        if m and pending and pending[-1][1] == "":
            pending[-1] = (pending[-1][0], m.group(1).strip())
            continue
        m = re.search(r"\[match (\w+) seat=([\w-]+)\] starting", ln)
        if m:
            for i, (g, opp) in enumerate(pending):
                if g == m.group(1):
                    seat_info[m.group(2)] = (g, opp)
                    if opp != "SELF":
                        pending.pop(i)
                    elif sum(1 for s in seat_info.values() if s == (g, "SELF")) % 2 == 0:
                        pending.pop(i)
                    break
            continue
        m = re.search(r"\[match (\w+) seat=([\w-]+)\] finished: your agent \(player \d\) (won|lost|drew)", ln)
        if m and m.group(2) in seat_info:
            g, opp = seat_info[m.group(2)]
            if opp and opp != "SELF" and "," not in opp:
                rows.append({"game": g, "opponent": opp, "result": m.group(3), "time": ln[:16]})
    return pd.DataFrame(rows)


def fig_real(df: pd.DataFrame) -> dict:
    stats = {}
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    for ax, g in zip(axes, ["pokemon_vgc_doubles_draft", "red_alert"]):
        d = df[df.game == g]
        tab = d.groupby(["opponent", "result"]).size().unstack(fill_value=0)
        for c in ("won", "lost"):
            if c not in tab:
                tab[c] = 0
        tab["n"] = tab.won + tab.lost
        tab = tab.sort_values("n", ascending=True)
        tab[["won", "lost"]].plot.barh(stacked=True, ax=ax, color=[PAL["won"], PAL["lost"]], width=0.75)
        w, n = int(tab.won.sum()), int(tab.n.sum())
        lo, hi = wilson(w, n)
        stats[g] = (w, n - w, lo, hi)
        ax.set_title(f"{GAME[g]}: {w}-{n - w} ({100 * w / n:.0f}%, 95% CI {100 * lo:.0f}-{100 * hi:.0f}%)", fontsize=11)
        ax.set_xlabel("games")
        ax.set_ylabel("")
        ax.legend(title="", loc="lower right", frameon=True)
    fig.suptitle("Real platform results vs other contestants (Testing matches, Oct 8-15, self-play excluded)", fontsize=12)
    fig.tight_layout()
    fig.savefig(OUT / "real_results.png", dpi=160)
    plt.close(fig)
    return stats


def fig_gains() -> None:
    rows = [  # paired measurements, percentage points, 95% CI (docs/progress.md)
        ("Pokémon v4: engine-search battle\nvs v3.2, ladder seed 7 (400)", 9.8, 4.0, 15.5, "Pokémon"),
        ("Pokémon v4: engine-search battle\nvs v3.2, ladder seed 11 (400)", 10.8, 5.3, 16.2, "Pokémon"),
        ("Pokémon v4 vs VGC-Bench\n(learned agent, 200)", 15.5, 6.7, 24.3, "Pokémon"),
        ("Pokémon v4.4: opponent-aware draft\nvs v4.3, two seeds (800)", 5.5, 1.1, 9.9, "Pokémon"),
        ("Pokémon draft set 1013 vs a\nreal-opponent stand-in (160, fresh seed)", 9.4, 1.1, 17.7, "Pokémon"),
        ("Red Alert v4.5: economy first\nvs balanced bot, fresh seeds (480)", 7.5, 3.1, 11.9, "Red Alert"),
        ("Werewolf v4.3 hardening: wolves\nvs red-team tables (16,000)", 25.0, 24.0, 26.1, "Werewolf"),
        ("Werewolf v4.3 hardening: wolves\nvs ordinary tables (36,000)", 10.4, 10.0, 10.9, "Werewolf"),
    ]
    d = pd.DataFrame(rows, columns=["label", "diff", "lo", "hi", "game"])[::-1].reset_index(drop=True)
    colors = {"Pokémon": "#264653", "Red Alert": "#e9c46a", "Werewolf": "#8ab17d"}
    fig, ax = plt.subplots(figsize=(9, 5.4))
    for i, r in d.iterrows():
        ax.plot([r.lo, r.hi], [i, i], color=colors[r.game], lw=2.4)
        ax.plot(r["diff"], i, "o", color=colors[r.game], ms=7)
    ax.axvline(0, color="#555", lw=1, ls="--")
    ax.set_yticks(range(len(d)))
    ax.set_yticklabels(d.label, fontsize=8.5)
    ax.set_xlabel("win-rate change vs the previous version (percentage points, paired, 95% CI)")
    ax.set_title("What each release measured locally (same deals for both versions)", fontsize=11)
    for g, c in colors.items():
        ax.plot([], [], "o-", color=c, label=g)
    ax.legend(loc="lower right")
    fig.tight_layout()
    fig.savefig(OUT / "dev_gains.png", dpi=160)
    plt.close(fig)


def fig_latency() -> None:
    lines = []
    for f in glob.glob(str(LOGS / "agent-main.out*")):
        lines += [ln for ln in open(f, errors="replace") if "[match-summary]" in ln]
    pk = [float(m.group(1)) / 1000 for ln in lines if "game=pokemon" in ln
          for m in [re.search(r"search_ms p50=[\d.]+ max=([\d.]+)", ln)] if m]
    ra = [float(m.group(1)) for ln in lines if "game=red_alert" in ln
          for m in [re.search(r"ms_p99=([\d.]+)", ln)] if m]
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6))
    sns.histplot(pk, binwidth=1, binrange=(0, 52), ax=axes[0], color="#264653")
    axes[0].axvline(50, color=PAL["lost"], ls="--")
    axes[0].text(49, axes[0].get_ylim()[1] * 0.85, "platform limit 50 s", ha="right", color=PAL["lost"])
    axes[0].set_xlim(0, 52)
    axes[0].set_title(f"Pokémon: slowest battle decision per match ({len(pk)} matches)", fontsize=10.5)
    axes[0].set_xlabel("seconds")
    sns.histplot(ra, bins=25, ax=axes[1], color="#e9c46a")
    axes[1].set_title(f"Red Alert: p99 decision time per match ({len(ra)} matches)", fontsize=10.5)
    axes[1].set_xlabel("milliseconds (the world runs at 40 ms per tick)")
    fig.tight_layout()
    fig.savefig(OUT / "decision_time.png", dpi=160)
    plt.close(fig)


def fig_ra_timeline(session: str = "ba57c13e") -> None:
    path = glob.glob(str(LOGS / f"{session}-*.jsonl"))
    if not path:
        return
    rows = []
    for ln in open(path[0]):
        raw = (json.loads(ln) or {}).get("raw") or {}
        ob = raw.get("observation")
        if not isinstance(ob, dict):
            continue
        t = ((raw.get("time") or {}).get("game_seconds") or ob.get("tick", 0) / 25.0) / 60
        eco, mil = ob.get("economy") or {}, ob.get("military") or {}
        rows.append({"minute": t, "harvesters": eco.get("harvesters") or 0, "army value": mil.get("army_value") or 0,
                     "enemy value destroyed": mil.get("kills_cost") or 0, "own value lost": mil.get("deaths_cost") or 0})
    d = pd.DataFrame(rows).sort_values("minute")
    fig, ax = plt.subplots(figsize=(9, 3.8))
    for col, c in (("army value", "#264653"), ("enemy value destroyed", PAL["won"]), ("own value lost", PAL["lost"])):
        ax.plot(d.minute, d[col] / 1000, label=col, color=c, lw=2)
    ax2 = ax.twinx()
    ax2.step(d.minute, d.harvesters, where="post", color="#e9c46a", lw=1.6, label="harvesters")
    ax2.set_ylabel("harvesters")
    ax2.grid(False)
    ax.set_xlabel("game minute")
    ax.set_ylabel("credits (thousands)")
    ax.set_title("Red Alert, one real win (sidestep, Oct 14): economy first, one decisive wave", fontsize=11)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=8.5)
    fig.tight_layout()
    fig.savefig(OUT / "redalert_timeline.png", dpi=160)
    plt.close(fig)


def fig_werewolf() -> None:
    # docs/werewolf_benchmark.md: 28 backgrounds, one controlled seat, strict realtime discussion, 28,000 games;
    # and the same tables with our seat replaced by the official first-legal-action agent / a random legal agent.
    d = pd.DataFrame([("villager", 42.67), ("seer", 53.93), ("wolf", 67.59), ("all roles", 51.38)],
                     columns=["role", "win rate"])
    b = pd.DataFrame([("our policy", 51.77), ("first legal action", 39.79), ("random legal action", 37.54)],
                     columns=["agent in our seat", "win rate"])
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.4))
    sns.barplot(d, x="role", y="win rate", ax=axes[0], color="#8ab17d")
    axes[0].set_title("Win rate by role (28 opponent mixes, 28,000 games)", fontsize=10.5)
    sns.barplot(b, x="agent in our seat", y="win rate", ax=axes[1], palette=["#8ab17d", "#bbb", "#ddd"],
                hue="agent in our seat", legend=False)
    axes[1].set_title("Same deals, same seats, only our seat swapped (5,600 tables)", fontsize=10.5)
    for ax in axes:
        ax.set_ylim(0, 80)
        ax.set_xlabel("")
        ax.set_ylabel("% games won")
        for p in ax.patches:
            ax.annotate(f"{p.get_height():.1f}", (p.get_x() + p.get_width() / 2, p.get_height() + 1), ha="center",
                        fontsize=9)
    fig.tight_layout()
    fig.savefig(OUT / "werewolf.png", dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    df = real_results()
    stats = fig_real(df)
    fig_gains()
    fig_latency()
    fig_ra_timeline()
    fig_werewolf()
    print(df.groupby(["game", "result"]).size())
    for g, (w, lo_, lo, hi) in stats.items():
        print(g, w, lo_, f"{lo:.2f}-{hi:.2f}")
    print(sorted(p.name for p in OUT.glob("*.png")))
