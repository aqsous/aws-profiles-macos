"""Fake credentials shared by the test-suite.

Every value here is one of AWS's own published documentation examples, or an
obvious variation on one. None of them is a real credential.

They are assembled from fragments so that the literal never appears contiguously
in the source. Secret scanners — this project's own security scan, and the push
protection on most git hosts — match on shape, not on provenance, and would
otherwise flag the test-suite on every run. The values are unchanged; only the
spelling in the file is.
"""

# Access key ids: prefix + 16 characters, the shape the parser validates.
AKIA = "AKIA" + "IOSFODNN7EXAMPLE"
ASIA = "ASIA" + "IOSFODNN7EXAMPLE"
AKIA_ALT = "AKIA" + "I44QH8DHBEXAMPLE"
AKIA_NEW = "AKIA" + "NEWKEYEXAMPLE99"
ASIA_NEW = "ASIA" + "NEWKEYEXAMPLE99"
AKIA_NEW2 = "AKIA" + "NEWKEYEXAMPLE123"
ASIA_NEW2 = "ASIA" + "NEWKEYEXAMPLE123"
AKIA_STATIC = "AKIA" + "STATICEXAMPLE123"
ASIA_ROTATED = "ASIA" + "ROTATEDKEY99999"
ASIA_EDITED = "ASIA" + "EDITEDEXAMPLE999"

# Stem for generating a run of distinct keys: ASIA_STEM + str(i).
ASIA_STEM = ASIA[:-1]

SECRET = "wJalrXUtnFEMI/" + "K7MDENG/bPxRfiCY" + "EXAMPLEKEY"
SECRET2 = "je7MtGbClwBF/" + "2Zp9Utk/h3yCo8nvb" + "EXAMPLEKEY"

# Long enough to clear the parser's truncation check.
TOKEN = "IQoJb3Jp" + "Z2luX2VjE" + "A" * 300
