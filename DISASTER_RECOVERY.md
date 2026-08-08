# 🆘 GUIDE DE RESTAURATION — TOUS SCÉNARIOS

Ce document couvre la restauration du profil Thunderbird depuis le dépôt
restic, du cas le plus simple (erreur de manipulation) au pire cas (PC mort,
OS réinstallé, disque de secours à récupérer sur une machine inconnue).

**Principe de sécurité dans tous les cas** : la restauration écrit toujours
dans un dossier séparé (`~/.thunderbird-restored-<date>`), jamais directement
sur `~/.thunderbird`. Rien n'est écrasé tant que tu n'as pas vérifié le
résultat et fait le basculement toi-même.

---

## Scénario 1 — Erreur de manipulation (cas courant)

Le PC actuel fonctionne, le disque de sauvegarde est branché, tu veux juste
revenir en arrière (mail supprimé par erreur, dossier corrompu...).

```bash
cd "/run/media/$USER/Disk_1/Backup Thunderbird"
bash RESTORE_EMERGENCY.sh
```

Le script :
1. Te demande le mot de passe restic (trousseau système, Proton Pass, ou mémoire)
2. Affiche la liste des sauvegardes disponibles (`restic snapshots`)
3. Te laisse choisir laquelle restaurer (vide = la plus récente)
4. Restaure dans `~/.thunderbird-restored-<date>` — jamais en écrasant l'actif

Pour ne récupérer qu'un élément précis (un dossier, un fichier) sans tout
restaurer :
```bash
export RESTIC_PASSWORD='ton_mot_de_passe'
restic -r "/run/media/$USER/Disk_1/Backup Thunderbird/restic-repo" \
  restore latest --target /tmp/restauration_partielle \
  --include "*.thunderbird/ImapMail/nom_du_dossier*"
```

Pour explorer le contenu d'une sauvegarde sans rien extraire (montage FUSE
en lecture seule) :
```bash
mkdir -p /tmp/tb-browse
restic -r "/run/media/$USER/Disk_1/Backup Thunderbird/restic-repo" mount /tmp/tb-browse
# navigue dans /tmp/tb-browse/snapshots/... puis Ctrl+C pour démonter
```

---

## Scénario 2 — Catastrophe totale : PC mort, Kubuntu réinstallé

### Étape 0 — Avant de commencer : le mot de passe

Sans le mot de passe du dépôt restic, **aucune récupération n'est possible**
— c'est un vrai chiffrement AES-256, pas de porte dérobée. Cherche-le dans
l'ordre :
1. **Proton Pass** (app mobile ou web, si tu l'as configuré) — survit à la
   mort du PC puisqu'il est synchronisé sur les serveurs Proton
2. Une copie papier si tu en as fait une
3. Ta mémoire

Si aucune des trois ne fonctionne, arrête-toi ici : ni moi ni personne ne
peut déchiffrer le dépôt sans ce mot de passe.

### Étape 1 — Réinstaller les paquets nécessaires

Seul `restic` est un vrai prérequis pour restaurer les données — la
restauration elle-même (localiser le disque, extraire les fichiers) ne
touche jamais à Thunderbird. **Thunderbird n'est nécessaire qu'à la toute
dernière étape**, pour rouvrir le profil une fois restauré — tu peux
l'installer avant, après, ou pendant que la restauration tourne, l'ordre
n'a aucune importance technique.

Si tu as accès à internet sur la nouvelle installation :
```bash
sudo apt update
sudo apt install restic thunderbird
```

Si tu n'as **pas** accès à internet (ou que `apt install restic` échoue), le
binaire restic officiel est déjà sur le disque de sauvegarde — voir Scénario 3.
Thunderbird, lui, peut attendre d'avoir de nouveau internet si besoin,
puisqu'il n'est requis qu'à la fin.

### Étape 2 — Brancher le disque et le localiser

Le point de montage automatique varie selon la version d'Ubuntu/l'environnement
de bureau (KDE monte souvent sous `/run/media/`, d'autres sous `/media/`) :
```bash
ls /run/media/$USER/ 2>/dev/null
ls /media/$USER/ 2>/dev/null
```
Repère le dossier `Backup Thunderbird` sur le disque `Disk_1`.

### Étape 3 — Restaurer

```bash
cd "/chemin/trouvé/à/l'étape/2/Backup Thunderbird"
bash RESTORE_EMERGENCY.sh
```

Le script détecte lui-même si `restic` est absent du système et bascule
automatiquement sur le binaire embarqué (`restic-bin/restic` à côté du
script) — aucune action supplémentaire de ta part.

### Étape 4 — Vérifier avant de basculer

Les fichiers sont dans `~/.thunderbird-restored-<date>`. Vérifie que ça a
l'air complet (taille cohérente avec tes sauvegardes habituelles, dossiers
de mails présents) avant de continuer.

### Étape 5 — Activer le profil restauré

```bash
pkill -x thunderbird 2>/dev/null || true
mv ~/.thunderbird-restored-<date>/.thunderbird ~/.thunderbird
thunderbird &
```

Le nom d'utilisateur Linux de la nouvelle installation peut être différent
de celui d'origine — aucun problème, tout le processus utilise `$HOME`
dynamiquement, et la sauvegarde elle-même ne contient aucun chemin absolu
ni nom d'utilisateur (voir `thunderbird_guardian.py::do_backup`).

---

## Scénario 3 — `restic` introuvable ET le binaire embarqué a disparu/est corrompu

Télécharge le binaire officiel statique (aucune dépendance système, tourne
sur n'importe quelle distribution Linux x86-64) **avec vérification du
checksum avant utilisation** — ne jamais exécuter un binaire téléchargé sans
vérifier qu'il correspond exactement à ce que restic a publié :

```bash
API=$(curl -s https://api.github.com/repos/restic/restic/releases/latest)
BIN_URL=$(echo "$API" | grep browser_download_url | grep "linux_amd64.bz2" | cut -d '"' -f4)
SUMS_URL=$(echo "$API" | grep browser_download_url | grep '"SHA256SUMS"' | cut -d '"' -f4)

curl -sL -o restic.bz2 "$BIN_URL"
curl -sL -o SHA256SUMS "$SUMS_URL"

EXPECTED=$(grep "linux_amd64.bz2" SHA256SUMS | awk '{print $1}')
ACTUAL=$(sha256sum restic.bz2 | awk '{print $1}')

if [ "$EXPECTED" != "$ACTUAL" ]; then
    echo "❌ Checksum invalide — fichier corrompu ou compromis, NE PAS UTILISER"
    exit 1
fi
echo "✅ Checksum vérifié : $ACTUAL"

bunzip2 restic.bz2
chmod +x restic
sudo mv restic /usr/local/bin/restic
```

Nécessite un accès internet sur la machine de secours. Le script utilise
uniquement `curl`/`grep`/`awk`/`sha256sum` (aucune dépendance à `jq` ou autre
outil qui pourrait manquer sur une installation fraîche).

---

## Scénario 4 — Le disque de sauvegarde montre des signes de défaillance

Si le disque a des erreurs de lecture (ralentissements anormaux, erreurs
`Input/output error`, comme observé le 25/07/2026) : **ne lance aucune
opération d'écriture dessus** (ni backup, ni `restic forget`/`prune`). Fais
d'abord une image bit-à-bit sur un disque sain avant toute autre tentative :

```bash
sudo ddrescue -d -r3 /dev/sdX /chemin/vers/nouveau/disque/image.img /chemin/vers/mapfile.log
```

Puis travaille sur la copie, jamais sur le disque défaillant original.

---

## Scénario 5 — Vérifier l'état du dépôt sans restaurer

```bash
export RESTIC_PASSWORD='ton_mot_de_passe'
restic -r "/run/media/$USER/Disk_1/Backup Thunderbird/restic-repo" snapshots
restic -r "/run/media/$USER/Disk_1/Backup Thunderbird/restic-repo" check --read-data
```
Équivalent à `python3 thunderbird_guardian.py --verify` si le venv du projet
est disponible sur la machine.

---

## Ce qui ne peut PAS arriver à cette sauvegarde

- **Mot de passe oublié** : ce n'est pas un bug à corriger — c'est le
  fonctionnement voulu d'un vrai chiffrement AES-256. La seule protection
  est d'avoir une copie durable du mot de passe (voir Scénario 2, étape 0).
- **Changement de nom d'utilisateur Linux ou de machine** : aucun impact,
  tout repose sur `$HOME` et sur le disque externe, jamais sur une donnée
  propre à l'ancienne installation.
