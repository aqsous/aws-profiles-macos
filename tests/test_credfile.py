import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixtures import AKIA, AKIA_ALT, AKIA_NEW2, AKIA_STATIC, ASIA, ASIA_EDITED, ASIA_NEW2, SECRET, SECRET2
from awsprofiles.credfile import IniFile, atomic_write, backup

SAMPLE = f"""\
# my aws credentials
# keep this file private

[default-1]
aws_access_key_id = {AKIA}
aws_secret_access_key = {SECRET}

[ten-dev]
aws_access_key_id={ASIA}
aws_secret_access_key={SECRET}
aws_session_token=IQoJb3JpZ2luX2VjEXAMPLETOKEN

; a trailing comment
[ten-staging]
aws_access_key_id = {AKIA_ALT}
aws_secret_access_key = {SECRET2}
region = eu-central-1
"""


class RoundTripTests(unittest.TestCase):
    def test_untouched_file_is_byte_identical(self):
        ini = IniFile(SAMPLE.split("\n")[:-1], trailing_newline=True)
        self.assertEqual(ini.render(), SAMPLE)

    def test_parses_every_section(self):
        ini = IniFile(SAMPLE.split("\n")[:-1])
        self.assertEqual(ini.section_names(), ["default-1", "ten-dev", "ten-staging"])

    def test_comments_and_blank_lines_survive_an_edit(self):
        ini = IniFile(SAMPLE.split("\n")[:-1])
        ini.set_values("ten-dev", {"aws_access_key_id": ASIA_NEW2})
        rendered = ini.render()
        self.assertIn("# my aws credentials", rendered)
        self.assertIn("; a trailing comment", rendered)
        self.assertIn(ASIA_NEW2, rendered)
        # Other profiles are untouched, character for character.
        self.assertIn(f"[default-1]\naws_access_key_id = {AKIA}", rendered)

    def test_edit_preserves_that_sections_spacing_style(self):
        ini = IniFile(SAMPLE.split("\n")[:-1])
        ini.set_values("ten-dev", {"aws_access_key_id": ASIA_NEW2})
        ini.set_values("default-1", {"aws_access_key_id": AKIA_NEW2})
        rendered = ini.render()
        self.assertIn(f"aws_access_key_id={ASIA_NEW2}", rendered)  # no-space style kept
        self.assertIn(f"aws_access_key_id = {AKIA_NEW2}", rendered)  # spaced style kept

    def test_removing_a_key_leaves_the_rest_of_the_profile(self):
        ini = IniFile(SAMPLE.split("\n")[:-1])
        ini.set_values("ten-dev", {"aws_access_key_id": AKIA_STATIC}, remove=("aws_session_token",))
        rendered = ini.render()
        self.assertNotIn("aws_session_token", rendered)
        self.assertIn("aws_secret_access_key=wJalrXUtnFEMI", rendered)

    def test_new_key_lands_inside_its_own_section(self):
        ini = IniFile(SAMPLE.split("\n")[:-1])
        ini.set_values("default-1", {"aws_session_token": "TOKENVALUE"})
        lines = ini.render().split("\n")
        start = lines.index("[default-1]")
        end = lines.index("[ten-dev]")
        self.assertTrue(any("aws_session_token = TOKENVALUE" in l for l in lines[start:end]))

    def test_remove_section_keeps_neighbours_intact(self):
        ini = IniFile(SAMPLE.split("\n")[:-1])
        ini.remove_section("ten-dev")
        rendered = ini.render()
        self.assertNotIn("[ten-dev]", rendered)
        self.assertNotIn("IQoJb3JpZ2luX2Vj", rendered)
        self.assertIn("[default-1]", rendered)
        self.assertIn("[ten-staging]", rendered)
        self.assertIn("; a trailing comment", rendered)

    def test_rename_only_changes_the_header(self):
        ini = IniFile(SAMPLE.split("\n")[:-1])
        ini.rename_section("ten-dev", "ten-development")
        rendered = ini.render()
        self.assertIn("[ten-development]", rendered)
        self.assertNotIn("[ten-dev]", rendered)
        self.assertIn("aws_session_token=IQoJb3JpZ2luX2VjEXAMPLETOKEN", rendered)

    def test_nested_config_subkeys_are_not_mistaken_for_entries(self):
        nested = "[profile work]\nregion = eu-west-1\ns3 =\n    max_concurrent_requests = 20\n"
        ini = IniFile(nested.split("\n")[:-1])
        self.assertIn("s3", ini.values("profile work"))
        self.assertNotIn("max_concurrent_requests", ini.values("profile work"))
        self.assertEqual(ini.render(), nested)

    def test_section_created_when_absent(self):
        ini = IniFile([])
        ini.set_values("brand-new", {"aws_access_key_id": "AKIA1", "aws_secret_access_key": "s"})
        self.assertIn("[brand-new]", ini.render())
        self.assertEqual(ini.values("brand-new")["aws_access_key_id"], "AKIA1")


class AtomicWriteTests(unittest.TestCase):
    def test_written_file_is_private_to_the_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "credentials")
            atomic_write(path, "secret")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_no_temp_files_are_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "credentials")
            atomic_write(path, "one")
            atomic_write(path, "two")
            self.assertEqual(os.listdir(tmp), ["credentials"])
            with open(path) as fh:
                self.assertEqual(fh.read(), "two")

    def test_backup_captures_previous_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "credentials")
            backups = os.path.join(tmp, "backups")
            atomic_write(path, "original")
            snapshot = backup(path, backups)
            atomic_write(path, "replaced")
            with open(snapshot) as fh:
                self.assertEqual(fh.read(), "original")
            self.assertEqual(os.stat(snapshot).st_mode & 0o777, 0o600)

    def test_backups_are_pruned_to_the_keep_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "credentials")
            backups = os.path.join(tmp, "backups")
            atomic_write(path, "x")
            for _ in range(8):
                backup(path, backups, keep=3)
            self.assertEqual(len(os.listdir(backups)), 3)


class RealFileTests(unittest.TestCase):
    """The strongest guarantee: a no-op round-trip of the user's own file."""

    def test_users_real_credentials_file_round_trips_unchanged(self):
        real = os.path.expanduser("~/.aws/credentials")
        if not os.path.exists(real):
            self.skipTest("no ~/.aws/credentials on this machine")
        with open(real, "r", encoding="utf-8") as fh:
            original = fh.read()
        rendered = IniFile.load(real).render()
        self.assertEqual(rendered, original, "round-trip altered the real credentials file")

    def test_editing_one_real_profile_leaves_the_others_byte_identical(self):
        real = os.path.expanduser("~/.aws/credentials")
        if not os.path.exists(real):
            self.skipTest("no ~/.aws/credentials on this machine")
        with open(real, encoding="utf-8") as fh:
            original = fh.read()
        ini = IniFile.load(real)
        names = ini.section_names()
        if not names:
            self.skipTest("no profiles to edit")
        target = names[0]
        ini.set_values(target, {"aws_access_key_id": ASIA_EDITED})
        rendered = ini.render()

        # Every line that is not part of the edited profile must be unchanged.
        before = original.split("\n")
        after = rendered.split("\n")
        self.assertEqual(len(before), len(after), "editing changed the line count")
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(len(differing), 1, "an edit touched more than one line")
        self.assertIn(ASIA_EDITED, after[differing[0]])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class BackupAgeTests(unittest.TestCase):
    def test_old_snapshots_are_pruned_by_age(self):
        import os, tempfile, time, shutil
        from awsprofiles.credfile import backup
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        src = os.path.join(tmp, "credentials")
        with open(src, "w") as fh:
            fh.write("[a]\nx=1\n")
        backups = os.path.join(tmp, "backups")
        old = backup(src, backups)
        stale = time.time() - 40 * 86400
        os.utime(old, (stale, stale))
        fresh = backup(src, backups)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
