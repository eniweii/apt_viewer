# Apt decompiler notes (apt_decompile.py)

Standalone notes for the Undercover apt scripts. FNG notes can get their own section later.
Status: all ~150 UI movies decompile with no raw op comments, no unresolved `dict[N]`, no stack underflow, balanced braces
(checked with the UI `.big` folder, 6,891 code blocks). `python Apt/test_decompile.py` runs the synthetic tests.

## How the decompiler works
Linear walk over the decoded `Insn` list with an expression stack. Control flow is rebuilt from jump targets (indices):
- Forward `branchIfTrue` after `not` is an `if`. The test is normalised: `X; not; branchIfTrue E` gives `if (X)`, no `not` gives `if (!(X))`.
- A forward `branchAlways` as the last instruction of an if-block makes it `if/else`, unless that jump is a break or only jumps over no-op jumps.
- A backward `branchAlways` ends a `while`; the header is the first conditional jump from the loop start that leaves just past the end.
  No header gives `while (true)`. A backward `branchIfTrue` with no back jump is `do { } while`.
- Jump to loop end + 1 is `break`; jump back to loop start is `continue`. A `continue` in a `for` loop that jumps to an increment before the back jump still prints as `else`.
- `switch`: `storeRegister R; <case value code>; strictEquals; branchIfTrue Lcase`, further tests start with `push R, ...`, then one `branchAlways` to the default (or the end). Case values can be any expression. The last body's jump to the end is not printed; an empty default is dropped.
- `if (a || b)`: `A; branchIfTrue L; B; branchIfTrue L; branchAlways S; L:`. Also covers `if (c) {..} else {..}` compiled as `c; branchIfTrue L; branchAlways S`.
- `c ? a : b` looks like if/else but both branches end in a value; told apart by the last instruction before the join (see `STATEMENT_END`).
- A `branchAlways` to the next instruction (the compiler's empty else) is dropped. 58 empty `if (...) {}` blocks remain; they come from the original source.
- `for (v in obj)`: `enumerate2; L: storeRegister k; push undefined; equals2; branchIfTrue End; [push k; storeRegister v; pop]; body; branchAlways L`.
- Named `defineFunction/2` is a declaration (nothing left on the stack); anonymous ones are values.
- Inside `defineFunction2` bodies the preloaded registers (this, arguments, super, _root, _parent, _global) and parameters print by name.

## Op semantics confirmed on the real files
- `trace` pops one message (always preceded by `add2` or a push). It was the biggest source of garbage because it was not popped.
- `defineLocal2` pops the name only (`var x;`); `defineLocal` pops value then name.
- `callMethod` with name `undefined` or `""` calls the object itself: that is `super(...)` in constructors.
- `extends` pops super then sub (`Sub extends Super`). `implementsOp` pops the class, a count, then that many interfaces. `castOp` pops object then constructor.
- `getProperty` / `setProperty` use the 22 standard property indices (`_x` .. `_ymouse`).
- `initArray` and `initObject` pop a count first. Array elements pop in index order. Object pairs pop value then name (printed in pop order, so key order is reversed; it does not matter).
- `getVariable` / `setVariable` with a computed name print as `eval(expr)`.
- `gotoFrame2` prints `gotoAndPlay` when its flag is set, else `gotoAndStop`.
- Not covered: `try`, `with`, scene bias on `gotoFrame2`, `enumerate` (old op). None showed up as raw in the data.

## What nfs.exe feeds to apt (from the decompiled scripts; names confirmed as strings inside nfs.exe)
The scripts read `_global.X` names that no script ever assigns. 73 such names exist, so the game registers them as native objects
before the movies run. 57 of them (and 37 of their member names, for example all the `EventTranslator.On*` events) appear as plain
strings in `nfs.exe`. Not verified yet: argument and return types, and how the exe wires them (that needs the exe).

Core objects:
- `AptDown`: (value or function)
- `AptNext`: (value or function)
- `AptUp`: (value or function)
- `CustMainMenu`: GetTreeID
- `EventTranslator`: OnButtonPressed, OnEnterComplete, OnExitComplete, OnOptionHighlighted, OnOptionSelected, OnPadAccept, OnPadBack, OnPadButton0, OnPadButton1, OnPadButton2, OnPadButton3, OnPadButton4, OnPadButton5, OnPadButton6
- `Game`: OnMenuGetItemEnabled, OnMenuHilight, OnMenuSelect
- `Input`: PopInputState, PushInputState
- `MenuTree`: Busy, GetMessage, GetTreeData, IsMessageAvailable
- `PlaySound`: (value or function)
- `ScreenMessageTranslator`: EnableProcessing, GetMessage, GetMovieScreenName, GetScreenName, GetStringHash, IsMessageAvailable
- `TempScrollItem`: (value or function)
- `Util`: TruncateField, TruncateFieldEllipse, loadCafeIcon
- `etfStdColorClass`: (value or function)
- `extern`: Platform, WideScreen
- `gCT`: register
- `gTransMan`: goBackScreen, gotoScreen
- `gVKB`: finish

Per-screen command objects (`Cmd*`, one per screen; the members the scripts call):
- `CmdAudio`: PlaySound
- `CmdAutosculptScreen`: (object only)
- `CmdFECarLot`: CarHighlighted, CarSelected, GetCarList, GetRegions, UpdateRegion
- `CmdFECarSelectScreen`: (object only)
- `CmdFEDebugCarTestScreen`: ScrollNextBodyKitNumber, ScrollNextBodyKitType, ScrollNextCar, ScrollNextCarSlot, ScrollNextKit, ScrollNextPart, ScrollNextPresetRide, ScrollPrevBodyKitNumber, ScrollPrevBodyKitType, ScrollPrevCar, ScrollPrevCarSlot, ScrollPrevKit, ScrollPrevPart, ScrollPrevPresetRide
- `CmdFELanguage`: GetLanguage, GetLocaleText
- `CmdFEPCSaveSelectScreen`: (object only)
- `CmdFEWMQuickRaceCustomizeScreen`: (object only)
- `CmdHud`: GetPlayerCarInfo
- `CmdLauncher`: SetCarName, StartGame
- `CmdPauseMenu`: PauseGame, ResumeGame
- `CmdScreenManagement`: EventReceived, GetMaxMovieClipLoaderLevels, GetMovieClipLoaderLevel, MovieClipLoaded, MovieClipUnloaded
- `CmdUtil`: ArePermanentPreloadsInProgress, Assert, BootLoadScreenLoaded, DisableCharacterRenderingOverApt, EnableCharacterRenderingOverApt, FormatAsCash, FormatAsNumber, FormatAsPercentage, FormatAsTime, GetBootLoadingPrecentComplete, GetBootLoadingScreenText, GetDALID, GetScreenHeight, GetScreenWidth, GetWidescreen, IsHighDef, IsInputNecessary, IsInputNecessaryError, IsSimPaused, MainScreenLoaded
- `CmdVinylPaintScreen`: (object only)
- `CmdVinylSelectScreen`: (object only)
- `CmdVinylTransformScreen`: (object only)

Lowercase `cmd*` objects (data providers per screen):
- `cmdAptControllerErrorMessage`: GetErrorMessage
- `cmdChallenge`: GetAnalogueStickValues, GetCurrentSeries, GetDataGrid, GetTickerText, IsProMode, SelectLBPlayer, SetCurrentSeries
- `cmdChallengeSummary`: GetDataGrid, GetPercent, IsProMode
- `cmdChyron`: GetShowButtonHelp
- `cmdControllerConfigOptions`: ChangeOption, GetController, GetData
- `cmdCredits`: CallBackLineDeleted, NotifySpawnLinePossible, OnShiftDown, OnShiftUp
- `cmdFECars`: EnteredFromImpound, GetButtonInfo, IsCarLoading
- `cmdFECreateAccount`: GetEmailAndPassword
- `cmdFEDialogScreen`: GetDialogConfig, UpdateEndOfTextPos
- `cmdFEHintsTipsScreen`: GetPageData, GetToggleData
- `cmdFELeaderboardScreen`: RetrieveData, UpdateColumnHeadings, UpdateFilters, UpdateLabels
- `cmdFEMyCars`: CarHighlighted, CarSelected, GetCarList, GetCurrentToggle, UpdateCarList
- `cmdFENISOverlay`: GetName, OnScreenEntered, OnScreenUpdate
- `cmdFEOnlineTOS`: GetMode, GetNumTOSWords, GetWordRange
- `cmdFEPerformanceTuning`: GetRealPos
- `cmdFEPinkSlip`: OptionSelected
- `cmdFEVisualPaint`: CycleGroup, FadingBack, FadingFront, GetSlotInfo, SelectSlot
- `cmdFEWMDistrictViewScreen`: ItemSelectedAnimationDone
- `cmdFEWMWorldViewScreen`: ItemSelectedAnimationDone
- `cmdGenericError`: GetErrorMessage
- `cmdLanguageSelect`: GetLanguageList
- `cmdLevelUpScreen`: ExitLevelUp
- `cmdLoadingScreen`: ChangeState, GetEventData, GetLoadingTexts, OnEnterComplete, OnScreenExitComplete, SetAnimationCompleted, TextFadeOutComplete
- `cmdMessenger`: GetMessengerConfig, MoveTab
- `cmdMovieScreen`: GetMovieName, StartMovie, StopMovie
- `cmdNewsScreen`: GetNews, ShowScrollButton
- `cmdOlnSettings`: GetCurrentTrackLength, GetCurrentTrackTextureHash, GetLapOrTimeData, GetToggleData, GetToggleDefaults, GetTrackData, IsCopsNRobbers, IsSprint, SettingsAccepted
- `cmdOnlineGameRoom`: GetPlayerList, GetTimers, GetTreeID, IsPlayerListUpdated, TreeIntroDone, ViewPlayerProfile
- `cmdOptions`: GetData, ValueChanged
- `cmdPCControllerConfig`: ClearKey, DefaultSettings, HandleLTrigger, HandleRTrigger, InitializeData, OptionHighlighted, OptionSelected, UpdateControllerConfigScreen
- `cmdPCCountrySelect`: CountrySelected, GetCountryList
- `cmdPCCreateAccount`: GetYearInfo, IsCapturingInput
- `cmdPCForgotPassword`: IsCapturingInput
- `cmdPCOnlineLogin`: IsCapturingInput
- `cmdPCOnlinePersona`: CreateNewPersona, GetPersonas, GetToggleItems, IsCapturingInput, PadBack, PersonaSelected
- `cmdPhotoMode`: ForceCameraStop
- `cmdPlayerProfile`: GetConfig
- `cmdPostMission`: IsWinner
- `cmdPostRaceTransition`: OpenSummary
- `cmdVideoCalibration`: ExitScreen, Step1ScreenData, Step2ScreenData, Step3ScreenData

What this means for a replacement UI (Vue): every one of these members is data or an action the UI needs from the game. They are
the interface to stub, or to feed from BeamNG. `EventTranslator.On*` are the events the UI sends back to the game (screen constructed,
enter/exit complete, pad buttons). `loadMovie` appears with computed file names (`overlay + ".swf"`, `this.mClipFile`); movies load other movies at run time.

## ScreenMessageTranslator is a game-to-UI message queue (correction)
`ScreenMessageTranslator.GetStringHash` is NOT text localisation. The root movie polls `GetMessage()` and compares `msg.message`
against `GetStringHash("<ID>")`. Message ids seen (17): ADDLEVEL, COLOUR_CENTRE, DOINTRO, GOTOANDPLAY, GOTOANDSTOP, LEAVE, PLAY, POP,
PUSH, SETPOSITION, SETROTATION, SETSIZE, SETTEXT, SETTEXTUREHASH, SETVISIBLE, STOP, VISIBLE. Message fields used: `message`, `level`,
`mvClip`, then per id `gotoAndPlay`/`gotoAndStop`, `text`, `visible`, `textureHash`, `x`, `y`, `width`, `height`, `angle`.
`GetScreenName(msg.screenId)` gives the movie file name; `GetMovieScreenName`, `IsMessageAvailable`, `EnableProcessing` are the rest.

## Language files (English_*.bin) and the string hash
- Layout: `u32 magic 0x39000, u32 size, u32 count, u32 table offset (0x1c), u32 string base, name`. The table is `count` entries of
  `(u32 hash, u32 offset)`, sorted by hash; the first entry is (0, 0). The strings start 8 bytes after the end of the table (string base + 8):
  with that base every offset lands on the start of a NUL-terminated latin-1 string (1,856 of 1,856 in Frontend, 4,959 of 4,959 in Global). `Apt/apt_data.py` reads them.
  Frontend 1,856, InGame 104, Global 4,959 strings. `English_Subtitles.bin` has a different layout (not decoded).
- The hash is bStringHash: `h = 0xFFFFFFFF; for each byte: h = h * 33 + byte` (32-bit, case as given). Confirmed by 510 hits of
  `hashes_main.txt` against the tables. Only 511 of the 6,919 language hashes have a name in `hashes_main.txt`; the rest need names from elsewhere.

## Widescreen
`MenuBase.UpdateWidescreen` reads `_global.CmdUtil.GetWidescreen()` (object with `.widescreen`, 1 = `MenuBase.RATIO_WIDESCREEN`) and sends each
screen's `leftMover` and `rightMover` clips to frame label `"16x9"` or `"4x3"`. `CmdUtil.GetScreenWidth/GetScreenHeight` also exist.
There is no THINSCREEN string in the scripts or in `nfs.exe`; the game has this one 16x9 / 4x3 switch, and `nfs.exe` also has
`UpdateWidescreen` and `IsWidescreen` as strings.

## Engine map from nfs.exe (Apt/beamng/engine_map.py -> apt_engine_map.json)
`python Apt/beamng/engine_map.py nfs.exe <UI folder> Apt/beamng/apt_engine_map.json` (needs `pip install pefile capstone`).
Result: 272 member registrations in 61 registrar functions; 73 script-side `_global` objects; 236 of the 272 script-used member names
are found with their handler address. 36 are not: 10 exist as strings in the exe but use another registration shape
(`GetTrackData`, `GetToggleDefaults`, `IsSprint`, `IsCopsNRobbers`, `GetCurrentTrackLength`, `GetMovieName`, `IsHighDef`, `PersonaSelected`,
`Step3ScreenData`, `Platform`) and 26 are not in the exe at all (`gTransMan.gotoScreen`, `gVKB.finish`, `gCT.register` are script-side;
others such as `CmdUtil.GetScreenWidth/GetScreenHeight`, `CmdFELanguage.GetLocaleText` may use another native or be guarded in the scripts).
5 names are registered on two objects (`GetCarList`, `CarHighlighted`, `OnPadStart`) and are listed as ambiguous.

How a native is built in the exe (Undercover PC build):
`push handler; call 0x9ADAB0 (new native function); push eax; push "Name"; mov ecx, object; call 0x9ADBC0 (set member)`.
Handlers are small wrappers around game code. Helpers they use: `0x805B90` int argument i, `0x805F50` string argument i, `0x9AD9D0` raw argument i,
`0x9ADA70(type)` new result object (types seen 1, 2, 5, 9 (tree data), 32 (message)), property setters `0x805DD0/0x805C80/0x805E50` (name pushed before the call)
and `0x805CF0` (string), `0x9ADA20` return undefined, `0x805FB0` return bool (confirmed by IsWidescreen), `0x805FC0` and `0x805FD0` return other plain
values (type not confirmed). The map records, per member: argument kinds and indexes, result kind, the property names set on the result object
(241 names, e.g. `GetWidescreen` returns an object with `widescreen`), strings it references and how many game functions it calls.
111 handlers show no result kind: they return through the game (event posting) or another helper not classified yet.

Screens are looked up through a manager (`[0x12AA044]` with function `0x5391D0(id)`), e.g. `GetIsScreenDirty`, `GetTreeData` read fields of that
screen object (`+0x80` dirty, `+0x84` tree-data flag). Handlers that post commands to the game call `0x535B80`.
`CmdAudio.PlaySound` takes one argument (`any@0`): the UI sound ids in the scripts are SOUND_ACCEPT, SOUND_UP, SOUND_DOWN, SOUND_LEFT, SOUND_RIGHT,
SOUND_RIGHT_DISABLED, SOUND_ZOOM_IN_START/STOP, SOUND_ZOOM_OUT_START/STOP, plus a `soundId` variable passed in by the caller.

## Attrib data (UndercoverVLT_Dumper, pure Python, no VaultLib) and names
The dump (`undercover_global_dump.json`, 171 classes, 15,050 collections, 491 types) was made by a Python reader of the four GLOBAL vaults
written outside this repo; treat it as research input. Names: the VLT key hash is lookup2 with seed 0xABCDEF00 (VaultLib Vlt32Hasher);
the BIN key hash (and language hash) is bStringHash. With `hashes_main.txt` all 164 hashed class names, 2,703 of 2,720 field names and
3,105 of 3,664 collection names resolve (`engine_map.load_names`). Classes the UI reads include `frontend`, `frontend_sfx`, `fe_hints_tips`,
`fe_global`, `car_list`, `PresetRide`, `car_part`, `car_parts`, `car_kit`, `car_paint_group`, `fecooling`, `career_constants`, `gameplay`.

Where the natives get their data (engine_map.py follows 3 levels of direct calls from each handler and looks for name hashes; a hit means
"the code behind it reaches this attrib name", not a proven read):
- `CmdAudio.PlaySound`: frontend_sfx; frontend_sfx.SoundMapping
- `CmdFEDebugCarTestScreen.ScrollNextBodyKitNumber`: car_part
- `CmdFEDebugCarTestScreen.ScrollNextBodyKitType`: car_kit, car_part, car_parts
- `CmdFEDebugCarTestScreen.ScrollNextCar`: PresetRide, car_list, car_paint_group, pvehicle; PresetRide.Autosculpt, PresetRide.CarType, PresetRide.DecalLayers, PresetRide.Livery, PresetRide.Paints
- `CmdFEDebugCarTestScreen.ScrollNextCarSlot`: car_part, car_parts
- `CmdFEDebugCarTestScreen.ScrollNextKit`: car_part
- `CmdFEDebugCarTestScreen.ScrollNextPart`: car_part, car_parts
- `CmdFEDebugCarTestScreen.ScrollNextPresetRide`: PresetRide, pvehicle; PresetRide.CarType, car_list.Cars
- `CmdFEDebugCarTestScreen.ScrollPrevBodyKitType`: car_kit, car_part, car_parts
- `CmdFEDebugCarTestScreen.ScrollPrevCar`: PresetRide, car_list, car_paint_group, pvehicle; PresetRide.Autosculpt, PresetRide.CarType, PresetRide.DecalLayers, PresetRide.Livery, PresetRide.Paints
- `CmdFEDebugCarTestScreen.ScrollPrevCarSlot`: car_part, car_parts
- `CmdFEDebugCarTestScreen.ScrollPrevKit`: car_part
- `CmdFEDebugCarTestScreen.ScrollPrevPart`: car_part, car_parts
- `CmdFEDebugCarTestScreen.ScrollPrevPresetRide`: PresetRide, pvehicle; PresetRide.CarType, car_list.Cars
- `CmdLauncher.StartGame`: PresetRide, car_tuning, pvehicle
- `CmdPauseMenu.ResumeGame`: PresetRide, car_tuning, pvehicle
- `cmdChallenge.GetDataGrid`: frontend_sfx.MovieVolumes
- `cmdChallengeSummary.GetDataGrid`: frontend_sfx.MovieVolumes
- `cmdChyron.GetShowButtonHelp`: career_constants; career_constants.zone_multiplier_max, gameplay.GameMode
- `cmdFEHintsTipsScreen.GetPageData`: fe_hints_tips; fe_hints_tips.content
- `cmdFEHintsTipsScreen.GetToggleData`: fe_hints_tips; fe_hints_tips.content
- `cmdFEPerformanceTuning.GetRealPos`: gameplay; gameplay.BoostValue, gameplay.Children, gameplay.RewardMarkerType
- `cmdFEVisualPaint.CycleGroup`: car_paint_group
- `cmdFEVisualPaint.SelectSlot`: car_paint_group; frontend.part_camera
- `cmdLoadingScreen.GetEventData`: frontend_sfx.MovieVolumes
- `cmdMovieScreen.StartMovie`: frontend_sfx; frontend_sfx.MovieVolumes
- `cmdOlnSettings.GetCurrentTrackTextureHash`: frontend_sfx.MovieVolumes

UI sounds: `CmdAudio.PlaySound` reaches `frontend_sfx.SoundMapping` (280 entries: `fe_message` hash -> `sound_trigger` index) and the fields
`sound_accept`, `sound_back`, `sound_scroll_*` ... (type `FEMsgToSoundTrigger`); `MovieVolumes` (per movie volume) is reached by `StartMovie`,
`GetEventData`, `GetDataGrid`. The trigger index is the number the sound system maps to an event; the bnk order list you have is the candidate for that.
Hints and tips: `fe_hints_tips.content` items have `picture` (a texture name; the script's `TextureHash` is its bStringHash), and `subject_hal_id` /
`text_hal_id` which are bStringHash keys into the English tables (110 of 110 found). (The earlier mid-string offsets came from using the table end as the string base; the base is 8 bytes later.)

## Runtime-bound images (Apt/beamng/runtime_images.py)
Unnamed images in apt_report.json (id 65535, "(no name entry)") are placeholders; scripts bind them with `clip.TextureHash = hash`.
`runtime_images.py` lists 123 placeholder images in 30 movies and 104 `.TextureHash =` sites in the decompiled scripts, with the source:
the game message SETTEXTUREHASH (the root handler `mc.TextureHash = msg.textureHash`, in most movies), a provider call, or a script value
(for example `mLogo.TextureHash = hashKey`). It also writes `runtime_texture_names.json`: texture names the attrib data can hand to the UI with
their bStringHash (55 hints pictures, 19 event icon types, 4 icon models, 1 lens flare texture).

## Viewer: VM fixes and the engine feed (apt_vm.py, apt_runtime.py, apt_data.py, apt_engine.py)
The VM lacked 10 opcodes the movies use and failed on 9 of 150 movies. Now all 150 run headless; opcode errors went from 124 (120 of them `extends`) to 0.
- `extends` links `Sub.prototype` to a new object whose prototype is `Super.prototype` and whose `constructor` is the superclass; `super` (the preloaded
  register 0x10) is a proxy: calling it runs the superclass constructor, `super.name()` the superclass method, both on the same `this`. If the superclass is
  not defined in the movie (MenuBase lives in another movie) it is noted and listed in `vm.externals`, as before.
- `implementsOp` (interfaces are not kept), `castOp` (the object, unchecked), `getTimer` (ticks at an assumed 30 per second, `TICKS_PER_SECOND`),
  `getProperty`/`setProperty` (the 22 `_x` .. `_ymouse` names), `enumerate2` (own and inherited keys, no `length`), `stopSounds`, `removeSprite`, `newMethod`.
- `defineLocal2` pops only the name (it popped two before: the cause of the `IndexError`s). A stray `pop` on an empty stack is tolerated and noted: the compiler
  emits one after `if (!x) { x = function ... }` and the game ignores it.

Engine feed: `python apt_view.py <UI> --res ... --engine-map beamng/apt_engine_map.json --lang <folder with English_*.bin> --attrib undercover_global_dump.json
--names hashes_main.txt [--hints-category hints_career] [--4x3]`. Every native of the engine map that has a member proven in nfs.exe is installed in `_global`
under its object name (script-defined names such as gCT and gTransMan are not touched). A call is listed in `vm.externals` and answers, in order: your stub in
`apt_stubs.json` for `_global.Obj.Member()`, a provider, or undefined. Providers so far (`apt_engine.py`): `CmdUtil.GetWidescreen` (viewer option),
`cmdFEHintsTipsScreen.GetToggleData` and `.GetPageData(id)` (real text from attrib `fe_hints_tips` + the English tables; checked end to end on hintstips.big:
option 1 of hints_career gives the GPS Map text). Assumed, marked ASSUMED in the code: the hints category in use and ID = item index; `hash`/`imageHash` of
`GetPageData` are not produced yet (the exe builds them from the picture name with a `%s%d` format).

### Data folder, dependencies, tools
Put the data in `Apt/engine_data/` (README.txt there lists it; the folder is gitignored except the README), or use `--engine-data DIR`:
`English_Global.bin`, `English_Frontend.bin`, `English_InGame.bin`, `undercover_global_dump.json`, `hashes_main.txt`; `apt_engine_map.json` ships in `Apt/beamng/`
(a copy in the data folder wins); `nfs.exe` is only needed to remake the map. The viewer loads the folder by itself at start-up (`apt_view.py` prints an
"engine feed" table: found or MISSING per item) and runs without any of it. Flags: `--engine-data`, `--engine-map/--lang/--attrib/--names` (single files),
`--no-engine`, `--hints-category`, `--4x3`. Python modules needed only by the engine-map tool: `pefile`, `capstone`.
`python Apt/apt_tools.py check` shows what is found with counts; `census --ui <UI>` runs every movie headless and lists the natives the screens call on their own
and which are fed; `engine-map --ui <UI>` remakes the map from nfs.exe; `runtime-images --ui <UI>`; `decompile <movie.big>`.

What the screens do by themselves (census, 250 ticks, 150 movies): they call `EventTranslator.OnScreenConstructed` (90 movies) and `OnEnterComplete` (85),
`CmdUtil.GetWidescreen` (50, fed); the data natives (`CmdFECarLot.GetCarList/GetRegions`, `cmdChallenge.*`, `cmdPCCountrySelect.GetCountryList`,
`cmdLanguageSelect.GetLanguageList`, `cmdFEDialogScreen.GetDialogConfig`, `cmdNewsScreen.GetNews`, `cmdLoadingScreen.GetEventData`, `cmdFEMyCars.GetCarList`,
`CmdHud.GetPlayerCarInfo`, `cmdMessenger.GetMessengerConfig`, `cmdChyron.GetChyronInfo`, error message natives ...) are the providers still to write. The lifecycle
(constructor, InitGUI, OnEnterComplete) is driven by the movie's own frame scripts as it plays; no scenario is needed.
Message queue: `ScreenMessageTranslator.IsMessageAvailable/GetMessage/GetStringHash` are fed by `Engine.post_message` and the scenario op
`{"at": tick, "op": "message", "message": "SETVISIBLE", "level": 1, "mvClip": "mcX", "visible": 0}` (`texture` is sent as `textureHash`); checked: the id equals
`GetStringHash("SETVISIBLE")` and the screen's `MenuBaseMessageHandler` accepts it. Widescreen: `MenuBase.UpdateWidescreen` runs by itself and reads the fed
`GetWidescreen`; checked on dialog.big: `leftMover` ends on frame 9 for 16x9 and frame 4 for 4x3 (the option is `--4x3`). Natives the exe registers on two objects
(`GetCarList`, `CarHighlighted`, `OnPadStart`) are installed unfed so calls are recorded. `gotoAndPlay(undefined)` (NaN) is ignored, as in Flash.

## nfs.exe reference file and decoded tables
`Apt/beamng/EXE_ADDRESSES.md` (and `apt_exe_addresses.json`, remake with `python apt_tools.py exe-addresses`) lists every exe address the apt UI depends on: the helper
functions the natives are built from (with how sure each role is), the `_global` object creation (function 0x80F680 and the constructors), all 272 natives
by registrar with their handler address, the data tables the natives walk, the game functions the handlers call most, and one signature line per native.
Corrections made while building it: `0x805F50` is *return string* (it was listed as an argument reader); the three 36-byte tables reached from
`GetErrorMessage` hold results-table column titles (language hashes resolving to "Cost to State", "Time", "# of Busts", "Car"), not error text.
Tables found (all read from nfs.exe data): resolutions (13: width, height, language hash of "800x480" ...), languages (19: id, flags, "ENGLISH", "EN", pointer),
screens (95: id, movie name, handler; 92 names are UI movies), result columns (A 4, B 3, C 10), car slots (115: index, name like BADGING_BUMPER_SET_FRONT, x) and a 254 x 20 byte
slot table (not decoded). `GetScreenName` is fed from the screens table (`apt_screen_names.json`, made by engine_map.py next to the map): screen id to movie name,
the root loads name + ".swf" into OVERLAY_<level>; `GetMovieScreenName` is not in the exe (script side).

Viewer fixes found while following the root movie: a plain function call runs with the timeline as `this` (it ran with nothing, so `this.onEnterFrame = ...` inside
CallMainWhenPermPreloadsDone did nothing); `createEmptyMovieClip` exists (the root builds its update manager on an empty clip); `gotoAndPlay(undefined)` is ignored.

## Open items
- Engine map: classify the remaining return helpers, resolve the 10 members with another registration shape, and read the handlers that feed attrib data (needs the attrib dump).
- Attrib: confirm the provider -> class links by reading the handlers (the trace only shows reachable hashes); decode the exe's PlaySound lookup.
- Language: decode English_Subtitles.bin.
- Viewer, blocking: the VM has no built-in `Array` (`new Array()`, `.concat`, `.push`, `.length` are listed as externals), so `UpdateEventManager.updateEvent` in the root cannot
  copy its listener list and never calls `gMySM.onUpdate`: the message loop (`IsMessageAvailable`/`GetMessage`/`GetScreenName`/`loadMovie`) never runs. Next: built-in Array, then Object/String methods the movies use.
- Viewer: the data providers the screens call (see the census): language and resolution lists (tables above; the element shape of `languageArr`/`Data` is not decoded), car lot and my cars lists
  (attrib `car_list`/`PresetRide`), challenge natives, country list, news, dialog config, loading event data, messenger, chyron info, HUD car info.
- Viewer: `loadMovie` of the screen the message names, runtime texture binding by hash in the renderer (`runtime_images.py` lists them; the VM keeps `TextureHash`), GUI checks (not run here: no display), FNG for Undercover (separate conversation).
- Exe: result of the unknown helpers (`0x805FC0`, `0x805FD0`), the setter types (`0x805C80`, `0x805E50`), the 254-entry slot table, `GetSlotInfo` (0x87B030 is called by 78 handlers), the 36 members that are not in the exe or use another registration shape.
- `else { if ... }` is not flattened to `else if` (cosmetic).
- `break` out of a `for-in` does not emit the stack cleanup.
