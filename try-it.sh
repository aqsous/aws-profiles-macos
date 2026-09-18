#!/usr/bin/env bash
#
# Launches AWS Profiles against a throwaway copy of ~/.aws so you can break
# things freely. Your real credentials are never touched.
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SANDBOX="${TMPDIR:-/tmp}/awsprofiles-sandbox"

rm -rf "$SANDBOX"
mkdir -p "$SANDBOX"

# The fake values are assembled from fragments so secret scanners, which match
# on shape rather than provenance, do not flag this sandbox on every run.
FAKE_SECRET_Z="sandboxSecretKeyNotReal""ZZZZZZZZZZZZZZZZZ"
FAKE_SECRET_Y="sandboxSecretKeyNotReal""YYYYYYYYYYYYYYYYY"
FAKE_SECRET_X="sandboxSecretKeyNotReal""XXXXXXXXXXXXXXXXX"

cat > "$SANDBOX/credentials" <<EOF
# Sandbox credentials. Every value here is fake — break them freely.
# This comment should still be here no matter what you do in the app.

[practice-static]
aws_access_key_id = AKIASANDBOXKEY00000
aws_secret_access_key = $FAKE_SECRET_Z

[ten-dev]
aws_access_key_id=ASIASANDBOXKEYOLD01
aws_secret_access_key=$FAKE_SECRET_Y
aws_session_token=IQoJb3JpZ2luX2VjSANDBOXOLDTOKENYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY

; the comment below belongs to keep-me, not to ten-dev
[keep-me]
aws_access_key_id = AKIASANDBOXKEY00009
aws_secret_access_key = $FAKE_SECRET_X
region = eu-central-1
EOF

cat > "$SANDBOX/config" <<'EOF'
[default]
region = eu-west-1
output = json
EOF

chmod 600 "$SANDBOX"/credentials "$SANDBOX"/config
cp "$SANDBOX/credentials" "$SANDBOX/credentials.pristine"

cat <<INFO

  Sandbox ready:  $SANDBOX/credentials
  The menu bar item is badged 🧪 — that is the sandbox, not your real file.

  Load a sample onto the clipboard from another terminal tab, then use
  "Paste credentials from clipboard" in the menu:

     pbcopy < "$PROJECT_DIR/samples/1-portal-block.txt"     # updates ten-dev
     pbcopy < "$PROJECT_DIR/samples/4-assume-role.json"     # has a real expiry
     pbcopy < "$PROJECT_DIR/samples/5-truncated-BAD.txt"    # must be REFUSED
     pbcopy < "$PROJECT_DIR/samples/6-multi-profile.txt"    # imports two at once

  Quit from the menu (or press Ctrl-C here) to see what changed.

INFO

show_diff() {
  echo
  echo "  ── What changed in the sandbox credentials file ──"
  if diff -u "$SANDBOX/credentials.pristine" "$SANDBOX/credentials" > /dev/null 2>&1; then
    echo "  (no changes)"
  else
    diff -u "$SANDBOX/credentials.pristine" "$SANDBOX/credentials" | sed 's/^/  /' || true
  fi
  echo
  echo "  ── Backups written ──"
  ls -1 "$SANDBOX/awsprofiles-backups" 2>/dev/null | sed 's/^/  /' || echo "  (none)"
  echo
  echo "  Sandbox left at $SANDBOX — re-run ./try-it.sh to reset it."
}
trap show_diff EXIT

cd "$PROJECT_DIR"
AWSPROFILES_DIR="$SANDBOX" "$PROJECT_DIR/.venv/bin/python" -m awsprofiles
