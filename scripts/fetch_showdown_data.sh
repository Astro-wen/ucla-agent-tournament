#!/usr/bin/env bash
# Pull Showdown's dex + move data into data/, and generate item/ability name maps from the simulator package
# (play.pokemonshowdown.com serves no items.json/abilities.json; the damage calculator needs display names).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data
for f in pokedex moves; do
  curl -sfL "https://play.pokemonshowdown.com/data/$f.json" -o "data/$f.json"
  echo "data/$f.json $(wc -c < data/$f.json) bytes"
done
node -e '
const {Dex} = require("./local/js/node_modules/pokemon-showdown");
const fs = require("fs");
for (const [kind, list] of [["items", Dex.items.all()], ["abilities", Dex.abilities.all()]]) {
  const out = {};
  for (const x of list) if (x.exists) out[x.id] = {name: x.name};
  fs.writeFileSync(`data/${kind}.json`, JSON.stringify(out));
  console.log(`data/${kind}.json ${Object.keys(out).length} entries`);
}'
