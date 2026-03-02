#!/bin/bash
# Clean pycache and sync files to remote server


echo "=========================================="
echo "Starting deployment process..."
echo "=========================================="
echo ""

# Step 1: Clean Python cache files
echo "[Step 1/2] Cleaning Python cache files..."
bash clean_pycache.sh
echo ""

# Step 2: Sync files to remote server
echo "[Step 2/2] Syncing files to remote server..."
echo "Source: ./"
echo "Destination: torchpc:/c/Users/torch/Desktop/XiaoQing/XiaoQing_V3/"
echo ""

# Check for dry-run argument
if [[ "$1" == "d" ]]; then
    echo "[Dry-run mode] Only showing what would be copied."
    RSYNC_DRY="--dry-run"
else
    RSYNC_DRY=""
fi

rsync -avzP ./ torchpc:/c/Users/torch/Desktop/XiaoQing/XiaoQing_V3/ \
    --exclude='bot.log' \
    --exclude='.git' \
    --exclude='data' \
    --exclude='logs' \
    --exclude='/config' \
    --exclude='.agent' \
    $RSYNC_DRY

echo ""
echo "=========================================="
echo "Deployment completed!"
echo "=========================================="
