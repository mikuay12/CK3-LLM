import tempfile
from pathlib import Path
import unittest

from repair_succession_save import repair_bytes, repair_file


SAVE = b'''SAV010000\nmeta_data={ version="1.20.1.2" }\nplayed_character={
 name="Antigravity"
 character=38035
 player=-1
 gold=140
 legacy={ { character=33336 note="braces { } and # quoted text" } }
 has_open_succession=yes
 succession_data={ previous_played=33336 player_heir=38035 titles={ { heir=38035 titles={ 555 612 } } } }
}
played_character={ name="Host" character=33385 player=1 has_open_succession=yes succession_data={ player_heir=33385 } }
other_data={ gold=999 wars={ 12 34 } }
'''


class RepairSuccessionSaveTests(unittest.TestCase):
    def test_removes_only_the_offline_players_two_properties(self):
        repaired, report = repair_bytes(SAVE, player_name="Antigravity", character=38035)
        self.assertEqual(report["previous_character"], 33336)
        self.assertEqual(report["removed_fields"], ["has_open_succession", "succession_data"])
        expected = SAVE.replace(b"has_open_succession=yes", b"", 1).replace(
            b"succession_data={ previous_played=33336 player_heir=38035 titles={ { heir=38035 titles={ 555 612 } } } }", b"", 1)
        self.assertEqual(repaired, expected)
        self.assertIn(b'player=1 has_open_succession=yes', repaired)
        self.assertIn(b'gold=140', repaired)

    def test_refuses_active_missing_mismatched_or_ambiguous_targets(self):
        cases = [b"PK\x03\x04", SAVE.replace(b'player=-1', b'player=2', 1),
                 SAVE.replace(b'player_heir=38035', b'player_heir=999', 1),
                 SAVE.replace(b'has_open_succession=yes', b'has_open_succession=no', 1),
                 SAVE + SAVE[SAVE.index(b'played_character='):]]
        for data in cases:
            with self.subTest(data=data[:40]), self.assertRaises(ValueError):
                repair_bytes(data, player_name="Antigravity", character=38035)
        with self.assertRaises(ValueError):
            repair_bytes(SAVE, player_name="Unknown", character=38035)

    def test_keeps_source_identical_and_never_overwrites_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.ck3", Path(directory) / "fixed.ck3"
            source.write_bytes(SAVE)
            report = repair_file(source, output, player_name="Antigravity", character=38035)
            self.assertEqual(source.read_bytes(), SAVE)
            self.assertNotEqual(report["source_sha256"], report["output_sha256"])
            self.assertTrue(output.with_suffix(".ck3.repair.json").exists())
            with self.assertRaises(ValueError):
                repair_file(source, output, player_name="Antigravity", character=38035)
            with self.assertRaises(ValueError):
                repair_file(source, source, player_name="Antigravity", character=38035)


if __name__ == "__main__":
    unittest.main()
