NEXT_STEPS.md: the hand-over for a new conversation (baseline, patches applied, what exists, tests, open items in order). Read it first.
tools/: sandbox tools (screen_sweep.py, native_gaps.py, exe_disasm.py, gui_drive.py); each takes the UI folder as its first argument (see NEXT_STEPS.md).
decompiled_all_movies.json: the inventory text (decompiled scripts) of the 150 UI movies, made with the current decompiler (not in git).
attrib_classes_resolved.txt: the 171 attrib classes with resolved names, collection and field counts.
census_250ticks.txt: natives the screens call on their own (apt_tools.py census), first round.
scratch/: throw-away analysis scripts from the first round (paths point to /tmp); the repo tools (engine_data readers, engine_map.py, exe_addresses.py, runtime_images.py, apt_tools.py) and tools/ replace them.
