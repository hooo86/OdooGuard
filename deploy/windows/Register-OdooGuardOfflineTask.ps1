<#
.SYNOPSIS
    Crée (ou met à jour) la tâche planifiée de copie hors ligne OdooGuard vers G:\.

.DESCRIPTION
    À lancer une fois, en administrateur, sur le poste Windows qui reçoit le disque G:.
    La tâche s'exécute avec un compte de service local dédié, administrateur (mise
    en ligne / hors ligne du disque), « que l'utilisateur soit connecté ou non ».

    Avant : avec CE compte, enregistrer l'identifiant NAS (chiffré DPAPI) :
      runas /user:.\svc-odooguard "powershell -NoProfile -File C:\OdooGuard\Copy-OdooGuardOffline.ps1 -SaveNasCredential -CredentialFile C:\OdooGuard\nas.cred.xml"

.EXAMPLE
    .\Register-OdooGuardOfflineTask.ps1 -Account '.\svc-odooguard' -Day Saturday -At '10:00' `
        -Arguments "-CredentialFile C:\OdooGuard\nas.cred.xml -AllowedVolumeLabels ODOOGUARD-A,ODOOGUARD-B -CertThumbprint 0123ABCD..."
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Account,
    [ValidateSet('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')]
    [string] $Day = 'Saturday',
    [string] $At = '10:00',
    [string] $ScriptPath = 'C:\OdooGuard\Copy-OdooGuardOffline.ps1',
    [string] $Arguments = '',
    [string] $TaskName = 'OdooGuard - copie hors ligne G'
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $ScriptPath)) { throw "script introuvable : $ScriptPath" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" $Arguments" `
    -WorkingDirectory (Split-Path -Parent $ScriptPath)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
    -MultipleInstances IgnoreNew -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 30)
$secure = Read-Host -AsSecureString "Mot de passe de $Account"
$plain = (New-Object System.Management.Automation.PSCredential($Account, $secure)).GetNetworkCredential().Password
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -User $Account -Password $plain -RunLevel Highest -Force | Out-Null
$plain = $null
Write-Output "tâche « $TaskName » : chaque $Day à $At, compte $Account"
Write-Output "test immédiat : Start-ScheduledTask -TaskName '$TaskName' ; journaux : C:\OdooGuard\logs"
