import unittest

import scrim


class TeamBalanceTests(unittest.TestCase):
    def setUp(self):
        self.players = [
            {"id": i, "elo": elo, "level": level}
            for i, (elo, level) in enumerate(
                [(2500, 10), (2200, 9), (2000, 8), (1800, 7), (1600, 6),
                 (1500, 6), (1400, 5), (1300, 4), (1200, 3), (1100, 2)],
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

    def test_random_balancing_assigns_all_players(self):
        teams = scrim._balanced(self.players, "random")
        self.assertEqual(sorted(map(len, teams)), [5, 5])

    def test_missing_faceit_data_is_supported(self):
        players = [{"id": 1, "elo": None, "level": None} for _ in range(10)]
        teams = scrim._balanced(players, "elo")
        self.assertEqual(sorted(map(len, teams)), [5, 5])


if __name__ == "__main__":
    unittest.main()
