#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════════
THUNDERBIRD SECURE GUARDIAN v22.0 - RESTIC EDITION
═══════════════════════════════════════════════════════════════════════════════

VERSION: 22.0

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
import sys
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


def attach_file_logging(dest_dir: Path) -> None:
    """Adds file rotation once the destination is confirmed reachable."""
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            dest_dir / "guardian_automated.log",
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
    password = keyring.get_password(Config.KEYRING_SERVICE, Config.KEYRING_USERNAME)
    if password is None:
        log.error("❌ Password not initialized.")
        log.error(f"   Run: python3 {sys.argv[0]} --init")
        sys.exit(2)
    return password


# =============================================================================
# THUNDERBIRD
# =============================================================================

THUNDERBIRD_PROCESS_PATTERN = "thunderbird|thunderbird-bin"


def close_thunderbird() -> None:
    """The real process name depends on the install method: the native
    binary is called `thunderbird`, but a snap package exec()s into a
    separate `thunderbird-bin` — pkill/pgrep -x must cover both, otherwise
    the shutdown check is a silent false positive."""
    log.info("Closing Thunderbird...")
    subprocess.run(["pkill", "-x", THUNDERBIRD_PROCESS_PATTERN], capture_output=True)
    time.sleep(3)

    if subprocess.run(["pgrep", "-x", THUNDERBIRD_PROCESS_PATTERN], capture_output=True).returncode == 0:
        raise RuntimeError(
            "Thunderbird refuses to close — backup aborted to avoid "
            "reading a profile that's still being written to"
        )
    log.info("✅ Thunderbird closed")

    for lock in Config.SOURCE_DIR.rglob("*.lock"):
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
        log.info("✅ Thunderbird restarted")
    except OSError:
        log.warning("⚠️ Could not restart Thunderbird automatically")


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

    result = subprocess.run(
        [restic_bin, "-r", str(repo)] + args,
        env=env, capture_output=True, text=True, cwd=cwd
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"restic {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def ensure_repo_initialized(repo: Path, password: str) -> None:
    result = run_restic(repo, password, ["snapshots", "--json"], check=False)
    if result.returncode == 0:
        return
    log.info("Initializing the restic repository...")
    run_restic(repo, password, ["init"])
    log.info("✅ restic repository initialized")


def check_repo(repo: Path, password: str, deep: bool = False) -> bool:
    # A structural check alone doesn't catch silent corruption of data
    # already written (bit rot) — sample 5% of the data blocks on every
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
    never records the original absolute path — restoring must be able to
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

TARGET="$HOME/.thunderbird-restored-$(date +%Y%m%d_%H%M%S)"
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

TARGET="$HOME/.thunderbird-restored-$(date +%Y%m%d_%H%M%S)"
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


def write_restore_script(dest_dir: Path, repo: Path) -> None:
    """do_backup() backs up a RELATIVE path (cwd=parent) precisely so that
    restoring lands directly under $TARGET/<name>, never depending on an
    absolute path or a username. The search-based fallback stays as a
    safety net (older backup taken differently, or different restic
    version behavior).

    Written in both English (public repo language) and French (readable
    in the language you actually think in during a real emergency)."""
    source_basename = Config.SOURCE_DIR.name

    en_path = dest_dir / "RESTORE_EMERGENCY.sh"
    en_path.write_text(_restore_script_en(repo.name, source_basename))
    en_path.chmod(0o700)

    fr_path = dest_dir / "RESTORE_EMERGENCY.fr.sh"
    fr_path.write_text(_restore_script_fr(repo.name, source_basename))
    fr_path.chmod(0o700)

    log.info(f"✅ Restore scripts up to date: {en_path.name}, {fr_path.name}")


def sync_docs_to_disk(dest_dir: Path) -> None:
    """Copies the restore docs onto the disk itself, both languages: in a
    crash, they must be readable without depending on GitHub or this PC."""
    for name in (
        "README.md", "README.fr.md",
        "DISASTER_RECOVERY.md", "DISASTER_RECOVERY.fr.md",
    ):
        source = BASE_DIR / name
        if source.exists():
            shutil.copy2(source, dest_dir / name)
    log.info("✅ Restore documentation synced to disk (EN + FR)")


# =============================================================================
# NOTIFICATIONS
# =============================================================================

def notify_desktop(title: str, message: str, urgency: str = "normal") -> None:
    # Popup size is a Plasma theme setting (System Settings > Notifications),
    # not something notify-send controls — icon + urgency + display duration
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
                f"<pre>{error_message}</pre>"
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

    dest_dir.mkdir(parents=True, exist_ok=True)
    attach_file_logging(dest_dir)
    log.info(f"✅ Destination: {dest_dir}")

    check_disk_space(dest_dir)

    repo = dest_dir / "restic-repo"
    password = get_password()

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

    write_restore_script(dest_dir, repo)
    sync_docs_to_disk(dest_dir)

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
            "before the backup ran — check the repository manually "
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
    print("  THUNDERBIRD SECURE GUARDIAN v22.0 — Restic Edition")
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
            repo = dest_dir / "restic-repo"
            if not repo.exists():
                log.error(f"❌ Repository not found: {repo}")
                sys.exit(1)
            password = get_password()
            ok = check_repo(repo, password, deep=True)
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
