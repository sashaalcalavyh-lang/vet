#!/usr/bin/env bash
set -e
echo "[CoVet Deploy] 1/3 Compilando Frontend PWA..."
cd /home/ashasasha/vet/frontend && npm run build
echo "[CoVet Deploy] 2/3 Sincronizando código y estáticos con VPS Hetzner..."
rsync -avz --exclude 'node_modules' --exclude '.git' /home/ashasasha/vet/ root@142.132.233.38:/opt/vet/
ssh root@142.132.233.38 "mkdir -p /var/www/covet && cp -r /opt/vet/frontend/dist/* /var/www/covet/ && chown -R caddy:caddy /var/www/covet"
echo "[CoVet Deploy] 3/3 Actualizando contenedor vet-backend..."
ssh root@142.132.233.38 "cd /opt/vet && docker compose up -d --build vet-backend"
echo "[CoVet Deploy] Despliegue completado en https://vet.142.132.233.38.sslip.io"
