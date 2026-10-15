"""Our rule-based Red Alert bot (docs/redalert_design.md). No LLM: deterministic rules over each real-time view.

`RedAlertPolicy.decide(raw, game_config)` reads one state and returns (orders, phase tag): a list of 1-20 orders
that passed local validation, or None for "nothing to send now" (the Contestant then answers altruagent.WAIT; in
windows mode, where WAIT is fatal, a harmless valid no-op goes instead). `on_result(result)` takes every play_action
answer (the runner's on_action_result hook). One policy per match; nothing is persisted (after a worker restart it
rebuilds from the state). Never raises: an exception is logged once per kind, counted, and answered with None; five
within 30 s switch to safe mode (agent/redalert/safe.py) for 60 s.

Modules: world (static map data + parsed view), ledger (sent-not-seen, verdicts, blacklist), orders (validation,
batch limits), economy (Building / Defense queues, power, placement, repair, rally), production (Infantry / Vehicle
queues), army (squads, attack / defense, endgame), safe (fallback), params (ARENA_RA_* tunables).
"""
from __future__ import annotations

import logging
import time
from collections import deque

from . import safe
from .army import Army
from .economy import Economy
from .harvest import Harvest
from .ledger import Ledger
from .orders import MAX_ORDERS, Cand, anchor, anchor2, finalize, noop
from .params import load as load_params
from .production import Production
from .world import Static, View

log = logging.getLogger("agent.redalert")

SAFE_AFTER = 5                 # exceptions within EXC_WINDOW_S ...
EXC_WINDOW_S = 30.0
SAFE_FOR_TICKS = 60 * 25       # ... switch to safe mode for 60 s
RECORD_EVERY_TICKS = 250       # sparse recording: one state every 10 game seconds
STATUS_EVERY_S = 30.0
MAX_ARMY_SKIPS = 2             # slow frames in a row that may skip the army step


class RedAlertPolicy:
    def __init__(self, params=None, clock=time.perf_counter):
        self.p = params or load_params()
        self.clock = clock
        self.static: Static | None = None
        self.ledger = Ledger()
        if int(self.p.deploy_pending_ticks) > 0:
            # v4.6: a deploy the platform accepted is not sent again for this long (v4.5: 4 s like an unanswered
            # one). With a lagging view the MCV still showed after 4 s, the deploy went again alone (no building yet
            # for the anchor) and the MCV had become the yard meanwhile: the whole batch was refused (INVALID_ACTION;
            # the fake-platform test at 25x speed on a loaded machine, v4.5 too). Refused / unanswered deploys keep
            # the 4 s (ledger.on_result, PENDING_TICKS).
            self.ledger.accepted_ttl["deploy"] = int(self.p.deploy_pending_ticks)
        self.econ = Economy(self)
        self.prod = Production(self)
        self.army = Army(self)
        self.harv = Harvest(self)
        self.opening: str | None = None
        self.view: View | None = None
        self.tag = "opening"
        self.last_batch_tick = -10 ** 9
        self.safe_until = -10 ** 9
        self.safe_attack_tick = -10 ** 9
        self.safe_entries = 0
        self.exceptions = 0
        self.exc_kinds: dict[str, int] = {}
        self._exc_times: deque = deque()
        self.slow_frames = 0
        self.army_skipped = 0
        self._army_skips = 0
        self.ms: list[float] = []
        self.batches = 0
        self.orders_sent = 0
        self.waits = 0
        self.dropped = 0
        self.noops = 0
        self.anchors = 0
        self.first: dict[str, float] = {}
        self.max_army = 0
        self.low_power_s = 0.0
        self._prev_tick = None
        self._last_status = -10 ** 9
        self._last_record_tick = -10 ** 9
        self.want_record = False
        self.events: list[str] = []

    # ---- logging helpers ----------------------------------------------------------------------------------------
    def log_event(self, text: str) -> None:
        self.events.append(text)
        if len(self.events) > 50:
            del self.events[:10]
        log.info("[ra] %s", text)

    def note_exception(self, kind: str, exc: BaseException | None = None) -> None:
        self.exceptions += 1
        first = kind not in self.exc_kinds
        self.exc_kinds[kind] = self.exc_kinds.get(kind, 0) + 1
        if first:
            log.error("Red Alert policy: %s failed (logged once per kind; answering WAIT)", kind,
                      exc_info=(type(exc), exc, exc.__traceback__) if exc is not None else None)
        self.want_record = True
        now = time.monotonic()
        self._exc_times.append(now)
        while self._exc_times and now - self._exc_times[0] > EXC_WINDOW_S:
            self._exc_times.popleft()
        tick = self.view.tick if self.view is not None else 0
        if len(self._exc_times) >= SAFE_AFTER and tick >= self.safe_until:
            self.safe_until = tick + SAFE_FOR_TICKS
            self.safe_entries += 1
            self._exc_times.clear()
            log.error("Red Alert policy: %d exceptions within %.0f s; safe mode for 60 s", SAFE_AFTER, EXC_WINDOW_S)

    # ---- the decision --------------------------------------------------------------------------------------------
    def decide(self, raw, config=None):
        """(orders or None, phase tag). Never raises."""
        t0 = self.clock()
        self.want_record = False
        windows = False
        orders = None
        try:
            pacing = raw.get("pacing") if isinstance(raw, dict) else None
            windows = isinstance(pacing, dict) and pacing.get("mode") == "windows"
            orders = self._decide(raw, config, t0)
        except Exception as e:                              # noqa: BLE001 - never raise to the runner
            self.note_exception("decide:" + type(e).__name__, e)
            orders = None
        if not orders and windows:
            orders = self._noop()
            if orders:
                self.noops += 1
        ms = (self.clock() - t0) * 1000.0
        self.ms.append(ms)
        if orders:
            self.batches += 1
            self.orders_sent += len(orders)
        else:
            self.waits += 1
        return orders or None, self.tag

    def _noop(self):
        try:
            if self.view is not None:
                o = anchor(self.view) or noop(self.view)
                if o is not None:
                    return [o]
        except Exception as e:                              # noqa: BLE001
            self.note_exception("noop", e)
        return None

    def _decide(self, raw, config, t0):
        if not isinstance(raw, dict) or not isinstance(raw.get("observation"), dict):
            return None
        if self.static is None:
            self.static = Static(raw, config)
        v = View(raw, self.static)
        if not self.static.ready:
            self.static.setup(v.base, v.enemy_hint)
        self.view = v
        self.ledger.update(v)
        self._track(v)
        allow = v.windows or (v.tick - self.last_batch_tick) >= self.p.throttle_ticks
        cands = []
        if v.tick < self.safe_until:
            try:
                cands = safe.decide(v, self)
            except Exception as e:                          # noqa: BLE001
                self.note_exception("safe", e)
                cands = []
            self.tag = "economy"
        else:
            for name, fn in (("economy", lambda: self.econ.step(v, cands, allow)),
                             ("production", lambda: self.prod.step(v, cands))):
                try:
                    fn()
                except Exception as e:                      # noqa: BLE001 - one module failing must not stop the rest
                    self.note_exception(name, e)
            elapsed = (self.clock() - t0) * 1000.0
            if elapsed > self.p.budget_ms:
                self.slow_frames += 1
            if elapsed > self.p.budget_ms and self._army_skips < MAX_ARMY_SKIPS:
                # a slow frame (wall time: the machine may be busy) skips the army step, but never more than
                # MAX_ARMY_SKIPS views in a row: the step itself takes a few ms, and a starved CPU must not leave the
                # army without orders for long
                self._army_skips += 1
                self.army_skipped += 1
                if self.army_skipped == 1:
                    log.warning("[ra] slow frame (%.0f ms before the army step): army skipped this view", elapsed)
                try:
                    self.army.refresh(v)
                except Exception as e:                      # noqa: BLE001
                    self.note_exception("army.refresh", e)
            else:
                self._army_skips = 0
                try:
                    self.army.step(v, cands, allow)
                except Exception as e:                      # noqa: BLE001
                    self.note_exception("army", e)
            self.tag = self.army.tag
        orders, kept, dropped = finalize(cands, v, self.ledger, urgent_only=not allow)
        self.dropped += dropped
        if orders and len(orders) < MAX_ORDERS:
            # every batch could be invalidated as a whole by the world before the platform checks it (in windows mode
            # that would end the seat): add an order that is always valid and costs no engine command. (v4.5: every
            # batch, not only all-fragile ones; with 1 s of latency, 2 v4.4 / 3 v4.5 games out of 48 had a batch of
            # 3+-unit attack_moves whose units had all died -> INVALID_ACTION; the real-time e2e rehearsal had one.)
            a = anchor(v)
            if a is not None and not any(o.get("cmd") == "power_toggle" and o.get("building") == a["building"]
                                         for o in orders):
                orders.append(a)
                kept.append(Cand(a))
                self.anchors += 1
            if a is not None and self.p.anchor2 and len(orders) < MAX_ORDERS:
                a2 = anchor2(v, a["building"])
                if a2 is not None and not any(o.get("cmd") == "power_toggle" and o.get("building") == a2["building"]
                                              for o in orders):
                    orders.append(a2)
                    kept.append(Cand(a2))
        new = self.ledger.take_new_reasons()
        if new:
            for r in new:
                log.warning("[ra] first refusal with reason %r at %.0fs (counted from now on)", r, v.game_s)
            self.want_record = True
        if v.tick - self._last_record_tick >= RECORD_EVERY_TICKS:
            self.want_record = True
        if self.want_record:
            self._last_record_tick = v.tick
        if not orders:
            return None
        keys = []
        for c in kept:
            if c.key is not None:
                self.ledger.add_pending(c.key, v.sv, c.base)
            if c.sig is not None:
                for u in c.order.get("units") or []:
                    self.ledger.note_unit(u, c.sig)
            keys.append(c.key)
        self.ledger.note_sent(v.sv, orders, keys)
        self.last_batch_tick = v.tick
        return orders

    def on_result(self, result) -> None:
        """play_action's answer (or the runner's recovered refusal). Never raises."""
        try:
            self.ledger.on_result(result)
        except Exception as e:                              # noqa: BLE001
            self.note_exception("on_result", e)

    # ---- metrics -----------------------------------------------------------------------------------------------
    def _track(self, v) -> None:
        for t in ("powr", "proc", "tent", "barr", "weap", "fix"):
            if t not in self.first and v.have.get(t):
                self.first[t] = round(v.game_s, 1)
        self.max_army = max(self.max_army, v.army_value)
        if self.p.harv_first:
            try:
                self.harv.track(v)
                self.harv.serialized(v)                     # once per view (army state of the previous view)
            except Exception as e:                          # noqa: BLE001
                self.note_exception("harvest", e)
        if self._prev_tick is not None and v.buildings and v.surplus < 0:
            self.low_power_s += max(0, v.tick - self._prev_tick) / 25.0
        self._prev_tick = v.tick
        if v.game_s - self._last_status >= STATUS_EVERY_S:
            self._last_status = v.game_s
            a = self.army
            log.info("[ra] t=%.0fs cash=%d ore=%d pow=%+d harv=%d bld=%d army=%d K=%d D=%d assets=%d phase=%s "
                     "state=%s home/main/reinf=%d/%d/%d enemy=%.0f veh=%.2f rush=%s batches=%d rej=%d", v.game_s,
                     v.cash, v.ore, v.surplus, v.unit_count.get("harv", 0), len(v.buildings), v.army_value, v.kills,
                     v.deaths, v.assets, self.tag, a.state, len(a.home), len(a.main), len(a.reinf), a.enemy_army(v),
                     a.enemy_vehicle_share(), a.rush, self.batches, self.ledger.rejected)

    def summary(self) -> dict:
        ms = sorted(self.ms)
        v = self.view
        return {
            "opening": self.opening, "first_powr_s": self.first.get("powr"), "first_proc_s": self.first.get("proc"),
            "first_weap_s": self.first.get("weap"), "first_fix_s": self.first.get("fix"), "max_army": self.max_army,
            "K": v.kills if v else None, "D": v.deaths if v else None, "assets": v.assets if v else None,
            "batches": self.batches, "orders": self.orders_sent, "waits": self.waits, "noops": self.noops,
            "anchors": self.anchors,
            "rejected": dict(self.ledger.reasons), "lost": self.ledger.lost,
            "invalid_action": self.ledger.invalid_actions, "errors": dict(self.ledger.errors),
            "ms_p50": round(ms[len(ms) // 2], 2) if ms else None,
            "ms_p99": round(ms[min(len(ms) - 1, int(0.99 * len(ms)))], 2) if ms else None,
            "ms_max": round(ms[-1], 1) if ms else None, "exceptions": self.exceptions,
            "exception_kinds": dict(self.exc_kinds), "safe_mode": self.safe_entries, "slow_frames": self.slow_frames,
            "low_power_s": round(self.low_power_s, 1), "army": self.army.summary(),
            "game_s": round(v.game_s, 1) if v else None, "harv_emergencies": self.econ.emergencies,
            "sales": self.econ.sales, "power_cancels": self.econ.power_cancels, "army_skips": self.army_skipped,
            "at_towers": self.econ.at_towers,
            "harv": self.harv.summary() if self.p.harv_first else None,
        }

    def summary_text(self) -> str:
        s = self.summary()
        rej = ",".join(f"{k}:{n}" for k, n in sorted(s["rejected"].items())) or "0"
        h = s.get("harv")
        harv = "" if not h else (f" harv_180={h['harv_180']} harv_270={h['harv_270']} harv_390={h['harv_390']} "
                                 f"serial_s={h['serial_s']} harv_spreads={h['harv_spreads']} "
                                 f"harv_rebalances={h['harv_rebalances']}")
        return (f"opening={s['opening']} first_powr_s={s['first_powr_s']} first_proc_s={s['first_proc_s']} "
                f"first_weap_s={s['first_weap_s']} first_fix_s={s['first_fix_s']} max_army={s['max_army']} "
                f"K={s['K']} D={s['D']} assets={s['assets']} game_s={s['game_s']} batches={s['batches']} "
                f"orders={s['orders']} rejected={rej} lost={s['lost']} invalid_action={s['invalid_action']} "
                f"waits={s['waits']} noops={s['noops']} anchors={s['anchors']} ms_p50={s['ms_p50']} ms_p99={s['ms_p99']} "
                f"exceptions={s['exceptions']} safe_mode={s['safe_mode']} low_power_s={s['low_power_s']} "
                f"waves={s['army']['waves']} harv_emergencies={s['harv_emergencies']} sales={s['sales']} "
                f"power_cancels={s['power_cancels']} hunt_seen={s['army']['hunt_seen']} "
                f"rescouts={s['army']['rescouts']} harv_pulls={s['army']['harv_pulls']} "
                f"harv_helps={s['army']['harv_helps']} early_army={int(s['army']['early_army'])} "
                f"base_seen={int(s['army']['base_seen'])} trade_retreats={s['army']['trade_retreats']} "
                f"comp_kept={s['army']['comp_kept']} tank_army_s={s['army']['tank_army_s']} "
                f"at_towers={s['at_towers']}" + harv)
