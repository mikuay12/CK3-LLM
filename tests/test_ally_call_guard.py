import unittest

from ck3_llm.gameplay.ally_call_guard import AllyCallGuard, MarriageProposalGuard, PeaceArmyGuard


class World:
    def __init__(self, items):
        self.items = items
        self.characters = []

    def query(self, request):
        self.characters.append(request["character"])
        return {"data": {"items": self.items, "next_offset": None}}


def pending(identity, actor=32396, interaction="call_ally_interaction", role="recipient"):
    return {"id": identity, "actor": {"id": actor}, "interaction": {"key": interaction}, "role": role}


class AllyCallGuardTests(unittest.TestCase):
    def test_accepts_only_configured_allies_war_calls(self):
        world = World([pending(10), pending(11, actor=99), pending(12, interaction="gift_interaction"),
                       pending(13, role="intermediary")])
        commands = AllyCallGuard([32396]).poll(world, 33336, 0)
        self.assertEqual([item["fields"] for item in commands],
                         [{"character_interaction": 10, "accept": "accept"}])

    def test_retry_is_bounded_and_stops_when_request_disappears(self):
        world = World([pending(10)])
        guard = AllyCallGuard([32396])
        self.assertEqual(len(guard.poll(world, 33336, 0)), 1)
        self.assertEqual(guard.poll(world, 33336, 1), [])
        self.assertEqual(len(guard.poll(world, 33336, 5)), 1)
        self.assertEqual(len(guard.poll(world, 33336, 10)), 1)
        self.assertEqual(guard.poll(world, 33336, 15), [])
        world.items = []
        self.assertEqual(guard.poll(world, 33336, 20), [])
        self.assertEqual(guard.attempts, {})

    def test_policy_is_off_by_default_and_follows_succession(self):
        world = World([pending(10)])
        self.assertEqual(AllyCallGuard().poll(world, 33336, 0), [])
        self.assertEqual(world.characters, [])
        guard = AllyCallGuard([32396])
        guard.poll(world, 33336, 0)
        commands = guard.poll(world, 38035, 1)
        self.assertEqual(len(commands), 1)
        self.assertIn("38035", commands[0]["request_id"])

    def test_player_identity_follows_the_humans_new_ruler(self):
        class PlayerWorld(World):
            ruler = 32396

            def query(self, request):
                if request["kind"] == "session":
                    return {"data": {"players": [{"machineid": 1, "character_id": self.ruler}]}}
                return super().query(request)

        world = PlayerWorld([pending(10)])
        guard = AllyCallGuard(machines=[1])
        self.assertEqual(len(guard.poll(world, 33336, 0)), 1)
        world.ruler = 38295
        world.items = [pending(11, actor=32396), pending(12, actor=38295)]
        commands = guard.poll(world, 33336, 1)
        self.assertEqual([c["fields"]["character_interaction"] for c in commands], [12])


class MarriageProposalGuardTests(unittest.TestCase):
    def test_disabled_by_default(self):
        world = World([pending(10, interaction="arrange_marriage_interaction")])
        self.assertEqual(MarriageProposalGuard().poll(world, 33336, 0), [])
        self.assertEqual(world.characters, [])

    def test_accepts_only_selected_players_marriages_and_follows_succession(self):
        class PlayerWorld(World):
            ruler = 38295

            def query(self, request):
                if request["kind"] == "session":
                    return {"data": {"players": [{"machineid": 1, "character_id": self.ruler},
                                                 {"machineid": 2, "character_id": 99}]}}
                return super().query(request)

        world = PlayerWorld([pending(10, actor=38295, interaction="arrange_marriage_interaction"),
                             pending(11, actor=99, interaction="arrange_marriage_interaction"),
                             pending(12, actor=38295),
                             pending(13, actor=38295, interaction="arrange_marriage_interaction", role="intermediary")])
        guard = MarriageProposalGuard(machines=[1])
        commands = guard.poll(world, 33336, 0)
        self.assertEqual([c["fields"] for c in commands],
                         [{"character_interaction": 10, "accept": "accept"}])
        self.assertTrue(commands[0]["request_id"].startswith("marriage-proposal-"))
        self.assertEqual(guard.poll(world, 33336, 1), [])
        world.ruler = 500
        world.items = [pending(14, actor=38295, interaction="arrange_marriage_interaction"),
                       pending(15, actor=500, interaction="arrange_marriage_interaction")]
        commands = guard.poll(world, 38035, 2)
        self.assertEqual([c["fields"]["character_interaction"] for c in commands], [15])


class PeaceArmyGuardTests(unittest.TestCase):
    def test_disbands_only_after_final_war_ends_and_uses_army_reference(self):
        class MilitaryWorld:
            wars = []
            units = [700]

            def query(self, request):
                if request["kind"] == "unit":
                    return {"data": {"army_id": 900}}
                return {"data": {"military": {"war_ids": self.wars, "unit_ids": self.units}}}

        world = MilitaryWorld()
        guard = PeaceArmyGuard()
        self.assertEqual(guard.poll(world, 33336, 0), [])
        world.wars = [1, 2]
        self.assertEqual(guard.poll(world, 33336, 1), [])
        world.wars = [2]
        self.assertEqual(guard.poll(world, 33336, 2), [])
        world.wars = []
        commands = guard.poll(world, 33336, 3)
        self.assertEqual(commands[0]["fields"], {"valid_for": "player", "army": 900})
        self.assertEqual(guard.poll(world, 33336, 4), [])
        world.units = []
        self.assertEqual(guard.poll(world, 33336, 5), [])
        self.assertFalse(guard.war_seen)


if __name__ == "__main__":
    unittest.main()
