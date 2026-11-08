import unittest

import scrim


class TeamBalanceTests(unittest.TestCase):
    def setUp(self):
        self.players = [
            {"id": i, "elo": elo, "level": level, "kd": kd}
            for i, (elo, level, kd) in enumerate(
                [(2500, 10, 1.45), (2200, 9, 1.30), (2000, 8, 1.20), (1800, 7, 1.10),
                 (1600, 6, 1.05), (1500, 6, 1.00), (1400, 5, 0.95), (1300, 4, 0.90),
                 (1200, 3, 0.85), (1100, 2, 0.70)],
                start=1,
            )
        ]

    def test_elo_balancing_assigns_all_players(self):
        teams = scrim._balanced(self.players, "elo")
        self.assertEqual([len(team) for team in teams], [5, 5])
        self.assertEqual({p["id"] for team in teams for p in team},
                         {p["id"] for p in self.players})

    def test_level_balancing_assigns_all_players(self):
        teams = scrim._balanced(self.players, "level")
        self.assertEqual(sum(map(len, teams)), 10)

    def test_kd_balancing_is_even_and_close(self):
        teams = scrim._balanced(self.players, "kd")
        self.assertEqual([len(team) for team in teams], [5, 5])
        sums = [sum(p["kd"] for p in team) for team in teams]
        self.assertLess(abs(sums[0] - sums[1]), 0.2)

    def test_stat_text_matches_mode(self):
        p = {"id": 1, "elo": 1800.0, "level": 7.0, "kd": 1.1234}
        self.assertEqual(scrim._stat_text(p, "level"), "Lv 7")
        self.assertEqual(scrim._stat_text(p, "elo"), "1800 ELO")
        self.assertEqual(scrim._stat_text(p, "kd"), "1.12 K/D")

    def test_open_scrim_embed_lists_players(self):
        item = {"id": "abc", "organizer_id": 1, "status": "open", "mode": "level",
                "players": self.players, "teams": [[], []]}
        desc = scrim._embed(item).description
        self.assertIn("Записались:** 10/10", desc)
        self.assertIn("<@1> · Lv 10", desc)

    def test_teams_appear_automatically_at_ten_players(self):
        item = {"id": "abc", "organizer_id": 1, "status": "open", "mode": "level",
                "players": list(self.players), "teams": [[], []]}
        scrim._auto_teams(item)
        self.assertEqual([len(t) for t in item["teams"]], [5, 5])
        desc = scrim._embed(item).description
        self.assertIn("Команда A", desc)
        self.assertIn("Команда B", desc)
        item["players"].pop()
        scrim._auto_teams(item)
        self.assertEqual(item["teams"], [[], []])
        self.assertIn("Готовы к игре", scrim._embed(item).description)

    def test_random_balancing_assigns_all_players(self):
        teams = scrim._balanced(self.players, "random")
        self.assertEqual(sorted(map(len, teams)), [5, 5])

    def test_missing_faceit_data_is_supported(self):
        players = [{"id": 1, "elo": None, "level": None} for _ in range(10)]
        teams = scrim._balanced(players, "elo")
        self.assertEqual(sorted(map(len, teams)), [5, 5])


if __name__ == "__main__":
    unittest.main()
