# 🎯 CONFIGURATION DU RÉPERTOIRE DE SAUVEGARDE

## Comment le script choisit sa destination

`resolve_dest_dir()` dans `thunderbird_guardian.py` suit une logique simple,
volontairement sans magie :

1. **Variable d'environnement `TB_BACKUP_DIR`**, si définie — utilisée telle
   quelle, sans exception.
2. **Repli** : `~/thunderbird_backups`, si `TB_BACKUP_DIR` n'est pas définie.

Le script ne tente **pas** de deviner le nom d'un disque externe — un nom
de disque est propre à chaque installation, rien à généraliser. Si tu
sauvegardes sur un disque externe, définis `TB_BACKUP_DIR` explicitement.

## Vérifier où le script sauvegarde actuellement

```bash
.venv/bin/python3 thunderbird_guardian.py
```
Cherche cette ligne dans les logs :
```
✅ Destination: /chemin/vers/repertoire
```

## Configurer un disque externe comme destination

Le point de montage automatique d'un disque externe varie selon
l'environnement de bureau (KDE monte généralement sous `/run/media/`,
d'autres sous `/media/`) — vérifie le tien :
```bash
ls /run/media/$USER/ 2>/dev/null
ls /media/$USER/ 2>/dev/null
```

### Temporaire (session courante)
```bash
export TB_BACKUP_DIR="/media/$USER/<NOM_DISQUE>/Backup Thunderbird"
.venv/bin/python3 thunderbird_guardian.py
```

### Permanent
```bash
echo 'export TB_BACKUP_DIR="/media/$USER/<NOM_DISQUE>/Backup Thunderbird"' >> ~/.bashrc
source ~/.bashrc
```

### Pour CRON
```bash
crontab -e
```
```
TB_BACKUP_DIR="/media/$USER/<NOM_DISQUE>/Backup Thunderbird"
40 18 * * * export DISPLAY=:0 && ... && .venv/bin/python3 thunderbird_guardian.py
```
Une variable définie en tête de crontab s'applique à toutes les lignes qui
suivent — nécessaire ici puisque cron ne charge pas `~/.bashrc`.

## 🐛 DÉPANNAGE

### Le disque n'existe pas ?
```bash
ls -la "/media/$USER/<NOM_DISQUE>/Backup Thunderbird"
mkdir -p "/media/$USER/<NOM_DISQUE>/Backup Thunderbird"
chmod 700 "/media/$USER/<NOM_DISQUE>/Backup Thunderbird"
```

### Le script attend le disque puis abandonne

C'est volontaire : si le disque n'est pas monté à l'heure du cron, le script
réessaie automatiquement (`TB_MOUNT_RETRY_ATTEMPTS` / `TB_MOUNT_RETRY_DELAY`,
30 min de marge par défaut) avant d'envoyer une alerte d'échec. Vérifie que
le disque est bien branché et monté avant l'heure programmée, ou augmente
`TB_MOUNT_RETRY_DELAY`/`TB_MOUNT_RETRY_ATTEMPTS`.

### Vérification finale
```bash
test -d "/media/$USER/<NOM_DISQUE>/Backup Thunderbird" && echo "✅ OK" || echo "❌ MANQUANT"
test -w "/media/$USER/<NOM_DISQUE>/Backup Thunderbird" && echo "✅ ÉCRITURE OK" || echo "❌ PAS DE DROITS"

export TB_LOG_LEVEL=DEBUG
.venv/bin/python3 thunderbird_guardian.py
```
