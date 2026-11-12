import os
import random
import tempfile
import unittest

import db
import tournament as t


def make_players(n, mode_vals=True):
    return [{"id": i + 1, "nickname": f"nick{i + 1}", "name": f"n{i + 1}",
             "elo": 800 + 90 * i if mode_vals else None,
             "level": 1 + (i * 10) // max(n, 1) if mode_vals else None,
             "kd": 0.7 + 0.02 * i if mode_vals else None} for i in range(n)]


def make_item(teams, mode="elo", status="open"):
    players = make_players(teams * 5)
    item = {"id": "t1", "guild_id": 1, "channel_id": 2, "organizer_id": 3, "status": status,
            "mode": mode, "team_count": teams, "name": "Cup", "players": players,
            "teams": [], "bracket": None, "message_id": None}
    t._auto_teams(item)
    return item


class BalanceTests(unittest.TestCase):
    def test_every_size_gives_full_teams_with_all_players(self):
        for teams in range(t.MIN_TEAMS, t.MAX_TEAMS + 1):
            for mode in ("random", "elo", "level", "kd"):
                item = make_item(teams, mode)
                self.assertEqual([len(x) for x in item["teams"]], [5] * teams)
                self.assertEqual({p["id"] for x in item["teams"] for p in x},
                                 {p["id"] for p in item["players"]})

    def test_elo_teams_are_close(self):
        item = make_item(4, "elo")
        sums = [sum(p["elo"] for p in x) for x in item["teams"]]
        self.assertLessEqual(max(sums) - min(sums), 90)

    def test_missing_data_is_supported(self):
        players = make_players(20, mode_vals=False)
        teams = t._balanced_teams(players, "elo", 4)
        self.assertEqual([len(x) for x in teams], [5] * 4)

    def test_no_teams_until_full(self):
        item = make_item(4)
        item["players"] = item["players"][:19]
        t._auto_teams(item)
        self.assertEqual(item["teams"], [])

    def test_seeds_sorted_by_strength(self):
        item = make_item(6, "elo")
        s = [t._strength(x, "elo") for x in item["teams"]]
        self.assertEqual(s, sorted(s, reverse=True))


class BracketTests(unittest.TestCase):
    def play_all(self, n, favourite_wins=True):
        b = t._build_bracket(n)
        b["names"] = [f"T{i}" for i in range(n)]
        for _ in range(100):
            ready = t._ready(b)
            if not ready:
                break
            key, _, m = ready[0]
            t._set_winner(b, key, min(m["a"], m["b"]) if favourite_wins else random.choice([m["a"], m["b"]]))
        return b

    def test_four_teams_semis_final_third(self):
        b = t._build_bracket(4)
        labels = [l for _, l, _ in t._match_list(b)]
        self.assertEqual(labels, ["Полуфинал · Лобби 1", "Полуфинал · Лобби 2", "Финал"])
        self.assertEqual([(m["a"], m["b"]) for m in b["rounds"][0]], [(0, 3), (1, 2)])
        for key, _, m in t._ready(b):
            t._set_winner(b, key, m["a"])
        self.assertEqual(sorted(l for _, l, _ in t._ready(b)), ["Матч за 3 место", "Финал"])
        self.assertEqual({(m["a"], m["b"]) for _, _, m in t._ready(b)}, {(0, 1), (3, 2)})

    def test_all_sizes_finish_with_podium(self):
        for n in range(2, 9):
            for fav in (True, False):
                for _ in range(20):
                    b = self.play_all(n, fav)
                    self.assertTrue(t._finished(b), n)
                    gold, silver, bronze = t._podium(b)
                    self.assertNotEqual(gold, silver)
                    if n >= 4:
                        self.assertIsNotNone(bronze)
                        self.assertEqual(len({gold, silver, bronze}), 3)
                    if n == 2:
                        self.assertIsNone(bronze)

    def test_three_teams_top_seed_gets_bye(self):
        b = t._build_bracket(3)
        self.assertTrue(b["rounds"][0][0].get("bye"))
        self.assertEqual(b["rounds"][0][0]["winner"], 0)
        self.assertEqual(len(t._ready(b)), 1)

    def test_byes_for_odd_sizes_never_stall(self):
        for n in (3, 5, 6, 7):
            b = t._build_bracket(n)
            self.assertTrue(t._ready(b), n)


class UndoTests(unittest.TestCase):
    def semis_done(self):
        b = t._build_bracket(4)
        b["names"] = ["A", "B", "C", "D"]
        for key, _, m in t._ready(b):
            t._set_winner(b, key, m["a"])
        return b

    def test_undo_semifinal_removes_winner_and_third_place(self):
        b = self.semis_done()
        self.assertIsNotNone(b["third"])
        self.assertTrue(t._undo(b, "0:0"))
        self.assertIsNone(b["third"])
        self.assertIsNone(b["rounds"][1][0]["a"])
        self.assertEqual([k for k, _, _ in t._ready(b)], ["0:0"])
        t._set_winner(b, "0:0", b["rounds"][0][0]["b"])  # исправленный результат
        self.assertEqual(b["rounds"][1][0]["a"], 3)
        self.assertEqual(b["third"]["a"], 0)

    def test_cannot_undo_after_next_match_played(self):
        b = self.semis_done()
        t._set_winner(b, "1:0", b["rounds"][1][0]["a"])
        self.assertFalse(t._undo(b, "0:0"))
        self.assertEqual(b["rounds"][0][0]["winner"], 0)
        self.assertFalse(t._can_undo(b, "0:1"))

    def test_undo_final_after_finish_then_replay(self):
        b = self.semis_done()
        t._set_winner(b, "1:0", 0)
        t._set_winner(b, "third", 3)
        self.assertTrue(t._finished(b))
        self.assertTrue(t._undo(b, "1:0"))
        self.assertFalse(t._finished(b))
        t._set_winner(b, "1:0", 1)
        self.assertEqual(t._podium(b)[0], 1)

    def test_bye_matches_are_never_undoable(self):
        b = t._build_bracket(3)
        self.assertFalse(t._can_undo(b, "0:0"))
        self.assertEqual(t._undoable(b), [])


class PersistenceTests(unittest.TestCase):
    def test_roundtrip_and_embed_size(self):
        old = db.DB_PATH
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db.DB_PATH = path
            t.init_db()
            for teams in (2, 4, 8):
                item = make_item(teams, "elo")
                item["id"] = f"t{teams}"
                t._insert(item)
                t._start(item)
                t._save(item)
                loaded = t._load(item["id"])
                self.assertEqual(loaded["status"], "locked")
                self.assertEqual(loaded["bracket"]["names"], item["bracket"]["names"])
                emb = t._embed(loaded)
                total = len(emb.title) + len(emb.description) + sum(
                    len(f.name) + len(f.value) for f in emb.fields) + len(emb.footer.text)
                self.assertLess(total, 6000)
                while t._ready(loaded["bracket"]):
                    key, _, m = t._ready(loaded["bracket"])[0]
                    t._set_winner(loaded["bracket"], key, m["a"])
                self.assertTrue(t._finished(loaded["bracket"]))
                loaded["status"] = "finished"
                emb = t._embed(loaded)
                self.assertIn("🥇", emb.description)
                self.assertIn("Чемпион", t._finish_text(loaded).replace("Поздравляем чемпионов", "Чемпион"))
        finally:
            db.DB_PATH = old
            os.unlink(path)

    def test_open_embed_with_players(self):
        item = make_item(8, "elo")
        item["players"] = item["players"][:17]
        t._auto_teams(item)
        emb = t._embed(item)
        self.assertIn("17/40", emb.description)


if __name__ == "__main__":
    unittest.main()