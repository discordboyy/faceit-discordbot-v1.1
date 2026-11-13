import os
import types
import unittest

import scrim


def fake_interaction(user_id, manage_guild=False, manage_messages=False):
    perms = types.SimpleNamespace(manage_guild=manage_guild, manage_messages=manage_messages)
    return types.SimpleNamespace(user=types.SimpleNamespace(id=user_id, guild_permissions=perms))


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.get("BOT_OWNER_IDS")
        os.environ["BOT_OWNER_IDS"] = "677545362558550167"
        self.item = {"organizer_id": 1}

    def tearDown(self):
        if self.old is None:
            os.environ.pop("BOT_OWNER_IDS", None)
        else:
            os.environ["BOT_OWNER_IDS"] = self.old

    def test_owner_is_staff_without_any_permissions(self):
        self.assertTrue(scrim._is_staff(fake_interaction(677545362558550167), self.item))

    def test_regular_user_is_not_staff(self):
        self.assertFalse(scrim._is_staff(fake_interaction(42), self.item))

    def test_existing_rules_still_work(self):
        self.assertTrue(scrim._is_staff(fake_interaction(1), self.item))  # организатор
        self.assertTrue(scrim._is_staff(fake_interaction(42, manage_messages=True), self.item))

    def test_no_env_means_no_owner(self):
        os.environ.pop("BOT_OWNER_IDS")
        self.assertFalse(scrim._is_owner(fake_interaction(677545362558550167)))

    def test_several_ids_and_garbage(self):
        os.environ["BOT_OWNER_IDS"] = " 5, abc ,677545362558550167,"
        self.assertEqual(scrim._owner_ids(), {5, 677545362558550167})
        self.assertTrue(scrim._is_owner(fake_interaction(5)))


if __name__ == "__main__":
    unittest.main()