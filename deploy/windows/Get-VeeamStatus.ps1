<#
.SYNOPSIS
    Relève l'état des travaux Veeam Backup & Replication et l'envoie à OdooGuard.

.DESCRIPTION
    À planifier sur le serveur Veeam (Planificateur de tâches, toutes les heures, compte
    disposant du rôle « Veeam Backup Viewer » au minimum) :

      powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\OdooGuard\Get-VeeamStatus.ps1

    Résultat envoyé :
      * failed  si un travail surveillé a échoué, ou n'a pas tourné depuis -MaxAgeHours ;
      * warning si un travail s'est terminé avec avertissements ;
      * success sinon.
    Le détail par travail (résultat, fin, taille) apparaît dans le tableau de bord.

    Testé sur la structure d'objets de VBR 12 (module Veeam.Backup.PowerShell).
    [À VÉRIFIER] sur la version installée : exécuter une première fois à la main avec -Verbose.

.PARAMETER JobNames
    Travaux à surveiller (par défaut : tous les travaux de sauvegarde planifiés).
#>
[CmdletBinding()]
param(
    [string[]] $JobNames = @(),
    [int] $MaxAgeHours = 26,
    [string] $Server = 'https://192.168.8.81:8443',
    [string] $TokenFile = 'C:\OdooGuard\veeam.token',
    [string] $CertThumbprint = ''
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

try {
    Import-Module Veeam.Backup.PowerShell -DisableNameChecking -ErrorAction Stop
    $jobs = @(Get-VBRJob | Where-Object { $_.IsScheduleEnabled -or $JobNames.Count -gt 0 })
    if ($JobNames.Count -gt 0) { $jobs = @($jobs | Where-Object { $JobNames -contains $_.Name }) }
    if ($jobs.Count -eq 0) { throw "aucun travail Veeam trouvé (filtre : $($JobNames -join ', '))" }

    $now = Get-Date
    $details = @()
    $worst = 'success'
    $lastEnd = $null
    $totalSize = [long]0
    $totalDuration = 0.0

    foreach ($job in $jobs) {
        $s = $job.FindLastSession()
        $result = 'None'; $end = $null; $size = $null; $dur = $null
        if ($s) {
            $result = [string]$s.Result
            if ($s.EndTime -and $s.EndTime.Year -gt 2000) { $end = $s.EndTime }
            try { $size = [long]$s.BackupStats.BackupSize } catch { $size = $null }
            if ($end) { $dur = ($end - $s.CreationTime).TotalSeconds }
        }
        $state = switch ($result) {
            'Success' { 'success' }
            'Warning' { 'warning' }
            default   { 'failed' }    # Failed, None (jamais exécuté / en cours sans fin)
        }
        if ($end -and ($now - $end).TotalHours -gt $MaxAgeHours) { $state = 'failed'; $result = "$result (trop ancien)" }
        if (-not $end -and $result -eq 'None') { $state = 'failed'; $result = 'jamais exécuté' }

        if ($state -eq 'failed' -or ($state -eq 'warning' -and $worst -eq 'success')) { $worst = $state }
        if ($end -and (-not $lastEnd -or $end -gt $lastEnd)) { $lastEnd = $end }
        if ($size) { $totalSize += $size }
        if ($dur) { $totalDuration += $dur }
        $details += [ordered]@{
            job      = $job.Name
            result   = $result
            end      = $(if ($end) { $end.ToString('yyyy-MM-ddTHH:mm:sszzz') } else { $null })
            size     = $size
        }
        Write-Verbose ("{0,-40} {1,-10} {2}" -f $job.Name, $result, $end)
    }

    $bad = @($details | Where-Object { $_.result -notmatch '^Success$' })
    if ($bad.Count -eq 0) {
        $msg = "$($jobs.Count) travail(aux) Veeam OK"
    } else {
        $msg = ($bad | ForEach-Object { "$($_.job) : $($_.result)" }) -join ' ; '
    }

    & (Join-Path $here 'Send-OdooGuardReport.ps1') -Name veeam -Status $worst -Message $msg `
        -FinishedAt $lastEnd -SizeBytes $totalSize -DurationSeconds $totalDuration `
        -Details @{ jobs = $details } -Server $Server -TokenFile $TokenFile -CertThumbprint $CertThumbprint
}
catch {
    # Même une erreur du script de relevé doit apparaître dans OdooGuard
    & (Join-Path $here 'Send-OdooGuardReport.ps1') -Name veeam -Status failed `
        -Message "relevé Veeam impossible : $($_.Exception.Message)" `
        -Server $Server -TokenFile $TokenFile -CertThumbprint $CertThumbprint
    exit 1
}
