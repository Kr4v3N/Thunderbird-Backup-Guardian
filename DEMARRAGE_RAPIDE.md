# 🚀 DÉMARRAGE RAPIDE - Thunderbird Guardian v22.0

## Édition restic — chiffrement AES-256 réel, déduplication, rétention fine

---

## 📥 INSTALLATION

```bash
cd ~/PycharmProjects/Backup-Thunderbird

# 1. Dépendance système (paquet, nécessite sudo)
sudo apt install restic thunderbird

# 2. Environnement virtuel Python (une seule fois)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Configuration des notifications e-mail (optionnel mais recommandé)
cp .env.example .env
nano .env   # renseigner RESEND_API_KEY, EMAIL_FROM, EMAIL_TO

# 4. Initialiser le mot de passe de chiffrement (PREMIÈRE FOIS)
.venv/bin/python3 thunderbird_guardian.py --init
```

Le script détecte automatiquement s'il tourne dans le bon environnement
Python et affiche une erreur claire (avec la commande à lancer) si ce n'est
pas le cas — il n'installe plus rien tout seul au runtime (voir plus bas
pourquoi).

---

## 🎯 PREMIÈRE UTILISATION

### Étape 1 : Initialisation du mot de passe (une seule fois)

```bash
.venv/bin/python3 thunderbird_guardian.py --init
```

Si un mot de passe existe déjà dans le trousseau système, le script le
détecte et te propose de le conserver plutôt que d'en créer un nouveau.

⚠️ **Ce mot de passe est la seule clé du dépôt chiffré.** Il vit uniquement
dans le trousseau système de cette machine. Garde-en une copie durable
(gestionnaire de mots de passe type Proton Pass/Bitwarden/KeePassXC, ou
copie papier) — sans lui, aucune restauration n'est possible en cas de
panne de ce PC. Voir `DISASTER_RECOVERY.md`.

### Étape 2 : Première sauvegarde

```bash
.venv/bin/python3 thunderbird_guardian.py
```

Le premier run initialise le dépôt restic et fait une sauvegarde complète
(plus long que les suivants). Les runs suivants sont incrémentaux —
seuls les fichiers nouveaux/modifiés sont transférés, donc beaucoup plus
rapides.

---

## 📁 STRUCTURE DU PROJET

```
~/PycharmProjects/Backup-Thunderbird/
│
├── thunderbird_guardian.py     ← Script principal
├── requirements.txt            ← Dépendances Python (venv)
├── .venv/                      ← Environnement virtuel (non versionné)
├── .env                        ← Secrets (clé Resend, adresse e-mail — non versionné)
├── .env.example                ← Modèle de .env, sans secret
├── DISASTER_RECOVERY.md        ← Procédures de restauration, tous scénarios
└── CONFIGURATION_REPERTOIRE.md ← Détail de la résolution du répertoire de destination
```

Sur le disque de sauvegarde lui-même :
```
Backup Thunderbird/
├── restic-repo/            ← Dépôt chiffré (données + métadonnées restic)
├── restic-bin/restic       ← Binaire restic statique embarqué (secours hors-ligne)
├── RESTORE_EMERGENCY.sh    ← Régénéré à chaque sauvegarde réussie
└── guardian_automated.log  ← Log applicatif (rotation automatique, 10 Mo x5)
```

---

## ⚙️ CONFIGURATION

### Répertoire de destination

Voir `CONFIGURATION_REPERTOIRE.md` pour le détail. En résumé :
```bash
export TB_BACKUP_DIR="/chemin/vers/mes/backups"
.venv/bin/python3 thunderbird_guardian.py
```

### Politique de rétention

```bash
export TB_KEEP_DAILY=7      # 7 derniers jours
export TB_KEEP_WEEKLY=4     # 4 dernières semaines
export TB_KEEP_MONTHLY=6    # 6 derniers mois
```
Grâce à la déduplication de restic, garder un historique plus long que
l'ancien système (7 sauvegardes complètes, point) coûte très peu d'espace
disque supplémentaire.

### Notifications

- **Desktop (notify-send)** : automatique à chaque run (succès et échec),
  aucune configuration nécessaire au-delà de `DISPLAY`/`DBUS_SESSION_BUS_ADDRESS`
  exportés dans la crontab.
- **E-mail (Resend)** : uniquement en cas d'échec, configuré via `.env`
  (`RESEND_API_KEY`, `EMAIL_FROM`, `EMAIL_TO`). Proton Mail ne peut pas être
  l'expéditeur sans abonnement payant (SMTP nécessite Proton Mail Bridge) —
  Resend envoie *vers* une adresse Proton sans problème, il sert seulement
  de relais d'envoi.

---

## 🔄 AUTOMATISATION CRON

```bash
crontab -e
```
```
40 18 * * * export DISPLAY=:0 && export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus && cd /home/$USER/PycharmProjects/Backup-Thunderbird && .venv/bin/python3 thunderbird_guardian.py
```

Pas de redirection `>> fichier.log 2>&1` : le script gère déjà son propre
log rotaté (`guardian_automated.log` sur le disque de destination). Une
redirection brute en plus a fait grossir un fichier à 21 Mo en 6 mois sur
l'ancienne version — inutile avec le logging actuel.

Si le disque externe n'est pas encore monté à l'heure du cron, le script
réessaie automatiquement (`TB_MOUNT_RETRY_ATTEMPTS` tentatives espacées de
`TB_MOUNT_RETRY_DELAY` secondes — 6 tentatives / 5 min par défaut, soit 30
min de marge) avant d'abandonner et d'alerter.

---

## 🛠️ COMMANDES UTILES

```bash
# Sauvegarde manuelle
.venv/bin/python3 thunderbird_guardian.py

# Vérification complète du dépôt (lit et vérifie chaque bloc, lent)
.venv/bin/python3 thunderbird_guardian.py --verify

# Lister les sauvegardes disponibles
export RESTIC_PASSWORD="$(python3 -c "import keyring; print(keyring.get_password('thunderbird_backup_guardian','encryption_password'))")"
restic -r "/chemin/vers/Backup Thunderbird/restic-repo" snapshots

# Réinitialiser le mot de passe
python3 -c "import keyring; keyring.delete_password('thunderbird_backup_guardian', 'encryption_password')"
.venv/bin/python3 thunderbird_guardian.py --init
```

---

## 📊 CE QUE FAIT LE SCRIPT AUTOMATIQUEMENT

1. ✅ **Attente/retry** si le disque externe n'est pas encore monté
2. ✅ **Fermeture propre** de Thunderbird — annule la sauvegarde s'il refuse de se fermer
3. ✅ **Chiffrement AES-256 réel** + déduplication (restic)
4. ✅ **Vérification d'intégrité** avant et après chaque sauvegarde
5. ✅ **Rétention fine** (quotidien/hebdo/mensuel) avec purge automatique
6. ✅ **Script de restauration** auto-régénéré à chaque run, autonome (bascule
   sur le binaire restic embarqué si absent du système)
7. ✅ **Notifications** desktop systématiques + e-mail en cas d'échec
8. ✅ **Redémarrage** de Thunderbird après la sauvegarde

---

## 🔐 RESTAURATION

Voir **`DISASTER_RECOVERY.md`** — couvre la restauration simple, la panne
totale avec réinstallation d'OS, l'absence de `restic` sur la machine de
secours, un disque en début de défaillance, et la perte du mot de passe.

---

## 🐛 DÉPANNAGE

### `❌ ERREUR: module 'X' manquant`

Le script n'est pas lancé avec l'interpréteur du venv :
```bash
.venv/bin/python3 thunderbird_guardian.py
# et pas: python3 thunderbird_guardian.py
```

### `❌ Dépendances système manquantes : restic`

```bash
sudo apt install restic
```

### `❌ Mot de passe non initialisé`

```bash
.venv/bin/python3 thunderbird_guardian.py --init
```

### Le disque n'est jamais détecté

```bash
export TB_LOG_LEVEL=DEBUG
.venv/bin/python3 thunderbird_guardian.py
```
Vérifie le point de montage réel (`/media/$USER/...` vs `/run/media/$USER/...`
selon l'environnement de bureau) et ajuste `TB_BACKUP_DIR` si besoin — voir
`CONFIGURATION_REPERTOIRE.md`.

### Le script ne s'exécute pas en CRON

```bash
grep CRON /var/log/syslog | tail -20
crontab -l
```

---

## 📝 VARIABLES D'ENVIRONNEMENT

| Variable | Défaut | Description |
|----------|--------|-------------|
| `TB_BACKUP_DIR` | Auto-détecté | Répertoire de destination (priorité absolue) |
| `TB_SOURCE_DIR` | `~/.thunderbird` | Répertoire source |
| `TB_KEEP_DAILY` | `7` | Sauvegardes quotidiennes conservées |
| `TB_KEEP_WEEKLY` | `4` | Sauvegardes hebdomadaires conservées |
| `TB_KEEP_MONTHLY` | `6` | Sauvegardes mensuelles conservées |
| `TB_MOUNT_RETRY_ATTEMPTS` | `6` | Tentatives d'attente du disque |
| `TB_MOUNT_RETRY_DELAY` | `300` | Secondes entre deux tentatives |
| `TB_LOG_LEVEL` | `INFO` | Verbosité (DEBUG/INFO/WARNING/ERROR) |

Dans `.env` (jamais versionné) :

| Variable | Description |
|----------|--------------|
| `RESEND_API_KEY` | Clé API Resend pour l'envoi d'alertes e-mail |
| `EMAIL_FROM` | Adresse expéditrice (ex: `onboarding@resend.dev`) |
| `EMAIL_FROM_NAME` | Nom affiché de l'expéditeur |
| `EMAIL_TO` | Adresse destinataire des alertes d'échec |

---

**🎉 Le système est autonome dès que le cron est en place — il n'a besoin
d'aucune intervention tant que le disque est branché à l'heure prévue.**
