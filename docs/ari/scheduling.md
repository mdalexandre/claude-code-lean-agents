# Scheduling the nightly run (examples only)

ARI never installs or enables a schedule. These are examples you can adapt and install yourself. Each runs one read only pass over your transcripts and writes only to the state directory you choose. Replace `/path/to/claude-code-lean-agents` and the state directory.

Since 2.0 the nightly run reads every transcript, including files written in the last 30 minutes (`--quiet-seconds 1800` only marks them active in the summary). Only complete lines are read, and each file's checkpoint is compared with the previous run: a day touched by a truncated or replaced file is recomputed and draws no lesson until a later run sees it stable, while an open session no longer holds back lessons for other days. Running it twice in one night adds no snapshots or lessons (they are keyed by day and content); each run appends one line to the run log in `state.json`. A run interrupted part way leaves a journal file; the next run reports `recovered_from` and ends in the same state as a clean run.

Create the state directory once before installing any of these, because the log redirections below need it: `mkdir -p "$HOME/.ari-state"` (on Windows, `mkdir %USERPROFILE%\.ari-state`).

## macOS launchd

Save as `~/Library/LaunchAgents/com.example.ari-nightly.plist`, then load it yourself with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.ari-nightly.plist`.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.ari-nightly</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/claude-code-lean-agents/scripts/ari_nightly.py</string>
    <string>run</string>
    <string>--state-dir</string>
    <string>/Users/YOU/.ari-state</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>15</integer></dict>
  <key>StandardOutPath</key><string>/Users/YOU/.ari-state/nightly.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/.ari-state/nightly.err</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
</dict>
</plist>
```

## Linux systemd user timer

`~/.config/systemd/user/ari-nightly.service`:

```ini
[Unit]
Description=ARI nightly analysis (read only over transcripts)

[Service]
Type=oneshot
Nice=10
MemoryHigh=2G
MemoryMax=4G
ExecStart=/usr/bin/python3 /path/to/claude-code-lean-agents/scripts/ari_nightly.py run --state-dir %h/.ari-state
```

`~/.config/systemd/user/ari-nightly.timer`:

```ini
[Unit]
Description=Run ARI nightly at 03:15

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it yourself with `systemctl --user enable --now ari-nightly.timer`.

## cron

```
15 3 * * * /usr/bin/python3 /path/to/claude-code-lean-agents/scripts/ari_nightly.py run --state-dir "$HOME/.ari-state" >> "$HOME/.ari-state/nightly.log" 2>&1
```

## Windows Task Scheduler

```
schtasks /Create /SC DAILY /ST 03:15 /TN ARI-nightly /TR "py -3 C:\path\to\claude-code-lean-agents\scripts\ari_nightly.py run --state-dir %USERPROFILE%\.ari-state"
```
