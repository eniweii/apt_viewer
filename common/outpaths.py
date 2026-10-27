"""Where the tools write. Everything goes under outputs/ in the repo root."""
import os

COMMON = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(COMMON)
OUTPUTS = os.path.join(ROOT, "outputs")


def out_dir(tool):
    """outputs/<tool> (tool may be 'apt/convert'), created if needed."""
    path = os.path.join(OUTPUTS, *tool.split("/"))
    os.makedirs(path, exist_ok=True)
    return path


def out_path(tool, path):
    """A relative path is placed in outputs/<tool>; an absolute path, or an empty one, is kept."""
    if not path or os.path.isabs(path):
        return path
    return os.path.join(out_dir(tool), path)
