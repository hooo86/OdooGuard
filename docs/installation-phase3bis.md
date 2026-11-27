# Phase 3 bis : copie hors ligne vers G:\

## 1. Principe

La copie hors ligne est le **dernier recours**. Elle sert si OdooGuard, server01 et
les deux NAS sont perdus (incendie, rançongiciel qui a chiffré tout le réseau…). Elle ne
protège que si le disque est **déconnecté** en dehors de la copie : disque débranché et
rangé ailleurs, ou au minimum mis hors ligne par le script.

```
OdooGuard ──(copie horaire vérifiée + MANIFEST)──► NAS 192.168.8.30  \\…\odoo\odooguard
                                               └─► NAS 192.168.3.19  \\…\Backups\odooguard
Poste Windows ── Copy-OdooGuardOffline.ps1 (hebdomadaire) ──► G:\OdooGuard ── hors ligne
       └── rapport ──► OdooGuard (tableau de bord, alertes, Zabbix : composant ext:offline)
```

`deploy/windows/Copy-OdooGuardOffline.ps1` remplace l'ancien script de copie. Ce dernier
ne m'a pas été transmis : le nouveau script est écrit à partir des besoins, et l'ancien
peut tourner en parallèle pendant la transition (paramètre `-ExtraSources`, voir 4.3).

**Ce que le script garantit :**

| Risque | Parade |
|---|---|
| Mauvais disque branché | étiquette de volume vérifiée (`ODOOGUARD-A`, `ODOOGUARD-B`…) |
| Disque défaillant ou en lecture seule | test d'écriture de 4 Mo avec relecture avant la copie |
| Disque trop petit | espace nécessaire calculé d'après le manifeste, plus une marge de 50 Gio |
| NAS principal en panne ou en erreur d'E/S | bascule automatique sur le NAS secondaire, avec avertissement |
| Copie NAS ancienne (OdooGuard arrêté) | âge du manifeste contrôlé (26 h max) : avertissement |
| Copie interrompue ou incohérente | **vérification fichier par fichier** contre le manifeste d'OdooGuard (présence et taille), seconde passe puis échec |
| Dépôt copié pendant qu'OdooGuard l'alimente | ordre de copie sûr (index avant données), puis vérification |
| Rançongiciel sur le poste Windows | disque mis **hors ligne** après la copie (option), copie hebdomadaire seulement |
| Copie qui ne tourne plus | rapport à OdooGuard après chaque exécution ; sans rapport réussi depuis 8 jours, alerte critique |
| Données lisibles par qui trouve le disque | dépôts chiffrés (AES-256) ; secrets sous pli scellé |

Le **manifeste** (`MANIFEST.json` + `MANIFEST.files.tsv`) est écrit par OdooGuard sur
chaque NAS à la fin de chaque copie horaire, **après** vérification. Il liste chaque
fichier des deux dépôts avec sa taille. Il est recopié sur G:\ avec la copie, ce qui
permet d'en revérifier le contenu plus tard.

## 2. Matériel

- **Deux disques externes** au moins, en rotation : l'un branché pour la copie, l'autre
  hors site. Capacité ≥ 1,5 × la taille des dépôts OdooGuard (`total_bytes` de
  `MANIFEST.json`, ou page « Sauvegardes » du tableau de bord), soit environ 1 To au départ.
- Formatage NTFS. Étiquettes `ODOOGUARD-A`, `ODOOGUARD-B`, et lettre G: fixée dans
  `diskmgmt.msc`.
- Relever le numéro de série de chaque disque, pour la mise hors ligne automatique :

  ```powershell
  Get-Disk | Select-Object Number, FriendlyName, SerialNumber, Size
  ```

## 3. Mise à jour d'OdooGuard

Le manifeste est produit par OdooGuard à partir de cette version :

```bash
cd /root/odooguard-src && git pull
```
```bash
sudo rsync -a --delete --exclude .git --exclude tests --exclude venv ./ /opt/odooguard/
```
```bash
sudo systemctl start odooguard-copy.service
```

Vérifier ensuite la présence de `\\192.168.8.30\odoo\odooguard\MANIFEST.json`. Le
manifeste est réécrit à chaque copie horaire.

## 4. Installation sur le poste Windows

### 4.1 Fichiers et compte de service

1. Créer `C:\OdooGuard\` et y copier les quatre scripts de `deploy\windows\` :
   `Copy-OdooGuardOffline.ps1`, `Send-OdooGuardReport.ps1`,
   `Register-OdooGuardOfflineTask.ps1` et `Get-VeeamStatus.ps1`.
2. Copier le jeton `/etc/odooguard/tokens/offline` d'OdooGuard dans
   `C:\OdooGuard\offline.token`, puis en restreindre l'accès :

   ```powershell
   icacls C:\OdooGuard\offline.token /inheritance:r /grant:r "SYSTEM:R" "Administrateurs:R" "svc-odooguard:R"
   ```

3. Créer un compte local dédié `svc-odooguard`, membre des Administrateurs : la mise en
   ligne et hors ligne du disque l'exige. Mot de passe long, rangé avec les autres secrets.
4. Sur les NAS, donner à un compte l'accès **en lecture seule** aux dossiers
   `odoo\odooguard` et `Backups\odooguard`.
5. Enregistrer l'identifiant NAS **avec le compte de service**. Il est chiffré par DPAPI
   et lisible uniquement par ce compte, sur ce poste :

   ```powershell
   runas /user:.\svc-odooguard "powershell -NoProfile -ExecutionPolicy Bypass -File C:\OdooGuard\Copy-OdooGuardOffline.ps1 -SaveNasCredential -CredentialFile C:\OdooGuard\nas.cred.xml"
   ```

### 4.2 Premier essai, à la main

Le disque G: étant branché, dans une console PowerShell **administrateur** :

```powershell
C:\OdooGuard\Copy-OdooGuardOffline.ps1 -CredentialFile C:\OdooGuard\nas.cred.xml -AllowedVolumeLabels ODOOGUARD-A,ODOOGUARD-B -CertThumbprint <EMPREINTE SHA-1 d'install-phase2.sh> -Verbose
```

La première copie transfère tout, de l'ordre de 300 Gio à 1 To selon l'historique : compter
plusieurs heures. Les suivantes ne copient que les nouveaux fichiers. En fin de copie :
- `G:\OdooGuard\DERNIERE_COPIE.txt` résume la copie ;
- `C:\OdooGuard\logs\` contient le journal et le détail robocopy ;
- la page d'état d'OdooGuard affiche « Copie hors ligne G:\ » en OK.

Codes de retour du script : `0` réussi, `1` réussi avec avertissements (NAS secondaire
utilisé, source ancienne…), `2` échec.

### 4.3 Tâche planifiée

```powershell
C:\OdooGuard\Register-OdooGuardOfflineTask.ps1 -Account '.\svc-odooguard' -Day Saturday -At '10:00' -Arguments "-CredentialFile C:\OdooGuard\nas.cred.xml -AllowedVolumeLabels ODOOGUARD-A,ODOOGUARD-B -DiskSerialNumbers <SÉRIE-A>,<SÉRIE-B> -CertThumbprint <EMPREINTE>"
```

- `-DiskSerialNumbers` active la mise **en ligne** du disque au début et **hors ligne**
  à la fin. Le disque peut ainsi rester branché sans être exposé. Le débrancher et
  l'échanger reste préférable.
- Transition : pour continuer à copier les anciennes archives d'`odoo_backup.sh`, ajouter
  `-ExtraSources @{ancien='\\192.168.8.30\odoo\<dossier des anciennes sauvegardes>'}`.
  Ce paramètre ne passe pas par `-File` ; utiliser plutôt une petite surcouche
  `C:\OdooGuard\copie.ps1` qui appelle le script avec ce paramètre.
- Rythme : hebdomadaire par défaut. S'il change, ajuster `[external:offline] max_age`
  dans `/etc/odooguard/odooguard.conf` (8 jours par défaut).

### 4.4 Rotation des disques

Chaque semaine (par exemple le lundi) :
1. vérifier au tableau de bord que la copie du samedi est OK ;
2. débrancher le disque en service et le ranger **hors site** (coffre, autre bâtiment) ;
3. brancher l'autre disque.

Le contenu de `DERNIERE_COPIE.txt` indique de quand date chaque disque. Le rapport précise
aussi l'étiquette du disque utilisé.

## 5. Contrôle périodique du contenu (recommandé chaque trimestre)

La vérification par manifeste garantit une copie **complète**. Pour garantir aussi que
les données sont **lisibles**, on peut tester restic depuis Windows avec `restic.exe`
(binaire officiel Windows) et le mot de passe restic du pli scellé :

```powershell
restic.exe -r G:\OdooGuard\restic check --read-data-subset=5%
```

Le test complet, une restauration réelle depuis le disque, est décrit dans
`docs/restauration.md`, partie B4.

## 6. Retour arrière

```powershell
Unregister-ScheduledTask -TaskName 'OdooGuard - copie hors ligne G' -Confirm:$false
```

Réactiver ensuite l'ancien script de copie. Côté OdooGuard, le manifeste est inoffensif
et peut rester. Pour supprimer l'alerte « pas de rapport », mettre
`[external:offline] enabled = no`.

## 7. Tests

- **OdooGuard** (`python3 -m pytest -q`) : écriture du manifeste. Il contient les deux
  dépôts, mais ni les verrous restic ni les fichiers temporaires. Le TSV est exact et son
  empreinte SHA-256 figure dans l'en-tête.
- **Scripts PowerShell** : **pas encore exécutés.** Je n'ai pas pu installer PowerShell
  dans mon environnement de développement. Seuls ont été vérifiés l'équilibre des
  délimiteurs, l'encodage UTF-8 avec BOM et la compatibilité avec Windows PowerShell 5.1
  (pas d'opérateur `?.`, `??`, etc.). Le premier essai manuel (4.2) sert donc de test
  d'acceptation. Faire aussi un essai avec le NAS principal débranché, pour vérifier la
  bascule, et un avec un disque d'étiquette non autorisée, qui doit être refusé.
