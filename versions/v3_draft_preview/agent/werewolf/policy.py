"""Baseline heuristic Werewolf policy (v0).

Deterministic on purpose: every copy of this agent computes the same suspicion
ranking from the same public record, so villagers converge their votes and the
two wolves never split their night kill (ties go to the lower seat — werewolf.md §5).

Upgrade path: an LLM brain replaces chat()/rank decisions, with this module as
the guardrail + timeout fallback (see docs/strategy.md).
"""
from __future__ import annotations

from collections import defaultdict

from .state import ACCUSE_RE, SEER_CLAIM_RE, View, parse

ABSTAIN = "7"


class WerewolfPolicy:
    """One instance per match (the starter runs each match in its own process)."""

    def __init__(self):
        self.transcript: dict[int, dict] = {}   # seq -> message envelope, whole game

    # ---- helpers -------------------------------------------------------
    def observe(self, st: dict) -> None:
        for m in st.get("new_messages") or []:
            key = m.get("seq", m.get("index"))
            if key is not None:
                self.transcript[key] = m

    def _view(self, st: dict) -> View:
        self.observe(st)
        return parse(st, [self.transcript[k] for k in sorted(self.transcript)])

    @staticmethod
    def seer_claims(v: View) -> dict[int, list[int]]:
        """Broadcast seer claims: claimer seat -> seats they called wolf."""
        claims: dict[int, list[int]] = {}
        for m in v.messages:
            if m.get("type") != "chat" or m.get("visibility") == "direct":
                continue
            text, sender = m.get("content") or "", m.get("sender")
            if SEER_CLAIM_RE.search(text):
                claims.setdefault(sender, [])
                claims[sender] += [int(s) for s in ACCUSE_RE.findall(text)]
        return claims

    @staticmethod
    def suspicion(v: View, exclude: set[int] = frozenset()) -> dict[int, float]:
        """Public-record suspicion of every living seat except `exclude` and me."""
        score: dict[int, float] = defaultdict(float)
        for day, votes, elim in v.votes:
            elim_role = v.dead.get(elim) if elim is not None else None
            for voter, tgt in votes.items():
                if tgt is None:
                    score[voter] += 0.3 if elim is None else 0.1   # abstaining feeds the stall rule
                elif elim is not None and tgt == elim:
                    score[voter] += 1.0 if elim_role in ("villager", "seer") else -0.7
                elif tgt is not None and v.dead.get(tgt) in ("villager", "seer") and v.dead_cause.get(tgt) == "night_kill":
                    score[voter] += 0.2   # voted someone the wolves later killed -> slightly suspicious
        # Chat accusations: weak signal; seer claims: strong unless contested.
        claims = WerewolfPolicy.seer_claims(v)
        live_claimers = [c for c in claims if c in v.alive or v.dead.get(c) == "seer"]
        for m in v.messages:
            if m.get("type") == "chat" and m.get("visibility") != "direct":
                for s in ACCUSE_RE.findall(m.get("content") or ""):
                    score[int(s)] += 0.25
        for claimer, targets in claims.items():
            confirmed = v.dead.get(claimer) == "seer"
            if len(live_claimers) > 1 and not confirmed:
                score[claimer] += 0.8     # counter-claims: at least one is a wolf
            weight = 3.0 if confirmed else (1.5 if len(live_claimers) == 1 else 0.5)
            for t in targets:
                score[t] += weight
        return {s: score[s] for s in v.alive if s != v.me and s not in exclude}

    @staticmethod
    def _top(scores: dict[int, float], pool: list[int]) -> int | None:
        pool = [s for s in pool if s in scores]
        if not pool:
            return None
        return max(pool, key=lambda s: (scores[s], -s))   # tie -> lower seat, identical across copies

    @staticmethod
    def _legal(st: dict) -> dict[str, dict]:
        return {a["action_id"]: a for a in (st.get("legal_actions") or {}).get("actions", [])}

    # ---- decisions -----------------------------------------------------
    def act(self, st: dict) -> dict:
        v = self._view(st)
        legal = self._legal(st)
        targets = [int(a) for a in legal if a != ABSTAIN]
        pick: int | None = None
        why = ""

        if v.sub_phase == "night_wolf":
            pick, why = self._wolf_kill(v, targets)
        elif v.sub_phase == "night_seer":
            unknown = [t for t in targets if t not in v.investigations]
            sus = self.suspicion(v)
            pick = self._top(sus, unknown) if unknown else None
            if pick is None and unknown:
                pick = min(unknown)
            why = "investigate highest-suspicion unread seat"
        elif v.sub_phase == "day_vote":
            pick, why = self._day_vote(v, targets)

        if pick is None or str(pick) not in legal:
            return self.fallback(st)
        return {"action_id": str(pick), "reasoning_summary": why}

    def _wolf_kill(self, v: View, targets: list[int]) -> tuple[int | None, str]:
        claims = self.seer_claims(v)
        claimers = [c for c in claims if c in targets]
        if claimers:
            return min(claimers), "remove the claimed seer"
        # Kill whoever the table trusts most (lowest suspicion) — trusted villagers steer votes.
        sus = self.suspicion(v, exclude=set(v.allies))
        pool = [t for t in targets if t in sus]
        if not pool:
            return (min(targets) if targets else None), "default"
        return min(pool, key=lambda s: (sus[s], s)), "remove the most trusted villager"

    def _day_vote(self, v: View, targets: list[int]) -> tuple[int | None, str]:
        if v.role == "seer":
            wolves = [s for s, r in v.investigations.items() if r == "wolf" and s in targets]
            if wolves:
                return min(wolves), "seer result: wolf"
        if v.role == "wolf":
            sus = self.suspicion(v, exclude=set(v.allies))
            pick = self._top(sus, [t for t in targets if t not in v.allies])
            return pick, "vote with the table"
        cleared = {s for s, r in v.investigations.items() if r == "villager"}
        sus = self.suspicion(v, exclude=cleared)
        pick = self._top(sus, targets)
        return pick, "highest public suspicion (never abstain — stall rule favors wolves)"

    def fallback(self, st: dict) -> dict:
        legal = self._legal(st)
        non_abstain = [a for a in legal if a != ABSTAIN]
        aid = min(non_abstain, key=int) if non_abstain else next(iter(legal))
        return {"action_id": aid}

    # ---- discussion ----------------------------------------------------
    def chat(self, st: dict) -> list[dict]:
        v = self._view(st)
        if v.role == "seer":
            wolves = [s for s, r in v.investigations.items() if r == "wolf" and s in v.alive]
            clears = [s for s, r in v.investigations.items() if r == "villager" and s in v.alive]
            if wolves:
                txt = f"I am the Seer. Player{wolves[0]} is a wolf."
                if clears:
                    txt += " " + ", ".join(f"Player{c}" for c in clears) + " checked villager."
                return [{"content": txt + " Vote Player%d today." % wolves[0]}]
            if v.day and v.day >= 2 and clears:
                return [{"content": "I am the Seer. " + ", ".join(f"Player{c} is villager" for c in clears) + "."}]
        exclude = set(v.allies) if v.role == "wolf" else set()
        sus = self.suspicion(v, exclude=exclude)
        top = self._top(sus, list(sus))
        if top is None or sus.get(top, 0) <= 0:
            return [{"content": "No hard info yet. Let's not abstain — three stalled days hands it to the wolves. Seer, speak up if you have a result."}]
        return [{"content": f"My read from the vote record: Player{top} is most suspicious. I'll vote Player{top} unless someone has better info."}]
