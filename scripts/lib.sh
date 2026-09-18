#!/usr/bin/env bash
# 共用函式庫。所有 scripts/*.sh 都應先 source 此檔。
# 用法： source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

set -euo pipefail

# 專案根目錄（scripts/ 的上一層）
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PROJECT_ROOT

# ── 輸出 ────────────────────────────────────────────────
info()  { printf '\033[0;34m→\033[0m %s\n' "$*"; }
ok()    { printf '\033[0;32m✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[0;33m!\033[0m %s\n' "$*" >&2; }
fail()  { printf '\033[0;31m✗\033[0m %s\n' "$*" >&2; }

# ── Docker Compose 偵測 ─────────────────────────────────
# Codespaces 提供 compose v2 外掛（docker compose）；
# 部分較舊環境只有 v1 獨立執行檔（docker-compose）。
detect_compose() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
    warn "偵測到舊版 docker-compose (v1)，建議升級至 Compose v2"
  else
    fail "找不到 docker compose 或 docker-compose"
    fail "請確認 Docker 已安裝，且 daemon 正在執行：docker info"
    exit 1
  fi
  export COMPOSE
}

# ── Docker daemon 檢查 ──────────────────────────────────
require_docker() {
  if ! docker info >/dev/null 2>&1; then
    fail "無法連線 Docker daemon。"
    fail "在 Codespaces 中請確認已啟用 docker-in-docker feature；本機請確認服務已啟動。"
    exit 1
  fi
}

# ── 載入 .env 並確保必要變數存在 ────────────────────────
load_env() {
  cd "$PROJECT_ROOT"

  if [[ ! -f .env ]]; then
    info "找不到 .env，從 .env.example 建立"
    cp .env.example .env
  fi

  # 產生 WEBUI_SECRET_KEY（冪等：已有值就不覆蓋）
  if ! grep -qE '^WEBUI_SECRET_KEY=.+$' .env; then
    info "產生 WEBUI_SECRET_KEY"
    local key
    if command -v openssl >/dev/null 2>&1; then
      key="$(openssl rand -hex 32)"
    else
      # 退化路徑：以 /dev/urandom 產生 32 bytes 十六進位
      key="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    fi
    # 移除既有的空行，再寫入
    sed -i.bak '/^WEBUI_SECRET_KEY=/d' .env && rm -f .env.bak
    printf 'WEBUI_SECRET_KEY=%s\n' "$key" >> .env
    ok "WEBUI_SECRET_KEY 已寫入 .env"
  fi

  # 載入為環境變數（供 compose 插值使用）
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
}
