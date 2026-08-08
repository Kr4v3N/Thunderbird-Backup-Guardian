# 🆘 RESTORE GUIDE — ALL SCENARIOS

*[🇫🇷 Lire en français](DISASTER_RECOVERY.fr.md)*

This document covers restoring the Thunderbird profile from the restic
repository, from the simplest case (a mistake) to the worst case (dead
PC, reinstalled OS, recovery disk on an unfamiliar machine).

**Safety principle in every case**: a restore always writes to a separate
folder (`~/<profile>-restored-<date>`, where `<profile>` is your
Thunderbird profile folder's name — `.thunderbird` by default, unless you
customized `TB_SOURCE_DIR`), never directly onto `~/<profile>`. Nothing is
overwritten until you've checked the result and done the switch-over
yourself.

---

## Scenario 1 — Mistake / accidental deletion (common case)

The current PC works fine, the backup disk is plugged in, you just want
to roll back (deleted an e-mail by mistake, a corrupted folder...).

```bash
cd "/run/media/$USER/<DISK_NAME>/Backup Thunderbird"
bash RESTORE_EMERGENCY.sh
```

The script:
1. Asks for the restic password (system keyring, Proton Pass, or memory)
2. Lists the available backups (`restic snapshots`)
3. Lets you pick which one to restore (empty = most recent)
4. Restores to `~/<profile>-restored-<date>` — never overwriting the active profile

To recover just one specific item (a folder, a file) without restoring
everything:
```bash
export RESTIC_PASSWORD='your_password'
restic -r "/run/media/$USER/<DISK_NAME>/Backup Thunderbird/restic-repo" \
  restore latest --target /tmp/partial_restore \
  --include "*.thunderbird/ImapMail/folder_name*"
```

To browse a backup's content without extracting anything (read-only FUSE
mount):
```bash
mkdir -p /tmp/tb-browse
restic -r "/run/media/$USER/<DISK_NAME>/Backup Thunderbird/restic-repo" mount /tmp/tb-browse
# browse /tmp/tb-browse/snapshots/... then Ctrl+C to unmount
```

---

## Scenario 2 — Total disaster: dead PC, reinstalled OS

### Step 0 — Before you start: the password

Without the restic repository password, **no recovery is possible** —
this is real AES-256 encryption, no backdoor. Look for it in this order:
1. **Proton Pass** (mobile or web app, if you set it up) — survives the
   PC dying since it's synced to Proton's servers
2. A paper backup, if you made one
3. Your memory

If none of the three work, stop here: neither I nor anyone else can
decrypt the repository without this password.

### Step 1 — Reinstall the required packages

Only `restic` is a real prerequisite to restore the data — the restore
itself (locating the disk, extracting files) never touches Thunderbird.
**Thunderbird is only needed at the very last step**, to reopen the
profile once restored — you can install it before, after, or while the
restore is running, install order has no technical importance.

If you have internet access on the new install:
```bash
sudo apt update
sudo apt install restic thunderbird
```

If you **don't** have internet access (or `apt install restic` fails),
the official restic binary is already on the backup disk — see Scenario 3.
Thunderbird itself can wait until you have internet again if needed,
since it's only required at the end.

### Step 2 — Plug in the disk and locate it

The automatic mount point varies depending on the Ubuntu version/desktop
environment (KDE usually mounts under `/run/media/`, others under
`/media/`):
```bash
ls /run/media/$USER/ 2>/dev/null
ls /media/$USER/ 2>/dev/null
```
Look for the `Backup Thunderbird` folder on the `<DISK_NAME>` disk.

### Step 3 — Restore

```bash
cd "/path/found/in/step/2/Backup Thunderbird"
bash RESTORE_EMERGENCY.sh
```

The script detects on its own whether `restic` is missing from the
system and automatically falls back to the bundled binary
(`restic-bin/restic` next to the script) — no extra action needed on
your part.

### Step 4 — Check before switching over

The files are in `~/<profile>-restored-<date>`. Check that it looks
complete (size consistent with your usual backups, mail folders present)
before continuing.

### Step 5 — Activate the restored profile

```bash
pkill -x thunderbird 2>/dev/null || true
mv ~/<profile>-restored-<date>/<profile> ~/<profile>
thunderbird &
```

The new install's Linux username can be different from the original one
— no problem, the whole process relies on `$HOME` dynamically, and the
backup itself contains no absolute path or username (see
`thunderbird_guardian.py::do_backup`).

---

## Scenario 3 — `restic` not found AND the bundled binary is missing/corrupted

Download the official static binary (no system dependency, runs on any
x86-64 Linux distribution) **with checksum verification before use** —
never run a downloaded binary without checking that it exactly matches
what restic actually published:

```bash
API=$(curl -s https://api.github.com/repos/restic/restic/releases/latest)
BIN_URL=$(echo "$API" | grep browser_download_url | grep "linux_amd64.bz2" | cut -d '"' -f4)
SUMS_URL=$(echo "$API" | grep browser_download_url | grep '"SHA256SUMS"' | cut -d '"' -f4)

curl -sL -o restic.bz2 "$BIN_URL"
curl -sL -o SHA256SUMS "$SUMS_URL"

EXPECTED=$(grep "linux_amd64.bz2" SHA256SUMS | awk '{print $1}')
ACTUAL=$(sha256sum restic.bz2 | awk '{print $1}')

if [ "$EXPECTED" != "$ACTUAL" ]; then
    echo "❌ Invalid checksum — file is corrupted or compromised, DO NOT USE"
    exit 1
fi
echo "✅ Checksum verified: $ACTUAL"

bunzip2 restic.bz2
chmod +x restic
sudo mv restic /usr/local/bin/restic
```

Requires internet access on the recovery machine. The script only uses
`curl`/`grep`/`awk`/`sha256sum` (no dependency on `jq` or any other tool
that might be missing on a fresh install).

---

## Scenario 4 — The backup disk shows signs of failure

If the disk has read errors (unusual slowdowns, `Input/output error`):
**don't run any write operation on it** (no backup, no `restic
forget`/`prune`). First make a bit-for-bit image onto a healthy disk
before trying anything else:

```bash
sudo ddrescue -d -r3 /dev/sdX /path/to/new/disk/image.img /path/to/mapfile.log
```

Then work from the copy, never on the original failing disk.

---

## Scenario 5 — Checking the repository's health without restoring

```bash
export RESTIC_PASSWORD='your_password'
restic -r "/run/media/$USER/<DISK_NAME>/Backup Thunderbird/restic-repo" snapshots
restic -r "/run/media/$USER/<DISK_NAME>/Backup Thunderbird/restic-repo" check --read-data
```
Equivalent to `python3 thunderbird_guardian.py --verify` if the project's
venv is available on the machine.

---

## What CANNOT happen to this backup

- **Forgotten password**: this isn't a bug to fix — it's the intended
  behavior of real AES-256 encryption. The only protection is keeping a
  durable copy of the password (see Scenario 2, step 0).
- **Change of Linux username or machine**: no impact, everything relies
  on `$HOME` and the external disk, never on data specific to the
  original install.
