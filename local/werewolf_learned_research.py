from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import logging
import math
from pathlib import Path

from agent.agent import Contestant
from agent.werewolf.policy import WerewolfPolicy, _chats
from local.werewolf_belief_research import Beliefs, DEALS, feasible


ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "data" / "werewolf_learned_likelihoods.json"
FEATURES = ("seer_claim_compatibility", "wolf_claim_rate", "seer_report_consistency",
            "wolf_report_consistency", "wolf_vote_residual", "town_vote_residual")
TRAIN_FAMILIES = ("first", "random", "heuristic", "llmq", "rt_wolfpack", "rt_talker", "rt_chaos",
                  "guard_fake", "pop_pressure_bus", "pop_late_claim")
HELD_FAMILIES = ("rt_detective", "pop_observed_switch")
PROFILES = {"learned": (1.0, 4.0), "clipped": (0.5, 2.0), "uniform_control": (0.0, 2.0)}
NOISE = 0.05
SWITCH_MARGIN = 0.12
MIN_WOLF_PROBABILITY = 0.45


@dataclass(frozen=True)
class Model:
    weights: tuple[float, ...]
    training_digest: str
    samples: int
    sha256: str


def load_model(path=MODEL_PATH):
    with Path(path).open("rb") as handle:
        data = handle.read(16001)
    if len(data) > 16000:
        raise ValueError("oversized model")
    obj = json.loads(data)
    if not isinstance(obj, dict) or set(obj) != {"schema_version", "features", "weights", "training_digest", "samples"}:
        raise ValueError("unexpected model schema")
    if obj["schema_version"] != 1 or obj["features"] != list(FEATURES):
        raise ValueError("incompatible feature definitions")
    weights = obj["weights"]
    if not isinstance(weights, list) or len(weights) != len(FEATURES) or any(
            type(w) not in (int, float) or not math.isfinite(w) or abs(w) > 8 for w in weights):
        raise ValueError("invalid fixed coefficients")
    digest = obj["training_digest"]
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid training digest")
    if type(obj["samples"]) is not int or not 0 <= obj["samples"] <= 30000:
        raise ValueError("invalid sample count")
    return Model(tuple(float(w) for w in weights), digest, obj["samples"], hashlib.sha256(data).hexdigest())


@dataclass(frozen=True)
class Evidence:
    claims: tuple
    ballots: tuple


def public_evidence(v):
    reports, order = {}, []
    for _, sender, facts, _ in _chats(v):
        if sender == v.me or facts.injection:
            continue
        if facts.claim and sender not in reports:
            reports[sender] = [set(), set(), set(), False]
            order.append(sender)
        if sender not in reports:
            continue
        wolves, clears, conflicts, self_wolf = reports[sender]
        wolves.update(facts.wolves)
        clears.update(facts.clears)
        conflicts.update(facts.conflicts)
        conflicts.update(wolves & clears)
        reports[sender][3] = self_wolf or facts.self_wolf
    claims, signatures = [], set()
    for sender in order:
        wolves, clears, conflicts, self_wolf = reports[sender]
        signature = (tuple(sorted(wolves)), tuple(sorted(clears)), tuple(sorted(conflicts)), self_wolf)
        if signature in signatures:
            continue
        signatures.add(signature)
        results = tuple((t, 1 if t in wolves else -1) for t in sorted((wolves | clears) - conflicts)
                        if t != sender and t in range(7))
        claims.append((sender, -1 if conflicts or self_wolf else 1, results))
    ballots, seen = [], set()
    for _, votes, _ in v.votes:
        voters = tuple(sorted(s for s in votes if type(s) is int and s in range(7)))
        for voter in voters:
            target = votes[voter]
            if voter == v.me or len(voters) < 2 or target == voter:
                continue
            if target is not None and (type(target) is not int or target not in voters):
                continue
            if (voter, target) in seen:
                continue
            seen.add((voter, target))
            ballots.append((voter, target, tuple(s for s in voters if s != voter)))
    return Evidence(tuple(claims), tuple(ballots))


def feature_vector(evidence, deal):
    out = [0.0] * 6
    n = max(1, len(evidence.claims))
    for sender, quality, reports in evidence.claims:
        seer, wolf = sender == deal.seer, sender in deal.wolves
        out[0] += quality * seer / n
        out[1] += wolf / n
        consistency = sum(verdict * (1 if target in deal.wolves else -1) for target, verdict in reports) / max(1, len(reports))
        out[2] += seer * consistency / n
        out[3] += wolf * consistency / n
    residuals = {}
    for voter, target, others in evidence.ballots:
        residual = 0.0 if target is None else int(target in deal.wolves) - sum(s in deal.wolves for s in others) / len(others)
        residuals.setdefault(voter, []).append(residual)
    n = max(1, len(residuals))
    for voter, values in residuals.items():
        out[4 if voter in deal.wolves else 5] += sum(values) / len(values) / n
    return tuple(out)


def probabilities(rows, weights, strength=1.0, clip=4.0):
    if not rows:
        return ()
    scores = [max(-clip, min(clip, strength * sum(w * x for w, x in zip(weights, row)))) for row in rows]
    peak = max(scores)
    values = [math.exp(score - peak) for score in scores]
    total = math.fsum(values)
    return tuple((1 - NOISE) * value / total + NOISE / len(rows) for value in values)


def _beliefs(worlds, probs):
    if not worlds:
        return Beliefs((), (), (0.0,) * 7, (0.0,) * 7, frozenset(), frozenset())
    hard_wolves = frozenset(s for s in range(7) if all(s in d.wolves for d in worlds))
    hard_clears = frozenset(s for s in range(7) if all(s not in d.wolves for d in worlds))
    wolves = tuple(1.0 if s in hard_wolves else 0.0 if s in hard_clears else
                   math.fsum(p for d, p in zip(worlds, probs) if s in d.wolves) for s in range(7))
    seers = tuple(min(1.0, math.fsum(p for d, p in zip(worlds, probs) if d.seer == s)) for s in range(7))
    return Beliefs(worlds, probs, wolves, seers, hard_wolves, hard_clears)


def posterior(v, model, strength=1.0, clip=4.0):
    worlds = tuple(d for d in DEALS if feasible(d, v))
    evidence = public_evidence(v)
    rows = tuple(feature_vector(evidence, d) for d in worlds)
    weights = model.weights if model is not None else (0.0,) * 6
    return _beliefs(worlds, probabilities(rows, weights, strength, clip))


def select_target(b, targets, baseline):
    if not targets:
        return None
    if not b.consistent:
        return baseline if baseline in targets else min(targets)
    known = sorted(set(targets) & b.hard_wolves)
    if known:
        return baseline if baseline in known else known[0]
    pool = [s for s in targets if s not in b.hard_clears] or list(targets)
    best = max(pool, key=lambda s: (round(b.wolf[s], 12), s == baseline, -s))
    if baseline not in pool:
        return best
    if b.wolf[best] >= MIN_WOLF_PROBABILITY and b.wolf[best] - b.wolf[baseline] >= SWITCH_MARGIN:
        return best
    return baseline


class LearnedPolicy(WerewolfPolicy):
    def __init__(self, profile="learned", model=None):
        super().__init__()
        if profile not in PROFILES:
            raise ValueError("unknown fixed profile")
        self.profile = profile
        if model is None:
            try:
                model = load_model()
            except (OSError, ValueError):
                logging.getLogger(__name__).warning("fixed research model unavailable; using uniform control")
        self.model = model

    def beliefs(self, v):
        strength, clip = PROFILES[self.profile]
        return posterior(v, self.model, strength, clip)

    def _day_vote(self, v, targets):
        original = super()._day_vote(v, targets)
        if v.role == "wolf":
            return original
        pick = select_target(self.beliefs(v), targets, original[0])
        return original if pick == original[0] else (pick, "offline-learned role read; not a verified investigation")

    def _wolf_vote(self, v, targets):
        return WerewolfPolicy()._wolf_vote(v, targets)

    def _opening(self, v):
        return WerewolfPolicy()._opening(v) if v.role == "wolf" else super()._opening(v)

    def _seer_chat(self, v, c):
        wolves, clears = self.seer_knowledge(v)
        fakes = [s for s in c.order if s in v.alive and s != v.me]
        if fakes and not wolves:
            text = f"I am the Seer. Player{fakes[0]}'s claim is false; that alone does not prove a wolf."
            text += "".join(f" Player{s} is clear." for s in clears[:3])
            pick, _ = self._day_vote(v, v.others_alive)
            if pick is not None:
                text += f" I vote Player{pick}."
            return "claim-rebuttal", {"content": text, "recipients": []}
        return super()._seer_chat(v, c)

    def _villager_chat(self, v, c):
        if v.role != "wolf":
            pick = self._day_vote(v, v.others_alive)[0]
            original = WerewolfPolicy._day_vote(self, v, v.others_alive)[0]
            if pick is not None and pick != original:
                return "learned-vote", {"content": f"Public claims and past votes give a tentative read. I vote Player{pick}. "
                                                   "This is not a verified result.", "recipients": []}
        return super()._villager_chat(v, c)


class ActionOnlyPolicy(LearnedPolicy):
    def _day_vote(self, v, targets):
        if getattr(self, "_reference_chat", False):
            return WerewolfPolicy._day_vote(self, v, targets)
        return super()._day_vote(v, targets)

    def _plan(self, v):
        self._reference_chat = True
        try:
            return super()._plan(v)
        finally:
            self._reference_chat = False


class LearnedContestant(Contestant):
    profile = "learned"
    policy_class = LearnedPolicy

    def __init__(self):
        super().__init__()
        self._install()

    def _install(self):
        self.ww = self.policy_class(self.profile)

    def _fresh(self):
        super()._fresh()
        self._install()


class ClippedContestant(LearnedContestant):
    profile = "clipped"


class UniformContestant(LearnedContestant):
    profile = "uniform_control"


def learned():
    return LearnedContestant()


def clipped():
    return ClippedContestant()


def uniform_control():
    return UniformContestant()


class ActionOnlyContestant(LearnedContestant):
    policy_class = ActionOnlyPolicy


def action_only():
    return ActionOnlyContestant()


@dataclass(frozen=True)
class Example:
    worlds: tuple
    rows: tuple
    label: int
    me: int
    role: str
    alive: tuple
    baseline: int
    ballots: tuple
    family: str
    game: int
    day: int
    seed: int = 202


def validate_training_split(seed, families):
    if seed != 202:
        raise ValueError("fitting is restricted to training seed 202")
    if not families or any(f not in TRAIN_FAMILIES for f in families) or len(set(families)) != len(families):
        raise ValueError("training families exclude the reserved family tests")


def fit_weights(examples, iterations=160, regularization=0.02):
    if not examples:
        raise ValueError("no training examples")
    for ex in examples:
        validate_training_split(ex.seed, [ex.family])
    weights, momentum, variance = [0.0] * 6, [0.0] * 6, [0.0] * 6
    history = []
    for step in range(1, iterations + 1):
        gradient = [0.0] * 6
        loss = 0.0
        for ex in examples:
            scores = [sum(w * x for w, x in zip(weights, row)) for row in ex.rows]
            peak = max(scores)
            values = [math.exp(score - peak) for score in scores]
            total = math.fsum(values)
            probs = [value / total for value in values]
            p = (1 - NOISE) * probs[ex.label] + NOISE / len(probs)
            loss -= math.log(p)
            multiplier = (1 - NOISE) * probs[ex.label] / p
            actual = ex.rows[ex.label]
            expected = [0.0] * 6
            for prob, row in zip(probs, ex.rows):
                for i in range(6):
                    expected[i] += prob * row[i]
            for i in range(6):
                gradient[i] += multiplier * (expected[i] - actual[i])
        history.append(loss / len(examples) + 0.5 * regularization * sum(w * w for w in weights))
        for i in range(6):
            g = gradient[i] / len(examples) + regularization * weights[i]
            momentum[i] = 0.9 * momentum[i] + 0.1 * g
            variance[i] = 0.999 * variance[i] + 0.001 * g * g
            delta = 0.15 * (momentum[i] / (1 - 0.9 ** step)) / (math.sqrt(variance[i] / (1 - 0.999 ** step)) + 1e-8)
            weights[i] = max(-8.0, min(8.0, weights[i] - delta))
    return tuple(weights), history


def _elimination_value(ballots, truth):
    counts = Counter(t for t in ballots.values() if t is not None and t != 7)
    leaders = [s for s, n in counts.items() if n == max(counts.values())]
    return (1 if leaders[0] in truth.wolves else -1) if len(leaders) == 1 else 0


def calibration_report(examples, model):
    output = {}
    weights = model.weights if model is not None else (0.0,) * 6
    for profile, (strength, clip) in PROFILES.items():
        totals = Counter()
        bins = [[0, 0.0, 0.0] for _ in range(10)]
        for ex in examples:
            probs = probabilities(ex.rows, weights, strength, clip)
            belief = _beliefs(ex.worlds, probs)
            truth = ex.worlds[ex.label]
            totals["examples"] += 1
            totals["world_log_loss"] -= math.log(probs[ex.label])
            totals["true_world_mass"] += probs[ex.label]
            targets = [s for s in ex.alive if s != ex.me]
            for s in targets:
                if s in belief.hard_wolves or s in belief.hard_clears:
                    continue
                p, y = belief.wolf[s], int(s in truth.wolves)
                totals["targets"] += 1
                totals["wolf_brier"] += (p - y) ** 2
                totals["wolf_log_loss"] -= math.log(max(1e-15, p if y else 1 - p))
                index = min(9, int(p * 10))
                bins[index][0] += 1
                bins[index][1] += p
                bins[index][2] += y
            pool = [s for s in targets if s not in belief.hard_clears] or targets
            if pool:
                best = max(pool, key=lambda s: (round(belief.wolf[s], 12), s == ex.baseline, -s))
                pick = select_target(belief, targets, ex.baseline)
                totals["map_wolf_hit"] += best in truth.wolves
                totals["selector_wolf_hit"] += pick in truth.wolves
                totals["baseline_wolf_hit"] += ex.baseline in truth.wolves
                totals["switches"] += pick != ex.baseline
                totals["switch_to_wolf"] += pick != ex.baseline and pick in truth.wolves
                totals["switch_from_wolf"] += pick != ex.baseline and ex.baseline in truth.wolves
                if ex.ballots:
                    votes = dict(ex.ballots)
                    before = _elimination_value(votes, truth)
                    votes[ex.me] = pick
                    after = _elimination_value(votes, truth)
                    totals["fixed_ballot_improvements"] += after > before
                    totals["fixed_ballot_regressions"] += after < before
        n, targets = totals["examples"], totals["targets"]
        output[profile] = {"examples": n, "unresolved_targets": targets,
                           **{key: totals[key] / targets if targets else None for key in ("wolf_brier", "wolf_log_loss")},
                           **{key: totals[key] / n if n else None for key in ("world_log_loss", "true_world_mass")},
                           "ece": sum(abs(p - y) for count, p, y in bins) / targets if targets else None,
                           "calibration_bins": bins,
                           **{key + "_rate": totals[key] / n if n else None
                              for key in ("map_wolf_hit", "selector_wolf_hit", "baseline_wolf_hit")},
                           **{key: totals[key] for key in ("switches", "switch_to_wolf", "switch_from_wolf",
                                                          "fixed_ballot_improvements", "fixed_ballot_regressions")}}
    return output


def _collect_one(task):
    from copy import deepcopy
    import random
    from local import eval_werewolf as ev
    from local.werewolf_env import ContestantSeat, Game

    seed, family, index = task
    gseed, ours, types = ev.deal(seed, index, family, 1)

    class CaptureSeat(ContestantSeat):
        def reset(self):
            super().reset()
            self.snapshots = []

        def choose(self, state):
            capture = state["game_state"]["phase"] == "day_vote"
            v = deepcopy(self.agent.ww._view(state)) if capture else None
            action = super().choose(state)
            if v is not None and v.role != "wolf":
                self.snapshots.append((v, int(action)))
            return action

    focal = CaptureSeat(Contestant)
    seats = [focal if s in ours else ev.make_seat(types[s]) for s in range(7)]
    game = Game(seats, random.Random(gseed))
    game.play()
    if any(any(game.stats[k]) for k in ("crash", "illegal", "bad_chat", "timeout", "chat_after_terminate")):
        raise RuntimeError("offline collection had a protocol failure")
    if not focal.snapshots:
        return None
    v, action = focal.snapshots[index % len(focal.snapshots)]
    truth = next(d for d in DEALS if d.wolves == tuple(s for s, role in enumerate(game.roles) if role == "wolf")
                 and d.seer == game.roles.index("seer"))
    worlds = tuple(d for d in DEALS if feasible(d, v))
    evidence = public_evidence(v)
    ballots = next(row["votes"] for row in game.votes if row["day"] == v.day)
    return Example(worlds, tuple(feature_vector(evidence, d) for d in worlds), worlds.index(truth),
                   v.me, v.role, tuple(v.alive), action, tuple(sorted(ballots.items())), family, index, v.day, seed)


def _example_record(ex):
    return {"world_ids": [DEALS.index(d) for d in ex.worlds], "rows": ex.rows, "label": ex.label,
            "me": ex.me, "role": ex.role, "alive": ex.alive, "baseline": ex.baseline, "ballots": ex.ballots,
            "family": ex.family, "game": ex.game, "day": ex.day, "seed": ex.seed}


def main(argv=None):
    import argparse
    from concurrent.futures import ProcessPoolExecutor
    import os
    import sys
    import time
    from local.werewolf_benchmark import source_manifest

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("train", "evaluate"), required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--families")
    parser.add_argument("--games", type=int, default=500)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    families = tuple(args.families.split(",")) if args.families else TRAIN_FAMILIES if args.mode == "train" else TRAIN_FAMILIES + HELD_FAMILIES
    if args.mode == "train":
        validate_training_split(args.seed, families)
    elif args.seed not in (203, 204) or any(f not in TRAIN_FAMILIES + HELD_FAMILIES for f in families):
        parser.error("evaluation is restricted to development 203/204 and declared families")
    if not 1 <= args.jobs <= 2 or not 1 <= args.games or len(set(families)) != len(families):
        parser.error("invalid bounded collection")
    if args.games * len(families) > (30000 if args.mode == "train" else 10000):
        parser.error("game budget exceeded")
    if args.out.resolve().parent != ROOT / "logs" / "werewolf_benchmarks" or not args.out.name.startswith("overnight-learned-"):
        parser.error("use logs/werewolf_benchmarks/overnight-learned-<run>")
    args.out.mkdir(exist_ok=False)
    for key in ("ARENA_RECORD", "ARENA_STATE", "ARENA_SUMMARY"):
        os.environ[key] = "0"
    os.environ["ARENA_WW_TIMING"] = "rt"
    os.environ["ARENA_WW_TACTICS"] = "off"
    source = source_manifest()
    started = time.perf_counter()
    samples, digest = [], hashlib.sha256()
    tasks = [(args.seed, f, g) for f in families for g in range(args.games)]
    with ProcessPoolExecutor(args.jobs) as pool, (args.out / "samples.jsonl").open("w") as output:
        for i, sample in enumerate(pool.map(_collect_one, tasks, chunksize=10), 1):
            if sample is not None:
                samples.append(sample)
                line = json.dumps(_example_record(sample), sort_keys=True, separators=(",", ":")) + "\n"
                output.write(line)
                digest.update(line.encode())
            if i % 500 == 0:
                print(f"{i}/{len(tasks)} games; {len(samples)} independent observed-seat snapshots", flush=True)
    training_digest, history = None, None
    if args.mode == "train":
        weights, history = fit_weights(samples)
        training_digest = digest.hexdigest()
        payload = {"schema_version": 1, "features": list(FEATURES), "weights": weights,
                   "training_digest": training_digest, "samples": len(samples)}
        MODEL_PATH.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n")
    fitted = load_model()
    report = {"mode": args.mode, "seed": args.seed, "families": families,
              "training_families": TRAIN_FAMILIES, "held_families": HELD_FAMILIES,
              "family_split_sha256": hashlib.sha256(json.dumps([TRAIN_FAMILIES, HELD_FAMILIES], separators=(",", ":")).encode()).hexdigest(),
              "command": [sys.executable, "-m", "local.werewolf_learned_research", *(argv if argv is not None else sys.argv[1:])],
              "feature_names": FEATURES, "profiles": PROFILES, "regularization": 0.02,
              "optimizer_steps": 160 if args.mode == "train" else 0, "objective_history": history,
              "selector": {"margin": SWITCH_MARGIN, "minimum_probability": MIN_WOLF_PROBABILITY},
              "games": len(tasks), "examples": len(samples), "samples_sha256": digest.hexdigest(),
              "model_sha256": fitted.sha256, "training_digest": fitted.training_digest,
              "weights": fitted.weights, "source_before": source, "source_after": source_manifest(),
              "metrics": {"all": calibration_report(samples, fitted),
                          "in_family": calibration_report([ex for ex in samples if ex.family in TRAIN_FAMILIES], fitted),
                          "held_family": calibration_report([ex for ex in samples if ex.family in HELD_FAMILIES], fitted),
                          **{f: calibration_report([ex for ex in samples if ex.family == f], fitted) for f in families}},
              "metric_contract": {"predictions": "wolf metrics exclude hard-known living seats; world loss uses all feasible worlds",
                                  "sampling": "one baseline-seat snapshot per completed game; no independent-seat inflation",
                                  "selector": "fixed other ballots are diagnostic counterfactuals, not causal win estimates",
                                  "map_ties": "baseline action breaks equal posterior scores, including the uniform control",
                                  "scope": "offline synthetic behavior; no empirical live-game calibration claim"},
              "elapsed_s": time.perf_counter() - started}
    (args.out / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    for scope in ("all", "in_family", "held_family"):
        for profile, metrics in report["metrics"][scope].items():
            print(scope, profile, json.dumps({k: v for k, v in metrics.items() if k != "calibration_bins"}, sort_keys=True))
    print("model_sha256", fitted.sha256, "training_digest", fitted.training_digest, "weights", fitted.weights)
    return report


if __name__ == "__main__":
    main()
