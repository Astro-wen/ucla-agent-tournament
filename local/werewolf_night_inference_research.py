"""Local-only night-selection inference; never imported by the deployed policy.

Preregistered profiles: random_min and mixed_first (per-night 50/50 random-min /
first-nonwolf). Both use a 15% uniform contamination likelihood and require a
0.10 top-versus-runner-up posterior gap. No day-ballot role likelihood is used.
Public voter sets locate nights; ballot targets do not identify player roles.
No opponent identities, hidden roles, seeds, death-list ordering or death dates
are inputs. The lower-target night resolution rule legitimately breaks seat
permutation symmetry. All 105 joint role worlds are enumerated before filtering.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
import math

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy


NOISE = 0.15
POSTERIOR_GAP = 0.10
MODELS = ("random_min", "mixed_first")
SEATS = frozenset(range(7))


@dataclass(frozen=True)
class World:
    wolves: tuple[int, int]
    seer: int


WORLDS = tuple(World(wolves, seer) for wolves in combinations(range(7), 2)
               for seer in range(7) if seer not in wolves)


@dataclass(frozen=True)
class Night:
    day: int
    alive: frozenset[int]  # after preceding public elimination, before the kill
    victim: int


@dataclass(frozen=True)
class Posterior:
    worlds: tuple[World, ...]
    probabilities: tuple[float, ...]
    wolf: tuple[float, ...]
    seer: tuple[float, ...]
    hard_wolves: frozenset[int]
    hard_clears: frozenset[int]
    nights: tuple[Night, ...] | None  # None means ambiguous/incomplete chronology


def _seat(s):
    return type(s) is int and s in SEATS


def reconstruct_nights(v):
    """Reconstruct only fully located nights, otherwise decline all soft evidence.

    A day's complete voter set is its pre-vote living set. Subtract its explicit
    elimination; the difference from the next day's voters (or current daytime
    living set) must be exactly one publicly night-killed seat. At a night action
    the final transition has not happened yet, so current alive must equal the
    post-vote set. Require the complete chain from peaceful day 1, with no missing
    voters, dates, resignations or unexplained deaths. Dead dict order is unused.
    """
    if type(v.day) is not int or not 1 <= v.day <= 8:
        return None
    if v.sub_phase not in ("day_vote", "night_wolf", "night_seer"):
        return None
    if any(not _seat(s) for s in v.alive) or len(set(v.alive)) != len(v.alive):
        return None
    living = frozenset(v.alive)
    if not living or set(v.dead) != SEATS - living:
        return None
    if set(v.dead_cause) != set(v.dead) or any(c not in ("vote", "night_kill") for c in v.dead_cause.values()):
        return None
    rows = {}
    for day, ballots, eliminated in v.votes:
        if type(day) is not int or day in rows or not 1 <= day < v.day or not isinstance(ballots, dict):
            return None
        voters = frozenset(ballots)
        if not voters or any(not _seat(s) for s in voters):
            return None
        if any(t is not None and (not _seat(t) or t not in voters or t == s) for s, t in ballots.items()):
            return None
        if eliminated is not None and (not _seat(eliminated) or eliminated not in voters
                                      or v.dead_cause.get(eliminated) != "vote"):
            return None
        rows[day] = (voters, eliminated)
    if set(rows) != set(range(1, v.day)):
        return None
    if not rows:
        return () if living == SEATS else None
    if rows[1][0] != SEATS:
        return None
    nights, accounted = [], set()
    for day in range(1, v.day):
        voters, eliminated = rows[day]
        after_vote = voters - ({eliminated} if eliminated is not None else set())
        if eliminated is not None:
            if eliminated in accounted:
                return None
            accounted.add(eliminated)
        next_alive = rows[day + 1][0] if day + 1 in rows else living
        if not next_alive <= after_vote:
            return None
        if day == v.day - 1 and v.sub_phase != "day_vote":
            if next_alive != after_vote:
                return None
            continue  # this night's kill is not public yet
        lost = after_vote - next_alive
        if len(lost) != 1:
            return None
        victim = next(iter(lost))
        if v.dead_cause.get(victim) != "night_kill" or victim in accounted:
            return None
        accounted.add(victim)
        nights.append(Night(day + 1, frozenset(after_vote), victim))
    return tuple(nights) if accounted == set(v.dead) else None


def random_min_probability(victim, nonwolves, wolves):
    """Exact law of the minimum of w independently uniform legal kill choices."""
    targets = frozenset(nonwolves)
    if wolves not in (1, 2) or not targets or victim not in targets:
        return 0.0
    n = len(targets)
    return (sum(s >= victim for s in targets) / n) ** wolves - (sum(s > victim for s in targets) / n) ** wolves


def night_probability(night, world, model="random_min", noise=NOISE):
    """Likelihood contamination cannot convert a model preference to a hard fact."""
    if model not in MODELS or not 0 < noise < 1:
        raise ValueError("unknown model or nonpositive/invalid noise floor")
    targets = night.alive - set(world.wolves)
    wolves = len(night.alive & set(world.wolves))
    if night.victim not in targets or wolves not in (1, 2):
        return 0.0  # only a hard-rule contradiction, never a target-model zero
    likelihood = random_min_probability(night.victim, targets, wolves)
    if model == "mixed_first":
        likelihood = 0.5 * likelihood + 0.5 * (night.victim == min(targets))
    return (1 - noise) * likelihood + noise / len(targets)


def role_of(world, seat):
    return "wolf" if seat in world.wolves else "seer" if seat == world.seer else "villager"


def feasible(world, v):
    """Only own private truth and publicly revealed roles/rules filter worlds."""
    if _seat(v.me) and v.role in ("wolf", "seer", "villager") and role_of(world, v.me) != v.role:
        return False
    for s, role in v.dead.items():
        if not _seat(s):
            return False
        if role in ("wolf", "seer", "villager") and role_of(world, s) != role:
            return False
        if v.dead_cause.get(s) == "night_kill" and s in world.wolves:
            return False
    if v.role == "wolf" and any(s not in world.wolves for s in v.allies if _seat(s)):
        return False
    if v.role == "seer" and any((s in world.wolves) != (r == "wolf")
                                for s, r in v.investigations.items() if _seat(s) and r in ("wolf", "villager")):
        return False
    return True


def posterior(v, model="random_min"):
    if model not in MODELS:
        raise ValueError("unknown night model")
    nights = reconstruct_nights(v)
    worlds = tuple(w for w in WORLDS if feasible(w, v))
    if not worlds:
        return Posterior((), (), (0.0,) * 7, (0.0,) * 7, frozenset(), frozenset(), nights)
    hard_wolves = frozenset(s for s in SEATS if all(s in w.wolves for w in worlds))
    hard_clears = frozenset(s for s in SEATS if all(s not in w.wolves for w in worlds))
    likelihoods = [tuple(night_probability(n, w, model) for n in nights or ()) for w in worlds]
    # A no-living-wolf history contradicts continuing play. Do not silently remove
    # worlds using a model zero; fail closed and leave the reference action alone.
    if any(any(p <= 0 for p in row) for row in likelihoods):
        nights = None
        likelihoods = [() for _ in worlds]
    logs = [math.fsum(math.log(p) for p in row) for row in likelihoods]
    maximum = max(logs)
    weights = [math.exp(x - maximum) for x in logs]
    total = math.fsum(weights)
    probabilities = tuple(x / total for x in weights)
    wolf = tuple(1.0 if s in hard_wolves else 0.0 if s in hard_clears else
                 math.fsum(p for w, p in zip(worlds, probabilities) if s in w.wolves) for s in range(7))
    seer = tuple(1.0 if all(s == w.seer for w in worlds) else
                 math.fsum(p for w, p in zip(worlds, probabilities) if s == w.seer) for s in range(7))
    return Posterior(worlds, probabilities, wolf, seer, hard_wolves, hard_clears, nights)


class NightInferencePolicy(WerewolfPolicy):
    model = "random_min"

    def beliefs(self, v):
        return posterior(v, self.model)

    def _day_vote(self, v, targets):
        base = super()._day_vote(v, targets)
        if v.role != "villager" or v.sub_phase != "day_vote" or v.me not in v.alive:
            return base
        if v.evidence is not None:
            roles = {r for r in (v.evidence.text, v.evidence.engine, v.evidence.hint, v.evidence.night) if r}
            if roles != {"villager"} or not (v.evidence.text or v.evidence.engine):
                return base  # parser default or conflicting role evidence is not a village observation
        claims = self.claims(v, private=True)
        # Conservative superset of the substantive-claim gate: even a bare seer
        # claimant preserves the existing claim policy and all hard-result priority.
        if any(s != v.me for s in claims.order) or claims.trusted_wolves(v.alive):
            return base
        b = self.beliefs(v)
        if not b.worlds or not b.nights or base[0] not in targets:
            return base
        if base[0] in b.hard_wolves:
            return base
        pool = [s for s in targets if s in v.alive and s != v.me and s not in b.hard_clears]
        if not pool:
            return base
        ordered = sorted(pool, key=lambda s: (-b.wolf[s], s))
        best = ordered[0]
        runner = b.wolf[ordered[1]] if len(ordered) > 1 else 0.0
        if b.wolf[best] - runner < POSTERIOR_GAP or b.wolf[best] - b.wolf[base[0]] < POSTERIOR_GAP:
            return base
        return best, "night-selection likelihood: uncertain read, not a verified role"


class NightInferenceContestant(Contestant):
    model = "random_min"

    def __init__(self):
        super().__init__()
        self._install()

    def _install(self):
        self.ww = NightInferencePolicy()
        self.ww.model = self.model

    def _fresh(self):
        super()._fresh()
        self._install()


class MixedFirstContestant(NightInferenceContestant):
    model = "mixed_first"


def random_min():
    return NightInferenceContestant()


def mixed_first():
    return MixedFirstContestant()


def main(argv=None):
    """Use the existing strict ResearchGame CLI, with a compact audit report."""
    import json
    from . import werewolf_benchmark as benchmark
    from .werewolf_research import main as research_main

    source = benchmark.source_manifest()
    print("candidate_sha256", source["files"]["local/werewolf_night_inference_research.py"], flush=True)
    summary = research_main(argv)
    for name, scopes in summary["metrics"].items():
        print("PROFILE", name, "scope role n candidate_wins reference_wins delta_pp improved worsened", flush=True)
        for scope, roles in scopes.items():
            if scope != "all" and not scope.startswith(("opponent:", "seed:")):
                continue
            for role in ("seat", "villager", "seer", "wolf"):
                if role in roles:
                    r = roles[role]
                    print(scope, role, r["n"], r["candidate_wins"], r["reference_wins"],
                          f"{100 * r['delta']:+.3f}", r["improved"], r["worsened"], flush=True)
    print("AUDIT", json.dumps({k: summary[k] for k in (
        "gates", "completed_scenarios", "games", "harness_errors", "failures", "background_failures",
        "latency", "elapsed_s", "source_digest_end")}, sort_keys=True), flush=True)
    return summary


if __name__ == "__main__":
    main()
