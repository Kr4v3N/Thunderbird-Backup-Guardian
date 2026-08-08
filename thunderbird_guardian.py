#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════════
THUNDERBIRD SECURE GUARDIAN v22.0 - ÉDITION RESTIC
═══════════════════════════════════════════════════════════════════════════════

VERSION: 22.0

Sauvegarde du profil Thunderbird via restic : chiffrement AES-256 réel,
déduplication, vérification d'intégrité native, rétention quotidien/
hebdomadaire/mensuel. Notifications desktop (notify-send) systématiques et
e-mail (Resend) en cas d'échec.

UTILISATION:
    python3 thunderbird_guardian.py --init      # première fois (mot de passe)
    python3 thunderbird_guardian.py             # sauvegarde
    python3 thunderbird_guardian.py --verify    # vérification complète (lente)
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
    print(f"❌ ERREUR: module '{e.name}' manquant.")
    print(f"   Ce script doit être lancé depuis son environnement virtuel :")
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
    """TB_BACKUP_DIR en priorité, sinon auto-détection, sinon repli sur le home."""
    env_dir = os.getenv("TB_BACKUP_DIR", "").strip()
    if env_dir:
        return Path(env_dir)

    user = os.getenv("USER") or Path.home().name
    candidates = [
        Path(f"/run/media/{user}/Disk_1/Backup Thunderbird"),
        Path(f"/media/{user}/Disk_1/Backup Thunderbird"),
        Path.home() / "thunderbird_backups",
    ]
    for path in candidates:
        if path.parent.exists():
            return path
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
    """Ajoute la rotation fichier une fois la destination confirmée accessible."""
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
        log.warning(f"Impossible de créer le fichier de log: {e}")


# =============================================================================
# MOT DE PASSE
# =============================================================================

def init_password() -> str:
    existing = keyring.get_password(Config.KEYRING_SERVICE, Config.KEYRING_USERNAME)
    if existing:
        print("⚠️  Un mot de passe est déjà enregistré dans le trousseau.")
        if input("Le remplacer ? (oui/non): ").strip().lower() != "oui":
            print("✅ Mot de passe existant conservé.")
            return existing

    print("╔════════════════════════════════════════════════════════════════╗")
    print("║   INITIALISATION DU MOT DE PASSE DE CHIFFREMENT               ║")
    print("╚════════════════════════════════════════════════════════════════╝")
    print(f"⚠️  Longueur minimale: {Config.MIN_PASSWORD_LENGTH} caractères\n")

    while True:
        password = getpass.getpass("Mot de passe: ")
        if len(password) < Config.MIN_PASSWORD_LENGTH:
            print(f"❌ Trop court ({Config.MIN_PASSWORD_LENGTH} min)")
            continue
        if password != getpass.getpass("Confirmation: "):
            print("❌ Ne correspondent pas")
            continue
        break

    keyring.set_password(Config.KEYRING_SERVICE, Config.KEYRING_USERNAME, password)
    print("\n✅ Mot de passe enregistré dans le trousseau système")
    print("⚠️  Pense à en garder une copie durable (gestionnaire de mots de passe,")
    print("   copie papier) : ce mot de passe ne vit QUE sur ce disque système,")
    print("   qui pourrait crasher en même temps que le PC.\n")
    return password


def get_password() -> str:
    password = keyring.get_password(Config.KEYRING_SERVICE, Config.KEYRING_USERNAME)
    if password is None:
        log.error("❌ Mot de passe non initialisé.")
        log.error(f"   Exécutez: python3 {sys.argv[0]} --init")
        sys.exit(2)
    return password


# =============================================================================
# THUNDERBIRD
# =============================================================================

THUNDERBIRD_PROCESS_PATTERN = "thunderbird|thunderbird-bin"


def close_thunderbird() -> None:
    """Le nom du process réel dépend du mode d'installation : le binaire
    natif s'appelle `thunderbird`, mais un paquet snap exec() vers un
    `thunderbird-bin` distinct — pkill/pgrep -x doivent couvrir les deux,
    sinon la détection de fermeture est un faux positif silencieux."""
    log.info("Fermeture de Thunderbird...")
    subprocess.run(["pkill", "-x", THUNDERBIRD_PROCESS_PATTERN], capture_output=True)
    time.sleep(3)

    if subprocess.run(["pgrep", "-x", THUNDERBIRD_PROCESS_PATTERN], capture_output=True).returncode == 0:
        raise RuntimeError(
            "Thunderbird refuse de se fermer — sauvegarde annulée pour éviter "
            "de lire un profil en cours d'écriture"
        )
    log.info("✅ Thunderbird fermé")

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
        log.info("✅ Thunderbird redémarré")
    except OSError:
        log.warning("⚠️ Impossible de redémarrer Thunderbird automatiquement")


# =============================================================================
# DISQUE
# =============================================================================

def wait_for_disk(dest_dir: Path) -> bool:
    for attempt in range(1, Config.MOUNT_RETRY_ATTEMPTS + 1):
        if dest_dir.parent.exists():
            return True
        log.warning(
            f"Disque introuvable ({dest_dir.parent}), tentative "
            f"{attempt}/{Config.MOUNT_RETRY_ATTEMPTS}..."
        )
        if attempt < Config.MOUNT_RETRY_ATTEMPTS:
            time.sleep(Config.MOUNT_RETRY_DELAY)
    return False


def check_disk_space(dest_dir: Path) -> None:
    source_size = sum(f.stat().st_size for f in Config.SOURCE_DIR.rglob("*") if f.is_file())
    required = int(source_size * 0.3)  # restic déduplique/compresse, marge large mais pas 1.5x brut
    stat = shutil.disk_usage(dest_dir)

    log.info(f"Espace: {stat.free / 1024**3:.1f} Go disponibles, {required / 1024**3:.1f} Go requis (estimation)")
    if stat.free < required:
        raise OSError(f"Espace insuffisant: {stat.free / 1024**3:.1f} Go < {required / 1024**3:.1f} Go")
    log.info("✅ Espace suffisant")


# =============================================================================
# RESTIC
# =============================================================================

def run_restic(
    repo: Path, password: str, args: list, check: bool = True, cwd: Path = None
) -> subprocess.CompletedProcess:
    restic_bin = shutil.which("restic")
    if not restic_bin:
        raise RuntimeError("restic introuvable. Installez-le : sudo apt install restic")

    env = os.environ.copy()
    env["RESTIC_PASSWORD"] = password

    result = subprocess.run(
        [restic_bin, "-r", str(repo)] + args,
        env=env, capture_output=True, text=True, cwd=cwd
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"restic {' '.join(args)} a échoué : {result.stderr.strip()}")
    return result


def ensure_repo_initialized(repo: Path, password: str) -> None:
    result = run_restic(repo, password, ["snapshots", "--json"], check=False)
    if result.returncode == 0:
        return
    log.info("Initialisation du dépôt restic...")
    run_restic(repo, password, ["init"])
    log.info("✅ Dépôt restic initialisé")


def check_repo(repo: Path, password: str, deep: bool = False) -> bool:
    # Un check structurel seul ne détecte pas la corruption silencieuse de
    # données déjà écrites (bit rot) — on échantillonne 5% des blocs à
    # chaque run courant, en plus du --read-data complet du --verify manuel.
    args = ["check"] + (["--read-data"] if deep else ["--read-data-subset=5%"])
    log.info("🔍 Vérification d'intégrité du dépôt" + (" (complète, lent)" if deep else "") + "...")
    result = run_restic(repo, password, args, check=False)
    if result.returncode != 0:
        log.error(f"❌ Dépôt corrompu ou incohérent : {result.stderr.strip()}")
        return False
    log.info("✅ Dépôt intègre")
    return True


def do_backup(repo: Path, password: str) -> dict:
    """Sauvegarde un chemin RELATIF (cwd = dossier parent) pour que restic
    n'enregistre jamais le chemin absolu d'origine — la restauration doit
    pouvoir recréer le profil sans dépendre du nom d'utilisateur ou de la
    machine sur laquelle la sauvegarde a été prise."""
    log.info(f"Sauvegarde de {Config.SOURCE_DIR}...")
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
        raise RuntimeError("restic n'a renvoyé aucun résumé de sauvegarde (sortie inattendue)")

    log.info(f"📊 Fichiers nouveaux: {summary.get('files_new', 0)}, modifiés: {summary.get('files_changed', 0)}")
    log.info(f"📊 Données ajoutées: {summary.get('data_added', 0) / 1024**3:.2f} Go")
    log.info(f"📊 Durée: {summary.get('total_duration', 0):.0f}s")
    return summary


def apply_retention(repo: Path, password: str) -> None:
    log.info(
        f"Rétention (quotidien:{Config.KEEP_DAILY} hebdo:{Config.KEEP_WEEKLY} "
        f"mensuel:{Config.KEEP_MONTHLY})..."
    )
    run_restic(repo, password, [
        "forget", "--tag", "thunderbird",
        "--keep-daily", str(Config.KEEP_DAILY),
        "--keep-weekly", str(Config.KEEP_WEEKLY),
        "--keep-monthly", str(Config.KEEP_MONTHLY),
        "--prune",
    ])
    log.info("✅ Rétention appliquée")


def write_restore_script(dest_dir: Path, repo: Path) -> None:
    """do_backup() sauvegarde un chemin RELATIF (cwd=parent) précisément
    pour que la restauration atterrisse directement sous $TARGET/<nom>,
    sans jamais dépendre d'un chemin absolu ni d'un nom d'utilisateur. Le
    repli par recherche reste en filet de sécurité (ancienne sauvegarde
    prise autrement, ou comportement restic différent selon la version)."""
    script_path = dest_dir / "RESTORE_EMERGENCY.sh"
    source_basename = Config.SOURCE_DIR.name

    content = f"""#!/bin/bash
set -e
echo "=== RESTAURATION D URGENCE THUNDERBIRD (restic) ==="

SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
REPO="{repo.name}"

RESTIC_BIN="restic"
if ! command -v restic &> /dev/null; then
    if [ -x "$SCRIPT_DIR/restic-bin/restic" ]; then
        RESTIC_BIN="$SCRIPT_DIR/restic-bin/restic"
        echo "restic non installe sur ce systeme, utilisation du binaire embarque."
    else
        echo "ERREUR: restic introuvable (ni installe, ni embarque)."
        echo "Installez-le : sudo apt install restic"
        exit 1
    fi
fi

echo "Mot de passe du depot restic :"
read -s RESTIC_PASSWORD
echo ""
export RESTIC_PASSWORD

echo ""
echo "Snapshots disponibles :"
"$RESTIC_BIN" -r "$SCRIPT_DIR/$REPO" snapshots

echo ""
read -p "ID du snapshot a restaurer (vide = le plus recent) : " SNAPSHOT
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
        echo "ERREUR: profil restaure introuvable sous $TARGET"
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
    script_path.write_text(content)
    script_path.chmod(0o700)
    log.info(f"✅ Script de restauration à jour : {script_path.name}")


def sync_docs_to_disk(dest_dir: Path) -> None:
    """Copie la doc de restauration sur le disque lui-même : en cas de
    crash, elle doit être lisible sans dépendre de GitHub ni de ce PC."""
    for name in ("README.md", "DISASTER_RECOVERY.md"):
        source = BASE_DIR / name
        if source.exists():
            shutil.copy2(source, dest_dir / name)
    log.info("✅ Documentation de restauration synchronisée sur le disque")


# =============================================================================
# NOTIFICATIONS
# =============================================================================

def notify_desktop(title: str, message: str, urgency: str = "normal") -> None:
    # La taille du popup est un réglage du thème Plasma (Système >
    # Notifications), pas quelque chose que notify-send contrôle — icône +
    # urgence + durée d'affichage sont les seuls leviers côté script.
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
        log.warning("⚠️ Notification e-mail non configurée (.env manquant)")
        return
    try:
        resend.api_key = Config.RESEND_API_KEY
        resend.Emails.send({
            "from": f"{Config.EMAIL_FROM_NAME} <{Config.EMAIL_FROM}>",
            "to": Config.EMAIL_TO,
            "subject": "🚨 Échec de la sauvegarde Thunderbird",
            "html": (
                f"<p>La sauvegarde Thunderbird du "
                f"{datetime.now().strftime('%d/%m/%Y %H:%M')} a échoué :</p>"
                f"<pre>{error_message}</pre>"
            ),
        })
        log.info("✅ Alerte e-mail envoyée")
    except Exception as e:
        log.error(f"❌ Échec de l'envoi de l'alerte e-mail : {e}")


# =============================================================================
# PROCESSUS PRINCIPAL
# =============================================================================

def run_backup() -> None:
    log.info("═" * 70)
    log.info("SAUVEGARDE THUNDERBIRD (restic)")
    log.info("═" * 70)
    start = time.time()

    if not Config.SOURCE_DIR.exists():
        raise RuntimeError(f"Source introuvable: {Config.SOURCE_DIR}")

    dest_dir = resolve_dest_dir()
    if not wait_for_disk(dest_dir):
        raise RuntimeError(
            f"Disque de sauvegarde introuvable après {Config.MOUNT_RETRY_ATTEMPTS} "
            f"tentatives : {dest_dir.parent}"
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
        log.warning("⚠️ Rétention ignorée : le dépôt a échoué au contrôle d'intégrité")

    write_restore_script(dest_dir, repo)
    sync_docs_to_disk(dest_dir)

    elapsed = time.time() - start
    log.info("═" * 70)
    log.info("✅ TERMINÉ")
    log.info(f"Durée: {int(elapsed // 60)}min {int(elapsed % 60)}s")
    log.info("═" * 70)

    notify_desktop(
        "✅ Sauvegarde Thunderbird réussie",
        f"{summary.get('files_new', 0)} nouveaux fichiers, "
        f"{summary.get('data_added', 0) / 1024**2:.0f} Mo ajoutés"
    )

    if not repo_healthy:
        notify_email_failure(
            "Le backup a réussi mais le contrôle d'intégrité restic a échoué "
            "avant la sauvegarde — vérifiez le dépôt manuellement "
            "(`python3 thunderbird_guardian.py --verify`)."
        )


def check_system_deps() -> bool:
    missing = []
    if not shutil.which("thunderbird"):
        missing.append("thunderbird")
    if not shutil.which("restic"):
        missing.append("restic")

    if missing:
        log.error(f"❌ Dépendances système manquantes : {', '.join(missing)}")
        log.error(f"   sudo apt install {' '.join(missing)}")
        return False
    return True


def main():
    print("\n" + "═" * 70)
    print("  THUNDERBIRD SECURE GUARDIAN v22.0 — Édition restic")
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
                log.error(f"❌ Dépôt introuvable: {repo}")
                sys.exit(1)
            password = get_password()
            ok = check_repo(repo, password, deep=True)
            sys.exit(0 if ok else 4)

        else:
            log.error(f"❌ Argument inconnu : {sys.argv[1]}")
            log.error("   Utilisez --help, --init ou --verify")
            sys.exit(2)

    if not check_system_deps():
        sys.exit(3)

    try:
        run_backup()
        sys.exit(0)
    except KeyboardInterrupt:
        log.warning("\n⚠️  Interruption")
        sys.exit(130)
    except Exception as e:
        log.exception(f"❌ ERREUR: {e}")
        notify_desktop("❌ Échec de la sauvegarde Thunderbird", str(e), urgency="critical")
        notify_email_failure(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
