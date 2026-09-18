"""Render the app icon (also used at runtime for dialogs and the Dock).

A bundle with no CFBundleIconFile gets a blank generic icon, which makes it
hard to pick out in Launchpad, the Dock and Spotlight — the exact problem this
app already had in the menu bar. Drawn with AppKit so there is no extra
dependency and no binary checked into the repo.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from AppKit import (
    NSBezierPath,
    NSBitmapImageRep,
    NSCalibratedRGBColorSpace,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSGraphicsContext,
    NSMakeRect,
    NSMakeSize,
    NSPNGFileType,
)
from Foundation import NSMutableDictionary, NSString

# .icns wants each size twice, once as @2x of the size below it.
SIZES = ((16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2))
GLYPH = "☁️"


def _render(pixels: int, path: str) -> None:
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, pixels, pixels, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0
    )
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(context)

    # macOS icons sit inside a rounded square with a margin around it.
    margin = pixels * 0.06
    radius = pixels * 0.22
    box = NSMakeRect(margin, margin, pixels - margin * 2, pixels - margin * 2)
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.13, 0.17, 0.23, 1.0).setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(box, radius, radius).fill()

    size = pixels * 0.56
    attributes = NSMutableDictionary.dictionary()
    attributes[NSFontAttributeName] = NSFont.fontWithName_size_("Apple Color Emoji", size) \
        or NSFont.systemFontOfSize_(size)
    text = NSString.stringWithString_(GLYPH)
    drawn = text.sizeWithAttributes_(attributes)
    origin = NSMakeRect(
        (pixels - drawn.width) / 2.0,
        (pixels - drawn.height) / 2.0 - pixels * 0.01,
        drawn.width,
        drawn.height,
    )
    text.drawInRect_withAttributes_(origin, attributes)

    NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(NSPNGFileType, NSMutableDictionary.dictionary())
    data.writeToFile_atomically_(path, True)


def build(destination: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        iconset = os.path.join(tmp, "AppIcon.iconset")
        os.makedirs(iconset)
        for base, scale in SIZES:
            suffix = "@2x" if scale == 2 else ""
            _render(base * scale, os.path.join(iconset, f"icon_{base}x{base}{suffix}.png"))
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", destination], check=True)
    return destination


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "AppIcon.icns"
    print("wrote", build(target))
