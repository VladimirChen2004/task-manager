#!/bin/bash
# Deploy task-automation sync to Ubuntu server
set -euo pipefail

# --- Configuration ---
REMOTE_USER="${REMOTE_USER:-vchen}"
REMOTE_HOST="${REMOTE_HOST:-10.20.40.232}"
REMOTE_DIR="${REMOTE_DIR:-/home/vchen/automations/task-automation}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Deploying task-automation to ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR} ==="

# 1. Create remote directory
ssh "${REMOTE_USER}@${REMOTE_HOST}" "mkdir -p ${REMOTE_DIR}"

# 2. Rsync project files
rsync -avz --delete \
    --exclude 'venv/' \
    --exclude '.venv/' \
    --exclude '.env' \
    --exclude '.git/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.sync_state.json' \
    --exclude '.pytest_cache/' \
    --exclude '.ruff_cache/' \
    "${LOCAL_DIR}/" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"

# 3. Setup venv + install deps on remote
ssh "${REMOTE_USER}@${REMOTE_HOST}" bash -s "${REMOTE_DIR}" << 'REMOTE_SETUP'
REMOTE_DIR="$1"
cd "$REMOTE_DIR"
if [ ! -d venv ]; then
    echo "Creating venv..."
    python3 -m venv venv
fi
echo "Installing dependencies..."
venv/bin/pip install -q -r requirements.txt
echo "Dependencies installed."
REMOTE_SETUP

echo ""
echo "=== Deploy complete ==="
echo ""
echo "Next steps on server:"
echo "  1. Configure .env (if first deploy):"
echo "     ssh ${REMOTE_USER}@${REMOTE_HOST} 'nano ${REMOTE_DIR}/.env'"
echo ""
echo "  2. Test daemon (dry-run):"
echo "     ssh ${REMOTE_USER}@${REMOTE_HOST} '${REMOTE_DIR}/venv/bin/python3 ${REMOTE_DIR}/sync_daemon.py --dry-run --verbose'"
echo ""
echo "  3. Start daemon in tmux:"
echo "     ssh ${REMOTE_USER}@${REMOTE_HOST}"
echo "     tmux new-session -d -s task-sync '${REMOTE_DIR}/venv/bin/python3 ${REMOTE_DIR}/sync_daemon.py --verbose'"
echo "     tmux attach -t task-sync  # to monitor"
