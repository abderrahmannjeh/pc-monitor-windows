#!/usr/bin/env bash
# Installation sur un VPS Ubuntu 22.04 / 24.04 neuf.
# Usage (sur le VPS, en root) :  bash /opt/pcmonitor/deploy/install.sh monitor.votredomaine.com
set -euo pipefail

DOMAIN="${1:?Usage : install.sh monitor.votredomaine.com}"
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Mise à jour du système"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq

echo "==> Installation de Docker"
if ! command -v docker >/dev/null; then
  apt-get install -y -qq ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

echo "==> Pare-feu (SSH, HTTP, HTTPS uniquement)"
apt-get install -y -qq ufw
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null

echo "==> Mises à jour de sécurité automatiques"
apt-get install -y -qq unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "==> Vérification du DNS"
IP_VPS="$(curl -fsS4 https://api.ipify.org || true)"
IP_DNS="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)"
if [ -n "$IP_VPS" ] && [ "$IP_VPS" != "$IP_DNS" ]; then
  echo "   ⚠ $DOMAIN pointe vers '${IP_DNS:-rien}' au lieu de $IP_VPS."
  echo "     Le HTTPS ne fonctionnera qu'une fois l'enregistrement DNS A corrigé (Caddy réessaiera tout seul)."
fi

echo "==> Démarrage du site"
cd "$APP_DIR/deploy"
echo "DOMAIN=$DOMAIN" > .env
docker compose pull -q
docker compose up -d

echo
echo "✅ Terminé : https://$DOMAIN"
echo "   Journaux : cd $APP_DIR/deploy && docker compose logs -f"
