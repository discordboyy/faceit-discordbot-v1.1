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

    def test_public_buttons_only_for_participants(self):
        base = {"id": "x", "mode": "level", "teams": [[], []]}
        few = dict(base, status="open", players=self.players[:3])
        self.assertEqual(scrim._public_buttons(few), {"join", "leave", "manage"})
        full = dict(base, status="open", players=list(self.players))
        self.assertEqual(scrim._public_buttons(full), {"manage"})
        locked = dict(base, status="locked", players=list(self.players))
        self.assertEqual(scrim._public_buttons(locked), {"manage"})
        for status in ("finished", "cancelled"):
            self.assertEqual(scrim._public_buttons(dict(base, status=status, players=[])), set())

    def test_staff_buttons_by_state(self):
        base = {"id": "x", "mode": "level", "teams": [[], []]}
        self.assertEqual(scrim._staff_buttons(dict(base, status="open", players=[])), {"cancel"})
        self.assertEqual(scrim._staff_buttons(dict(base, status="open", players=self.players[:3])),
                         {"kick", "cancel"})
        self.assertEqual(scrim._staff_buttons(dict(base, status="open", players=list(self.players))),
                         {"lock", "reshuffle", "swap", "kick", "ping", "cancel"})
        self.assertEqual(scrim._staff_buttons(dict(base, status="locked", players=list(self.players))),
                         {"finish", "swap", "kick", "ping", "cancel"})
        self.assertEqual(scrim._staff_buttons(dict(base, status="finished", players=[])), set())

    def test_swap_players(self):
        item = {"players": list(self.players), "mode": "level", "status": "locked",
                "teams": [list(self.players[:5]), list(self.players[5:])]}
        self.assertTrue(scrim._swap_players(item, 1, 6))
        self.assertEqual({p["id"] for p in item["teams"][0]}, {6, 2, 3, 4, 5})
        self.assertEqual({p["id"] for p in item["teams"][1]}, {1, 7, 8, 9, 10})
        self.assertFalse(scrim._swap_players(item, 1, 6))  # 1 уже не в команде A

    def test_remove_player_reopens_scrim(self):
        item = {"players": list(self.players), "mode": "level", "status": "locked", "teams": [[], []]}
        scrim._auto_teams(item)
        self.assertTrue(scrim._remove_player(item, 3))
        self.assertEqual(item["status"], "open")
        self.assertEqual(len(item["players"]), 9)
        self.assertEqual(item["teams"], [[], []])
        self.assertFalse(scrim._remove_player(item, 3))

    def test_finished_embed_shows_winner(self):
        item = {"id": "abc", "organizer_id": 1, "status": "finished", "mode": "level", "winner": 1,
                "players": list(self.players), "teams": scrim._balanced(self.players, "level")}
        self.assertIn("победила команда B", scrim._embed(item).description)

    def test_manual_level_is_marked(self):
        p = {"id": 1, "level": 7.0, "level_manual": True}
        self.assertEqual(scrim._stat_text(p, "level"), "Lv 7 ✎")
        item = {"id": "abc", "organizer_id": 1, "status": "open", "mode": "level",
                "players": [p], "teams": [[], []]}
        self.assertIn("выставлен администратором", scrim._embed(item).description)

    def test_level_override_is_saved_and_applied_to_active_scrim(self):
        import os, tempfile
        import db
        old_path = db.DB_PATH
        db.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
        try:
            scrim.init_db()
            self.assertIsNone(scrim._get_override(1, 42))
            scrim._set_override(1, 42, 6, set_by=1)
            scrim._set_override(1, 42, 8, set_by=1)  # повторная установка перезаписывает
            self.assertEqual(scrim._get_override(1, 42), 8)
            self.assertIsNone(scrim._get_override(2, 42))  # на другом сервере уровня нет
            self.assertEqual(scrim._list_overrides(1), [(42, 8)])

            players = [dict(p, id=40 + i) for i, p in enumerate(self.players)]
            item = {"id": "s1", "guild_id": 1, "channel_id": 5, "organizer_id": 1, "status": "open",
                    "mode": "level", "players": players, "teams": [[], []]}
            scrim._insert(item)
            changed = scrim._apply_level(1, 42, 9, True)
            self.assertEqual(len(changed), 1)
            saved = scrim._load("s1")
            self.assertTrue(any(p["id"] == 42 and p["level"] == 9 and p["level_manual"]
                                for p in saved["players"]))
            self.assertEqual([len(t) for t in saved["teams"]], [5, 5])
            self.assertEqual(scrim._apply_level(2, 42, 9, True), [])  # другой сервер не затронут

            self.assertTrue(scrim._delete_override(1, 42))
            self.assertFalse(scrim._delete_override(1, 42))
            self.assertIsNone(scrim._get_override(1, 42))
        finally:
            db.DB_PATH = old_path

    def test_random_balancing_assigns_all_players(self):
        teams = scrim._balanced(self.players, "random")
        self.assertEqual(sorted(map(len, teams)), [5, 5])

    def test_missing_faceit_data_is_supported(self):
        players = [{"id": 1, "elo": None, "level": None} for _ in range(10)]
        teams = scrim._balanced(players, "elo")
        self.assertEqual(sorted(map(len, teams)), [5, 5])


if __name__ == "__main__":
    unittest.main()
