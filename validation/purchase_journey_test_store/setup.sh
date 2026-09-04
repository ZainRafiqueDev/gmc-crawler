#!/usr/bin/env bash
# Disposable, self-contained WooCommerce test store for purchase-journey
# validation (app/checks/purchase_journey.py) - built so this never needs an
# external live store again. See README.md in this directory for the full
# story and a real, live-validated example run.
#
# Usage:
#   ./setup.sh              # bring up the store, install WooCommerce, create
#                            # the test product, configure Cash on Delivery
#   ngrok http 8085          # separately, in another terminal - see README
#                            # for the real site-url + header-bypass steps
#                            # needed after the tunnel starts
#
# Requires: Docker Desktop running, docker compose v2.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "==> Starting containers (mysql, wordpress, wp-cli)..."
docker compose up -d

WPCLI="docker compose exec -T wpcli wp"

echo "==> Waiting for WordPress to accept connections..."
until curl -s -o /dev/null http://localhost:8085/; do sleep 2; done

echo "==> Installing WordPress core..."
$WPCLI core install \
  --url=http://localhost:8085 \
  --title="Purchase Journey Test Store" \
  --admin_user=admin \
  --admin_password=TestAdmin123! \
  --admin_email=test@example.com \
  --skip-email

# MSYS_NO_PATHCONV=1: on Windows Git Bash, a leading "/" in a command-line
# argument gets silently rewritten to an absolute Windows path (e.g.
# "/%postname%/" becomes "C:/Program Files/Git/%postname%/") before docker
# ever sees it - this corrupts WordPress's stored permalink structure and
# every link the site generates afterward. Confirmed live: an earlier run of
# this exact setup, without this guard, produced real crawled URLs like
# ".../C:/Programcategory/uncategorized" in a real audit report. Harmless on
# real Linux/macOS shells (the env var is simply unused there).
echo "==> Setting permalinks..."
MSYS_NO_PATHCONV=1 $WPCLI rewrite structure '/%postname%/'
$WPCLI rewrite flush

echo "==> Installing and activating WooCommerce..."
$WPCLI plugin install woocommerce --activate

echo "==> Configuring store basics (US/CA, USD)..."
$WPCLI option update woocommerce_store_address "123 Test St"
$WPCLI option update woocommerce_onboarding_profile --format=json '{"skipped":true}'

echo "==> Creating the test product..."
$WPCLI wc product create \
  --name="Purchase Journey Test Widget" \
  --type=simple \
  --regular_price="24.99" \
  --sku="PJTEST-001" \
  --manage_stock=true \
  --stock_quantity=50 \
  --status=publish \
  --user=admin

echo "==> Enabling Cash on Delivery as the sole active payment gateway..."
$WPCLI option update woocommerce_cod_settings --format=json '{"enabled":"yes","title":"Cash on Delivery","description":"Pay with cash upon delivery.","instructions":"Pay with cash upon delivery.","enable_for_methods":[],"enable_for_virtual":"yes"}'
$WPCLI option update woocommerce_bacs_settings --format=json '{"enabled":"no"}'
$WPCLI option update woocommerce_cheque_settings --format=json '{"enabled":"no"}'
$WPCLI option update woocommerce_paypal_settings --format=json '{"enabled":"no"}'

echo ""
echo "==> Done. Store is live at http://localhost:8085"
echo "    Admin: http://localhost:8085/wp-admin (admin / TestAdmin123!)"
echo ""
echo "Next: start a tunnel (see README.md), then update the site URL:"
echo '    docker compose exec -T wpcli wp option update home "https://YOUR-TUNNEL-URL"'
echo '    docker compose exec -T wpcli wp option update siteurl "https://YOUR-TUNNEL-URL"'
