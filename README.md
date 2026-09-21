# AWS Profiles for macOS

A small macOS app for the `~/.aws/credentials` file, for people who work with
several AWS accounts through IAM Identity Center and are tired of the refresh
ritual: open the access portal, copy the credential block, open the file in an
editor, find the right profile, select exactly the old three lines, paste, save,
hope nothing else moved. Here that is: copy the block, then one click.

![The AWS Profiles window: profiles grouped by client, coloured by how much life
their credentials have left](docs/window.png)

- **Paste and go.** Recognises the portal block, `export` lines, PowerShell,
  and the JSON from `aws sts assume-role` or `credential_process`. Refuses
  truncated pastes before they reach the file.
- **Grouped by client.** Each organisation you sign in to gets a login page and
  a sign-in name; profiles sit under it, and **Login page** opens the right
  portal with that name ready to paste, then imports the block you copy.
- **Knows which credentials work.** Checks each profile against AWS and colours
  expiring and expired ones. Temporary credentials count down.
- **Never damages the file.** Line-preserving edits, a backup before every
  change, atomic writes and permission repair. Comments and formatting survive.
- **No boto3, no CLI.** The AWS call is hand-signed with the standard library,
  so it works even where the `aws` command is missing or broken.

🟢 works · 🔴 expired · 🟠 rejected by AWS · ⚪️ not checked yet

The window is the main interface. There is also a `☁️` menu bar item with the
same actions — but **do not rely on it**: when the menu bar is full macOS
silently collapses the newest status item behind a `«` chevron. The item still
reports itself visible and still has a correct title and frame; it simply is not
drawn. That is why this app has a window and a Dock icon.

## Requirements

macOS 11 or later and Python 3.13 (`brew install python@3.13` if you do not have
it). The only dependencies are `rumps` and the PyObjC Cocoa bindings, pinned
with hashes in `requirements.txt`.

## Install

```sh
./install.sh
```

It installs to **/Applications/AWS Profiles.app**, so it opens the way any other
app does: Finder → Applications, Spotlight (⌘Space → "AWS Profiles"), Launchpad,
or `open -a "AWS Profiles"`.

`install.sh` creates a virtualenv, runs the tests, draws the icon, builds the
bundle and registers it with LaunchServices and Spotlight. Pass a different
directory as the first argument to install elsewhere. It
is safe to re-run. The window opens on launch; closing it only hides it, and
⌘0, the Dock icon or the menu bar item bring it back. Turn on **Start at login**
from the menu bar item to have it come back after a reboot.

If the app ever vanishes, it logs to `~/Library/Logs/AWSProfiles.log` —
launched from Finder there is no terminal for a traceback to reach.

## The daily workflow

1. In the AWS access portal, click **Access keys** next to the account.
2. Copy the block under *Option 2: Add a profile to your AWS credentials file*.
3. Menu bar → **Paste credentials from clipboard**.

The profile name comes from the pasted block, so there is nothing to type. You
get a confirmation showing the old and new key before anything is written.

Any of these formats work — paste whichever the tool in front of you produces:

| Source | Shape |
| --- | --- |
| Access portal, *Option 2* | `[123_Admin]` + `aws_access_key_id=…` |
| Access portal, *Option 1* | `export AWS_ACCESS_KEY_ID=…` |
| Windows / PowerShell | `SET …` / `$Env:…` |
| `aws sts assume-role` | `{"Credentials": {"AccessKeyId": …}}` |
| `credential_process` helpers | `{"Version":1,"AccessKeyId":…}` |

When the source carries an expiry (the JSON forms do), the menu counts it down.

## Trying it safely

```sh
./try-it.sh
```

This seeds a throwaway `~/.aws` in your temp directory, launches the app against
it, and prints a diff of what changed when you quit. The menu bar item is badged
🧪 so a sandbox instance is never mistaken for the one editing your real file.
Your real credentials are not touched, and re-running resets the sandbox.

`samples/` holds one file per supported paste format — load one onto the
clipboard and use **Paste credentials from clipboard**:

```sh
pbcopy < samples/1-portal-block.txt     # updates ten-dev
pbcopy < samples/4-assume-role.json     # carries a real expiry, so it counts down
pbcopy < samples/5-truncated-BAD.txt    # missing its session token: must be refused
pbcopy < samples/6-multi-profile.txt    # two profiles in one paste
```

Worth checking while you are in there: the comments and the `keep-me` profile
survive every edit, deleting `ten-dev` leaves the comment above `keep-me` alone,
and **Credentials file ▸ Restore a backup** puts back whatever you broke.

Against your real profiles, **Check now** and **Copy `export AWS_PROFILE`** are
read-only and safe to try at any time.

## The window

The window is the main interface. Its toolbar holds every action; the table
underneath is grouped by client, and rows are coloured by how much life the
credentials have left — orange under an hour, red once expired or rejected.

- **Paste** (⌘I) — the one way credentials come in. A clean block for a known
  profile is imported after a single confirmation. Anything else opens the
  paste dialog: the text parses as you type, and the profile it belongs to is
  picked from the existing ones or given a new name.
- **Login page** (⌘L) — opens the selected profile's client portal with the
  sign-in email on the clipboard, then watches the clipboard for five minutes:
  copy the credential block in the portal and the import starts by itself.
  While waiting, every clipboard change is parsed in memory and discarded
  unless it is a credential block; nothing is stored or logged. Double-click
  a client header for the same thing.
- **Use as default** (⌘D), **Check** (⇧⌘R), **Edit** (⌘E or Return),
  **Delete** (⌘⌫ or the Delete key) act on the selected row.
- **Clients** (⌘K) — a sheet listing every client with its name, login URL and
  email. Save applies at once; renaming moves the grouped profiles along.
- **Backups**, **Check all** (⌘R) and a **Filter** field (⌘F) that matches
  profile names, clients, account IDs and roles.

Right-click any row for the full menu: default, login page, update, edit,
check, copy `export AWS_PROFILE`, copy secrets, client grouping, rename and
delete. Right-click a client header for its login page, email and settings.

## Actions

The same operations are in the menu bar item, under each profile's submenu:

- **Use as default** — copies the credentials into `[default]`. This is how you
  switch the profile that every tool picks up, *including terminals that are
  already open*; a menu bar app cannot reach into a running shell's environment
  to set `AWS_PROFILE`. `[default]` then tracks that profile: refresh it later
  and `[default]` is updated too.
- **Update from clipboard…** — refresh this specific profile, whatever the
  pasted block calls itself.
- **Edit credentials…** — a multi-line box for hand-editing one profile.
- **Check now** — asks AWS `sts:GetCallerIdentity` who these credentials are.
- **Copy `export AWS_PROFILE`** — the safe copy: no secrets leave the file.
- **Copy credentials as env vars…** — the real secrets, behind a warning, for
  pasting into a remote shell. The clipboard is wiped 60 seconds later unless
  you have copied something else in the meantime.
- **Rename…** / **Delete…** — both also keep `~/.aws/config` in step.

The menu bar title shows the client and profile that `[default]` currently
mirrors, and an ⏳ while the app is waiting for credentials on the clipboard.

## Clients: one login page per organisation

Working for several clients means several AWS access portals, each with its
own sign-in name, an email address or a plain username. A **client** records
both, and profiles are grouped under
the client whose portal issues their credentials:

- **Clients** in the toolbar (⌘K), or **Clients ▸** in the menu bar — add a
  client (name, login URL, email or username; a bare host gets `https://`, and
  only https is accepted), edit or rename it, or remove it.
- **Client** in a row's right-click menu (or the *Client* submenu in the menu
  bar) groups a profile. A brand-new profile is offered the most recently used
  client right after it is created, and a profile imported while the app is
  waiting after **Login page** is grouped under that client automatically.
- **Login page** — opens that client's portal and puts the sign-in email on the
  clipboard, so the login form is one ⌘V away. AWS ignores every prefill
  parameter on the portal and sign-in URLs, so the clipboard is the only route
  in; a one-time notice says so.
- **Remove** only forgets the grouping and URL; no credentials change.

Clients live in the state file, not in `~/.aws/credentials` or `~/.aws/config`,
so nothing AWS reads is altered.

## Security

What the app does with your credentials, in full:

- **They stay in `~/.aws/credentials`, in plain text**, because that is the file
  every AWS SDK, the CLI and Terraform read. The app does not move them into a
  keychain or its own store. Its job is to make the edits to that file safe: it
  checks the file is mode `0600`, offers to fix it when it is not, backs the
  file up before every change and writes atomically.
- **The only network call is `sts:GetCallerIdentity` to `sts.amazonaws.com`**,
  over TLS, signed locally with SigV4. Redirects are refused so a signed request
  is never replayed to another host. Nothing else leaves the machine.
- **Nothing is logged.** Error messages carry masked key ids only. The launcher
  keeps its log file private.
- **The clipboard** is read when you ask for an import, and for five minutes
  after **Login page** so the block you copy imports itself; contents are
  parsed in memory and discarded unless they are credentials. Secrets you copy
  out on purpose are wiped after 60 seconds.
- **State the app keeps** — client login pages and emails, which profile
  `[default]` mirrors, last known account ids — lives in
  `~/.aws/.awsprofiles-state.json`, mode `0600`. Backups live in
  `~/.aws/awsprofiles-backups/`, mode `0700`, and are pruned after 30 days
  because old snapshots hold old secrets.

If you find a way for the app to leak or corrupt credentials, please report it
privately through a GitHub security advisory.

## How it avoids damaging your credentials

This edits a file that is painful to lose, so:

- **Only the lines it must change are rewritten.** The file is kept as raw
  lines rather than re-serialised, so comments, blank-line grouping, key order
  and your mix of `key=value` and `key = value` all survive untouched. A test
  asserts that editing one profile in your real file changes *exactly one line*.
- **Every change is backed up first**, to `~/.aws/awsprofiles-backups/`
  (the last 30 are kept, and none older than 30 days, since old snapshots hold
  old secrets). **Credentials file ▸ Restore a backup** puts one back, and
  backs up the current file on the way, so a restore is itself undoable.
- **Writes are atomic** — a temp file in the same directory, fsynced, then
  renamed over the target. A crash mid-write leaves the original intact.
- **Files stay `0600`** after every write, and the app reports it if something
  else has loosened them.
- **Only credential keys are ever touched.** `region`, `role_arn`, `output` and
  anything custom in a profile are left exactly as they were.
- **Bad pastes are refused before anything is written** — a truncated session
  token, an `ASIA…` key with no token at all, a malformed key id, or a name
  with a space in it.
- **Secrets are never displayed**, only as `ASIA••••••••3SV3`. Smart quotes and
  dash substitution are disabled in the edit box, which would otherwise quietly
  corrupt a pasted secret.

No credential ever leaves the machine except to `sts.amazonaws.com`, to ask
whether it still works — and that request refuses to follow redirects, so the
signed Authorization header can never be replayed at another host.

## Layout

```
awsprofiles/
  credfile.py   line-preserving ini reader/writer, atomic save, backups
  parsers.py    recognises the five clipboard formats; rejects bad pastes
  sts.py        SigV4 GetCallerIdentity, standard library only
  store.py      profile operations, backups, permission checks
  window.py     the main window: toolbar, grouped table, context menu
  clients.py    the Clients sheet
  app.py        the menu bar item, dialogs and all the actions
tests/          82 tests, including a round-trip of your real file
```

`sts.py` signs its own requests rather than calling boto3 or the `aws` CLI, so
the app works even where the CLI is missing or broken.

State the app keeps for itself — which profile `[default]` mirrors, last known
account, expiry times, clients and their login pages — lives in `~/.aws/.awsprofiles-state.json`. Deleting it
loses nothing but that cache.

## Uninstall

```sh
rm -rf "/Applications/AWS Profiles.app"
rm -f  ~/Library/LaunchAgents/io.github.aqsous.awsprofiles.plist
rm -f  ~/.aws/.awsprofiles-state.json
rm -rf ~/.aws/awsprofiles-backups   # only once you are sure
```

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Two of them read your real `~/.aws/credentials` (read-only, and they skip if it
is absent) to prove a round-trip changes nothing. Everything else runs against
a throwaway directory. `AWSPROFILES_DIR=/some/copy` points the app itself at a
sandbox if you want to try it without touching the real file.
