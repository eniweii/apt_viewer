# Apt work: hand-over for a new conversation (third round: viewer speed, colour and text fixes; the first round's HANDOFF.md is not in git)

## Baseline = the user's tree
`github.com/eniweii/apt_viewer` at `e101b32` (the second round, his rename `Apt/beamng/` -> `Apt/game_stuff/`, this folder) plus, applied in this order with `git apply`:
`apt_render_skip`, `apt_fast_images`, `apt_gpu_renderer`, `apt_color_bias_align` (all applied and tested by him), plus the patch that added `tools/shots.py`, `tools/setup_sandbox.sh` and `SANDBOX.md`. Ask him which patch he applied last.
His `Apt/launch.py` is his own newer one (a friendly AI wrote it): it repeats the engine-feed set-up of `apt_view.main()` (engine map, language tables, attrib dump,
hash names, `apt_render.LOCALIZE`), which the old launcher skipped (the `~KEY` text stayed raw). Better: that set-up belongs in `apt_view.run()` so every entry point gets it.
Game data is not in the repo: `Apt/engine_data/` holds (his copy) nfs.exe, English_Global/Frontend/InGame.bin, hashes_main.txt, undercover_global_dump.json (all four vaults);
the UI folder is `C:\Users\ericm\Desktop\nfsuc1.18\UI` (150 .big). Put a copy in the sandbox as `<any folder>/UI` and pass it to the tools.

## Read first
`Apt/APT_DECOMPILER_NOTES.md` has the findings (exe addresses, input, screen loading, names ignore case, imports, textures and text, data natives) and the assumptions, with
section headers "Screen loading", "Runtime textures and text", "Data natives", "Input", "Built-ins of the VM". `Apt/game_stuff/EXE_ADDRESSES.md` lists exe addresses.

## What exists (all under Apt/)
| file | what |
|---|---|
| apt_vm.py | the VM; `CIProps` (names ignore case), `Env` closures, `Arr`, dotted `eval` paths, stack underflow = `Unsupported` |
| apt_builtins.py | Array, String methods, Object.registerClass, Key (isDown, getCode, getAscii, getController), Mouse, TextFormat, Color, Number, setInterval, escape |
| apt_runtime.py | `Ctx` (one per movie), `loadMovie`, imports of library movies, registerClass binding, text values, `_width/_height`, `attachMovie`, `swapDepths`, `hitTest`, `Runtime.key()` / `Runtime.mouse()` |
| apt_engine.py | native providers: message queue + gating, screen lifecycle, `~KEY` localizer, GetYearInfo, GetLanguageList, car lot (GetRegions/GetCarList/UpdateRegion), IsInputNecessary |
| apt_input.py | keyboard -> the game's input codes (viewer's choice, ASSUMED) |
| apt_render.py, apt_render_skia.py | per-clip movie, TextureHash binding (`_bound`), script-set text, `LOCALIZE` hook; colour transform = scale + bias (`cx_compose`, an 8-number cx); invisible draws skipped; Skia `fast_images` (off by default) |
| apt_render_skia_gpu.py | Skia on OpenGL (glfw hidden window, GPU textures, one readback per frame): `--renderer skia-gpu` and a launcher entry, never chosen by auto |
| apt_font.py | glyph text; `align` 0 left, 1 right, 2 centre |
| apt_resources.py | `image_for_hash` |

## Tests (run from Apt/)
`python test_decompile.py` (24), `test_apt_engine.py` (32 with the exe data, fewer without), `test_apt_builtins.py` (65), `test_apt_render.py` (33), `test_apt_input.py` (7),
`test_apt_runtime.py <UI folder>` (34 in a sandbox without the exe; skips without the UI folder). Older copies of two of them are in the repo's `tests/` folder.

## Tools of this folder (agents/claude/tools, take the UI folder as first argument or APT_UI)
- `screen_sweep.py` push every screen (95) through root.big for N ticks: loads, constructed, entered, errors (about 15-25 s).
- `native_gaps.py` the natives the screens call that have no provider, with the screens and arguments. `python native_gaps.py <UI> 120 tree` filters by screen name.
- `exe_disasm.py 0x71F5B0 ...` annotated disassembly of nfs.exe handlers (pip install pefile capstone).
- `shots.py` PNGs for checking by eye: a frame or runtime tick, crops, contact sheets, a pushed screen with keys/clicks, old-vs-new, his game screenshot next to the viewer, the whole Tk window. The user checks them on his phone. `SANDBOX.md` has the recipes.
- `setup_sandbox.sh` unpacks the uploads and installs what the tools need in a fresh sandbox (prints `APT_UI`, `APT_ENGINE_DATA`, `APT_RES`).
- `gui_drive.py` drives the real Tk viewer on a virtual display (sandbox: `apt-get update; apt-get install -y python3-tk xvfb`, then `xvfb-run -a python gui_drive.py <UI>`).

## State
92 of the 95 screens of the exe's table load and run (main, DropdownTest, TreeTest have no .big). All 150 movies run 300 ticks standalone. Pushed screens construct, call
InitGUI, OnEnterComplete; POP destructs; keys reach HandleInput and the game natives; mouse gives Flash button events.

## Open, in the order the user cares about
1. **Tree data** (round 4: model and Main/Cars levels done, see `EXE_TREE_NOTES.txt`). The menu tree is the slot table of nfs.exe (0xCB9140, rows `[item id, group, label hash, 0, 0]`), read by
   `Engine._tree_groups` from the committed exe tables; an item opens the group with its own id, other items are leaves. `MenuTree.GetTreeData/Busy`, `MainMenu.GetPauseTreeID` (0x27),
   `CustMainMenu.GetTreeID` (0x35) and the events `OnOptionSelected/OnTreePadRight/OnPadBack/OnPadLeft` are fed (tests in test_apt_engine.py). Two viewer gaps found on the way and fixed:
   a clip as a string is its target path (`MC.__str__`, AutoSizeText finds its fields with `eval(this._parent + "." + name)`) and `textWidth` (`Runtime.text_measure`, set by apt_view and shots).
   Leaves Paint (PaintMenu) and the 7 Hints and Tips topics (HintsTips, real hint pages) push their screens (TREE_SCREEN_LEAVES, pairs ASSUMED). Next: `cmdFEVisualPaint.GetSlotInfo` and the colour wheel
   data for PaintMenu (the region toggle shows `~Trunk` where the game shows "All Regions"), the other leaves (GPS Map, Garage, Shop, Vinyls, Photo Mode ...), Performance (group 0x8F) and the other
   groups were not compared with screenshots, `textHeight` is not provided, the Main list hides Exit (ASSUMED), levels open on the second item (ASSUMED from his screenshots),
   and the lookback labels differ from the game in his images 2 and 3: he confirms the paths are right and the game's labels wrong, so it is an exe quirk (see the notes).
2. Other natives without data (`native_gaps.py`): game state (my cars, challenge, loading event data, dialogs, messenger, player profile, video calibration, option rows) and the online ones
   (server data: stay missing). Feed by scenario/stubs only.
3. The engine map (apt_engine_map.json) is built from helper calls, not full decompilation: its signature of `IsInputNecessary` is wrong (it belongs to the next function). Trust it as a list of
   names, handlers and strings only.
4. Assumptions to revisit: processing flag starts on; `.swf` -> `.big` name; the constructor of a registered class runs after the first frame placed its children; setInterval gives no `this`;
   language list = first 15 rows of the exe table; car names = full `CARNAME_<MAKE>_<MODEL>` (three cars Stingray '67, SLR 722, GT500 '67 have unknown keys); keyboard map; getTimer 30 ticks/s;
   hitTest by bounds; colour bias unsigned bytes (alpha bias not on text); `Color.setRGB`/`setTransform` stored only; text align 0 left, 1 right, 2 centre.
5. VM: text layout members (maxscroll, scroll, textWidth; autoSize is done by the game at start-up), `addProperty` (14 calls in nfs_list.big) and `hasOwnProperty` (no string in nfs.exe), `instanceof`, a real `delete`.
   The Flash `Selection` object is not used (an earlier count was `CountrySelection`).
6. Textures: `image_for_hash` returns `dynamic` for packs compressed in place; the user parses dyntex in his other tools. Real pictures need the packs in the viewer's resource folders.
7. Exe leftovers from the first round: return helpers 0x805FC0 / 0x805FD0 (0x805FC0 builds an array from a count and a pointer list in GetLanguageList), setter types 0x805C80 / 0x805E50,
   the 254-entry slot table (0xCB9140), GetSlotInfo (0x87B030, 78 handlers), the 36 natives not found. He wants to fix exe-related apt behaviour later.
8. The viewer UI needs more changes for rendering; the user does those with another AI. FNG for Undercover is a separate conversation.
9. Leftover files at the repo root (`apt_resources.py`, `fng_tpk.py`) are not imported by anything in Apt/; he removes files by hand.

## The user's working rules
Concise answers, cause and effect; plan and confirm before coding; no diffs in chat, present changed files as cards and give one git patch (state its base); never put deletions in a patch;
ask which patch he applied last; give paths for files he adds by hand; list what is done and what is left at the end of each round; before running commands say how far along and what is next;
do not install dotnet in the sandbox (he builds C# locally); other AI output (the friendly AI) is research input to cross-check, not a plan; show visual results as small PNGs (before/after, side by side) in the work log.
