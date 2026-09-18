"""Build AppIcon.icns — a thin wrapper so install.sh keeps working."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from awsprofiles.icon import build  # noqa: E402

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "AppIcon.icns"
    print("wrote", build(target))
