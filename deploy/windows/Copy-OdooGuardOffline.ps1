<#
.SYNOPSIS
    Copie hors ligne des sauvegardes OdooGuard vers un disque amovible (G:\), avec
    vérification complète, et rapport à OdooGuard. Remplace l'ancien script de copie.

.DESCRIPTION
    Source : la copie NAS des dépôts OdooGuard (\\192.168.8.30\odoo\odooguard, à défaut
    \\192.168.3.19\Backups\odooguard). Chaque copie NAS contient :
      pgbackrest\   dépôt pgBackRest chiffré (base PostgreSQL, restauration à un instant précis)
      restic\       dépôt restic chiffré (filestore, modules, configuration)
      MANIFEST.json + MANIFEST.files.tsv   liste des fichiers d'une copie VÉRIFIÉE par OdooGuard

    Déroulement :
      1. (option) remise en ligne du disque hors ligne (-DiskSerialNumbers) ;
      2. contrôle du disque cible : lettre, étiquette autorisée (rotation de disques),
         test d'écriture avec relecture, espace libre ;
      3. choix de la source : le manifeste le plus récent et suffisamment frais ;
      4. copie robocopy dans un ordre sûr (fichiers d'index avant les données) ;
      5. VÉRIFICATION : chaque fichier du manifeste doit exister sur G: avec la même
         taille ; sinon une seconde passe est tentée, puis échec ;
      6. écriture de G:\OdooGuard\DERNIERE_COPIE.txt et du manifeste ;
      7. rapport à OdooGuard (tableau de bord, alertes, Zabbix) ;
      8. (option) mise hors ligne du disque : un rançongiciel ne peut pas chiffrer
         un disque hors ligne.

    Les dépôts sont chiffrés : la copie ne contient aucune donnée lisible sans les
    secrets conservés sous pli scellé (restic.pass, pgbackrest-cipher.pass).

    Compatible Windows PowerShell 5.1 et PowerShell 7. À exécuter en administrateur
    (mise en ligne / hors ligne du disque), par une tâche planifiée :
    voir docs/installation-phase3bis.md.

.EXAMPLE
    # Enregistrer l'identifiant du NAS (une fois, avec le compte de la tâche planifiée)
    .\Copy-OdooGuardOffline.ps1 -SaveNasCredential -CredentialFile C:\OdooGuard\nas.cred.xml

.EXAMPLE
    .\Copy-OdooGuardOffline.ps1 -CredentialFile C:\OdooGuard\nas.cred.xml `
        -AllowedVolumeLabels ODOOGUARD-A,ODOOGUARD-B -DiskSerialNumbers 'WD-WX12A3456789','WD-WX98B7654321' `
        -TokenFile C:\OdooGuard\offline.token -CertThumbprint 0123ABCD...
#>
[CmdletBinding()]
param(
    [string[]] $Sources = @('\\192.168.8.30\odoo\odooguard', '\\192.168.3.19\Backups\odooguard'),
    [string] $TargetDrive = 'G:',
    [string] $TargetFolder = 'OdooGuard',
    # Étiquettes de volume acceptées (rotation de plusieurs disques). Vide = toute étiquette (déconseillé).
    [string[]] $AllowedVolumeLabels = @('ODOOGUARD-A', 'ODOOGUARD-B'),
    # Numéros de série des disques à remettre en ligne puis hors ligne. Vide = pas de gestion.
    [string[]] $DiskSerialNumbers = @(),
    [int] $MaxSourceAgeHours = 26,
    [int] $MinFreeGB = 50,
    [int] $RobocopyThreads = 8,
    # Copies supplémentaires facultatives (transition) : @{ 'ancien' = '\\192.168.8.30\odoo\backups' }
    [hashtable] $ExtraSources = @{},
    [string] $CredentialFile = '',
    [switch] $SaveNasCredential,
    [string] $LogDir = 'C:\OdooGuard\logs',
    [int] $KeepLogs = 60,
    # Rapport à OdooGuard (Send-OdooGuardReport.ps1, même dossier que ce script)
    [string] $ReportServer = 'https://192.168.8.81:8443',
    [string] $TokenFile = 'C:\OdooGuard\offline.token',
    [string] $CertThumbprint = '',
    [switch] $NoReport
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# « powershell.exe -File script.ps1 -Param A,B » transmet « A,B » comme UNE chaîne :
# on découpe les listes sur la virgule.
function Split-List([string[]] $values) {
    return @($values | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
$Sources = Split-List $Sources
$AllowedVolumeLabels = Split-List $AllowedVolumeLabels
$DiskSerialNumbers = Split-List $DiskSerialNumbers
$started = Get-Date
$script:warnings = New-Object System.Collections.Generic.List[string]
$script:mappings = New-Object System.Collections.Generic.List[string]   # partages connectés par CE script

# --------------------------------------------------------------------- outils
function Write-Step([string] $msg) { Write-Output ("[{0:HH:mm:ss}] {1}" -f (Get-Date), $msg) }
function Add-Warning([string] $msg) { $script:warnings.Add($msg); Write-Warning $msg }

function Get-ShareRoot([string] $unc) {
    # \\serveur\partage\dossier -> \\serveur\partage
    $parts = $unc.TrimStart('\').Split('\')
    return '\\' + $parts[0] + '\' + $parts[1]
}

function Connect-Share([string] $unc, $cred) {
    if (-not $cred) { return }
    $root = Get-ShareRoot $unc
    $existing = Get-SmbMapping -RemotePath $root -ErrorAction SilentlyContinue
    if ($existing) { return }
    $plain = $cred.GetNetworkCredential().Password
    New-SmbMapping -RemotePath $root -UserName $cred.UserName -Password $plain -Persistent $false | Out-Null
    $plain = $null
    $script:mappings.Add($root)
}

function Disconnect-Shares {
    foreach ($root in $script:mappings) {
        Remove-SmbMapping -RemotePath $root -Force -UpdateProfile -ErrorAction SilentlyContinue | Out-Null
    }
}

function Set-TargetDisksOnline([bool] $online) {
    foreach ($sn in $DiskSerialNumbers) {
        $disk = Get-Disk | Where-Object { $_.SerialNumber -and $_.SerialNumber.Trim() -eq $sn }
        if (-not $disk) { continue }
        if ($online) {
            if ($disk.IsOffline) { Set-Disk -Number $disk.Number -IsOffline $false }
            if ($disk.IsReadOnly) { Set-Disk -Number $disk.Number -IsReadOnly $false }
            Write-Step "disque $sn (n°$($disk.Number)) en ligne"
        } elseif (-not $disk.IsOffline) {
            Set-Disk -Number $disk.Number -IsOffline $true
            Write-Step "disque $sn (n°$($disk.Number)) mis HORS LIGNE"
        }
    }
}

function Test-TargetVolume {
    $letter = $TargetDrive.TrimEnd(':', '\')
    $vol = $null
    for ($i = 0; $i -lt 30 -and -not $vol; $i++) {
        $vol = Get-Volume -DriveLetter $letter -ErrorAction SilentlyContinue
        if (-not $vol) { Start-Sleep -Seconds 2 }
    }
    if (-not $vol) { throw "lecteur $TargetDrive absent (disque non branché ou hors ligne)" }
    if ($AllowedVolumeLabels.Count -gt 0 -and $AllowedVolumeLabels -notcontains $vol.FileSystemLabel) {
        throw "le volume $TargetDrive porte l'étiquette '$($vol.FileSystemLabel)', non autorisée ($($AllowedVolumeLabels -join ', ')) : mauvais disque ?"
    }
    if ($vol.HealthStatus -ne 'Healthy') { throw "volume $TargetDrive en état $($vol.HealthStatus)" }
    if ($vol.FileSystem -notin @('NTFS', 'ReFS')) { Add-Warning "système de fichiers $($vol.FileSystem) : NTFS ou ReFS recommandé" }
    # Test d'écriture réel avec relecture (détecte un disque défaillant ou en lecture seule)
    $probe = Join-Path "$letter`:\" ".odooguard-probe-$PID"
    $bytes = New-Object byte[] (4MB)
    (New-Object System.Random).NextBytes($bytes)
    [IO.File]::WriteAllBytes($probe, $bytes)
    $back = [IO.File]::ReadAllBytes($probe)
    Remove-Item -LiteralPath $probe -Force
    $sha = [Security.Cryptography.SHA256]::Create()
    if ([BitConverter]::ToString($sha.ComputeHash($bytes)) -ne [BitConverter]::ToString($sha.ComputeHash($back))) {
        throw "relecture incohérente sur $TargetDrive (disque défaillant ?)"
    }
    return $vol
}

function Read-Manifest([string] $src) {
    $json = Join-Path $src 'MANIFEST.json'
    $tsv = Join-Path $src 'MANIFEST.files.tsv'
    if (-not (Test-Path -LiteralPath $json) -or -not (Test-Path -LiteralPath $tsv)) { return $null }
    $head = Get-Content -LiteralPath $json -Raw -Encoding UTF8 | ConvertFrom-Json
    $hash = (Get-FileHash -LiteralPath $tsv -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne $head.files_sha256) {
        Add-Warning "$src : manifeste en cours de réécriture ou incohérent (empreinte TSV différente)"
        return $null
    }
    $epoch = [DateTimeOffset]::FromUnixTimeSeconds([int64]$head.generated_at).LocalDateTime
    $head | Add-Member -NotePropertyName generated_local -NotePropertyValue $epoch
    $head | Add-Member -NotePropertyName source -NotePropertyValue $src
    $head | Add-Member -NotePropertyName age_hours -NotePropertyValue ([math]::Round(((Get-Date) - $epoch).TotalHours, 1))
    return $head
}

function Invoke-Robocopy([string] $from, [string] $to, [string[]] $extra, [string] $log) {
    $roboArgs = @($from, $to) + $extra + @('/R:2', '/W:15', '/FFT', '/XJ', '/NP', '/NDL', '/NFL',
        "/MT:$RobocopyThreads", '/COPY:DT', '/DCOPY:T', "/UNILOG+:$log")
    & robocopy.exe @roboArgs | Out-Null
    $rc = $LASTEXITCODE
    # 0-7 : succès (avec ou sans fichiers copiés / supprimés) ; >= 8 : échec
    if ($rc -ge 8) { throw "robocopy $from -> $to : code $rc (voir $log)" }
    return $rc
}

function Copy-Repositories([string] $src, [string] $dst, [string] $log) {
    $pgS = Join-Path $src 'pgbackrest'; $pgD = Join-Path $dst 'pgbackrest'
    $rsS = Join-Path $src 'restic';     $rsD = Join-Path $dst 'restic'
    # pgBackRest : fichiers *.info d'abord (ils ne citent que des sauvegardes déjà
    # écrites), puis miroir complet.
    Write-Step 'pgBackRest : fichiers d''index'
    Invoke-Robocopy $pgS $pgD @('*.info', '*.info.copy', '/S') $log | Out-Null
    Write-Step 'pgBackRest : miroir complet'
    Invoke-Robocopy $pgS $pgD @('/MIR') $log | Out-Null
    # restic : config, clés, instantanés, index, PUIS données : un instantané copié
    # ne cite que des paquets écrits avant lui, donc présents à l'étape des données.
    Write-Step 'restic : configuration, clés, instantanés, index'
    Invoke-Robocopy $rsS $rsD @('config', '/IS') $log | Out-Null
    foreach ($sub in 'keys', 'snapshots', 'index') {
        Invoke-Robocopy (Join-Path $rsS $sub) (Join-Path $rsD $sub) @('/MIR') $log | Out-Null
    }
    Write-Step 'restic : données'
    Invoke-Robocopy (Join-Path $rsS 'data') (Join-Path $rsD 'data') @('/MIR') $log | Out-Null
    Invoke-Robocopy $rsS $rsD @('/MIR', '/XD', 'locks') $log | Out-Null
}

function Test-CopyAgainstManifest([string] $src, [string] $dst) {
    $missing = 0; $badSize = 0; $checked = 0; $examples = @()
    $reader = New-Object IO.StreamReader((Join-Path $src 'MANIFEST.files.tsv'), [Text.Encoding]::UTF8)
    try {
        while ($null -ne ($line = $reader.ReadLine())) {
            if (-not $line) { continue }
            $tab = $line.LastIndexOf("`t")
            $rel = $line.Substring(0, $tab).Replace('/', '\')
            $size = [int64]$line.Substring($tab + 1)
            $checked++
            $fi = New-Object IO.FileInfo (Join-Path $dst $rel)
            if (-not $fi.Exists) { $missing++; if ($examples.Count -lt 5) { $examples += "absent : $rel" } }
            elseif ($fi.Length -ne $size) { $badSize++; if ($examples.Count -lt 5) { $examples += "taille : $rel" } }
        }
    } finally { $reader.Close() }
    return [pscustomobject]@{ Checked = $checked; Missing = $missing; BadSize = $badSize; Examples = $examples }
}

function Send-Report([string] $status, [string] $message, [int64] $size, [hashtable] $details) {
    if ($NoReport) { return }
    try {
        & (Join-Path $here 'Send-OdooGuardReport.ps1') -Name offline -Status $status -Message $message `
            -SizeBytes $size -DurationSeconds ((Get-Date) - $started).TotalSeconds -Details $details `
            -Server $ReportServer -TokenFile $TokenFile -CertThumbprint $CertThumbprint
    } catch {
        Write-Warning "rapport OdooGuard non envoyé : $($_.Exception.Message)"
    }
}

# --------------------------------------------------------------------- mode « enregistrer l'identifiant »
if ($SaveNasCredential) {
    if (-not $CredentialFile) { throw '-CredentialFile requis' }
    $c = Get-Credential -Message 'Compte d''accès aux partages NAS (lecture seule suffit)'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $CredentialFile) | Out-Null
    # Export-Clixml chiffre le mot de passe avec DPAPI : lisible UNIQUEMENT par ce
    # compte Windows sur cette machine. À exécuter avec le compte de la tâche planifiée.
    $c | Export-Clixml -LiteralPath $CredentialFile
    Write-Output "identifiant enregistré (DPAPI, compte $env:USERNAME) : $CredentialFile"
    exit 0
}

# --------------------------------------------------------------------- programme principal
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = $started.ToString('yyyyMMdd-HHmmss')
$transcript = Join-Path $LogDir "copie-$stamp.log"
$roboLog = Join-Path $LogDir "robocopy-$stamp.log"
Start-Transcript -LiteralPath $transcript | Out-Null
$exit = 0
$cred = $null
$volLabel = ''
try {
    if ($CredentialFile) { $cred = Import-Clixml -LiteralPath $CredentialFile }
    if ($DiskSerialNumbers.Count -gt 0) { Set-TargetDisksOnline $true }
    $vol = Test-TargetVolume
    $volLabel = $vol.FileSystemLabel
    Write-Step "cible $TargetDrive '$volLabel' : $([math]::Round($vol.SizeRemaining / 1GB)) Gio libres"

    # Source : manifeste valide le plus récent
    $candidates = @()
    foreach ($s in $Sources) {
        try {
            Connect-Share $s $cred
            $m = Read-Manifest $s
            if ($m) { $candidates += $m; Write-Step "source $s : copie du $($m.generated_local) ($($m.age_hours) h)" }
            else { Add-Warning "source $s : pas de manifeste exploitable" }
        } catch { Add-Warning "source $s inaccessible : $($_.Exception.Message)" }
    }
    if ($candidates.Count -eq 0) { throw 'aucune source exploitable (NAS injoignables ou sans manifeste)' }
    $m = $candidates | Sort-Object generated_at -Descending | Select-Object -First 1
    if ($m.age_hours -gt $MaxSourceAgeHours) {
        Add-Warning "la copie NAS la plus récente date de $($m.age_hours) h (> $MaxSourceAgeHours h) : OdooGuard ne copie plus vers les NAS ?"
    }
    $src = $m.source
    $dst = Join-Path ($TargetDrive.TrimEnd('\') + '\') $TargetFolder
    New-Item -ItemType Directory -Force -Path $dst | Out-Null

    # Espace : ce qui manque sur G: + marge
    $present = (Get-ChildItem -LiteralPath $dst -Recurse -File -ErrorAction SilentlyContinue |
                Measure-Object -Property Length -Sum).Sum
    if (-not $present) { $present = 0 }
    $needed = [int64]$m.total_bytes - [int64]$present + [int64]$MinFreeGB * 1GB
    if ($vol.SizeRemaining -lt $needed) {
        throw "espace insuffisant sur $TargetDrive : $([math]::Round($vol.SizeRemaining / 1GB)) Gio libres, ~$([math]::Round($needed / 1GB)) Gio nécessaires"
    }

    # Copie + vérification (2 passes au plus : la source peut évoluer pendant la copie)
    $check = $null
    for ($pass = 1; $pass -le 2; $pass++) {
        Write-Step "copie depuis $src (passe $pass)"
        Copy-Repositories $src $dst $roboLog
        if ($pass -eq 2) {
            $m2 = Read-Manifest $src
            if ($m2) { $m = $m2 }
        }
        Write-Step "vérification de $($m.file_count) fichiers"
        $check = Test-CopyAgainstManifest $src $dst
        if ($check.Missing -eq 0 -and $check.BadSize -eq 0) { break }
        Add-Warning "passe $pass : $($check.Missing) absent(s), $($check.BadSize) taille(s) différente(s) ; $($check.Examples -join ' ; ')"
        if ($pass -eq 1) { Start-Sleep -Seconds 300 }
    }
    if ($check.Missing -ne 0 -or $check.BadSize -ne 0) {
        throw "copie incomplète après 2 passes : $($check.Missing) fichier(s) absent(s), $($check.BadSize) de taille différente"
    }

    foreach ($k in $ExtraSources.Keys) {
        Write-Step "copie supplémentaire '$k' depuis $($ExtraSources[$k])"
        Connect-Share $ExtraSources[$k] $cred
        Invoke-Robocopy $ExtraSources[$k] (Join-Path $dst "extra\$k") @('/MIR') $roboLog | Out-Null
    }

    Copy-Item -LiteralPath (Join-Path $src 'MANIFEST.json') -Destination $dst -Force
    Copy-Item -LiteralPath (Join-Path $src 'MANIFEST.files.tsv') -Destination $dst -Force
    @(
        "Copie hors ligne OdooGuard",
        "Date            : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
        "Disque          : $TargetDrive '$volLabel'",
        "Source          : $src (copie NAS du $($m.generated_local))",
        "Dernière sauvegarde base : $($m.pgbackrest_last_backup)",
        "Instantanés restic : $($m.restic_snapshots)",
        "Fichiers vérifiés : $($check.Checked) ($([math]::Round($m.total_bytes / 1GB, 1)) Gio)",
        "Restauration : docs/restauration.md, partie B (secrets sous pli scellé)"
    ) | Set-Content -LiteralPath (Join-Path $dst 'DERNIERE_COPIE.txt') -Encoding UTF8
    Write-VolumeCache -DriveLetter $TargetDrive.TrimEnd(':', '\') -ErrorAction SilentlyContinue

    $status = if ($script:warnings.Count -gt 0) { 'warning' } else { 'success' }
    $msg = "Copie G: '$volLabel' vérifiée : $($check.Checked) fichiers, $([math]::Round($m.total_bytes / 1GB, 1)) Gio, sauvegarde $($m.pgbackrest_last_backup)"
    if ($script:warnings.Count -gt 0) { $msg += ' ; ' + ($script:warnings -join ' ; ') }
    Write-Step $msg
    Send-Report $status $msg ([int64]$m.total_bytes) @{ disk = $volLabel; source = $src; files = $check.Checked;
        pgbackrest_last_backup = $m.pgbackrest_last_backup; nas_copy = $m.generated_at_iso }
    if ($status -eq 'warning') { $exit = 1 }
}
catch {
    $exit = 2
    $err = $_.Exception.Message
    Write-Error "ÉCHEC de la copie hors ligne : $err" -ErrorAction Continue
    Send-Report 'failed' "Copie G: '$volLabel' en échec : $err" 0 @{ disk = $volLabel; log = $transcript }
}
finally {
    Disconnect-Shares
    if ($DiskSerialNumbers.Count -gt 0) {
        try { Set-TargetDisksOnline $false } catch { Write-Warning "mise hors ligne impossible : $($_.Exception.Message)" }
    }
    Get-ChildItem -LiteralPath $LogDir -File | Sort-Object LastWriteTime -Descending |
        Select-Object -Skip ($KeepLogs * 2) | Remove-Item -Force -ErrorAction SilentlyContinue
    Stop-Transcript | Out-Null
}
exit $exit
