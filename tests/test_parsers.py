import os
import sys
import unittest
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixtures import AKIA, ASIA, ASIA_STEM, SECRET, TOKEN
from awsprofiles.parsers import ParseError, mask, parse, parse_all



class PortalFormatTests(unittest.TestCase):
    """The four shapes the AWS access portal and CLI hand you."""

    def test_credentials_file_block(self):
        creds = parse(f"""
[453841503341_AdministratorAccess]
aws_access_key_id={ASIA}
aws_secret_access_key={SECRET}
aws_session_token={TOKEN}
""")
        self.assertEqual(creds.profile_name, "453841503341_AdministratorAccess")
        self.assertEqual(creds.access_key_id, ASIA)
        self.assertEqual(creds.secret_access_key, SECRET)
        self.assertTrue(creds.is_temporary)
        self.assertEqual(creds.source_format, "ini")

    def test_shell_export_block(self):
        creds = parse(
            f'export AWS_ACCESS_KEY_ID="{ASIA}"\n'
            f'export AWS_SECRET_ACCESS_KEY="{SECRET}"\n'
            f'export AWS_SESSION_TOKEN="{TOKEN}"'
        )
        self.assertEqual(creds.access_key_id, ASIA)
        self.assertEqual(creds.secret_access_key, SECRET)
        self.assertEqual(creds.source_format, "env")

    def test_windows_and_powershell_blocks(self):
        for prefix, quote in (("SET ", ""), ("$Env:", '"')):
            creds = parse(
                f"{prefix}AWS_ACCESS_KEY_ID={quote}{ASIA}{quote}\n"
                f"{prefix}AWS_SECRET_ACCESS_KEY={quote}{SECRET}{quote}\n"
                f"{prefix}AWS_SESSION_TOKEN={quote}{TOKEN}{quote}"
            )
            self.assertEqual(creds.access_key_id, ASIA, prefix)
            self.assertEqual(creds.secret_access_key, SECRET, prefix)

    def test_assume_role_json_with_expiration(self):
        creds = parse(
            '{"Credentials": {'
            f'"AccessKeyId": "{ASIA}",'
            f'"SecretAccessKey": "{SECRET}",'
            f'"SessionToken": "{TOKEN}",'
            '"Expiration": "2026-09-17T18:04:11Z"}}'
        )
        self.assertEqual(creds.access_key_id, ASIA)
        self.assertIsNotNone(creds.expiration)
        self.assertEqual(creds.expiration.tzinfo, timezone.utc)
        self.assertEqual(creds.expiration.hour, 18)

    def test_credential_process_json_is_flat(self):
        creds = parse(
            '{"Version":1,'
            f'"AccessKeyId":"{ASIA}",'
            f'"SecretAccessKey":"{SECRET}",'
            f'"SessionToken":"{TOKEN}"}}'
        )
        self.assertEqual(creds.access_key_id, ASIA)

    def test_config_style_profile_prefix_is_stripped(self):
        creds = parse(f"[profile ten-dev]\naws_access_key_id={AKIA}\n"
                      f"aws_secret_access_key={SECRET}\n")
        self.assertEqual(creds.profile_name, "ten-dev")

    def test_region_is_picked_up_when_present(self):
        creds = parse(f"[ten-dev]\naws_access_key_id={AKIA}\n"
                      f"aws_secret_access_key={SECRET}\nregion=eu-central-1\n")
        self.assertEqual(creds.region, "eu-central-1")

    def test_long_lived_key_needs_no_token(self):
        creds = parse(f"aws_access_key_id={AKIA}\naws_secret_access_key={SECRET}")
        self.assertFalse(creds.is_temporary)


class BadPasteTests(unittest.TestCase):
    """Refuse to write anything that would leave a broken profile behind."""

    def test_temporary_key_without_a_session_token_is_rejected(self):
        with self.assertRaises(ParseError) as ctx:
            parse(f"aws_access_key_id={ASIA}\naws_secret_access_key={SECRET}")
        self.assertIn("session token", str(ctx.exception).lower())

    def test_truncated_session_token_is_rejected(self):
        with self.assertRaises(ParseError) as ctx:
            parse(f"aws_access_key_id={ASIA}\n"
                  f"aws_secret_access_key={SECRET}\naws_session_token=IQoJb3JpZ2lu")
        self.assertIn("truncated", str(ctx.exception).lower())

    def test_missing_secret_is_rejected(self):
        with self.assertRaises(ParseError):
            parse(f"aws_access_key_id={AKIA}")

    def test_missing_access_key_is_rejected(self):
        with self.assertRaises(ParseError):
            parse(f"aws_secret_access_key={SECRET}")

    def test_malformed_access_key_is_rejected(self):
        with self.assertRaises(ParseError):
            parse(f"aws_access_key_id=not-a-key\naws_secret_access_key={SECRET}")

    def test_empty_clipboard_is_rejected(self):
        with self.assertRaises(ParseError) as ctx:
            parse("   \n  ")
        self.assertIn("empty", str(ctx.exception).lower())

    def test_unrelated_text_is_rejected(self):
        with self.assertRaises(ParseError):
            parse("Hey, can you send me the staging credentials?")

    def test_loose_text_containing_a_key_asks_for_the_whole_block(self):
        with self.assertRaises(ParseError) as ctx:
            parse(f"the key is {AKIA} somewhere in here")
        self.assertIn("whole block", str(ctx.exception))


class MultiProfileTests(unittest.TestCase):
    def test_several_blocks_parse_individually(self):
        text = "\n".join(
            f"[acct-{i}]\naws_access_key_id={ASIA_STEM}{i}\n"
            f"aws_secret_access_key={SECRET}\naws_session_token={TOKEN}\n"
            for i in range(3)
        )
        results = parse_all(text)
        self.assertEqual(len(results), 3)
        self.assertEqual([c.profile_name for c in results], ["acct-0", "acct-1", "acct-2"])

    def test_one_good_block_survives_a_bad_neighbour(self):
        text = (f"[good]\naws_access_key_id={AKIA}\naws_secret_access_key={SECRET}\n\n"
                f"[bad]\naws_access_key_id={ASIA}\n")
        results = parse_all(text)
        self.assertEqual([c.profile_name for c in results], ["good"])


class MaskTests(unittest.TestCase):
    def test_mask_keeps_only_the_recognisable_ends(self):
        masked = mask(ASIA)
        self.assertTrue(masked.startswith("ASIA"))
        self.assertTrue(masked.endswith("MPLE"))
        self.assertNotIn("IOSFODNN7EXA", masked)

    def test_short_secrets_are_fully_hidden(self):
        self.assertEqual(mask("abc"), "•••")

    def test_missing_secret_renders_as_a_dash(self):
        self.assertEqual(mask(None), "—")


class PlanImportTests(unittest.TestCase):
    def _creds(self, name=None):
        from fixtures import AKIA, SECRET
        return parse(f"aws_access_key_id={AKIA}\naws_secret_access_key={SECRET}"
                     + (f"\n[{name}]" if False else "")).__class__(
            access_key_id=AKIA, secret_access_key=SECRET, profile_name=name)

    def test_one_named_block_is_written_straight_away(self):
        from awsprofiles.parsers import plan_import
        creds = self._creds("dev")
        self.assertEqual(plan_import([creds], None), ("write", ("dev", creds)))

    def test_an_explicit_target_wins_over_the_block_name(self):
        from awsprofiles.parsers import plan_import
        creds = self._creds("dev")
        self.assertEqual(plan_import([creds], "prod")[1][0], "prod")

    def test_an_unnamed_block_needs_a_name(self):
        from awsprofiles.parsers import plan_import
        self.assertEqual(plan_import([self._creds()], None)[0], "ask")

    def test_several_blocks_import_together_unless_a_target_is_forced(self):
        from awsprofiles.parsers import plan_import
        many = [self._creds("a"), self._creds("b")]
        self.assertEqual(plan_import(many, None)[0], "many")
        self.assertEqual(plan_import(many, "a")[0], "ask")
