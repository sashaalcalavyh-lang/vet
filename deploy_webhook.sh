#!/bin/bash
# deploy_webhook.sh — Disparador manual o de webhook para desplegar en Coolify
#
# Uso:
#   ./deploy_webhook.sh [COOLIFY_WEBHOOK_URL]
#
# O configura tu variable de entorno:
#   export VET_COOLIFY_WEBHOOK="https://coolify.142.132.233.38.sslip.io/webhooks/deploy/..."

WEBHOOK_URL="${1:-$VET_COOLIFY_WEBHOOK}"

if [ -z "$WEBHOOK_URL" ]; then
    echo "⚠️ Por favor proporciona la URL del Webhook de Coolify:"
    echo "   ./deploy_webhook.sh https://coolify.142.132.233.38.sslip.io/webhooks/deploy/TU_UUID"
    exit 1
fi

echo "🚀 Disparando despliegue en Coolify..."
curl -X POST "$WEBHOOK_URL" -H "Content-Type: application/json" -d '{"branch":"main"}'
echo ""
echo "✅ Señal de despliegue enviada a Coolify."
