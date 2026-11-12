Engine data for the apt viewer (apt_engine.py). Put these files in this folder (Apt/engine_data/), or point the viewer at another folder
with --engine-data. Nothing here is committed (see .gitignore); the viewer starts without them, the natives of the game then answer undefined.

  apt_engine_map.json          which natives nfs.exe registers. Ships in Apt/beamng/; a copy here wins. Remake it with: python apt_tools.py engine-map --ui <UI folder>
  English_Global.bin           the game's English string tables (the three files from the game's language folder)
  English_Frontend.bin
  English_InGame.bin
  undercover_global_dump.json  attrib data: the UndercoverVLT_Dumper output of attributes.bin, cars_vault.bin, fe_attrib.bin, gameplay.bin
  hashes_main.txt              names for the attrib hashes (classes, fields, collections)
  nfs.exe                      only needed to remake apt_engine_map.json, not to run the viewer

Check what is found: python apt_tools.py check
