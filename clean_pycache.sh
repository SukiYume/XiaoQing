#!/bin/bash
# Clean all __pycache__ directories and .pyc files

echo "Cleaning Python cache files..."

# Remove __pycache__ directories recursively
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null

# Remove .pyc files recursively
find . -type f -name "*.pyc" -delete 2>/dev/null

# Remove .pyo files recursively
find . -type f -name "*.pyo" -delete 2>/dev/null

# Remove pytest cache directories
find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null

echo "Done! Python cache files cleaned."
