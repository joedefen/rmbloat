# Running rmbloat as a Daemon

## Why tmux?

`rmbloat` allows one instance at a time, and large collections can take hours or days to convert. Running it under tmux lets you detach, reconnect, and survive SSH disconnections without interrupting the work.

## The rmbloatd Wrapper

`rmbloatd` manages a persistent tmux session:

```bash
rmbloatd start
rmbloatd start -- --auto-hr 8 /path/to/videos
rmbloatd attach
rmbloatd status
rmbloatd stop
```

Only `start` accepts arguments; use `attach` to reconnect and `stop` to terminate.

## Auto Mode

`--auto-hr` runs unattended for the given number of hours, selecting and converting files automatically. It stops when the time expires or the queue drains, and uses saved defaults for paths.

```bash
rmbloatd start -- --auto-hr 48
```

## Scheduled Runs with Cron

Cron uses a minimal `PATH`, so either set it in the crontab or call `rmbloatd` by absolute path.

```bash
crontab -e

PATH=/home/yourusername/.local/bin:/usr/local/bin:/usr/bin:/bin

# Friday at 23:00 for 48 hours
0 23 * * 5 rmbloatd start -- --auto-hr 48
# Nightly at 02:00 for 6 hours
0 2 * * * rmbloatd start -- --auto-hr 6
# Stop at 08:00
0 8 * * * rmbloatd stop
```

Save collection paths first with `rmbloat --save-defaults /path/to/videos`. If `rmbloat` is already running, the single-instance lock makes a new `start` fail harmlessly, so overlapping cron entries are safe.

## Using systemd (Alternative)

For systemd, create a oneshot service and a timer in `/etc/systemd/system/`:

```ini
# rmbloat-maintenance.service
[Unit]
Description=rmbloat maintenance

[Service]
Type=oneshot
User=your-username
ExecStart=/usr/local/bin/rmbloatd start -- --auto-hr 6
```

```ini
# rmbloat-maintenance.timer
[Unit]
Description=Run rmbloat nightly

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now rmbloat-maintenance.timer
```
