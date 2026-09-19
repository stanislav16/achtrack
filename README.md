# AchTrack

Steam achievements, Steam-style unlock popups and playtime tracking for games that run on a Steam
emulator (Goldberg / GBE, CODEX, RUNE, EMPRESS, OnlineFix, SKIDROW, 3DM) under Wine or Proton on Linux.

- **Same achievements as Steam**: names, descriptions, icons and hidden flags come from the Steam Web
  API, and global unlock percentages come from Steam too. Anything under 10% counts as rare.
- **Unlock popups**: a slide-in toast in the corner of the screen with the icon, name, description,
  rarity and a chime. Rare achievements get a gold version.
- **Playtime**: sessions are counted while the game process runs, shown in hours like Steam.
- **Library window**: every game shows playtime, last played, a progress bar and the full achievement
  list (unlock dates, rarity, progress bars, hidden achievements collapsed).

## Your Steam library, too

With your Steam Web API key set, AchTrack copies the **Steam games you've played, their Steam playtime
and every Steam achievement with its real unlock date** into its own database. It does this at startup
and every 15 minutes, or on demand with **Library → Sync Steam library now** or `achtrack sync-steam`.
The data lives on your disk, so you keep it even if something changes on Steam's side.

- Your Steam account is detected from the Steam client on this PC. If that fails, paste your profile
  URL under **Settings → Steam account**.
- Steam only shares achievements if your profile's **Game details** are set to *Public* (Steam →
  Profile → Edit Profile → Privacy Settings). Playtime may still come through when they're private.
  The last sync's result, including any error, is shown in **Settings** under *Steam account*.
- Steam achievements are imported quietly, because Steam already showed its own popup. Games owned
  but never played are left out.

## Achievements from save files

Most games store your progress, such as bosses beaten, current chapter and items collected, in their
save file. AchTrack reads saves in common engine formats and compares each new save with the previous
one. When a change fits an achievement's description, the achievement unlocks with the normal popup.
This runs locally with no AI, and only reads saves when they change on disk.

- **Formats:** Unreal Engine saves (`.sav`, including compressed ones), JSON, XML, INI,
  .NET BinaryFormatter (common in Unity games), BSON, MessagePack, plist, SQLite, RPG Maker MV/MZ,
  base64-wrapped and zlib/gzip-compressed saves, and Unity settings (Wine registry or
  `~/.config/unity3d`). Encrypted or custom binary saves
  can't be read. The game page then says so, and you use the review instead.
- **Finding saves:** no setup. While a game runs, AchTrack notices which files in its Wine prefix
  (AppData, Documents, Saved Games) or its install folder's save folders it writes.
- **Matching:** "Collect 50 moonstones" ↔ `Moonstones = 50`, "Slay the Soul of Cinder" ↔
  `bSoulOfCinderDefeated = true`, "Complete Chapter 5" ↔ `CurrentChapter = 6`, "Stagger an enemy" ↔
  `StaggerCount = 1`. Counters also show as progress bars (23/50). Limits and settings (`MaxLevel`,
  `MoonstonesRequired`, volume, positions, timers) are ignored.
- **Safety:** only a clear change seen *during play* unlocks by itself. Weaker matches, and anything
  already in a save the first time AchTrack reads it, are **pre-ticked in the review** instead.
  Right-click a save unlock → *Not earned* to undo it; that field won't be matched to that
  achievement again.
- **Checking what it sees:** `achtrack saves` lists the save files it reads and what matched.
  `achtrack saves <appid> --fields` also prints every value it read.

## Screen watcher (for games with unreadable saves)

Some games encrypt their saves or use a custom format without names (FF7 Rebirth, for example).
For those, AchTrack can read on-screen text while you play. It runs locally with tesseract OCR,
with no AI and nothing uploaded:

- Every 2.5 s it grabs five small areas of the screen (top banner, center, bottom boss-bar area,
  left and right notifications) and reads them in a single low-priority tesseract call.
- **Direct text:** "STAGGERED" → "Stagger an enemy", "CHAPTER 6" → "Complete Chapter 5",
  "Moonstones 50/50" → "Collect 50 moonstones". A hit has to be seen twice before it counts.
  Quest-log lines that just repeat the objective ("Defeat Iudex Gundyr") are ignored.
- **Bosses:** a named target on screen, then a victory banner with no death screen in between,
  counts as a defeat.
- **Learning:** when you confirm an achievement (review or right-click), it looks back at what was
  on screen and learns that game's victory banner. From then on, that game's boss kills unlock
  automatically instead of being suggested.
- Clear hits unlock with a popup, and uncertain ones are pre-ticked in the review. Right-click →
  *Not earned* undoes a hit and stops that text from matching again.

Setup: `sudo apt install tesseract-ocr`. To check what it reads on your screen, start a game and
run `achtrack screen-test`. It lists every capture method on your desktop and whether it works.
On X11 it captures directly. On Wayland it uses the desktop's screenshot tool, whichever is present:
`grim` (wlroots), `cosmic-screenshot` (COSMIC), `gnome-screenshot` (GNOME) or `spectacle` (KDE).

## When a crack doesn't record achievements

Some cracks never save achievements. For example, some RUNE releases leave `achievements.ini` at
`Count=0`. No tracker can see those unlocks, so AchTrack helps you add them yourself:

- **Post-session review:** when you quit such a game, AchTrack asks *"Did you earn any of these?"*.
  It lists the locked achievements, most likely first (based on how many Steam players have each one
  and how long you've played). Hidden achievements are included, with their real description where
  Steam's public achievement page has it. Tick what you did and they unlock with the normal popup.
  Once the crack records an achievement itself, the question stops. You can change this per game by
  right-clicking the game → **After a session**.
- **Mark as unlocked:** right-click any achievement → *Mark as unlocked* (now or on a date you pick).
  Manual unlocks are labelled *Added by you* and can be undone.
- **Search** the achievement list from the game page.

## Install (Pop!_OS)

```bash
sudo apt install python3-venv libxcb-cursor0 tesseract-ocr
./install.sh --autostart
```

This installs into `~/.local/share/achtrack/venv`, puts `achtrack` in `~/.local/bin`, adds a menu
entry and (with `--autostart`) starts it in the background at login. Run `./install.sh` again to update.
`./uninstall.sh` removes it and keeps your data; `./uninstall.sh --purge` deletes the data too.

### First run

1. Open **AchTrack**, go to **Library → Settings** and paste a **Steam Web API key**. Keys are free
   at <https://steamcommunity.com/dev/apikey> (any domain name works). Without a key you still get
   popups and playtime, but achievements only show their internal names.
2. Click **Test popup** to check the popups show up.
3. Start a game. It appears in the library, and new unlocks pop up as they happen.

Achievements you earned before AchTrack was running are imported quietly without popups.

## Heroic Games Launcher

Heroic works without setup, whether it's the Flatpak or native version, and with Wine or Proton:

- Each game's prefix is read from Heroic's config (`GamesConfig/*.json`), so custom prefix
  locations are found. `~/Games/Heroic/Prefixes/**` is also searched, including Proton-style
  `pfx/` prefixes.
- For games added through **Add game → Add to library** (sideloaded), AchTrack also watches the
  install folder, for emulators set to save next to the game (`local_save.txt`).
- Playtime starts when the game's process shows up. AchTrack finds the game's AppID from the
  crack's `steam_appid.txt` / `steam_settings/` / `steam_emu.ini` / `OnlineFix.ini` next to
  `steam_api64.dll`.

If a game doesn't start tracking playtime, either:

- in Heroic open the game's **Settings → Advanced → Environment Variables** and add
  `ACHTRACK_APPID` = the game's Steam AppID (the number in its store URL), or
- in AchTrack use **Add a game** (or right-click → **Edit game**) and set its executable name.

Other launchers (Lutris, Bottles, Steam non-Steam shortcuts, plain `~/.wine`) are found
automatically too. Add any other prefix folders under **Settings → Extra folders**.

## Popups over games

Popups are borderless always-on-top windows. They show over windowed and borderless-fullscreen
games. Notes:

- On Wayland (e.g. COSMIC), AchTrack runs through XWayland by default so popups can be placed and
  kept on top. You can turn this off in Settings.
- Popups can't draw over **gamescope** or true exclusive fullscreen. Use borderless/windowed mode,
  or switch **Unlock popups** to *Desktop notification*.
- If you have no system tray, closing the window keeps AchTrack running in the background. Run
  `achtrack` again to open it, and use **Library → Quit** to stop it.

## Command line

```text
achtrack                  open the library (starts tracking)
achtrack --background     start hidden (used by autostart)
achtrack scan             list detected prefixes, achievement files and running games
achtrack list             print the library with playtime and achievement counts
achtrack sync-steam       copy your Steam games, playtime and achievements in now
achtrack saves [appid]    show the save files AchTrack reads and what it matched (--fields for all values)
achtrack screen-test      read the screen like the screen watcher does and print what it sees
achtrack test-popup [--rare]
achtrack quit
```

Start with `achtrack scan` when something isn't detected. Logs are in
`~/.local/share/achtrack/achtrack.log` (add `-v` to also print them in the terminal).

## Where things are stored

| What | Where |
| --- | --- |
| Settings | `~/.config/achtrack/config.json` |
| Library, playtime, unlocks | `~/.local/share/achtrack/achtrack.db` (SQLite) |
| Icons and sounds cache | `~/.cache/achtrack/` |
