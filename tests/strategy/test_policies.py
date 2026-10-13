"""Offline tests built from the payload shapes in docs/platform/*.md."""
from altruagent import TERMINATE_MESSAGING, DecisionContext, GameState, SendMessage

from agent.agent import Contestant, doubles_ok
from agent.pokemon.policy import PokemonPolicy, card_score
from agent.reasoning import unwrap
from agent.werewolf.policy import WerewolfPolicy
from agent.werewolf.state import parse

PLAYERS = [{"position": i, "name": n, "agent_id": f"id{i}"}
           for i, n in enumerate(["alice", "bob", "carl", "dave", "erin", "fran", "gus"])]


def ww_state(role_block: str, sub_phase: str, actions: list[int], **gs) -> dict:
    game_state = {"day": 2, "phase": sub_phase, "alive": ["alice", "bob", "dave", "erin", "gus"],
                  "dead": [{"day": 1, "player": "carl", "role": "villager", "cause": "lynch"},
                           {"day": 2, "player": "fran", "role": "seer", "cause": "night_kill"}],
                  "vote_history": [{"day": 1, "votes": {"alice": "carl", "bob": "carl", "carl": "dave",
                                                        "dave": "carl", "erin": "abstain", "fran": "bob",
                                                        "gus": "bob"}, "eliminated": "carl"}],
                  "winner": None}
    game_state.update(gs)
    return {"observation": f"=== Werewolf ===\n{role_block}\nDay 2 | phase {sub_phase} | alive 5/7",
            "your_position": 3, "players": PLAYERS, "game_state": game_state, "phase": "moving",
            "state_version": 9,
            "legal_actions": {"actions": [{"action_id": str(a), "label": "", "input": {"action_id": str(a)}}
                                          for a in actions]}}


def test_parse_roles_and_record():
    v = parse(ww_state("Your role: SEER\nYour investigations: Player0 is wolf, Player4 is villager",
                       "day_vote", [0, 1, 4, 6, 7]))
    assert v.role == "seer" and v.investigations == {0: "wolf", 4: "villager"}
    assert v.alive == [0, 1, 3, 4, 6] and v.dead == {2: "villager", 5: "seer"}
    assert v.votes[0][1][4] is None and v.votes[0][2] == 2


def test_seer_votes_known_wolf():
    st = ww_state("Your role: SEER\nYour investigations: Player6 is wolf", "day_vote", [0, 1, 4, 6, 7])
    assert WerewolfPolicy().act(st)["action_id"] == "6"


def test_villager_never_abstains_and_follows_record():
    st = ww_state("Your role: VILLAGER\nYou do not know anyone else's role.", "day_vote", [0, 1, 4, 6, 7])
    # alice/bob voted out villager carl -> most suspicious; tie -> lower seat
    assert WerewolfPolicy().act(st)["action_id"] == "0"


def test_wolf_never_targets_ally_and_kills_claimed_seer():
    st = ww_state("Your role: WOLF\nYour fellow wolves: Player0", "night_wolf", [1, 4, 6])
    # A credible living seer claim requires that the public record has not already revealed a different seer.
    st["game_state"]["dead"][1]["role"] = "villager"
    msgs = [{"seq": 1, "sender": 4, "type": "chat", "visibility": "broadcast",
             "content": "I am the seer. Player3 is a wolf."}]
    st["new_messages"] = msgs
    assert WerewolfPolicy().act(st)["action_id"] == "4"


def test_wolf_does_not_prioritize_a_claim_disproven_by_the_revealed_seer():
    st = ww_state("Your role: WOLF\nYour fellow wolves: Player0", "night_wolf", [1, 4, 6])
    st["new_messages"] = [{"seq": 1, "sender": 4, "type": "chat", "visibility": "broadcast",
                           "content": "I am the seer. Player3 is a wolf."}]
    # The fixture publicly reveals Player5 as the dead seer. Player4's bait must not override the normal kill plan.
    assert WerewolfPolicy().act(st)["action_id"] == "6"


def test_seer_chat_claims_wolf():
    st = ww_state("Your role: SEER\nYour investigations: Player6 is wolf", "day_vote", [])
    out = WerewolfPolicy().chat(st)
    assert "Player6" in out[0]["content"] and len(out[0]["content"].split()) <= 50


# ---- Pokémon -------------------------------------------------------------

def test_draft_prefers_incineroar_over_filler():
    cards = [{"card_id": "a", "species": "Incineroar", "ability": "Intimidate",
              "moves": ["Fake Out", "Flare Blitz", "Knock Off", "Parting Shot"]},
             {"card_id": "b", "species": "Delibird", "ability": "Hustle", "moves": ["Present"]}]
    assert card_score(cards[0], []) > card_score(cards[1], [])
    st = {"phase": "draft", "current_actor": {"agent_id": "me"},
          "observation": {"phase": "draft", "current_seat": "me", "available_cards": cards, "rosters": {"me": []}},
          "legal_actions": {"actions": [{"action_id": "draft_pick:a", "input": {"action": {"card_id": "a"}}},
                                        {"action_id": "draft_pick:b", "input": {"action": {"card_id": "b"}}}]}}
    assert PokemonPolicy().act(st)["action"]["card_id"] == "a"


def doubles_state(turn=1):
    tmpl = {"type": "doubles_turn", "slots": [
        {"slot": 0, "species": "incineroar", "options": [
            {"type": "move", "move_id": "fakeout", "targets": [1, 2]},
            {"type": "move", "move_id": "flareblitz", "targets": [1, 2]},
            {"type": "switch", "species": "gholdengo"}]},
        {"slot": 1, "species": "rillaboom", "options": [
            {"type": "move", "move_id": "woodhammer", "targets": [1, 2]},
            {"type": "move", "move_id": "protect", "targets": [0]},
            {"type": "switch", "species": "gholdengo"}]}]}
    return {"phase": "moving", "state_version": 3,
            "observation": {"is_doubles": True, "turn": turn, "force_switch": [False, False],
                            "active_pokemon": [{"species": "Incineroar", "current_hp_fraction": 1.0},
                                               {"species": "Rillaboom", "current_hp_fraction": 1.0}],
                            "opponent_active_pokemon": [{"species": "Kyogre", "types": ["WATER"]},
                                                        {"species": "Amoonguss", "types": ["GRASS", "POISON"]}]},
            "legal_actions": {"actions": [{"action_id": "doubles_turn", "input": {"action": tmpl}}]}}


def test_doubles_fake_out_turn1_then_grass_into_water():
    pol = PokemonPolicy()
    a = pol.act(doubles_state(1))["action"]
    assert a["type"] == "doubles_turn"
    assert a["slot_0"]["move_id"] == "fakeout"
    assert a["slot_1"] == {"type": "move", "move_id": "woodhammer", "target": 1}   # Grass into Kyogre
    a2 = pol.act(doubles_state(2))["action"]
    assert a2["slot_0"]["move_id"] == "flareblitz" and a2["slot_0"]["target"] == 2  # Fire into Amoonguss


def test_doubles_fallback_never_double_switches_same_species():
    st = doubles_state()
    for s in st["legal_actions"]["actions"][0]["input"]["action"]["slots"]:
        s["options"] = [o for o in s["options"] if o["type"] == "switch"]
    a = PokemonPolicy().fallback(st)["action"]
    assert a["slot_0"] == {"type": "switch", "species": "gholdengo"} and a["slot_1"] == {"type": "pass"}


# ---- Adapter (agent/agent.py) through the official SDK types --------------

def ctx(game):
    return DecisionContext(session_id="sid", tournament_id=None, game_type=game, agent_id="me")


def test_adapter_doubles_returns_validated_dict():
    raw = doubles_state(1) | {"session_id": "sid", "game_type": "pokemon_vgc_doubles_draft",
                               "is_current_actor": True}
    out = unwrap(Contestant().choose_action(GameState.from_mcp_state(raw), ctx("pokemon_vgc_doubles_draft")))
    assert isinstance(out, dict) and out["type"] == "doubles_turn" and doubles_ok(raw, out)


def test_doubles_ok_rejects_illegal_target_and_double_switch():
    raw = doubles_state()
    assert not doubles_ok(raw, {"type": "doubles_turn", "slot_0": {"type": "move", "move_id": "protect", "target": 1},
                                "slot_1": {"type": "pass"}})
    assert not doubles_ok(raw, {"type": "doubles_turn", "slot_0": {"type": "switch", "species": "gholdengo"},
                                "slot_1": {"type": "switch", "species": "Gholdengo"}})


def test_adapter_survives_policy_crash():
    c = Contestant()
    c.ww.act = lambda st: 1 / 0
    raw = ww_state("Your role: VILLAGER", "day_vote", [0, 1, 7]) | {"game_type": "werewolf"}
    out = unwrap(c.choose_action(GameState.from_mcp_state(raw), ctx("werewolf")))
    assert out.action_id == "0"


def test_adapter_werewolf_chat_then_terminate_and_clipped():
    c = Contestant()
    c.ww.chat = lambda st: [{"content": "word " * 80}, {"content": "hi", "recipients": [3]}, {"content": "bye"}]
    raw = ww_state("Your role: VILLAGER", "day_vote", []) | {"game_type": "werewolf", "phase": "messaging"}
    gs = GameState.from_mcp_state(raw)
    m1 = c.choose_message(gs, ctx("werewolf"))
    assert isinstance(m1, SendMessage) and len(m1.content.split()) == 50
    m2 = c.choose_message(gs, ctx("werewolf"))
    # a PM to self is dropped, never broadcast (a private message must not go public by mistake)
    assert isinstance(m2, SendMessage) and m2.content == "bye" and not m2.recipients
    assert c.choose_message(gs, ctx("werewolf")) is TERMINATE_MESSAGING


def test_every_decision_is_recorded(tmp_path, monkeypatch):
    import json
    import agent.agent as A
    monkeypatch.setattr(A, "RECORD_DIR", str(tmp_path))
    monkeypatch.setenv("ARENA_RECORD", "1")          # local simulators switch recording off process-wide
    c = Contestant()
    raw = ww_state("Your role: VILLAGER", "day_vote", [0, 1, 7]) | {"game_type": "werewolf"}
    c.choose_action(GameState.from_mcp_state(raw), ctx("werewolf"))
    c.choose_message(GameState.from_mcp_state(raw | {"phase": "messaging"}), ctx("werewolf"))
    lines = [json.loads(l) for l in (tmp_path / "sid.jsonl").read_text().splitlines()]
    assert [l["kind"] for l in lines] == ["action", "message"]
    assert lines[0]["raw"]["your_position"] == 3 and lines[0]["out"]["action_id"] == "0"


def test_policy_remembers_full_opponent_sets_from_draft():
    pol = PokemonPolicy()
    inc = {"card_id": "vgc-incineroar", "species": "Incineroar", "item": "Safety Goggles", "ability": "Intimidate",
           "moves": ["Fake Out", "Knock Off", "Parting Shot", "Flare Blitz"]}
    rill = {"card_id": "vgc-rillaboom", "species": "Rillaboom", "item": "Assault Vest", "ability": "Grassy Surge",
            "moves": ["Fake Out", "Wood Hammer", "Grassy Glide", "U-turn"]}
    gar = {"card_id": "vgc-garchomp", "species": "Garchomp", "item": "Focus Sash", "ability": "Rough Skin",
           "moves": ["Earthquake", "Rock Slide", "Iron Head", "Protect"]}
    # Opponent already took Incineroar before our first draft state (only card_id + species visible).
    st = {"phase": "draft", "current_actor": {"agent_id": "me"},
          "observation": {"phase": "draft", "current_seat": "me", "available_cards": [rill, gar],
                          "rosters": {"me": [], "opp": [{"card_id": "vgc-incineroar", "species": "Incineroar"}]}},
          "legal_actions": {"actions": [{"action_id": "draft_pick:vgc-rillaboom", "input": {"action": {"card_id": "vgc-rillaboom"}}},
                                        {"action_id": "draft_pick:vgc-garchomp", "input": {"action": {"card_id": "vgc-garchomp"}}}]}}
    pol.seen.setdefault("vgc-incineroar", inc)          # from the real catalog in production
    pol.act(st)
    assert pol.opponent_set("Incineroar")["item"] == "Safety Goggles"
    # Garchomp was offered to us, the opponent took it after our last draft turn: recovered at team preview.
    prev = {"phase": "team_preview", "observation": {"your_roster": [{"species": "rillaboom"}] * 6,
                                                     "opponent_roster": [{"species": "incineroar"}, {"species": "garchomp"}]}}
    pol.act(prev)
    assert pol.opponent_set("garchomp")["moves"][1] == "Rock Slide"



def test_recording_can_be_switched_off(tmp_path, monkeypatch):
    import agent.agent as A
    monkeypatch.setattr(A, "RECORD_DIR", str(tmp_path))
    monkeypatch.setenv("ARENA_RECORD", "0")
    raw = ww_state("Your role: VILLAGER", "day_vote", [0, 1, 7]) | {"game_type": "werewolf"}
    Contestant().choose_action(GameState.from_mcp_state(raw), ctx("werewolf"))
    assert not list(tmp_path.iterdir())
