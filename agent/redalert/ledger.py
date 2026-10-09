"""The bot's memory of what it sent: the "sent but not yet seen" ledger (never queue the same build twice), per-unit
command times, verdict tracking (play_action answers, last_orders, recent_order_problems) and the refusal blacklist
ported from upstream's examples/llm/redalert.py (the same order refused 3 times for the same reason is dropped for
30 s while the reason still holds)."""
from __future__ import annotations

import json
import re

# How long a pending entry lives without confirmation (ticks, 25 a second), per kind.
PENDING_TICKS = {"build": 75, "train": 75, "place": 50, "deploy": 100, "rally": 125, "repair": 50, "cancel": 50,
                 "power": 50, "primary": 125, "stance": 125}
BLOCK_AFTER = 3
BLOCK_TICKS = 30 * 25
FORGET_TICKS = 120 * 25
_REFUSAL_ROW = re.compile(r"\[(\d+)\] ([a-z_]+): ([a-z_]+)(?: \(([^)]*)\))?")
# Batch-level errors: nothing in the batch was applied. Any other code the runner reports comes from a temporary
# failure it recovered from (SDK PR #7: a gateway / server error, a tool failure): the batch may have landed.
NOTHING_APPLIED = ("NOT_YOUR_TURN", "STALE_STATE", "INVALID_ACTION", "RUNTIME_TEMPORARILY_UNAVAILABLE",
                   "GAME_ALREADY_COMPLETE", "WRONG_PHASE", "PLAYER_ELIMINATED")


def order_key(order: dict) -> str:
    body = {k: v for k, v in order.items() if k != "queued"}
    if isinstance(body.get("units"), list):
        body["units"] = sorted(body["units"], key=str)
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def refusal_rows(detail) -> list[tuple[int, str, str, str | None]]:
    if not isinstance(detail, str):
        return []
    return [(int(m.group(1)), m.group(2), m.group(3), m.group(4)) for m in _REFUSAL_ROW.finditer(detail)]


class Ledger:
    def __init__(self):
        self.pending: dict[tuple, dict] = {}          # key -> {tick, base, sv}
        self.batches: dict[int, dict] = {}            # state_version sent -> {orders, keys, tick, batch}
        self.by_batch: dict[int, int] = {}            # server batch number -> state_version
        self.last_sv: int | None = None               # the batch play_action answers next
        self.unit_cmd: dict[int, tuple] = {}          # unit id -> (tick, signature)
        self.refusals: dict[str, dict] = {}           # order key -> {order, reason, count, last}
        self.reasons: dict[str, int] = {}             # rejection reason -> count (whole match)
        self.new_reasons: list[str] = []              # reasons seen for the first time since the last read
        self.place_invalid: dict[str, int] = {}       # item -> placements refused as invalid (in a row)
        self.deploy_invalid = 0
        self.seen_rows: dict = {}                     # (batch, index) of final rows already handled (ordered)
        self.invalid_actions = 0
        self.errors: dict[str, int] = {}
        self.uncertain = 0                            # batches a temporary failure left unknown (may have landed)
        self.lost = 0
        self.rejected = 0
        self.tick = 0

    # ---- pending ---------------------------------------------------------------------------------------------------
    def is_pending(self, key: tuple) -> bool:
        e = self.pending.get(key)
        if e is None:
            return False
        if self.tick - e["tick"] > PENDING_TICKS.get(key[0], 75):
            del self.pending[key]
            return False
        return True

    def add_pending(self, key: tuple, sv, base: int = 0) -> None:
        self.pending[key] = {"tick": self.tick, "base": base, "sv": sv}

    def clear(self, key: tuple) -> None:
        self.pending.pop(key, None)

    # ---- unit commands -------------------------------------------------------------------------------------------
    def unit_ok(self, uid: int, sig, idle: bool, min_gap: int = 20, same_gap: int = 75) -> bool:
        """May unit `uid` get a command with signature `sig` now: not within min_gap ticks of its last command, and
        not the same command again within same_gap ticks unless it went idle."""
        last = self.unit_cmd.get(uid)
        if last is None:
            return True
        dt = self.tick - last[0]
        if dt < min_gap:
            return False
        if last[1] == sig and dt < same_gap and not idle:
            return False
        return True

    def note_unit(self, uid: int, sig) -> None:
        self.unit_cmd[uid] = (self.tick, sig)

    # ---- sending ---------------------------------------------------------------------------------------------------
    def note_sent(self, sv, orders: list[dict], keys: list) -> None:
        if not isinstance(sv, int):
            sv = -self.tick - 1
        self.batches[sv] = {"orders": orders, "keys": keys, "tick": self.tick, "batch": None, "answered": set()}
        self.last_sv = sv
        if len(self.batches) > 60:
            for old in sorted(self.batches)[:-40]:
                rec = self.batches.pop(old)
                if rec.get("batch") is not None:
                    self.by_batch.pop(rec["batch"], None)

    def on_result(self, result: dict) -> None:
        """play_action's answer for the batch just sent (or {"error", "detail"} when the runner recovered)."""
        rec = self.batches.get(self.last_sv) if self.last_sv is not None else None
        if not isinstance(result, dict):
            return
        err = result.get("error")
        if err:
            self.errors[str(err)] = self.errors.get(str(err), 0) + 1
            if rec is None:
                return
            if str(err) not in NOTHING_APPLIED:
                # A temporary failure the runner recovered from without resending: the batch may have landed. Its
                # pending entries and unit command times stay (the next states show what landed, and an entry that
                # never shows up expires after PENDING_TICKS), so nothing is queued twice.
                self.uncertain += 1
                return
            # A refusal of the whole batch (NOTHING_APPLIED): nothing of it ran, so its pending entries and unit
            # command times are dropped and the orders may go again at once.
            for key in rec["keys"]:
                if key is not None:
                    self.pending.pop(key, None)
            for o in rec["orders"]:
                for u in o.get("units") or []:
                    if self.unit_cmd.get(u, (None,))[0] == rec["tick"]:
                        self.unit_cmd.pop(u, None)
            if err == "INVALID_ACTION":
                self.invalid_actions += 1
                for idx, cmd, reason, _why in refusal_rows(result.get("detail")):
                    if 0 <= idx < len(rec["orders"]) and rec["orders"][idx].get("cmd") == cmd:
                        self._refused(rec["orders"][idx], reason)
            self.batches.pop(self.last_sv, None)
            return
        if rec is None:
            return
        for row in result.get("results") or []:
            if not isinstance(row, dict):
                continue
            idx = row.get("index")
            if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(rec["orders"]):
                continue
            status = row.get("status")
            if status == "rejected":
                key = rec["keys"][idx]
                if key is not None:
                    self.pending.pop(key, None)
                rec["answered"].add(idx)
                self._refused(rec["orders"][idx], str(row.get("reason") or "rejected"))
            elif status in ("pending", "ok", "partial"):
                self.refusals.pop(order_key(rec["orders"][idx]), None)

    def _refused(self, order: dict, reason: str) -> None:
        self.rejected += 1
        if reason not in self.reasons:
            self.new_reasons.append(reason)
        self.reasons[reason] = self.reasons.get(reason, 0) + 1
        if order.get("cmd") == "place":
            item = str(order.get("item") or "")
            if reason == "invalid":
                self.place_invalid[item] = self.place_invalid.get(item, 0) + 1
        if order.get("cmd") == "deploy" and reason == "invalid":
            self.deploy_invalid += 1
        if reason == "target_not_visible":
            return
        k = order_key(order)
        e = self.refusals.get(k)
        count = e["count"] + 1 if e and e["reason"] == reason else 1
        self.refusals[k] = {"order": order, "reason": reason, "count": count, "last": self.tick}

    # ---- every view: verdicts in the state -------------------------------------------------------------------------
    def update(self, view) -> None:
        self.tick = view.tick
        lo = view.last_orders
        if lo is not None:
            sv = lo.get("state_version")
            rec = self.batches.get(sv) if isinstance(sv, int) else None
            if rec is not None:
                b = lo.get("batch")
                if isinstance(b, int) and not isinstance(b, bool):
                    rec["batch"] = b
                    self.by_batch[b] = sv
                if lo.get("pending") is False:
                    self._final_rows(rec, lo.get("results") or [], b)
        for row in view.problems:
            b, idx = row.get("batch"), row.get("index")
            if (b, idx) in self.seen_rows:
                continue
            self.seen_rows[(b, idx)] = True
            sv = self.by_batch.get(b)
            rec = self.batches.get(sv) if sv is not None else None
            if rec is None or not isinstance(idx, int) or not 0 <= idx < len(rec["orders"]):
                continue
            self._final_row(rec, idx, row.get("status"), row.get("reason"))
        if len(self.seen_rows) > 600:
            for k in list(self.seen_rows)[:300]:          # oldest first (insertion order)
                del self.seen_rows[k]
        # pending builds / trains: seen in the production list = done
        for key in [k for k in self.pending if k[0] in ("build", "train")]:
            e = self.pending[key]
            if view.in_prod.get(key[1], 0) > e["base"]:
                self.pending.pop(key, None)
        for key in [k for k in self.pending if k[0] == "place"]:
            if not any(item == key[1] for _q, item in view.ready):
                self.pending.pop(key, None)
        # forget old refusals
        for k in [k for k, e in self.refusals.items() if self.tick - e["last"] > FORGET_TICKS]:
            del self.refusals[k]
        for k in [k for k, (t, _s) in self.unit_cmd.items() if self.tick - t > 750]:
            del self.unit_cmd[k]

    def _final_rows(self, rec: dict, rows: list, batch) -> None:
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("index"), int):
                key = (batch, row["index"])
                if key in self.seen_rows:
                    continue
                self.seen_rows[key] = True
                self._final_row(rec, row["index"], row.get("status"), row.get("reason"))

    def _final_row(self, rec: dict, idx: int, status, reason) -> None:
        if not 0 <= idx < len(rec["orders"]):
            return
        key = rec["keys"][idx]
        order = rec["orders"][idx]
        if status in ("rejected", "lost"):
            if key is not None:
                self.pending.pop(key, None)
            if status == "lost":
                self.lost += 1
                for u in order.get("units") or []:
                    self.unit_cmd.pop(u, None)
            elif idx not in rec["answered"]:                 # (a runner-side refusal was counted from the answer)
                self._refused(order, str(reason or "rejected"))
        elif status in ("ok", "partial"):
            if key is not None and key[0] in ("build", "train"):
                self.pending.pop(key, None)                  # applied: the item is in this state's production list
            if order.get("cmd") == "place":
                self.place_invalid.pop(str(order.get("item") or ""), None)
            if order.get("cmd") == "deploy":
                self.deploy_invalid = 0

    # ---- blacklist --------------------------------------------------------------------------------------------------
    def blocked(self, order: dict, view) -> bool:
        e = self.refusals.get(order_key(order))
        if e is None or e["count"] < BLOCK_AFTER or self.tick - e["last"] >= BLOCK_TICKS:
            return False
        reason, cmd = e["reason"], order.get("cmd")
        item = str(order.get("item") or "").lower()
        if reason == "not_ready" and cmd == "place":
            return not any(i == item for _q, i in view.ready)
        if reason == "not_available" and cmd in ("build", "train"):
            return item not in view.avail
        if reason in ("is_building", "not_owned", "wreck") and order.get("units"):
            return not set(order["units"]) <= set(view.unit_by_id)
        return True

    def take_new_reasons(self) -> list[str]:
        out, self.new_reasons = self.new_reasons, []
        return out
