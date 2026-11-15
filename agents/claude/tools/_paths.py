"""Shared set-up of the tools in this folder: the repo's Apt/ and common/ on sys.path, the UI folder, the engine feed."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
sys.path[:0] = [os.path.join(REPO, "Apt"), os.path.join(REPO, "common")]


def ui_folder(argv_index=1):
    """The folder with the UI .big files: argument, else APT_UI, else Apt/engine_data/UI."""
    cand = sys.argv[argv_index] if len(sys.argv) > argv_index and os.path.isdir(sys.argv[argv_index]) else None
    cand = cand or os.environ.get("APT_UI") or os.path.join(REPO, "Apt", "engine_data", "UI")
    if not os.path.isfile(os.path.join(cand, "root.big")):
        sys.exit("no UI folder with root.big: give it as the first argument or set APT_UI")
    return cand


def engine():
    """The engine feed from Apt/engine_data (what the viewer builds); sets apt_runtime.ENGINE."""
    import apt_engine
    import apt_runtime
    apt_runtime.ENGINE = apt_engine.build_default(None)
    if apt_runtime.ENGINE is None:
        sys.exit("no engine feed: check Apt/engine_data (python Apt/apt_tools.py check)")
    return apt_runtime.ENGINE
