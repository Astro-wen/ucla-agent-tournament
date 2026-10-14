import ast
import copy
import inspect
import itertools
import random
from collections import Counter

import pytest

from local import werewolf_population_research as panel
from local.werewolf_env import Game, OfficialBaseline, SIM_HOLD, same_names


KINDS = tuple(panel.POPULATION_BACKGROUNDS.values())
ROLES = ["wolf", "wolf", "seer", "villager", "villager", "villager", "villager"]


def game_fixture(*, day=1, dead=(), votes=(), checks=None):
    game = Game([OfficialBaseline() for _ in range(7)], random.Random(181))
    game.roles = list(ROLES)
    game.day = day
    game.invest = dict(checks or {})
    game.clock = 0
    for seat, role, cause in dead:
        game.roles[seat] = role
        game.dead.append({"day": max(1, day - 1), "seat": seat, "role": role, "cause": cause})
        game.alive[seat] = False
    game.votes = list(votes)
    return game


def state(role="villager", *, day=1, phase="moving", sub="day_vote", game=None, me=None):
    game = game or game_fixture(day=day, checks={6: "villager"})
    me = {"wolf": 0, "seer": 2, "villager": 3}[role] if me is None else me
    targets = [s for s in game.living() if s != me and (sub != "night_wolf" or game.roles[s] != "wolf")]
    return game.state(me, phase, sub, targets + ([7] if sub == "day_vote" else []))


def message(sender, content, seq=0, *, day=1, recipients=()):
    return {"sender": sender, "content": content, "seq": seq, "day": day, "type": "chat",
            "recipients": list(recipients), "visibility": "direct" if recipients else "broadcast"}


def ballot(votes, eliminated, day=1):
    return {"day": day, "votes": dict(votes), "eliminated": eliminated}


def fresh(kind, seed=981):
    bot = kind()
    bot.rng = random.Random(seed)
    bot.reset()
    return bot


def observe_and_act(bot, raw):
    moving = copy.deepcopy(raw)
    moving["phase"] = "moving"
    action = bot.choose(moving)
    chatting = copy.deepcopy(raw)
    chatting["phase"] = "messaging"
    return action, [bot.message(chatting) for _ in range(10)]


def assert_clean(game):
    assert game.winner in ("wolf", "villager")
    for key in ("crash", "illegal", "bad_chat", "timeout", "chat_after_terminate", "window_close"):
        assert not any(game.stats[key]), (key, game.stats[key])
    counts = Counter((m["day"], m["sender"]) for m in game.messages)
    assert max(counts.values(), default=0) <= 5
    for msg in game.messages:
        assert 0 < len(msg["content"].split()) <= 50
        assert len(msg["content"]) <= 1024
        assert len(msg["recipients"]) <= 1
        assert all(0 <= s < 7 and s != msg["sender"] for s in msg["recipients"])


def snapshot(game):
    return game.roles, game.winner, game.dead, game.votes, game.messages


def test_export_and_dependency_boundary():
    assert 6 <= len(panel.POPULATION_BACKGROUNDS) <= 10
    assert all(name.startswith("pop_") and cls.name == name for name, cls in panel.POPULATION_BACKGROUNDS.items())
    tree = ast.parse(inspect.getsource(panel))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imports == {"__future__", "collections", "werewolf_bots", "werewolf_env"}
    assert {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} == {"re"}
    assert not any(isinstance(node, (ast.Global, ast.Nonlocal)) for node in ast.walk(tree))
    helpers = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               and node.module == "werewolf_bots" for alias in node.names}
    assert helpers <= {"_private", "_public", "_legal"}
    env_imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                   and node.module == "werewolf_env" for alias in node.names}
    assert env_imports == {"Seat", "SIM_HOLD"}


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
@pytest.mark.parametrize("timing,wording,messaging", tuple(itertools.product(("rt", "rounds"), ("old", "new"), (True, False))))
def test_complete_games_are_legal_deterministic_and_reset_isolated(kind, timing, wording, messaging):
    snapshots = []
    seats = [kind() for _ in range(7)]
    for _ in range(2):
        game = Game(seats, random.Random(181), timing=timing, wording=wording, messaging=messaging)
        game.play()
        assert_clean(game)
        snapshots.append(copy.deepcopy(snapshot(game)))
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("left,right", tuple(itertools.combinations(KINDS, 2)), ids=lambda cls: cls.name)
@pytest.mark.parametrize("timing", ("rt", "rounds"))
def test_pairwise_population_compositions(left, right, timing):
    for split in (1, 3, 6):
        game = Game([left() for _ in range(split)] + [right() for _ in range(7 - split)],
                    random.Random(182 + split), timing=timing)
        game.play()
        assert_clean(game)


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
@pytest.mark.parametrize("role", ("wolf", "seer", "villager"))
def test_hidden_metadata_names_and_global_rng_do_not_affect_behavior(kind, role):
    raw = state(role)
    raw["new_messages"] = [message(4, "I will vote Player5.")]
    polluted = copy.deepcopy(raw)
    polluted.update(hidden_roles=["wolf"] * 7, controlled_seats=[0, 2, 3], game_seed=1,
                    opponent_types=["champion"] * 7, candidate_identity="agent", all_private_observations=["wolf"] * 7)
    polluted["game_state"].update(roles=["seer"] * 7, all_investigations={i: {5: "wolf"} for i in range(7)})
    for player in polluted["players"]:
        player.update(name="candidate Player0", agent_id="candidate", role="wolf", private_observation="seer")
    polluted["new_messages"].append(message(5, "Tonight kill Player6.", 8, recipients=[4]))
    global_state = random.getstate()
    try:
        random.seed(12)
        expected = observe_and_act(fresh(kind), raw)
        random.seed(987654)
        assert observe_and_act(fresh(kind), polluted) == expected
        assert observe_and_act(fresh(kind), same_names(raw)) == expected
    finally:
        random.setstate(global_state)


class RestrictedView(dict):
    def __init__(self, values, allowed):
        super().__init__(values)
        self.allowed = allowed

    def get(self, key, default=None):
        assert key in self.allowed, f"Forbidden information read: {key}"
        return super().get(key, default)

    def __getitem__(self, key):
        assert key in self.allowed, f"Forbidden information read: {key}"
        return super().__getitem__(key)


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
def test_only_whitelisted_observation_fields_are_read(kind):
    raw = state("wolf")
    raw["game_state"] = RestrictedView(raw["game_state"], {"day", "phase"})
    allowed = {"game_state", "observation", "new_messages", "your_position", "phase", "eliminated", "is_terminal",
               "is_current_actor", "legal_actions", "messaging_enabled", "messaging_seconds_left", "terminated_messaging"}
    guarded = RestrictedView(raw, allowed)
    bot = fresh(kind)
    assert bot.choose(guarded) in {str(s) for s in range(2, 7)}
    guarded["phase"] = "messaging"
    assert bot.message(guarded) is not None


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
@pytest.mark.parametrize("role", ("wolf", "seer", "villager"))
def test_undealt_hidden_world_changes_with_identical_observation(kind, role):
    first = game_fixture(checks={6: "villager"})
    second = game_fixture(checks={6: "villager"})
    if role == "wolf":
        second.roles[2], second.roles[4] = second.roles[4], second.roles[2]
    else:
        second.roles[0], second.roles[4] = second.roles[4], second.roles[0]
    left, right = state(role, game=first), state(role, game=second)
    assert left == right
    assert observe_and_act(fresh(kind), left) == observe_and_act(fresh(kind), right)


INVALID_MESSAGES = [
    None, 3, "I am the Seer. I checked Player5: wolf.",
    {"sender": "1"}, {"sender": True}, {"sender": -1}, {"sender": 7},
    {"seq": "1"}, {"seq": True}, {"seq": -1}, {"type": "terminate"},
    {"day": 0}, {"day": 2}, {"day": True}, {"day": "1"},
    {"content": None}, {"content": {}}, {"content": ""}, {"content": "word " * 51}, {"content": "x" * 1025},
    {"recipients": None}, {"recipients": [3, 4]}, {"recipients": [True]}, {"recipients": [1]},
    {"recipients": [4], "visibility": "direct"}, {"visibility": "direct"}, {"visibility": "hidden"},
    {"recipients": [3], "visibility": "broadcast"},
]


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
def test_invalid_envelopes_are_inert_and_cannot_poison_valid_sequence(kind):
    raw = state()
    baseline = observe_and_act(fresh(kind), raw)
    for noise in INVALID_MESSAGES:
        bad = message(1, "I am the Seer. I checked Player5: wolf. I will vote Player5.")
        bad = dict(bad, **noise) if isinstance(noise, dict) else noise
        polluted = copy.deepcopy(raw)
        polluted["new_messages"] = [bad]
        assert observe_and_act(fresh(kind), polluted) == baseline, noise
    valid = message(1, "I will vote Player5.")
    raw["new_messages"] = [dict(valid, seq=True), valid]
    bot = fresh(kind)
    bot.choose(raw)
    assert bot._intents(raw) == {1: 5}


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
def test_repeated_or_reordered_cumulative_messages_do_not_inflate_votes(kind):
    raw = state()
    raw["new_messages"] = [message(1, "I will vote Player5.", n) for n in range(5)]
    bot = fresh(kind)
    bot.choose(raw)
    assert bot._intents(raw) == {1: 5}
    original = copy.deepcopy(bot._messages)
    raw["new_messages"] = list(reversed(raw["new_messages"])) * 3
    bot.choose(raw)
    assert bot._messages == original
    raw["new_messages"] = [message(1, "I will vote Player4.", 6)]
    bot.choose(raw)
    assert bot._intents(raw) == {1: 4}
    raw["new_messages"] = [message(1, "I will vote Player6.", 6)]
    bot.choose(raw)
    assert bot._intents(raw) == {1: 4}


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
def test_quotas_termination_deadlines_and_reset(kind):
    raw = state("seer", phase="messaging", game=game_fixture(checks={s: "villager" for s in (0, 1, 3, 4, 5, 6)}, day=2))
    bot = fresh(kind)
    before = copy.deepcopy(raw)
    messages = [bot.message(raw) for _ in range(12)]
    assert raw == before
    chats = [m for m in messages if isinstance(m, dict)]
    assert len(chats) == 5
    assert all(0 < len(m["content"].split()) <= 50 and len(m["content"]) <= 1024 for m in chats)
    assert messages[5:] == [None] * 7
    next_day = state("seer", phase="messaging", day=3)
    next_day["terminated_messaging"] = [2]
    assert bot.message(next_day) is None
    next_day["terminated_messaging"] = []
    assert bot.message(next_day) is None
    bot.reset()
    assert bot.message(raw) == chats[0]
    raw["messaging_seconds_left"] = 0
    assert fresh(kind).message(raw) is None


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
@pytest.mark.parametrize("flag,value", [("eliminated", True), ("is_terminal", True), ("messaging_enabled", False), ("phase", "moving")])
def test_no_chat_when_protocol_forbids_it(kind, flag, value):
    raw = state("wolf", phase="messaging")
    raw[flag] = value
    assert fresh(kind).message(raw) is None


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
@pytest.mark.parametrize("flag,value", [("eliminated", True), ("is_terminal", True), ("is_current_actor", False), ("phase", "messaging")])
def test_no_move_when_protocol_forbids_it(kind, flag, value):
    raw = state()
    raw[flag] = value
    assert fresh(kind).choose(raw) is None


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
def test_legal_actions_night_filters_and_zero_seat_check(kind):
    wolf = state("wolf", sub="night_wolf")
    wolf["legal_actions"]["actions"] = [{"action_id": a} for a in ("7", "0", "1", "5")]
    assert fresh(kind).choose(wolf) == "5"
    seer = state("seer", sub="night_seer", game=game_fixture(checks={s: "villager" for s in (1, 3, 4, 5, 6)}))
    assert fresh(kind).choose(seer) == "0"
    raw = state()
    for target in ("0", "1", "2", "4", "5", "6", "7"):
        raw["legal_actions"]["actions"] = [{"action_id": target}]
        assert fresh(kind).choose(raw) == target
    raw["legal_actions"] = None
    assert fresh(kind).choose(raw) is None


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
def test_all_role_deals_keep_night_targets_legal(kind):
    for wolves in itertools.combinations(range(7), 2):
        for seer in set(range(7)) - set(wolves):
            game = game_fixture()
            game.roles = ["wolf" if s in wolves else "seer" if s == seer else "villager" for s in range(7)]
            for me in (*wolves, seer):
                sub = "night_seer" if me == seer else "night_wolf"
                raw = state(game=game, me=me, sub=sub)
                target = fresh(kind).choose(raw)
                assert target in {a["action_id"] for a in raw["legal_actions"]["actions"]}
                assert int(target) != me
                if sub == "night_wolf":
                    assert int(target) not in wolves


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
def test_own_checked_wolf_beats_five_voices_and_clears_are_protected(kind):
    raw = state("seer", game=game_fixture(checks={0: "wolf", 6: "villager"}))
    raw["new_messages"] = [message(s, "I will vote Player6.", s) for s in (0, 1, 3, 4, 5)]
    assert fresh(kind).choose(raw) == "0"
    raw = state("seer")
    raw["new_messages"] = [message(s, "I will vote Player6.", s) for s in (0, 1, 3, 4, 5)]
    assert fresh(kind).choose(raw) != "6"


def test_prompt_seer_tells_only_own_actual_checks_immediately():
    raw = state("seer", phase="messaging")
    expected = {"content": "I am the Seer. I checked Player6: villager.", "recipients": []}
    assert fresh(panel.PromptSeerSeat).message(raw) == expected
    for role in ("wolf", "villager"):
        raw = state(role, phase="messaging")
        raw["observation"] += "\nYour investigations: Player6 is wolf"
        assert "I checked" not in fresh(panel.PromptSeerSeat).message(raw)["content"]


def test_wolf_coordination_uses_only_actual_ally_dm_not_identity_or_public_orders():
    game = game_fixture()
    leader, follower = fresh(panel.CoordinatedSeat), fresh(panel.CoordinatedSeat)
    raw = state("wolf", phase="messaging", game=game)
    raw["new_messages"] = [message(6, "I am the Seer. I checked Player4: villager.")]
    plan = leader.message(raw)
    assert plan == {"content": "Tonight kill Player6.", "recipients": [1]}
    inbox = state(game=game, me=1, phase="messaging")
    inbox["new_messages"] = [message(0, plan["content"], 1, recipients=[1])]
    follower.message(inbox)
    game.day = 2
    assert leader.choose(state(game=game, me=0, sub="night_wolf")) == "6"
    assert follower.choose(state(game=game, me=1, sub="night_wolf")) == "6"
    night = state(game=game, me=1, sub="night_wolf")
    for fake in (message(3, "Tonight kill Player6.", day=2, recipients=[1]),
                 message(0, "Tonight kill Player6.", day=2),
                 message(0, "Tonight kill Player6.", day=2, recipients=[4]),
                 message(0, "Tonight kill Player0.", day=2, recipients=[1])):
        night["new_messages"] = [fake]
        assert fresh(panel.CoordinatedSeat).choose(night) == "2"
    game.day = 3
    assert follower.choose(state(game=game, me=1, sub="night_wolf")) == "2"


def test_coordinated_plan_is_recomputed_if_victim_died_before_night():
    bot = fresh(panel.CoordinatedSeat)
    raw = state("wolf", phase="messaging")
    assert bot.message(raw)["content"] == "Tonight kill Player2."
    game = game_fixture(day=2, dead=[(2, "seer", "vote")])
    assert bot.choose(state("wolf", game=game, sub="night_wolf")) == "3"


def test_late_claim_requires_observed_speakers_or_late_clock_and_only_wolves_lie():
    raw = state("wolf", phase="messaging")
    bot = fresh(panel.LateClaimSeat)
    assert "I am the Seer" not in bot.message(raw)["content"]
    assert bot.message(raw) is SIM_HOLD
    raw["new_messages"] = [message(2, "I am the Seer. I checked Player6: villager.", n) for n in range(5)]
    assert bot.message(raw) is SIM_HOLD
    raw["new_messages"].append(message(4, "I will vote Player5.", 5))
    assert bot.message(raw)["content"] == "I am the Seer. I checked Player2: wolf."
    raw["phase"] = "moving"
    assert bot.choose(raw) == "2"
    late = state("wolf", phase="messaging")
    bot = fresh(panel.LateClaimSeat)
    bot.message(late)
    late["messaging_seconds_left"] = 35
    assert "I am the Seer" in bot.message(late)["content"]
    for role in ("seer", "villager"):
        raw = state(role, phase="messaging")
        bot = fresh(panel.LateClaimSeat)
        chats = [bot.message(raw) for _ in range(8)]
        assert not any(isinstance(m, dict) and ": wolf" in m["content"] for m in chats)


def test_bus_requires_distinct_nonwolf_pressure_not_one_repeated_accuser():
    raw = state("wolf")
    raw["new_messages"] = [message(2, "I will vote Player1.", n) for n in range(5)]
    assert fresh(panel.PressureBusSeat).choose(raw) != "1"
    raw["new_messages"] = [message(s, "I will vote Player1.", s) for s in (2, 3, 4)]
    assert fresh(panel.PressureBusSeat).choose(raw) == "1"
    raw["game_state"]["phase"] = "night_wolf"
    raw["legal_actions"]["actions"] = [{"action_id": str(s)} for s in (2, 3, 4, 5, 6)]
    assert fresh(panel.PressureBusSeat).choose(raw) in {"2", "3", "4", "5", "6"}


def test_distance_is_conditional_and_never_a_blind_vote_for_partner():
    raw = state("wolf")
    raw["new_messages"] = [message(1, "I will vote Player5.")]
    assert fresh(panel.PromptSeerSeat).choose(raw) == "5"
    assert fresh(panel.DistanceSeat).choose(raw) not in ("1", "5")
    raw["new_messages"] += [message(s, "I will vote Player5.", s) for s in (2, 3)]
    assert fresh(panel.DistanceSeat).choose(raw) == "5"


def test_observed_switch_uses_revealed_bad_bandwagon_not_calendar_or_claim():
    bot = fresh(panel.ObservedSwitchSeat)
    raw = state(day=2)
    raw["new_messages"] = [message(4, "I will vote Player6.", day=2)]
    assert bot.choose(raw) == "6"
    assert not bot._adapted
    game = game_fixture(day=2, dead=[(5, "villager", "vote")],
                        votes=[ballot({0: 5, 1: 5, 2: 5, 3: 4, 4: 6, 5: 4, 6: 0}, 5)])
    updated = state(game=game)
    updated["new_messages"] = raw["new_messages"]
    assert bot.choose(updated) == "0"
    assert bot._adapted
    assert fresh(panel.PromptSeerSeat).choose(updated) == "0"
    updated["new_messages"] += [message(1, "I will vote Player6.", 1, day=2), message(2, "I will vote Player6.", 2, day=2)]
    assert bot.choose(updated) == "0"
    assert fresh(panel.PromptSeerSeat).choose(updated) == "6"
    for dead_role, count in (("wolf", 3), ("villager", 2)):
        other = game_fixture(day=2, dead=[(5, dead_role, "vote")],
                             votes=[ballot({s: 5 for s in range(count)}, 5)])
        probe = fresh(panel.ObservedSwitchSeat)
        probe.choose(state(game=other))
        assert not probe._adapted
    bot.reset()
    assert not bot._adapted


def test_survivor_night_kill_targets_publicly_dangerous_survivor_not_hidden_seer():
    game = game_fixture(day=2, dead=[(1, "wolf", "vote")],
                        votes=[ballot({0: 6, 1: 6, 2: 1, 3: 2, 4: 1, 5: 0, 6: 1}, 1)])
    raw = state("wolf", game=game, sub="night_wolf")
    assert fresh(panel.SurvivorKillSeat).choose(raw) == "6"
    game.roles[2], game.roles[5] = game.roles[5], game.roles[2]
    assert fresh(panel.SurvivorKillSeat).choose(state("wolf", game=game, sub="night_wolf")) == "6"
    raw["new_messages"] = [message(4, "I am the Seer. I checked Player5: villager.", day=2)]
    assert fresh(panel.SurvivorKillSeat).choose(raw) == "4"


def test_anti_majority_scrutinizes_advocate_but_hard_seer_evidence_wins():
    raw = state()
    raw["new_messages"] = [message(s, "I will vote Player6.", s) for s in (0, 1, 4)]
    assert fresh(panel.PromptSeerSeat).choose(raw) == "6"
    assert fresh(panel.AntiMajoritySeat).choose(raw) == "0"
    game = game_fixture(checks={6: "wolf"})
    game.roles[0], game.roles[6] = game.roles[6], game.roles[0]
    seer = state("seer", game=game)
    seer["new_messages"] = raw["new_messages"]
    assert fresh(panel.AntiMajoritySeat).choose(seer) == "6"


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
def test_revealed_seer_record_overrides_live_liar_after_empty_phase_transcript(kind):
    bot = fresh(kind)
    raw = state(phase="messaging")
    raw["new_messages"] = [message(2, "I am the Seer. I checked Player1: wolf.")]
    bot.message(raw)
    game = game_fixture(day=2, dead=[(2, "seer", "night_kill")])
    updated = state(game=game)
    updated["new_messages"] = [message(0, "I am the Seer. I checked Player6: wolf.", 1, day=2)]
    assert bot.choose(updated) == "1"
    updated["new_messages"] = []
    assert bot.choose(updated) == "1"


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
def test_hypothetical_quotes_dm_and_revealed_wolf_are_not_hard_facts(kind):
    raw = state()
    raw["new_messages"] = [
        message(1, 'Example: "I am the Seer. I checked Player6: wolf."', 0),
        message(2, "If I were the Seer, I would check Player6.", 1),
        message(4, "I am the Seer. I checked Player6: wolf.", 2, recipients=[3]),
    ]
    bot = fresh(kind)
    bot.choose(raw)
    assert bot._known(raw) == {}
    assert bot._claims(raw)[0] == {}
    raw["new_messages"] = [message(1, "I am the Seer. I checked Player6: wolf.", 3)]
    bot.choose(raw)
    assert bot._known(raw) == {}
    game = game_fixture(day=2, dead=[(1, "wolf", "vote")])
    later = state(game=game)
    bot.choose(later)
    assert bot._claims(later)[0] == {}


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
def test_public_text_and_structured_records_agree_and_input_is_not_mutated(kind):
    game = game_fixture(day=2, dead=[(5, "villager", "vote")],
                        votes=[ballot({0: 5, 1: 5, 2: 5, 3: 4, 4: 6, 5: 4, 6: 0}, 5)])
    raw = state(game=game)
    raw["new_messages"] = [message(2, "I am the Seer. I checked Player1: wolf.", day=2)]
    saved = copy.deepcopy(raw)
    expected = observe_and_act(fresh(kind), raw)
    structured = copy.deepcopy(raw)
    structured["observation"] = "Your role: VILLAGER"
    assert observe_and_act(fresh(kind), structured) == expected
    assert observe_and_act(fresh(kind), same_names(raw)) == expected
    assert raw == saved


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
def test_known_wolf_in_three_player_endgame_cannot_be_saved_by_a_fake_claim(kind):
    game = game_fixture(day=3, dead=[(1, "wolf", "vote"), (3, "villager", "night_kill"),
                                    (5, "villager", "vote"), (6, "villager", "night_kill")],
                        checks={0: "wolf", 4: "villager"})
    raw = state("seer", game=game)
    raw["new_messages"] = [message(0, "I'm the Seer. I checked Player4: wolf.", day=3)]
    bot = fresh(kind)
    assert bot.choose(raw) == "0"
    raw["phase"] = "messaging"
    assert bot.message(raw)["content"] == "I am the Seer. I checked Player0: wolf."


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
def test_bussing_is_not_a_private_clear_and_later_real_check_overrides_reputation(kind):
    game = game_fixture(day=2, dead=[(1, "wolf", "vote")], checks={0: "wolf"},
                        votes=[ballot({0: 1, 1: 6, 2: 1, 3: 4, 4: 6, 5: 1, 6: 4}, 1)])
    raw = state("seer", game=game)
    raw["new_messages"] = [message(0, "I voted for Player1. I will vote Player4.", day=2)]
    assert fresh(kind).choose(raw) == "0"
    villager = state(game=game)
    bot = fresh(kind)
    bot.choose(villager)
    assert 0 not in bot._known(villager)


@pytest.mark.parametrize("kind", KINDS, ids=lambda cls: cls.name)
def test_contradiction_and_new_day_pressure_have_no_shared_meaning_or_memory_leak(kind):
    raw = state()
    raw["new_messages"] = [message(1, "I'm the Seer. I checked Player6: wolf.")]
    first = fresh(kind)
    first.choose(raw)
    assert first._claims(raw)[0] == {1: {6: "wolf"}}
    raw["new_messages"] = [message(1, "I checked Player6: villager.", 1)]
    first.choose(raw)
    assert first._claims(raw) == ({}, {1})
    independent = fresh(kind)
    independent.choose(raw)
    assert independent._claims(raw) == ({}, set())
    first.reset()
    raw["new_messages"] = [message(1, "I will vote Player5.")]
    assert first.choose(raw) == "5"
    later = state(day=2)
    first.choose(later)
    assert first._intents(later) == {}
    assert first._known(later) == {}
