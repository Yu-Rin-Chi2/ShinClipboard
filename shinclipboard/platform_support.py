"""The handful of facts the rest of the app needs about the OS it runs on."""

from __future__ import annotations

import os
import platform


IS_WINDOWS = os.name == "nt"
IS_MAC = platform.system() == "Darwin"

# The face the app writes its own lists and labels in, next to native controls.
# `.AppleSystemUIFont` is how the Aqua port of Tk spells San Francisco, and macOS
# substitutes Hiragino for the Japanese runs inside it, so one family covers both
# scripts. Point sizes need no adjusting: Tk resolves them to the same physical
# text on both platforms, so a layout tuned on Windows still fits here.
UI_FONT_FAMILY = ".AppleSystemUIFont" if IS_MAC else "Yu Gothic UI"
