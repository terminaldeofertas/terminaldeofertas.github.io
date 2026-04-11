#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "=== Atualizando repositório ==="
git pull origin main

echo "=== Gerando site ==="
python3 generator.py

echo ""
echo "=== Publicando no GitHub Pages ==="
git add index.html sitemap.xml
git diff --cached --quiet && echo "Nenhuma alteração." || (
  git commit -m "chore: atualiza ofertas $(date '+%d/%m/%Y %H:%M')"
  git push
  echo "✅ Site publicado!"
)
