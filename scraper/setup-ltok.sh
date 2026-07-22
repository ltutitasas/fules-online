#!/bin/bash
set -e

echo "🏅 LTOK scraperio setup"
echo ""

# Tikrinti KV credentials
if [ -z "$KV_REST_API_URL" ] || [ -z "$KV_REST_API_TOKEN" ]; then
  echo "❌ Klaida: nustatykite environment variables prieš paleisiant:"
  echo "   export KV_REST_API_URL='https://...'"
  echo "   export KV_REST_API_TOKEN='...'"
  echo ""
  echo "Jei nežinote reikšmių – žiūrėkite Vercel aplinkoje (Settings → Environment Variables)"
  exit 1
fi

PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_FILE="$PLIST_DIR/lt.fules.ltok.plist"

echo "✅ KV credentials rastos"
echo ""

# Nusikopijuoti šabloną
echo "📝 Setup'inam launchd agentą..."
mkdir -p "$PLIST_DIR"
cp "$(dirname "$0")/lt.fules.ltok.plist.example" "$PLIST_FILE"

# Pakeisti KV reikšmes (macOS sed turi -i '')
sed -i '' "s|ĮRAŠYKITE_URL|$KV_REST_API_URL|g" "$PLIST_FILE"
sed -i '' "s|ĮRAŠYKITE_TOKEN|$KV_REST_API_TOKEN|g" "$PLIST_FILE"

# Jei jau įjungtas – išjungti
if launchctl list lt.fules.ltok 2>/dev/null; then
  launchctl unload "$PLIST_FILE" 2>/dev/null || true
  sleep 1
fi

# Įjungti
launchctl load "$PLIST_FILE"

echo "✅ LTOK scraperis aktyvus!"
echo ""
echo "📋 Darbas:"
echo "   • Paleis automatiškai kas 10 minučių (kai Mac budintis)"
echo "   • Log'ai: tail -f /tmp/fules-ltok.log"
echo "   • Išjungti: launchctl unload $PLIST_FILE"
echo "   • Perkrauti: bash scraper/setup-ltok.sh (po KV pakeitimo)"
echo ""
