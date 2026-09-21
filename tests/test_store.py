import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixtures import AKIA, AKIA_ALT, AKIA_NEW, ASIA, ASIA_NEW, ASIA_ROTATED, SECRET, SECRET2, TOKEN
from awsprofiles.parsers import parse
from awsprofiles.store import ProfileStore, StoreError


CREDENTIALS = f"""\
# personal aws credentials

[default-1]
aws_access_key_id = {AKIA}
aws_secret_access_key = {SECRET}

[ten-dev]
aws_access_key_id={ASIA}
aws_secret_access_key={SECRET}
aws_session_token={TOKEN}

[keep-me]
aws_access_key_id = {AKIA_ALT}
aws_secret_access_key = {SECRET}
"""

CONFIG = "[default]\nregion = eu-west-1\noutput = json\n"


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._write("credentials", CREDENTIALS)
        self._write("config", CONFIG)
        self.store = ProfileStore(aws_dir=self.tmp)

    def _write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, 0o600)

    def _read(self, name="credentials"):
        with open(os.path.join(self.tmp, name), encoding="utf-8") as fh:
            return fh.read()

    def _block(self, text, name):
        """The lines of one [section], for comparing a profile in isolation."""
        lines = text.split("\n")
        start = lines.index(f"[{name}]")
        rest = [i for i, l in enumerate(lines[start + 1:], start + 1) if l.startswith("[")]
        return "\n".join(lines[start:rest[0] if rest else len(lines)]).strip()


class UpsertTests(StoreTestCase):
    def test_updating_one_profile_leaves_the_others_byte_identical(self):
        before = self._read()
        creds = parse(f"aws_access_key_id={ASIA_NEW}\n"
                      f"aws_secret_access_key={SECRET2}\naws_session_token={TOKEN}")
        self.store.upsert("ten-dev", creds)
        after = self._read()
        for untouched in ("default-1", "keep-me"):
            self.assertEqual(self._block(before, untouched), self._block(after, untouched))
        self.assertIn("# personal aws credentials", after)
        self.assertIn(ASIA_NEW, after)
        self.assertNotIn(ASIA, after)

    def test_replacing_temporary_with_static_drops_the_stale_token(self):
        creds = parse(f"aws_access_key_id={AKIA_NEW}\naws_secret_access_key={SECRET2}")
        self.store.upsert("ten-dev", creds)
        block = self._block(self._read(), "ten-dev")
        self.assertNotIn("aws_session_token", block)
        self.assertIn(AKIA_NEW, block)

    def test_a_backup_is_written_before_every_change(self):
        creds = parse(f"aws_access_key_id={AKIA_NEW}\naws_secret_access_key={SECRET2}")
        result = self.store.upsert("ten-dev", creds)
        self.assertIsNotNone(result.backup_path)
        with open(result.backup_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), CREDENTIALS)

    def test_new_profile_is_appended(self):
        creds = parse(f"aws_access_key_id={AKIA_NEW}\naws_secret_access_key={SECRET2}")
        self.store.upsert("brand-new", creds)
        self.assertIn("[brand-new]", self._read())
        self.assertEqual(len(self.store.list_profiles()), 4)

    def test_file_stays_private_after_writing(self):
        creds = parse(f"aws_access_key_id={AKIA_NEW}\naws_secret_access_key={SECRET2}")
        self.store.upsert("ten-dev", creds)
        self.assertEqual(os.stat(os.path.join(self.tmp, "credentials")).st_mode & 0o777, 0o600)

    def test_invalid_profile_names_are_refused(self):
        creds = parse(f"aws_access_key_id={AKIA_NEW}\naws_secret_access_key={SECRET2}")
        for bad in ("has space", "", "   ", "bad[bracket]"):
            with self.assertRaises(StoreError):
                self.store.upsert(bad, creds)
        self.assertEqual(self._read(), CREDENTIALS)  # nothing written

    def test_region_from_the_paste_lands_in_config_with_the_profile_prefix(self):
        creds = parse(f"aws_access_key_id={AKIA_NEW}\n"
                      f"aws_secret_access_key={SECRET2}\nregion=ap-south-1")
        self.store.upsert("ten-dev", creds)
        self.assertIn("[profile ten-dev]", self._read("config"))
        self.assertEqual(self.store.region_for("ten-dev"), "ap-south-1")


class DefaultTests(StoreTestCase):
    def test_set_default_copies_the_credentials(self):
        self.store.set_default("ten-dev")
        block = self._block(self._read(), "default")
        self.assertIn(ASIA, block)
        self.assertIn("aws_session_token", block)
        self.assertEqual([p.mirrors for p in self.store.list_profiles() if p.name == "default"], ["ten-dev"])

    def test_default_follows_later_updates_to_the_profile_it_mirrors(self):
        self.store.set_default("ten-dev")
        creds = parse(f"aws_access_key_id={ASIA_ROTATED}\n"
                      f"aws_secret_access_key={SECRET2}\naws_session_token={TOKEN}")
        result = self.store.upsert("ten-dev", creds)
        self.assertIn(ASIA_ROTATED, self._block(self._read(), "default"))
        self.assertTrue(any("mirrors" in w for w in result.warnings))

    def test_switching_default_from_temporary_to_static_drops_the_token(self):
        self.store.set_default("ten-dev")
        self.store.set_default("default-1")
        block = self._block(self._read(), "default")
        self.assertNotIn("aws_session_token", block)
        self.assertIn(AKIA, block)

    def test_profile_without_credentials_cannot_become_default(self):
        with self.assertRaises(StoreError):
            self.store.set_default("nope")


class RenameDeleteTests(StoreTestCase):
    def test_rename_keeps_the_credentials(self):
        self.store.rename("ten-dev", "ten-development")
        text = self._read()
        self.assertIn("[ten-development]", text)
        self.assertNotIn("[ten-dev]", text)
        self.assertIn(TOKEN, text)

    def test_rename_onto_an_existing_name_is_refused(self):
        with self.assertRaises(StoreError):
            self.store.rename("ten-dev", "keep-me")
        self.assertEqual(self._read(), CREDENTIALS)

    def test_delete_removes_only_that_profile(self):
        self.store.delete("ten-dev")
        text = self._read()
        self.assertNotIn("[ten-dev]", text)
        self.assertNotIn(TOKEN, text)
        self.assertIn("# personal aws credentials", text)
        self.assertEqual(self._block(text, "keep-me"), self._block(CREDENTIALS, "keep-me"))

    def test_delete_of_a_missing_profile_is_refused(self):
        with self.assertRaises(StoreError):
            self.store.delete("not-here")


class RestoreTests(StoreTestCase):
    def test_a_deletion_can_be_undone_from_its_backup(self):
        result = self.store.delete("ten-dev")
        self.assertNotIn("[ten-dev]", self._read())
        self.store.restore(result.backup_path)
        self.assertEqual(self._read(), CREDENTIALS)

    def test_restoring_also_backs_up_the_current_file(self):
        first = self.store.delete("ten-dev")
        restored = self.store.restore(first.backup_path)
        self.assertIsNotNone(restored.backup_path)
        with open(restored.backup_path, encoding="utf-8") as fh:
            self.assertNotIn("[ten-dev]", fh.read())


class DiagnosticTests(StoreTestCase):
    def test_world_readable_file_is_reported_and_fixed(self):
        os.chmod(os.path.join(self.tmp, "credentials"), 0o644)
        self.assertTrue(self.store.check_permissions())
        self.assertEqual(self.store.fix_permissions(), ["credentials"])
        self.assertEqual(self.store.check_permissions(), [])

    def test_config_section_missing_the_profile_prefix_is_reported(self):
        self._write("config", CONFIG + "\n[ten-dev]\nregion = eu-west-1\n")
        warnings = self.store.config_warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn("[profile ten-dev]", warnings[0])

    def test_a_correct_config_produces_no_warnings(self):
        self._write("config", CONFIG + "\n[profile ten-dev]\nregion = eu-west-1\n")
        self.assertEqual(self.store.config_warnings(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class RestoreSafetyTests(StoreTestCase):
    def test_restoring_from_outside_the_backup_directory_is_refused(self):
        outside = os.path.join(self.tmp, "not-a-backup")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("[evil]\n")
        with self.assertRaises(StoreError):
            self.store.restore(outside)
        self.assertEqual(self._read(), CREDENTIALS)

    def test_traversal_out_of_the_backup_directory_is_refused(self):
        self.store.delete("ten-dev")  # creates the backup directory
        sneaky = os.path.join(self.store.backup_dir, "..", "config")
        with self.assertRaises(StoreError):
            self.store.restore(sneaky)


class ClientTests(StoreTestCase):
    def test_save_client_normalises_a_bare_host_to_https(self):
        client = self.store.save_client("acme", "d-123.awsapps.com/start", "me@acme.com")
        self.assertEqual(client.login_url, "https://d-123.awsapps.com/start")
        self.assertEqual([c.name for c in self.store.list_clients()], ["acme"])

    def test_a_plain_username_is_accepted_as_the_sign_in_name(self):
        client = self.store.save_client("corp", "https://corp.awsapps.com/start", "jdoe")
        self.assertEqual(client.email, "jdoe")

    def test_saving_again_updates_in_place(self):
        self.store.save_client("acme", "https://old.example.com", "old@acme.com")
        self.store.save_client("acme", "https://new.example.com")
        self.assertEqual(len(self.store.list_clients()), 1)
        self.assertEqual(self.store.client("acme").login_url, "https://new.example.com")
        self.assertEqual(self.store.client("acme").email, "")

    def test_bad_input_is_refused_before_anything_is_saved(self):
        for name, url, email in (
            ("", "https://a.example.com", ""),
            ("has space", "https://a.example.com", ""),
            ("acme", "", ""),
            ("acme", "ftp://a.example.com", ""),
            ("acme", "https://a.example.com", "has a space"),
        ):
            with self.assertRaises(StoreError):
                self.store.save_client(name, url, email)
        self.assertEqual(self.store.list_clients(), [])

    def test_profiles_group_under_their_client(self):
        self.store.save_client("acme", "https://a.example.com", "me@acme.com")
        self.store.assign_client("ten-dev", "acme")
        by_name = {p.name: p.client for p in self.store.list_profiles()}
        self.assertEqual(by_name["ten-dev"], "acme")
        self.assertIsNone(by_name["keep-me"])
        self.assertEqual(self.store.client_for("ten-dev").email, "me@acme.com")
        self.assertEqual(self.store.last_client().name, "acme")

    def test_assigning_to_an_unknown_client_is_refused(self):
        with self.assertRaises(StoreError):
            self.store.assign_client("ten-dev", "nope")

    def test_unassigning_clears_the_group(self):
        self.store.save_client("acme", "https://a.example.com")
        self.store.assign_client("ten-dev", "acme")
        self.store.assign_client("ten-dev", None)
        self.assertIsNone(self.store.client_for("ten-dev"))

    def test_deleting_a_client_frees_its_profiles_and_keeps_credentials(self):
        self.store.save_client("acme", "https://a.example.com")
        self.store.assign_client("ten-dev", "acme")
        result = self.store.delete_client("acme")
        self.assertIn("1 profile", result.message)
        self.assertEqual(self.store.list_clients(), [])
        self.assertIsNone(self.store.client_for("ten-dev"))
        self.assertEqual(self._read(), CREDENTIALS)

    def test_rename_and_delete_of_a_profile_carry_the_assignment(self):
        self.store.save_client("acme", "https://a.example.com")
        self.store.assign_client("ten-dev", "acme")
        self.store.rename("ten-dev", "ten-development")
        self.assertEqual(self.store.client_for("ten-development").name, "acme")
        self.assertIsNone(self.store.client_for("ten-dev"))
        self.store.delete("ten-development")
        self.assertIsNone(self.store.client_for("ten-development"))

    def test_a_dangling_assignment_is_ignored_when_listing(self):
        self.store._record("ten-dev", client="ghost")
        self.assertIsNone(next(p for p in self.store.list_profiles() if p.name == "ten-dev").client)

    def test_only_web_links_can_be_opened(self):
        with self.assertRaises(StoreError):
            self.store.open_url("file:///etc/passwd")


class PreferenceTests(StoreTestCase):
    def test_preferences_round_trip_with_a_default(self):
        self.assertIsNone(self.store.preference("login_hint_seen"))
        self.assertFalse(self.store.preference("login_hint_seen", False))
        self.store.set_preference("login_hint_seen", True)
        self.assertTrue(self.store.preference("login_hint_seen"))


class RenameClientTests(StoreTestCase):
    def test_rename_moves_the_profiles_with_it(self):
        self.store.save_client("acme", "https://a.example.com", "me@acme.com")
        self.store.assign_client("ten-dev", "acme")
        client = self.store.rename_client("acme", "acme-corp")
        self.assertEqual((client.name, client.email), ("acme-corp", "me@acme.com"))
        self.assertEqual([c.name for c in self.store.list_clients()], ["acme-corp"])
        self.assertEqual(self.store.client_for("ten-dev").name, "acme-corp")
        self.assertEqual(self.store.last_client().name, "acme-corp")

    def test_rename_onto_an_existing_client_is_refused(self):
        self.store.save_client("acme", "https://a.example.com")
        self.store.save_client("beta", "https://b.example.com")
        with self.assertRaises(StoreError):
            self.store.rename_client("acme", "beta")
        self.assertEqual(len(self.store.list_clients()), 2)

    def test_rename_to_the_same_name_is_a_no_op(self):
        self.store.save_client("acme", "https://a.example.com")
        self.assertEqual(self.store.rename_client("acme", "acme").login_url, "https://a.example.com")


class UrgencyTests(StoreTestCase):
    def test_static_keys_have_no_urgency(self):
        self.assertEqual(next(p for p in self.store.list_profiles() if p.name == "keep-me").urgency, "none")

    def test_urgency_follows_the_remaining_time(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        for delta, expected in ((timedelta(hours=5), "ok"), (timedelta(minutes=20), "soon"),
                                (timedelta(minutes=-1), "expired")):
            self.store._record("ten-dev", expires_at=(now + delta).isoformat())
            profile = next(p for p in self.store.list_profiles() if p.name == "ten-dev")
            self.assertEqual(profile.urgency, expected, delta)


class ConcurrencyTests(StoreTestCase):
    def test_worker_records_never_drop_a_main_thread_assignment(self):
        import threading
        self.store.save_client("acme", "https://a.example.com")
        names = ["ten-dev", "keep-me", "default-1"]
        stop = threading.Event()

        def hammer():
            i = 0
            while not stop.is_set():
                self.store._record(names[i % 3], account=str(i))
                i += 1

        workers = [threading.Thread(target=hammer) for _ in range(3)]
        for w in workers:
            w.start()
        try:
            for _ in range(50):
                self.store.assign_client("ten-dev", "acme")
                self.assertEqual(self.store.client_for("ten-dev").name, "acme")
        finally:
            stop.set()
            for w in workers:
                w.join()
        self.assertEqual(self.store.client_for("ten-dev").name, "acme")
        self.assertEqual(len(self.store.list_clients()), 1)

    def test_plain_http_login_urls_are_refused(self):
        with self.assertRaises(StoreError):
            self.store.save_client("acme", "http://a.example.com")
        with self.assertRaises(StoreError):
            self.store.open_url("http://a.example.com")
