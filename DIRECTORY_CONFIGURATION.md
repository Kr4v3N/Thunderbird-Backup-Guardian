# 🎯 BACKUP DIRECTORY CONFIGURATION

## How the script picks its destination

`resolve_dest_dir()` in `thunderbird_guardian.py` follows a simple,
deliberately unmagical logic:

1. **`TB_BACKUP_DIR` environment variable**, if set, used as-is, no
   exceptions.
2. **Fallback**: `~/thunderbird_backups`, if `TB_BACKUP_DIR` isn't set.

The script does **not** try to guess an external disk's name: a disk
name is specific to each install, there's nothing to generalize. If you
back up to an external disk, set `TB_BACKUP_DIR` explicitly.

**Symbolic links in the path.** The destination is opened one directory at
a time, and a symbolic link along the way is followed only if it is
stored on `/` or on your home's filesystem. A link you made in your home
(for example `~/thunderbird_backups -> /run/media/$USER/<DISK>/Backup
Thunderbird`) works as before. A link stored on the backup disk itself,
or on any other mounted filesystem, makes the run fail with `Refusing to
follow the symbolic link ...`: anyone who can write to the disk could
plant one to redirect the backup's writes into your home. Use the real
directory in `TB_BACKUP_DIR` instead.

## Checking where the script currently backs up to

```bash
.venv/bin/python3 thunderbird_guardian.py
```
Look for this line in the logs:
```
✅ Destination: /path/to/directory
```

## Configuring an external disk as the destination

The automatic mount point for an external disk varies depending on the
desktop environment (KDE usually mounts under `/run/media/`, others
under `/media/`), check yours:
```bash
ls /run/media/$USER/ 2>/dev/null
ls /media/$USER/ 2>/dev/null
```

### Temporary (current session)
```bash
export TB_BACKUP_DIR="/media/$USER/<DISK_NAME>/Backup Thunderbird"
.venv/bin/python3 thunderbird_guardian.py
```

### Permanent
```bash
echo 'export TB_BACKUP_DIR="/media/$USER/<DISK_NAME>/Backup Thunderbird"' >> ~/.bashrc
source ~/.bashrc
```

### For CRON
```bash
crontab -e
```
```
TB_BACKUP_DIR="/media/$USER/<DISK_NAME>/Backup Thunderbird"
40 18 * * * export XDG_RUNTIME_DIR=/run/user/$(id -u) && export $(systemctl --user show-environment | grep -E '^(DISPLAY|WAYLAND_DISPLAY|XAUTHORITY|DBUS_SESSION_BUS_ADDRESS)=') && cd ~/PycharmProjects/Backup-Thunderbird && .venv/bin/python3 thunderbird_guardian.py
```
A variable set at the top of the crontab applies to every line below it. This is
necessary here since cron doesn't load `~/.bashrc`.

## 🐛 TROUBLESHOOTING

### The disk doesn't exist?
```bash
ls -la "/media/$USER/<DISK_NAME>/Backup Thunderbird"
mkdir -p "/media/$USER/<DISK_NAME>/Backup Thunderbird"
chmod 700 "/media/$USER/<DISK_NAME>/Backup Thunderbird"
```

### The script waits for the disk then gives up

This is intentional: if the disk isn't mounted at cron time, the script
automatically retries (`TB_MOUNT_RETRY_ATTEMPTS` / `TB_MOUNT_RETRY_DELAY`,
30 min of margin by default) before sending a failure alert. Check that
the disk is actually plugged in and mounted before the scheduled time, or
increase `TB_MOUNT_RETRY_DELAY`/`TB_MOUNT_RETRY_ATTEMPTS`.

### `keyring.errors.NoKeyringError` under cron

Not a directory/mount issue: the crontab line above is missing
`XDG_RUNTIME_DIR` (or exports it after, not before, the
`systemctl --user show-environment` call). See the full explanation in
[README.md](README.md#deploying-to-production) / [README.fr.md](README.fr.md#déploiement-en-production), step 6.

### Final check
```bash
test -d "/media/$USER/<DISK_NAME>/Backup Thunderbird" && echo "✅ OK" || echo "❌ MISSING"
test -w "/media/$USER/<DISK_NAME>/Backup Thunderbird" && echo "✅ WRITABLE" || echo "❌ NO PERMISSION"

export TB_LOG_LEVEL=DEBUG
.venv/bin/python3 thunderbird_guardian.py
```
