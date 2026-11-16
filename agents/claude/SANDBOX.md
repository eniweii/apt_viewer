# The Claude sandbox: what it can do, set-up, recipes, lessons

## What the sandbox is (state of the third round)
- Ubuntu 24, Python 3.12, **one CPU core, no GPU** (software OpenGL only): timings from it are not the user's. Reset between conversations: nothing installed or unpacked stays.
- Tools: shell, file create/edit, viewing PNGs (the user sees the same images in the work log, also on his phone), presenting files, GitHub/pip/apt only (no open web).
- Python: Pillow, NumPy; installed on top: `skia-python`, `glfw`, `pefile`, `capstone`, apt `python3-tk`; `xvfb-run` and Mesa GL are there. Tried and not worth it: `blend2d-py` (Blend2D bindings), `b2d` (does not build).
- Cannot: see the game, run a GPU, compile C#, run anything that needs the open internet. It sees only what the user uploads or screenshots.
- Fetch repo files with `git clone --depth 1 --filter=blob:none --sparse https://github.com/eniweii/apt_viewer <dir>` then `git sparse-checkout set Apt common FNG agents` (web_fetch refuses github.com tree URLs).

## Set-up (one minute)
`bash agents/claude/tools/setup_sandbox.sh` unpacks every zip of the uploads (UI, resources, the engine data) and prints
`APT_UI`, `APT_ENGINE_DATA` (nfs.exe, English_*.bin, hashes_main.txt, undercover_global_dump.json) and `APT_RES` (the folders with the .bun/.lzc/.bin textures). `export` them, then every tool in `tools/` runs with `$APT_UI` as its first argument.

## Pictures (`tools/shots.py`, the way to check anything visual)
```
python shots.py $APT_UI leaderboardscreen --frame 19 --crop 250,60,800,100 --zoom 2     one crop, enlarged
python shots.py $APT_UI loadingscreen --runtime 60 --sheet 0,20,40,60 --cols 2          contact sheet over ticks
python shots.py $APT_UI TreeMenu --push --runtime 140 --keys Down@100,Return@120        a screen pushed through root.big, keys at ticks
python shots.py $APT_UI carsmenu --frame 12 --compare /path/to/older/checkout           old on top, new below
python shots.py $APT_UI TreeMenu --push --runtime 140 --game his_screenshot.png         his game next to the viewer
python shots.py $APT_UI autosculptmenu --frame 34 --window                              the whole Tk viewer (tree, tabs, status line)
```
Show the result with the image viewer (it appears in the work log) and present the important ones as files. For before/after of a change: `git archive <state> Apt common | tar -x -C old_tree`, then `--compare old_tree`.

## Verifying a render change (what worked in the speed/colour round)
1. Keep the previous state as a tree (`git archive`) and render the same movies/ticks with both in separate processes (`--repo`), compare the arrays.
2. A new option must be **bit-identical when off**: switch the new input off (for example force the colour bias to 0) and compare against the old tree. Differences must come only from what was meant to change.
3. Real timing needs the user's machine: sandbox numbers show ratios only. `draw` and `show` in the viewer's status line are his numbers.
4. Frames that matter: loadingscreen, longloadingscreen, challengeseries (heavy), photomode - backup, leaderboardscreen (text, colour), autosculptmenu, carsmenu, causewayintro02.
5. Tk item ids of the movie list are `<name>.big`; tools that `os.chdir` break relative output paths (use absolute).

## What the rendering round found (so it is not done twice)
- Time went into Skia's CPU fill of large stretched textures (challengeseries covers about 14 screens of pixels per tick), not Python: path building was under 1 %, conversions about 4 ms. Shape-path caching, display-list checkpoints and image-copy trimming are not worth doing.
- Done and applied: draws whose final alpha rounds to zero are skipped (pixel identical); **Fast images** (off by default, Skia only): textures premultiplied in the surface format, alpha-only tint as paint alpha, tinted texture copies for big stretched draws, only for axis-aligned draws (a rotated draw is slower without the colour matrix); **skia-gpu** (opt-in).
- Skia holds the GIL while it draws, so threads do not help; Blend2D releases it and is uniform for rotated draws, but is a second backend without colour matrix and layers: not started.
- Colour transform = scale and **bias** (ARGB, unsigned bytes, out = in * scale + bias, composed child first then parents like Flash). The game uses scale 0 + bias colour for flat grey/white icons and logos; ignoring the bias made them black. `Color.setRGB` / `setTransform` are stored but not applied (no script of the checked movies uses them). No alpha bias on text (the glyph box would show).
- Text `align`: 0 left, **1 right, 2 centre** (was 1 centre, 2 right). Evidence: the leaderboard filter labels are `align 1` and their boxes share a right edge.
