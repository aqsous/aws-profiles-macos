# Contributing

Thanks for taking a look. This is a small, single-purpose tool, so the bar for
a change is: does it make managing `~/.aws/credentials` safer or quicker, and
does it keep every existing guarantee about never damaging that file?

## Running the tests

```sh
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

The suite runs without a display: it exercises the parsers, the line-preserving
ini writer and the profile store against temporary directories. One test
round-trips your real `~/.aws/credentials` through the writer and asserts it
comes back byte-identical; it is skipped when you have no such file.

## Trying the app without touching your credentials

```sh
./try-it.sh
```

That launches the app against a throwaway copy of `~/.aws` (badged 🧪 in the
menu bar) and prints a diff of what changed when you quit. Sample clipboard
blocks live in `samples/`.

## What a change should come with

- A test in `tests/` for any behaviour in `store.py`, `credfile.py` or
  `parsers.py`. UI code in `app.py`, `window.py` and `clients.py` is exercised
  by hand through `try-it.sh`.
- No new dependency unless it is unavoidable. The STS check is hand-signed
  precisely so the app works where boto3 and the `aws` CLI do not.
- If you add a dependency or bump one, regenerate the hashes in
  `requirements.txt`; the installer refuses anything unpinned.

## Reporting a security issue

If you find a way for the app to leak or corrupt credentials, please open a
GitHub security advisory on the repository rather than a public issue.
