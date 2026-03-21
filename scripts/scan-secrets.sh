#!/bin/bash
# Secret scanner for pre-commit hook
# Scans staged files for patterns that look like leaked credentials
# Exit 1 = secrets found (blocks commit), Exit 0 = clean

RED='\033[0;31m'
DIM='\033[2m'
NC='\033[0m'

FOUND=0
SCANNED=0

for file in "$@"; do
  # Skip things we don't need to scan
  [[ "$file" == scripts/scan-secrets.sh ]] && continue
  [[ "$file" == __pycache__/* ]] && continue
  [[ ! -f "$file" ]] && continue
  file -b --mime "$file" 2>/dev/null | grep -q "text/" || continue

  SCANNED=$((SCANNED+1))

  # Supabase service role keys (base64-encoded "service_role")
  if grep -qP 'eyJhbGciOi[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*c2VydmljZV9yb2xl[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+' "$file" 2>/dev/null; then
    echo -e "  ${RED}BLOCKED${NC}  Supabase service_role key  ${DIM}$file${NC}"
    FOUND=$((FOUND+1))
  fi

  # AWS keys
  if grep -qP '(AKIA|ASIA)[A-Z0-9]{16}' "$file" 2>/dev/null; then
    echo -e "  ${RED}BLOCKED${NC}  AWS access key              ${DIM}$file${NC}"
    FOUND=$((FOUND+1))
  fi

  # Private keys
  PRIVKEY_PATTERN="PRIVATE"" KEY"
  if grep -q "$PRIVKEY_PATTERN" "$file" 2>/dev/null; then
    echo -e "  ${RED}BLOCKED${NC}  Private key file             ${DIM}$file${NC}"
    FOUND=$((FOUND+1))
  fi

  # Fly.io access tokens
  if grep -qP 'FlyV1\s+fm2_' "$file" 2>/dev/null; then
    echo -e "  ${RED}BLOCKED${NC}  Fly.io access token          ${DIM}$file${NC}"
    FOUND=$((FOUND+1))
  fi

  # Grafana Cloud tokens
  if grep -qP 'glc_[A-Za-z0-9+/=]{20,}' "$file" 2>/dev/null; then
    echo -e "  ${RED}BLOCKED${NC}  Grafana Cloud token          ${DIM}$file${NC}"
    FOUND=$((FOUND+1))
  fi

  # Sentry auth tokens
  if grep -qP 'sntry[su]_[A-Za-z0-9]{40,}' "$file" 2>/dev/null; then
    echo -e "  ${RED}BLOCKED${NC}  Sentry auth token            ${DIM}$file${NC}"
    FOUND=$((FOUND+1))
  fi

  # Google/Gemini API keys
  if grep -qP 'AIzaSy[A-Za-z0-9_-]{33}' "$file" 2>/dev/null; then
    echo -e "  ${RED}BLOCKED${NC}  Google/Gemini API key        ${DIM}$file${NC}"
    FOUND=$((FOUND+1))
  fi

  # Alpaca API keys (paper or live)
  if grep -qP 'PK[A-Z0-9]{18,}' "$file" 2>/dev/null; then
    echo -e "  ${RED}BLOCKED${NC}  Alpaca API key               ${DIM}$file${NC}"
    FOUND=$((FOUND+1))
  fi

  # Anthropic API keys
  if grep -qP 'sk-ant-api[0-9]{2}-[A-Za-z0-9_-]{20,}' "$file" 2>/dev/null; then
    echo -e "  ${RED}BLOCKED${NC}  Anthropic API key            ${DIM}$file${NC}"
    FOUND=$((FOUND+1))
  fi

  # Perplexity API keys
  if grep -qP 'pplx-[A-Za-z0-9]{20,}' "$file" 2>/dev/null; then
    echo -e "  ${RED}BLOCKED${NC}  Perplexity API key           ${DIM}$file${NC}"
    FOUND=$((FOUND+1))
  fi

  # Telegram bot tokens
  if grep -qP '[0-9]{8,}:AA[A-Za-z0-9_-]{30,}' "$file" 2>/dev/null; then
    echo -e "  ${RED}BLOCKED${NC}  Telegram bot token           ${DIM}$file${NC}"
    FOUND=$((FOUND+1))
  fi

  # Finnhub API keys (hex string pattern)
  if grep -qP 'FINNHUB_API_KEY\s*=\s*[a-z0-9]{20,}' "$file" 2>/dev/null; then
    echo -e "  ${RED}BLOCKED${NC}  Finnhub API key              ${DIM}$file${NC}"
    FOUND=$((FOUND+1))
  fi

  # Protected env files
  if [[ "$file" == .env.local || "$file" == .env.*.local || "$file" == .env ]]; then
    echo -e "  ${RED}BLOCKED${NC}  Protected env file           ${DIM}$file${NC}"
    FOUND=$((FOUND+1))
  fi

done

if [ "$FOUND" -gt 0 ]; then
  echo "SCAN_RESULT:FAIL:${SCANNED}:${FOUND}"
  exit 1
else
  echo "SCAN_RESULT:PASS:${SCANNED}:0"
  exit 0
fi
