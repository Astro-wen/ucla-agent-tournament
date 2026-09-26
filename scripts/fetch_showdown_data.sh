#!/usr/bin/env bash
# Pull Showdown's dex + move data (types, base stats, move power/target) into data/.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data
for f in pokedex moves; do
  curl -sfL "https://play.pokemonshowdown.com/data/$f.json" -o "data/$f.json"
  echo "data/$f.json $(wc -c < data/$f.json) bytes"
done
