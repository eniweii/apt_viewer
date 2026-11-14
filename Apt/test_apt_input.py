#!/usr/bin/env python3
"""Checks for the viewer's keyboard map (apt_input), without Tk. Run: python test_apt_input.py"""
import sys

import apt_input

ok = True


def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


check("arrows are the game's 37-40", [apt_input.key_event(k)[0] for k in ("Left", "Up", "Right", "Down")] == [37, 38, 39, 40])
check("Return is accept (302, ascii 13), Escape and BackSpace are back (303)", apt_input.key_event("Return", "\r") == (302, 13) and apt_input.key_event("Escape", "\x1b") == (303, 27) and apt_input.key_event("BackSpace", "\x08") == (303, 8))
check("F1..F4 are MISC1, MISC2, MISC3, START", [apt_input.key_event(k)[0] for k in ("F1", "F2", "F3", "F4")] == [304, 305, 300, 301])
check("PageUp/PageDown, Home/End, Insert/Delete are L1/R1, L2/R2, L3/R3", [apt_input.key_event(k)[0] for k in ("Prior", "Next", "Home", "End", "Insert", "Delete")] == [306, 307, 308, 309, 310, 311])
check("a printable key goes with its character code", apt_input.key_event("a", "a") == (65, 97) and apt_input.key_event("7", "7") == (55, 55))
check("keys with no meaning are not sent", apt_input.key_event("Shift_L", "") is None and apt_input.key_event("Control_L", "") is None and apt_input.key_event("F12", "") is None)
check("every code is one the game's INPUTCODE knows", set(apt_input.KEYSYM_CODES.values()) <= {37, 38, 39, 40} | set(range(300, 312)))

sys.exit(0 if ok else 1)
