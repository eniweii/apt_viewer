"""
Keyboard -> the game's input codes, for the viewer (the Tk side is in apt_view.py). No Tk here, so it can be tested.

The codes are the game's own INPUTCODE values (root.big): arrows 37 LEFT, 38 UP, 39 RIGHT, 40 DOWN; pad buttons 300 MISC3, 301 START, 302 NEXT (accept),
303 PREV (back), 304 MISC1, 305 MISC2, 306 L1, 307 R1, 308 L2, 309 R2, 310 L3, 311 R3. The keyboard side is the viewer's own choice (ASSUMED, the game's
real PC key map is in the exe): Return = accept, Escape or BackSpace = back, F1..F4 = MISC1, MISC2, MISC3, START, PageUp/PageDown = L1/R1,
Home/End = L2/R2, Insert/Delete = L3/R3. Tab is not used: Tk keeps a more specific binding for it. Any other printable key is sent with its
character code (code = capital letter, ascii = the character) for the screens that read Key.getAscii().
"""

KEYSYM_CODES = {
    "Left": 37, "Up": 38, "Right": 39, "Down": 40,
    "Return": 302, "KP_Enter": 302, "Escape": 303, "BackSpace": 303,
    "F1": 304, "F2": 305, "F3": 300, "F4": 301,
    "Prior": 306, "Next": 307, "Home": 308, "End": 309, "Insert": 310, "Delete": 311,
}
ASCII_OF = {"Return": 13, "KP_Enter": 13, "Escape": 27, "BackSpace": 8}


def key_event(keysym, char=""):
    """(code, ascii) the game gets for a Tk key (its keysym and the character it types), or None for a key that is not sent."""
    if keysym in KEYSYM_CODES:
        return KEYSYM_CODES[keysym], ASCII_OF.get(keysym, ord(char) if char and ord(char) >= 32 else 0)
    if len(char) == 1 and char.isprintable():
        return ord(char.upper()), ord(char)
    return None
