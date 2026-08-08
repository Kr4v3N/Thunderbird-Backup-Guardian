# Thunderbird Guardian

Sauvegarde chiffrée et automatisée du profil Thunderbird, via [restic](https://restic.net/) : chiffrement AES-256 réel, déduplication, vérification d'intégrité, rétention quotidien/hebdomadaire/mensuel, notifications desktop + e-mail en cas d'échec.

Documentation complète : [DEMARRAGE_RAPIDE.md](DEMARRAGE_RAPIDE.md) (installation, configuration) · [CONFIGURATION_REPERTOIRE.md](CONFIGURATION_REPERTOIRE.md) (choix du répertoire de destination) · [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md) (matrice complète des scénarios de restauration).

Ce README se concentre sur **comment restaurer**, en détail, pour le cas où tu lis ce fichier dans l'urgence.

---

## Installation express

```bash
sudo apt install restic thunderbird
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # puis renseigner les identifiants Resend pour les alertes e-mail
.venv/bin/python3 thunderbird_guardian.py --init
```

Détails complets : [DEMARRAGE_RAPIDE.md](DEMARRAGE_RAPIDE.md).

---

## 🆘 RESTAURATION — Guide détaillé

### Principe de sécurité

**Rien n'est jamais écrasé automatiquement.** La restauration écrit toujours
dans un dossier neuf et horodaté (`~/.thunderbird-restored-<date>`), jamais
directement sur ton profil actif. Tu vérifies le résultat, puis c'est toi
qui bascules manuellement en une commande `mv`. Si quelque chose se passe
mal, ton ancien profil (ou l'absence de profil) n'a pas bougé.

### Étape par étape

**1. Localiser le disque de sauvegarde et le script**

Sur le disque externe, dans le dossier `Backup Thunderbird`, tu trouveras :
```
Backup Thunderbird/
├── restic-repo/           ← les données chiffrées elles-mêmes
├── restic-bin/restic      ← copie de secours du binaire restic (hors-ligne)
└── RESTORE_EMERGENCY.sh   ← le script à lancer, régénéré à chaque sauvegarde
```

**2. Lancer le script**
```bash
cd "/chemin/vers/Backup Thunderbird"
bash RESTORE_EMERGENCY.sh
```

**3. Ce qui se passe, dans l'ordre**

- Le script vérifie si `restic` est installé sur la machine. Sinon, il
  bascule automatiquement sur `restic-bin/restic` embarqué sur le disque —
  aucune action de ta part, aucun besoin d'internet à ce stade.
- Il te demande le **mot de passe du dépôt restic** (celui défini à
  `--init`, retrouvable dans Proton Pass/ton gestionnaire de mots de passe
  si ce PC n'existe plus — voir FAQ).
- Il affiche la liste des sauvegardes disponibles (`restic snapshots`),
  avec leur date.
- Il te demande quel snapshot restaurer (Entrée = le plus récent).
- Il restaure dans `~/.thunderbird-restored-<date>` et t'indique le chemin
  exact où se trouve le profil restauré une fois l'opération terminée.

**4. Vérifier avant de basculer**

Regarde dans le dossier indiqué que le contenu a l'air complet (taille
cohérente, dossiers de mails présents) avant de continuer.

**5. Activer le profil restauré**

Le script affiche lui-même les commandes exactes à copier-coller à la fin,
adaptées à ton `$HOME` réel et au nom exact de ton dossier de profil. Dans
les grandes lignes :
```bash
pkill -x thunderbird 2>/dev/null || true
mv ~/.thunderbird ~/.thunderbird.old_<date>       # garde l'actif de côté, au cas où
mv ~/.thunderbird-restored-<date>/.../.thunderbird ~/.thunderbird
thunderbird &
```

### Restaurer seulement un dossier ou un e-mail précis

Pas besoin de tout restaurer pour récupérer un seul élément :
```bash
export RESTIC_PASSWORD='ton_mot_de_passe'
restic -r "/chemin/vers/Backup Thunderbird/restic-repo" \
  restore latest --target /tmp/restauration_partielle \
  --include "*NomDuDossier*"
```
Ou explorer le contenu sans rien extraire (montage en lecture seule) :
```bash
mkdir -p /tmp/tb-browse
restic -r "/chemin/vers/Backup Thunderbird/restic-repo" mount /tmp/tb-browse
```

### Scénario catastrophe (PC mort, OS réinstallé, autre machine)

Le principe reste identique — brancher le disque, lancer
`RESTORE_EMERGENCY.sh` — mais quelques points changent (localisation du
point de montage, installation de `restic`/`thunderbird`, récupération du
mot de passe). Détail complet, scénario par scénario, dans
**[DISASTER_RECOVERY.md](DISASTER_RECOVERY.md)**.

---

## FAQ

**J'ai oublié le mot de passe du dépôt restic. Y a-t-il un moyen de contourner ça ?**
Non. C'est un vrai chiffrement AES-256, pas de porte dérobée — c'est le prix
d'une protection réelle (l'ancienne version du script *croyait* chiffrer
mais ne le faisait pas du tout, ce n'est plus le cas). Le mot de passe doit
être conservé quelque part d'indépendant de ce PC (Proton Pass, autre
gestionnaire de mots de passe, copie papier). Sans lui, le dépôt est
définitivement illisible.

**La restauration va-t-elle écraser mes e-mails actuels ?**
Non, jamais automatiquement. Elle restaure toujours dans un nouveau dossier
horodaté. C'est toi qui décides, ensuite, de remplacer ton profil actif —
et le script te propose même de sauvegarder l'ancien profil à côté
(`.thunderbird.old_<date>`) avant de le remplacer.

**Le disque externe ne se monte plus au même endroit qu'avant, ça pose problème ?**
Non. `RESTORE_EMERGENCY.sh` se situe toujours à la racine du disque et
calcule son propre emplacement dynamiquement (`SCRIPT_DIR`) — peu importe
où le disque est monté, tant que tu lances le script depuis ce dossier.

**Je restaure sur un autre PC, ou avec un nom d'utilisateur Linux différent, est-ce que ça marche ?**
Oui. Tout repose sur `$HOME` (résolu dynamiquement au moment de
l'exécution) et sur le disque externe — rien ne dépend du nom de la
machine ou de l'utilisateur d'origine.

**`restic` n'est pas installé sur la machine de secours, et je n'ai pas internet.**
Utilise le binaire embarqué sur le disque (`restic-bin/restic`) —
`RESTORE_EMERGENCY.sh` bascule dessus automatiquement, sans action de ta
part. Si ce binaire a lui-même disparu et que tu as internet, voir la
procédure de téléchargement avec vérification de checksum dans
[DISASTER_RECOVERY.md](DISASTER_RECOVERY.md#scénario-3--restic-introuvable-et-le-binaire-embarqué-a-disparuest-corrompu).

**Comment je sais quelle sauvegarde restaurer si je veux revenir à une date précise (pas la plus récente) ?**
`RESTORE_EMERGENCY.sh` affiche la liste complète des sauvegardes disponibles
avec leur date avant de te demander laquelle choisir. Tu peux aussi lister
manuellement : `restic -r "<repo>" snapshots`.

**Comment vérifier qu'une sauvegarde n'est pas corrompue avant de m'en servir ?**
```bash
.venv/bin/python3 thunderbird_guardian.py --verify
```
Lance un contrôle complet (`restic check --read-data`) — plus lent qu'une
vérification courante, mais qui relit vraiment chaque bloc de données.

**Le disque de sauvegarde lui-même montre des signes de faiblesse (erreurs, lenteurs).**
Ne lance aucune écriture dessus (ni sauvegarde, ni restauration). Fais
d'abord une image bit-à-bit sur un support sain (`ddrescue`), puis travaille
sur la copie. Détail dans [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md#scénario-4--le-disque-de-sauvegarde-montre-des-signes-de-défaillance).

**Ce script de restauration a-t-il vraiment été testé, ou juste écrit ?**
Testé en exécution réelle, pas seulement relu : un dépôt restic jetable a
été créé avec de vraies données de test, restauré de bout en bout via
`RESTORE_EMERGENCY.sh`, avec comparaison bit-à-bit (`diff -r`) confirmant
l'identité parfaite entre original et restauré — à la fois avec `restic`
système et avec le binaire embarqué en secours (PATH sans `restic` simulé
explicitement). Un bug réel a été trouvé et corrigé pendant ce test (le
chemin de restauration final calculé à partir du nom court du dossier ne
correspondait pas à l'arborescence absolue réellement recréée par restic).
