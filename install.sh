#!/bin/bash
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

# Création du venv et installation des dépendances
python3 -m venv "$REPO_DIR/.venv"
source "$REPO_DIR/.venv/bin/activate"
pip install -r "$REPO_DIR/requirements.txt"

# Préparation du dossier pour les data
mkdir -p "$REPO_DIR/data"

echo "Installation terminée"
