# 🎯 CONFIGURATION DU RÉPERTOIRE DE SAUVEGARDE

## Comment le script choisit sa destination

`resolve_dest_dir()` dans `thunderbird_guardian.py` suit cet ordre, dans
cette priorité :

1. **Variable d'environnement `TB_BACKUP_DIR`**, si définie — prioritaire,
   sans exception.
2. **Auto-détection**, dans l'ordre :
   - `/run/media/$USER/Disk_1/Backup Thunderbird`
   - `/media/$USER/Disk_1/Backup Thunderbird`
   - `~/thunderbird_backups`

   Le premier chemin dont le *parent* existe est retenu (le dossier final
   `Backup Thunderbird` est créé s'il n'existe pas encore).
3. **Repli final** : `~/thunderbird_backups` si rien d'autre n'a matché.

Les deux préfixes `/run/media/` et `/media/` sont testés parce que le point
de montage automatique d'un disque externe varie selon l'environnement de
bureau (KDE monte généralement sous `/run/media/`, d'autres sous `/media/`).

## Vérifier où le script sauvegarde actuellement

```bash
.venv/bin/python3 thunderbird_guardian.py
```
Cherche cette ligne dans les logs :
```
✅ Destination: /chemin/vers/repertoire
```

## Changer la destination

### Temporaire (session courante)
```bash
export TB_BACKUP_DIR="/media/$USER/Disk_1/Backup Thunderbird"
.venv/bin/python3 thunderbird_guardian.py
```

### Permanent
```bash
echo 'export TB_BACKUP_DIR="/media/$USER/Disk_1/Backup Thunderbird"' >> ~/.bashrc
source ~/.bashrc
```

### Pour CRON
```bash
crontab -e
```
```
TB_BACKUP_DIR="/media/$USER/Disk_1/Backup Thunderbird"
40 18 * * * export DISPLAY=:0 && ... && .venv/bin/python3 thunderbird_guardian.py
```

## 🐛 DÉPANNAGE

### Le disque n'existe pas ?
```bash
ls -la "/media/$USER/Disk_1/Backup Thunderbird"
mkdir -p "/media/$USER/Disk_1/Backup Thunderbird"
chmod 700 "/media/$USER/Disk_1/Backup Thunderbird"
```

### Le script attend le disque puis abandonne

C'est volontaire : si le disque n'est pas monté à l'heure du cron, le script
réessaie automatiquement (`TB_MOUNT_RETRY_ATTEMPTS` / `TB_MOUNT_RETRY_DELAY`,
30 min de marge par défaut) avant d'envoyer une alerte d'échec. Vérifie que
le disque est bien branché et monté avant l'heure programmée, ou augmente
`TB_MOUNT_RETRY_DELAY`/`TB_MOUNT_RETRY_ATTEMPTS`.

### Vérification finale
```bash
test -d "/media/$USER/Disk_1/Backup Thunderbird" && echo "✅ OK" || echo "❌ MANQUANT"
test -w "/media/$USER/Disk_1/Backup Thunderbird" && echo "✅ ÉCRITURE OK" || echo "❌ PAS DE DROITS"

export TB_LOG_LEVEL=DEBUG
.venv/bin/python3 thunderbird_guardian.py
```
