<#
.SYNOPSIS
    Envoie un rapport d'exécution à OdooGuard (tableau de bord, alertes, Zabbix).

.DESCRIPTION
    Utilisable seul en fin de n'importe quel script (copie hors ligne vers G:\, etc.)
    ou appelé par Get-VeeamStatus.ps1. Le jeton (fichier texte, une ligne) n'autorise
    QUE le dépôt d'un rapport pour le composant nommé : aucune lecture, aucune action.

    Protéger le fichier du jeton par ses ACL (lecture : compte de la tâche planifiée et
    Administrateurs uniquement).

    Le certificat d'OdooGuard est auto-signé : soit l'importer dans
    « Autorités de certification racines de confiance » de l'ordinateur, soit passer
    -CertThumbprint (empreinte SHA-1 affichée par install-phase2.sh) pour l'épingler.

.EXAMPLE
    .\Send-OdooGuardReport.ps1 -Name offline -Status success -Message "Copie G: OK, 412 Go" `
        -SizeBytes 442381172736 -DurationSeconds 5400 -TokenFile C:\OdooGuard\offline.token
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^[a-z0-9_-]+$')] [string] $Name,
    [Parameter(Mandatory)] [ValidateSet('success', 'warning', 'failed')] [string] $Status,
    [string] $Message = '',
    [Nullable[datetime]] $FinishedAt = $null,
    [Nullable[long]] $SizeBytes = $null,
    [Nullable[double]] $DurationSeconds = $null,
    [object] $Details = $null,
    [string] $Server = 'https://192.168.8.81:8443',
    [Parameter(Mandatory)] [string] $TokenFile,
    [string] $CertThumbprint = ''
)
$ErrorActionPreference = 'Stop'

$token = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
if (-not $FinishedAt) { $FinishedAt = Get-Date }

$body = [ordered]@{
    status      = $Status
    finished_at = $FinishedAt.ToString('yyyy-MM-ddTHH:mm:sszzz')
    message     = $Message
}
if ($null -ne $SizeBytes)       { $body.size_bytes = $SizeBytes }
if ($null -ne $DurationSeconds) { $body.duration_s = $DurationSeconds }
if ($null -ne $Details)         { $body.details = $Details }
$json = $body | ConvertTo-Json -Depth 6 -Compress

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if ($CertThumbprint) {
    # Épinglage du certificat auto-signé d'OdooGuard (Windows PowerShell 5.1).
    $expected = $CertThumbprint.Replace(' ', '').ToUpperInvariant()
    [Net.ServicePointManager]::ServerCertificateValidationCallback = {
        param($sender, $cert, $chain, $errors)
        return ($cert.GetCertHashString().ToUpperInvariant() -eq $expected)
    }.GetNewClosure()
}

$attempt = 0
while ($true) {
    $attempt++
    try {
        $r = Invoke-RestMethod -Method Post -Uri "$Server/api/v1/report/$Name" `
            -Headers @{ Authorization = "Bearer $token" } `
            -ContentType 'application/json; charset=utf-8' `
            -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 30
        Write-Output "OdooGuard : rapport '$Name' enregistré ($($r.status))"
        break
    } catch {
        if ($attempt -ge 3) {
            # L'absence de rapport sera de toute façon signalée par OdooGuard (max_age).
            Write-Warning "OdooGuard injoignable après 3 essais : $($_.Exception.Message)"
            exit 1
        }
        Start-Sleep -Seconds (10 * $attempt)
    }
}
