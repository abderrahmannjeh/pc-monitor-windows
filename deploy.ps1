# ---------------------------------------------------------------
#  Construit l'agent puis déploie le site sur le VPS.
#  Usage :  .\deploy.ps1 -Domain monitor.votredomaine.com -Server 1.2.3.4
#  Prérequis : connexion SSH au VPS (clé SSH recommandée), OpenSSH (inclus dans Windows 10/11)
# ---------------------------------------------------------------
param(
    [Parameter(Mandatory = $true)][string]$Domain,
    [Parameter(Mandatory = $true)][string]$Server,
    [string]$User = "root",
    [switch]$SkipBuild
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $SkipBuild) {
    Write-Host "==> Construction de l'agent pour https://$Domain" -ForegroundColor Cyan
    & .\build_agent.bat "https://$Domain"
    if ($LASTEXITCODE -ne 0) { throw "La construction a échoué" }
}

$target = "$User@$Server"
Write-Host "==> Envoi des fichiers vers ${target}:/opt/pcmonitor" -ForegroundColor Cyan
ssh $target "mkdir -p /opt/pcmonitor"
scp -r site deploy "${target}:/opt/pcmonitor/"
if ($LASTEXITCODE -ne 0) { throw "Envoi SCP échoué" }

Write-Host "==> Installation / mise à jour sur le VPS" -ForegroundColor Cyan
ssh $target "sed -i 's/\r$//' /opt/pcmonitor/deploy/install.sh && bash /opt/pcmonitor/deploy/install.sh $Domain"

Write-Host "`n✅ Site en ligne : https://$Domain" -ForegroundColor Green
