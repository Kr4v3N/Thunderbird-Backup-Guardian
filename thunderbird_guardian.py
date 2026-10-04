#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════════
THUNDERBIRD SECURE GUARDIAN v22.1 - RESTIC EDITION
═══════════════════════════════════════════════════════════════════════════════

VERSION: 22.1

Backs up the Thunderbird profile via restic: real AES-256 encryption,
deduplication, native integrity checking, daily/weekly/monthly retention.
Desktop notifications (notify-send) on every run and e-mail (Resend) on
failure.

USAGE:
    python3 thunderbird_guardian.py --init      # first time (password)
    python3 thunderbird_guardian.py             # backup
    python3 thunderbird_guardian.py --verify    # full check (slow)
    python3 thunderbird_guardian.py --help

═══════════════════════════════════════════════════════════════════════════════
"""

import os
import re
import sys
import errno
import html
import locale
import stat
import subprocess
import time
import logging
import json
import shutil
import getpass
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler

BASE_DIR = Path(__file__).resolve().parent

try:
    import keyring
    from dotenv import load_dotenv
    import resend
except ImportError as e:
    print(f"❌ ERROR: module '{e.name}' missing.")
    print(f"   This script must be run from its virtual environment:")
    print(f"   {BASE_DIR}/.venv/bin/pip install -r {BASE_DIR}/requirements.txt")
    print(f"   {BASE_DIR}/.venv/bin/python3 {BASE_DIR}/thunderbird_guardian.py")
    sys.exit(3)

load_dotenv(BASE_DIR / ".env")


# =============================================================================
# CONFIGURATION
# =============================================================================

class Config:
    SOURCE_DIR = Path(os.getenv("TB_SOURCE_DIR", Path.home() / ".thunderbird"))

    KEYRING_SERVICE = "thunderbird_backup_guardian"
    KEYRING_USERNAME = "encryption_password"
    MIN_PASSWORD_LENGTH = 8

    KEEP_DAILY = int(os.getenv("TB_KEEP_DAILY", "7"))
    KEEP_WEEKLY = int(os.getenv("TB_KEEP_WEEKLY", "4"))
    KEEP_MONTHLY = int(os.getenv("TB_KEEP_MONTHLY", "6"))

    MOUNT_RETRY_ATTEMPTS = int(os.getenv("TB_MOUNT_RETRY_ATTEMPTS", "6"))
    MOUNT_RETRY_DELAY = int(os.getenv("TB_MOUNT_RETRY_DELAY", "300"))  # 5 min

    LOG_LEVEL = os.getenv("TB_LOG_LEVEL", "INFO")

    RESEND_API_KEY = os.getenv("RESEND_API_KEY")
    EMAIL_FROM = os.getenv("EMAIL_FROM", "onboarding@resend.dev")
    EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "Thunderbird Guardian")
    EMAIL_TO = os.getenv("EMAIL_TO")


def resolve_dest_dir() -> Path:
    """TB_BACKUP_DIR takes priority, otherwise falls back to the home dir.

    No attempt is made to guess an external disk's name: that wouldn't
    generalize to anyone else's setup. If you back up to an external
    disk, set TB_BACKUP_DIR explicitly (see DIRECTORY_CONFIGURATION.md)."""
    env_dir = os.getenv("TB_BACKUP_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return Path.home() / "thunderbird_backups"


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("ThunderbirdGuardian")
    logger.setLevel(getattr(logging, Config.LOG_LEVEL))
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


log = setup_logging()


class _NoFollowRotatingFileHandler(RotatingFileHandler):
    """Refuses to open the log through a symlink planted at its name on the
    backup disk (which would append to any file the user can write), and
    refuses anything but a regular file: a FIFO planted there would block
    the open forever and hang the run before any failure notification."""

    def _open(self):
        fd = os.open(
            self.baseFilename,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o666,
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"Not a regular file: {self.baseFilename}")
            os.set_blocking(fd, True)
            return open(fd, self.mode, encoding=self.encoding, errors=self.errors)
        except BaseException:
            os.close(fd)
            raise


def attach_file_logging(dest_fd: int) -> None:
    """Adds file rotation once the destination is confirmed reachable.

    The log path goes through /proc/self/fd so that the open and every
    rotation rename land in the directory open_backup_dir() checked, even if
    the path to it is swapped afterwards."""
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    try:
        handler = _NoFollowRotatingFileHandler(
            f"/proc/self/fd/{dest_fd}/guardian_automated.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding='utf-8'
        )
        handler.setFormatter(formatter)
        log.addHandler(handler)
    except OSError as e:
        log.warning(f"Could not create the log file: {e}")


# =============================================================================
# PASSWORD
# =============================================================================

def init_password() -> str:
    existing = keyring.get_password(Config.KEYRING_SERVICE, Config.KEYRING_USERNAME)
    if existing:
        print("⚠️  A password is already stored in the keyring.")
        if input("Replace it? (yes/no): ").strip().lower() != "yes":
            print("✅ Existing password kept.")
            return existing

    print("╔════════════════════════════════════════════════════════════════╗")
    print("║   ENCRYPTION PASSWORD SETUP                                    ║")
    print("╚════════════════════════════════════════════════════════════════╝")
    print(f"⚠️  Minimum length: {Config.MIN_PASSWORD_LENGTH} characters\n")

    while True:
        password = getpass.getpass("Password: ")
        if len(password) < Config.MIN_PASSWORD_LENGTH:
            print(f"❌ Too short ({Config.MIN_PASSWORD_LENGTH} min)")
            continue
        if password != getpass.getpass("Confirm: "):
            print("❌ Passwords don't match")
            continue
        break

    keyring.set_password(Config.KEYRING_SERVICE, Config.KEYRING_USERNAME, password)
    print("\n✅ Password stored in the system keyring")
    print("⚠️  Keep a durable copy of it (password manager, paper backup):")
    print("   this password only lives on this machine's system keyring,")
    print("   which could be lost along with the PC.\n")
    return password


def get_password() -> str:
    # Raises RuntimeError rather than sys.exit() on failure: run_backup()'s
    # caller (main()) only catches Exception, not the BaseException-rooted
    # SystemExit, so sys.exit() here would skip notify_desktop()/
    # notify_email_failure() entirely on a production cron run (confirmed
    # by code review 2026-09-22, alongside the fix below). --verify calls
    # this outside that try/except and handles RuntimeError locally to
    # keep its own clean exit behavior.
    password = keyring.get_password(Config.KEYRING_SERVICE, Config.KEYRING_USERNAME)
    if password is None:
        raise RuntimeError(f"Password not initialized. Run: python3 {sys.argv[0]} --init")
    if not password:
        # keyring can return "" instead of None or raising when the
        # SecretService backend can't actually unlock the collection in
        # this context (e.g. no active/unlocked session at cron time):
        # confirmed 2026-09-22, same env vars as an interactive run, valid
        # password still in the keyring, yet this run got back "". Left
        # unchecked, an empty password silently reaches restic and fails
        # two layers down with a cryptic "empty password" error instead of
        # pointing at the real cause.
        raise RuntimeError(
            "Keyring returned an empty password (not None). The SecretService "
            "collection was likely locked/unreachable in this run's session, "
            "not a missing password."
        )
    return password


# =============================================================================
# THUNDERBIRD
# =============================================================================

THUNDERBIRD_PROCESS_PATTERN = "thunderbird|thunderbird-bin"


def close_thunderbird() -> None:
    """The real process name depends on the install method: the native
    binary is called `thunderbird`, but a snap package exec()s into a
    separate `thunderbird-bin`: pkill/pgrep -x must cover both, otherwise
    the shutdown check is a silent false positive."""
    log.info("Closing Thunderbird...")
    subprocess.run(["pkill", "-x", THUNDERBIRD_PROCESS_PATTERN], capture_output=True)
    time.sleep(3)

    if subprocess.run(["pgrep", "-x", THUNDERBIRD_PROCESS_PATTERN], capture_output=True).returncode == 0:
        raise RuntimeError(
            "Thunderbird refuses to close: backup aborted to avoid "
            "reading a profile that's still being written to"
        )
    log.info("✅ Thunderbird closed")

    # .parentlock isn't cleaned up by a normal shutdown (confirmed by
    # restoring a real backup and having it trigger Thunderbird's "already
    # running" false positive); *.lock alone doesn't match it, since the
    # glob requires a character before ".lock" and "parentlock" has none.
    for pattern in ("*.lock", ".parentlock", "lock"):
        for lock in Config.SOURCE_DIR.rglob(pattern):
            try:
                lock.unlink()
            except OSError:
                pass


def restart_thunderbird() -> None:
    if not shutil.which("thunderbird"):
        return
    try:
        subprocess.Popen(
            ["thunderbird"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
    except OSError:
        log.warning("⚠️ Could not restart Thunderbird automatically")
        return

    # Popen() succeeding only means the process was spawned, not that it
    # stayed up: a GUI app started without a valid display connection (e.g.
    # missing XAUTHORITY when launched from cron) exits within milliseconds,
    # which logging "restarted" right after Popen() would silently miss.
    # A single check after a fixed 2s sleep produced false-negative warnings
    # every day in production (confirmed 2026-08-19): the snap package's
    # exec chain (wrapper -> snap-confine -> thunderbird-bin) can take
    # longer than 2s to settle, especially right after the backup's own
    # disk I/O, so poll for up to 10s instead of checking once.
    for _ in range(10):
        time.sleep(1)
        if subprocess.run(["pgrep", "-x", THUNDERBIRD_PROCESS_PATTERN], capture_output=True).returncode == 0:
            log.info("✅ Thunderbird restarted")
            return
    log.warning(
        "⚠️ Thunderbird was relaunched but exited immediately "
        "(check DISPLAY/XAUTHORITY in the environment it was started from)"
    )


# =============================================================================
# DISK
# =============================================================================

def wait_for_disk(dest_dir: Path) -> bool:
    for attempt in range(1, Config.MOUNT_RETRY_ATTEMPTS + 1):
        if dest_dir.parent.exists():
            return True
        log.warning(
            f"Disk not found ({dest_dir.parent}), attempt "
            f"{attempt}/{Config.MOUNT_RETRY_ATTEMPTS}..."
        )
        if attempt < Config.MOUNT_RETRY_ATTEMPTS:
            time.sleep(Config.MOUNT_RETRY_DELAY)
    return False


MAX_SYMLINK_HOPS = 40


def trusted_devices() -> set:
    """Filesystems whose symlinks the backup may follow: / and the home
    directory's. Anyone who can write to the backup disk (physical access,
    shared mount) can plant a symlink there, but not on these."""
    return {os.stat("/").st_dev, os.stat(Path.home()).st_dev}


def open_backup_dir(path: Path, create: bool = True) -> int:
    """Opens the backup destination one component at a time and returns an
    O_PATH directory fd for it, creating missing components if asked.

    A symlink along the path is followed only when it is stored on a
    trusted filesystem (see trusted_devices()), so a link the user made
    (e.g. ~/thunderbird_backups -> /media/$USER/<disk>/...) still works,
    while one planted on the backup disk (e.g. the destination folder
    replaced by a link to a directory in $HOME) is refused instead of
    redirecting every write of the run off the disk. Each component is
    opened with O_NOFOLLOW, so a swap between the check and the open fails
    rather than being followed. Callers then write relative to the fd."""
    trusted = trusted_devices()
    parts = list(Path(os.path.abspath(path)).parts[1:])
    fd = os.open("/", os.O_PATH | os.O_DIRECTORY)
    hops = 0
    try:
        while parts:
            name = parts.pop(0)
            if name in ("", "."):
                continue
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(name, dir_fd=fd)
                except FileExistsError:
                    pass
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                if os.fstat(fd).st_dev not in trusted:
                    raise RuntimeError(
                        f"Refusing to follow the symbolic link '{name}' in the backup "
                        f"destination path ({path}): it is stored on the backup disk "
                        "(or another filesystem than / and your home), where anyone who "
                        "can write to the disk could plant it to redirect the backup's "
                        "writes. Point TB_BACKUP_DIR at the real directory instead."
                    )
                hops += 1
                if hops > MAX_SYMLINK_HOPS:
                    raise RuntimeError(f"Too many symbolic links in the backup destination path: {path}")
                target = os.readlink(name, dir_fd=fd)
                target_parts = list(Path(target).parts)
                if target.startswith("/"):
                    root_fd = os.open("/", os.O_PATH | os.O_DIRECTORY)
                    os.close(fd)
                    fd = root_fd
                    target_parts = target_parts[1:]
                parts = target_parts + parts
                continue
            child_fd = os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


REPO_NAME = "restic-repo"


def _linked_entries(dir_fd: int) -> list:
    with os.scandir(dir_fd) as it:
        return [e.name for e in it if e.is_symlink()]


def open_repo_fd(dest_fd: int, create: bool = False):
    """Opens the restic repository directory and returns a fd for it (None
    if it doesn't exist and `create` is False; with `create`, an empty one
    is made first, restic accepts an empty directory for `init`).

    restic follows symlinks in its repository path: a `restic-repo` planted
    as a link (or holding linked subdirectories, which restic creates new
    files in) would send the whole encrypted backup to a directory chosen by
    whoever can write to the disk. Both are refused here, before any restic
    command runs. restic itself names every file inside after its content
    hash, so only directory-level links matter.

    The returned fd is the directory itself, opened with O_NOFOLLOW: callers
    hand restic /proc/self/fd/<fd> (see repo_path()) instead of a path, so
    swapping `restic-repo` or any parent of it for a link after this point
    can't redirect restic's writes either. Only a link swapped in *inside*
    the repository (data/xx) while the run is in progress is not covered."""
    try:
        st = os.stat(REPO_NAME, dir_fd=dest_fd, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(REPO_NAME, 0o700, dir_fd=dest_fd)
        except FileExistsError:
            pass
        st = os.stat(REPO_NAME, dir_fd=dest_fd, follow_symlinks=False)
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(
            f"Refusing to use '{REPO_NAME}': it is a symbolic link, not the "
            "repository directory itself."
        )
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"'{REPO_NAME}' exists but is not a directory.")

    try:
        repo_fd = os.open(REPO_NAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dest_fd)
    except OSError as e:
        # Linux reports a link opened with O_DIRECTORY|O_NOFOLLOW as ENOTDIR
        # (ELOOP without O_DIRECTORY): both mean it was swapped for a link.
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise RuntimeError(
                f"Refusing to use '{REPO_NAME}': it became a symbolic link while "
                "the backup was starting."
            ) from e
        raise
    try:
        linked = _linked_entries(repo_fd)
        try:
            data_fd = os.open("data", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=repo_fd)
        except (FileNotFoundError, NotADirectoryError):
            data_fd = None
        except OSError as e:
            if e.errno not in (errno.ELOOP, errno.ENOTDIR):
                raise
            data_fd = None  # already reported as a link by _linked_entries()
        if data_fd is not None:
            try:
                linked += [f"data/{n}" for n in _linked_entries(data_fd)]
            finally:
                os.close(data_fd)
        if linked:
            # The names are chosen by whoever planted the links: kept out of
            # the message, which ends up in the desktop and e-mail alerts.
            raise RuntimeError(
                f"Refusing to use '{REPO_NAME}': it contains {len(linked)} symbolic "
                "link(s) that would send restic's writes outside the backup disk. "
                f"List them with: find '{REPO_NAME}' -maxdepth 2 -type l"
            )
    except BaseException:
        os.close(repo_fd)
        raise
    return repo_fd


def repo_path(repo_fd: int) -> Path:
    return Path(f"/proc/self/fd/{repo_fd}")


def check_disk_space(dest_dir: Path) -> None:
    source_size = sum(f.stat().st_size for f in Config.SOURCE_DIR.rglob("*") if f.is_file())
    required = int(source_size * 0.3)  # restic dedupes/compresses, generous margin but not 1.5x raw
    stat = shutil.disk_usage(dest_dir)

    log.info(f"Space: {stat.free / 1024**3:.1f} GB available, {required / 1024**3:.1f} GB required (estimate)")
    if stat.free < required:
        raise OSError(f"Not enough space: {stat.free / 1024**3:.1f} GB < {required / 1024**3:.1f} GB")
    log.info("✅ Enough space available")


# =============================================================================
# RESTIC
# =============================================================================

def run_restic(
    repo: Path, password: str, args: list, check: bool = True, cwd: Path = None
) -> subprocess.CompletedProcess:
    restic_bin = shutil.which("restic")
    if not restic_bin:
        raise RuntimeError("restic not found. Install it: sudo apt install restic")

    env = os.environ.copy()
    env["RESTIC_PASSWORD"] = password

    # A repository given as /proc/self/fd/<n> (see open_repo_fd) only
    # resolves in the child if that fd is inherited under the same number.
    m = re.fullmatch(r"/proc/self/fd/(\d+)", str(repo))
    pass_fds = (int(m.group(1)),) if m else ()

    result = subprocess.run(
        [restic_bin, "-r", str(repo)] + args,
        env=env, capture_output=True, text=True, cwd=cwd, pass_fds=pass_fds
    )
    result.stderr = result.stderr.replace(str(repo), REPO_NAME)
    if check and result.returncode != 0:
        raise RuntimeError(f"restic {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def ensure_repo_initialized(repo: Path, password: str) -> None:
    result = run_restic(repo, password, ["snapshots", "--json"], check=False)
    if result.returncode == 0:
        return
    # A failed `snapshots` on a repository that already exists is not a
    # reason to initialize one: `restic init` would refuse anyway, and its
    # "init failed" error hides the real cause, usually a keyring password
    # that isn't the one this repository was created with (confirmed by a
    # throwaway-repository test, 2026-10-03). Say that instead.
    if (repo / "config").exists():
        raise RuntimeError(
            f"The restic repository exists ('{REPO_NAME}') but could not be opened "
            f"(restic: {result.stderr.strip()}). Most likely cause: the password in the "
            "keyring is not the one this repository was created with. Do NOT "
            "run --init with a new password: enter the original one (see "
            "Troubleshooting in README.md)."
        )
    # No `config` but not empty either (keys/, data/...: a partial copy, a
    # half-restored repository): `restic init` there would start a second,
    # unrelated repository next to the old data. Stop instead.
    if repo.is_dir() and any(repo.iterdir()):
        raise RuntimeError(
            f"The directory '{REPO_NAME}' is not empty but holds no restic 'config' "
            "file (partial copy or half-restored repository?). Refusing to run "
            "'restic init' on it: restore the missing config file from the "
            "repository's source, or move the directory aside to start a new "
            "repository (a new repository cannot read the old backups)."
        )
    log.info("Initializing the restic repository...")
    run_restic(repo, password, ["init"])
    log.info("✅ restic repository initialized")


def check_repo(repo: Path, password: str, deep: bool = False) -> bool:
    # A structural check alone doesn't catch silent corruption of data
    # already written (bit rot): sample 5% of the data blocks on every
    # routine run, on top of the full --read-data of the manual --verify.
    args = ["check"] + (["--read-data"] if deep else ["--read-data-subset=5%"])
    log.info("🔍 Checking repository integrity" + (" (full, slow)" if deep else "") + "...")
    result = run_restic(repo, password, args, check=False)
    if result.returncode != 0:
        log.error(f"❌ Repository corrupted or inconsistent: {result.stderr.strip()}")
        return False
    log.info("✅ Repository is healthy")
    return True


def do_backup(repo: Path, password: str) -> dict:
    """Backs up a RELATIVE path (cwd = parent directory) so that restic
    never records the original absolute path: restoring must be able to
    recreate the profile without depending on the username or machine the
    backup was taken on."""
    log.info(f"Backing up {Config.SOURCE_DIR}...")
    result = run_restic(
        repo, password,
        ["backup", Config.SOURCE_DIR.name, "--tag", "thunderbird", "--json"],
        cwd=Config.SOURCE_DIR.parent
    )

    summary = {}
    for line in result.stdout.strip().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("message_type") == "summary":
            summary = event

    if not summary:
        raise RuntimeError("restic returned no backup summary (unexpected output)")

    log.info(f"📊 New files: {summary.get('files_new', 0)}, changed: {summary.get('files_changed', 0)}")
    log.info(f"📊 Data added: {summary.get('data_added', 0) / 1024**3:.2f} GB")
    log.info(f"📊 Duration: {summary.get('total_duration', 0):.0f}s")
    return summary


def apply_retention(repo: Path, password: str) -> None:
    log.info(
        f"Applying retention (daily:{Config.KEEP_DAILY} weekly:{Config.KEEP_WEEKLY} "
        f"monthly:{Config.KEEP_MONTHLY})..."
    )
    run_restic(repo, password, [
        "forget", "--tag", "thunderbird",
        "--keep-daily", str(Config.KEEP_DAILY),
        "--keep-weekly", str(Config.KEEP_WEEKLY),
        "--keep-monthly", str(Config.KEEP_MONTHLY),
        "--prune",
    ])
    log.info("✅ Retention applied")


def _restore_script_en(repo_name: str, source_basename: str) -> str:
    return f"""#!/bin/bash
set -e
echo "=== THUNDERBIRD EMERGENCY RESTORE (restic) ==="

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
REPO="{repo_name}"

RESTIC_BIN="restic"
if ! command -v restic &> /dev/null; then
    if [ -x "$SCRIPT_DIR/restic-bin/restic" ]; then
        RESTIC_BIN="$SCRIPT_DIR/restic-bin/restic"
        echo "restic is not installed on this system, using the bundled binary."
    else
        echo "ERROR: restic not found (neither installed, nor bundled)."
        echo "Install it: sudo apt install restic"
        exit 1
    fi
fi

echo "restic repository password:"
read -s RESTIC_PASSWORD
echo ""
export RESTIC_PASSWORD

echo ""
echo "Available snapshots:"
"$RESTIC_BIN" -r "$SCRIPT_DIR/$REPO" snapshots

echo ""
read -p "Snapshot ID to restore (empty = most recent) : " SNAPSHOT
SNAPSHOT="${{SNAPSHOT:-latest}}"

TARGET="$HOME/{source_basename}-restored-$(date +%Y%m%d_%H%M%S)"
echo "Restoring to $TARGET ..."
"$RESTIC_BIN" -r "$SCRIPT_DIR/$REPO" restore "$SNAPSHOT" --target "$TARGET"

RESTORED_PROFILE="$TARGET/{source_basename}"
if [ ! -d "$RESTORED_PROFILE" ]; then
    echo "Expected path not found ($RESTORED_PROFILE), searching automatically..."
    FOUND=$(find "$TARGET" -maxdepth 15 -type d -name "{source_basename}" | head -1)
    if [ -n "$FOUND" ]; then
        RESTORED_PROFILE="$FOUND"
    else
        echo "ERROR: restored profile not found under $TARGET"
        echo "Inspect the content of $TARGET manually before continuing."
        exit 1
    fi
fi

echo ""
echo "Profile restored to: $RESTORED_PROFILE"
echo ""
echo "To activate it (do this manually, nothing is overwritten automatically):"
echo "  pkill -x thunderbird 2>/dev/null || true"
echo "  [ -d \\"\\$HOME/{source_basename}\\" ] && mv \\"\\$HOME/{source_basename}\\" \\"\\$HOME/{source_basename}.old_\\$(date +%Y%m%d_%H%M%S)\\""
echo "  mv \\"$RESTORED_PROFILE\\" \\"\\$HOME/{source_basename}\\""
echo "  thunderbird &"
"""


def _restore_script_fr(repo_name: str, source_basename: str) -> str:
    return f"""#!/bin/bash
set -e
echo "=== RESTAURATION D URGENCE THUNDERBIRD (restic) ==="

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
REPO="{repo_name}"

RESTIC_BIN="restic"
if ! command -v restic &> /dev/null; then
    if [ -x "$SCRIPT_DIR/restic-bin/restic" ]; then
        RESTIC_BIN="$SCRIPT_DIR/restic-bin/restic"
        echo "restic n'est pas installe sur ce systeme, utilisation du binaire embarque."
    else
        echo "ERREUR : restic introuvable (ni installe, ni embarque)."
        echo "Installez-le : sudo apt install restic"
        exit 1
    fi
fi

echo "Mot de passe du depot restic :"
read -s RESTIC_PASSWORD
echo ""
export RESTIC_PASSWORD

echo ""
echo "Sauvegardes disponibles :"
"$RESTIC_BIN" -r "$SCRIPT_DIR/$REPO" snapshots

echo ""
read -p "ID de la sauvegarde a restaurer (vide = la plus recente) : " SNAPSHOT
SNAPSHOT="${{SNAPSHOT:-latest}}"

TARGET="$HOME/{source_basename}-restored-$(date +%Y%m%d_%H%M%S)"
echo "Restauration vers $TARGET ..."
"$RESTIC_BIN" -r "$SCRIPT_DIR/$REPO" restore "$SNAPSHOT" --target "$TARGET"

RESTORED_PROFILE="$TARGET/{source_basename}"
if [ ! -d "$RESTORED_PROFILE" ]; then
    echo "Chemin attendu introuvable ($RESTORED_PROFILE), recherche automatique..."
    FOUND=$(find "$TARGET" -maxdepth 15 -type d -name "{source_basename}" | head -1)
    if [ -n "$FOUND" ]; then
        RESTORED_PROFILE="$FOUND"
    else
        echo "ERREUR : profil restaure introuvable sous $TARGET"
        echo "Inspecte manuellement le contenu de $TARGET avant de continuer."
        exit 1
    fi
fi

echo ""
echo "Profil restaure dans : $RESTORED_PROFILE"
echo ""
echo "Pour l activer (a faire manuellement, rien n est ecrase automatiquement) :"
echo "  pkill -x thunderbird 2>/dev/null || true"
echo "  [ -d \\"\\$HOME/{source_basename}\\" ] && mv \\"\\$HOME/{source_basename}\\" \\"\\$HOME/{source_basename}.old_\\$(date +%Y%m%d_%H%M%S)\\""
echo "  mv \\"$RESTORED_PROFILE\\" \\"\\$HOME/{source_basename}\\""
echo "  thunderbird &"
"""


def _copy_xattrs(source: Path, fd: int) -> None:
    """Same best-effort extended-attribute copy as shutil.copy2."""
    ignored = (errno.EPERM, errno.ENOTSUP, errno.ENODATA, errno.EINVAL, errno.EACCES)
    try:
        names = os.listxattr(source)
    except OSError as e:
        if e.errno not in (errno.ENOTSUP, errno.ENODATA, errno.EINVAL):
            raise
        return
    for xattr_name in names:
        try:
            os.setxattr(fd, xattr_name, os.getxattr(source, xattr_name))
        except OSError as e:
            if e.errno not in ignored:
                raise


def replace_file_in_dir(
    dir_fd: int, name: str, data: bytes, mode: int, copy_meta_from: Path = None
) -> None:
    """Replaces <dir>/<name> with a new regular file, never following a
    symlink planted at that name on the backup disk: the content goes to a
    temp file created with O_EXCL|O_NOFOLLOW, its mode (and, for copied
    docs, timestamps and extended attributes, as shutil.copy2 would) is set
    on the open fd, then it is renamed over <name>, which replaces a link
    instead of writing through it.

    A regular file the user can't write still fails the run with
    PermissionError, as the former write_text()/copy2() did, instead of
    being silently replaced."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        st = None
    if st is not None and stat.S_ISREG(st.st_mode) and not os.access(
        name, os.W_OK, dir_fd=dir_fd, follow_symlinks=False
    ):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), name)

    # Fixed temp name: a run killed mid-write leaves at most this one file,
    # cleared by the next run (unlink removes a link without following it).
    tmp_name = f".{name}.tmp"
    try:
        os.unlink(tmp_name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    fd = os.open(
        tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd
    )
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            if copy_meta_from is not None:
                src_st = copy_meta_from.stat()
                os.utime(fd, ns=(src_st.st_atime_ns, src_st.st_mtime_ns))
                _copy_xattrs(copy_meta_from, fd)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except OSError:
            pass
        raise


def write_restore_script(dest_fd: int, repo_name: str) -> None:
    """do_backup() backs up a RELATIVE path (cwd=parent) precisely so that
    restoring lands directly under $TARGET/<name>, never depending on an
    absolute path or a username. The search-based fallback stays as a
    safety net (older backup taken differently, or different restic
    version behavior).

    Written in both English (public repo language) and French (readable
    in the language you actually think in during a real emergency)."""
    source_basename = Config.SOURCE_DIR.name

    # Same bytes as the former Path.write_text() (locale encoding, no
    # newline translation on Linux).
    encoding = locale.getpreferredencoding(False)
    en_name = "RESTORE_EMERGENCY.sh"
    replace_file_in_dir(
        dest_fd, en_name, _restore_script_en(repo_name, source_basename).encode(encoding), 0o700
    )

    fr_name = "RESTORE_EMERGENCY.fr.sh"
    replace_file_in_dir(
        dest_fd, fr_name, _restore_script_fr(repo_name, source_basename).encode(encoding), 0o700
    )

    log.info(f"✅ Restore scripts up to date: {en_name}, {fr_name}")


def sync_docs_to_disk(dest_fd: int) -> None:
    """Copies the restore docs onto the disk itself, both languages: in a
    crash, they must be readable without depending on GitHub or this PC."""
    for name in (
        "README.md", "README.fr.md",
        "DISASTER_RECOVERY.md", "DISASTER_RECOVERY.fr.md",
    ):
        source = BASE_DIR / name
        if source.exists():
            replace_file_in_dir(
                dest_fd, name, source.read_bytes(), stat.S_IMODE(source.stat().st_mode),
                copy_meta_from=source,
            )
    log.info("✅ Restore documentation synced to disk (EN + FR)")


# =============================================================================
# NOTIFICATIONS
# =============================================================================

def notify_desktop(title: str, message: str, urgency: str = "normal") -> None:
    # Popup size is a Plasma theme setting (System Settings > Notifications),
    # not something notify-send controls: icon + urgency + display duration
    # are the only levers available from the script.
    icon = "dialog-error" if urgency == "critical" else "drive-harddisk"
    expire_ms = "0" if urgency == "critical" else "15000"
    try:
        subprocess.run(
            [
                "notify-send", "-u", urgency, "-i", icon, "-t", expire_ms,
                "-a", "Thunderbird Guardian", title, message,
            ],
            capture_output=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def notify_email_failure(error_message: str) -> None:
    if not Config.RESEND_API_KEY or not Config.EMAIL_TO:
        log.warning("⚠️ E-mail notification not configured (.env missing)")
        return
    try:
        resend.api_key = Config.RESEND_API_KEY
        resend.Emails.send({
            "from": f"{Config.EMAIL_FROM_NAME} <{Config.EMAIL_FROM}>",
            "to": Config.EMAIL_TO,
            "subject": "🚨 Thunderbird backup failed",
            "html": (
                f"<p>The Thunderbird backup on "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')} failed:</p>"
                f"<pre>{html.escape(error_message)}</pre>"
            ),
        })
        log.info("✅ Failure alert e-mail sent")
    except Exception as e:
        log.error(f"❌ Failed to send the alert e-mail: {e}")


# =============================================================================
# MAIN PROCESS
# =============================================================================

def run_backup() -> None:
    log.info("═" * 70)
    log.info("THUNDERBIRD BACKUP (restic)")
    log.info("═" * 70)
    start = time.time()

    if not Config.SOURCE_DIR.exists():
        raise RuntimeError(f"Source not found: {Config.SOURCE_DIR}")

    dest_dir = resolve_dest_dir()
    if not wait_for_disk(dest_dir):
        raise RuntimeError(
            f"Backup disk not found after {Config.MOUNT_RETRY_ATTEMPTS} "
            f"attempts: {dest_dir.parent}"
        )

    # Everything the run writes on the disk goes through these fds (log,
    # restore scripts, docs; restic gets the repository directory as
    # /proc/self/fd/<n>), never through a path a symlink planted on the disk
    # could redirect. Kept open for the whole process: the log handler
    # writes through the first one.
    dest_fd = open_backup_dir(dest_dir)
    attach_file_logging(dest_fd)
    log.info(f"✅ Destination: {dest_dir}")

    check_disk_space(Path(f"/proc/self/fd/{dest_fd}"))

    password = get_password()
    repo = repo_path(open_repo_fd(dest_fd, create=True))

    ensure_repo_initialized(repo, password)
    repo_healthy = check_repo(repo, password, deep=False)

    close_thunderbird()
    try:
        summary = do_backup(repo, password)
    finally:
        restart_thunderbird()

    if repo_healthy:
        apply_retention(repo, password)
    else:
        log.warning("⚠️ Retention skipped: the repository failed the integrity check")

    write_restore_script(dest_fd, REPO_NAME)
    sync_docs_to_disk(dest_fd)

    elapsed = time.time() - start
    log.info("═" * 70)
    log.info("✅ DONE")
    log.info(f"Duration: {int(elapsed // 60)}min {int(elapsed % 60)}s")
    log.info("═" * 70)

    notify_desktop(
        "✅ Thunderbird backup succeeded",
        f"{summary.get('files_new', 0)} new files, "
        f"{summary.get('data_added', 0) / 1024**2:.0f} MB added"
    )

    if not repo_healthy:
        notify_email_failure(
            "The backup succeeded, but the restic integrity check failed "
            "before the backup ran: check the repository manually "
            "(`python3 thunderbird_guardian.py --verify`)."
        )


def check_system_deps() -> bool:
    missing = []
    if not shutil.which("thunderbird"):
        missing.append("thunderbird")
    if not shutil.which("restic"):
        missing.append("restic")

    if missing:
        log.error(f"❌ Missing system dependencies: {', '.join(missing)}")
        log.error(f"   sudo apt install {' '.join(missing)}")
        return False
    return True


def main():
    print("\n" + "═" * 70)
    print("  THUNDERBIRD SECURE GUARDIAN v22.1 - Restic Edition")
    print("═" * 70 + "\n")

    if len(sys.argv) > 1:
        if sys.argv[1] == "--help":
            print(__doc__)
            sys.exit(0)

        elif sys.argv[1] == "--init":
            init_password()
            sys.exit(0)

        elif sys.argv[1] == "--verify":
            dest_dir = resolve_dest_dir()
            try:
                repo_fd = open_repo_fd(open_backup_dir(dest_dir, create=False))
            except FileNotFoundError:
                repo_fd = None
            except RuntimeError as e:
                log.error(f"❌ {e}")
                sys.exit(2)
            if repo_fd is None:
                log.error(f"❌ Repository not found: {dest_dir / REPO_NAME}")
                sys.exit(1)
            try:
                password = get_password()
            except RuntimeError as e:
                log.error(f"❌ {e}")
                sys.exit(2)
            ok = check_repo(repo_path(repo_fd), password, deep=True)
            sys.exit(0 if ok else 4)

        else:
            log.error(f"❌ Unknown argument: {sys.argv[1]}")
            log.error("   Use --help, --init or --verify")
            sys.exit(2)

    if not check_system_deps():
        sys.exit(3)

    try:
        run_backup()
        sys.exit(0)
    except KeyboardInterrupt:
        log.warning("\n⚠️  Interrupted")
        sys.exit(130)
    except Exception as e:
        log.exception(f"❌ ERROR: {e}")
        notify_desktop("❌ Thunderbird backup failed", str(e), urgency="critical")
        notify_email_failure(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
