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

## Install (Pop!_OS)

```bash
sudo apt install python3-venv libxcb-cursor0
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
