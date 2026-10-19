#!/usr/bin/env bash
# Bring the catalogue up to date, in two steps:
#   1. moderation (#22 part A, free): POST /admin/events/moderation checks the
#      events and places nobody had checked, hides what it refuses and removes
#      contacts from imported texts;
#   2. keywords (#22 part B, paid): POST /admin/events/keywords, after showing the
#      cost estimate and asking for an "s".
#
# Run it on your own machine. The admin token (ADMIN_INGEST_TOKEN, the value set in
# Vercel) is typed without echo, kept only in this process and never written to the
# shell history.
set -euo pipefail

API="${FLYER_API:-https://flyer-back.vercel.app}"

read -rsp "ADMIN_INGEST_TOKEN: " TOKEN
echo

# The header goes through a file descriptor (printf is a builtin), so the token
# never shows in the process list.
call() {
  curl -sS --fail-with-body -X POST "$API/admin/events/$1" \
    -H @<(printf 'X-Admin-Token: %s\n' "$TOKEN") -H 'content-type: application/json' -d "$2"
}

field() {
  printf '%s' "$1" | sed -n "s/.*\"$2\": *\([0-9.]*\).*/\1/p" | head -n 1
}

# Repeats one step until nothing is left (or a round does nothing).
rounds() {
  local step=$1 body=$2 done_field=$3 out remaining progressed
  for round in $(seq 1 20); do
    echo "Rodada $round (até 60 s)…"
    if ! out=$(call "$step" "$body"); then
      echo "$out"
      echo "A chamada falhou; rode de novo daqui a pouco."
      exit 1
    fi
    echo "$out"
    remaining=$(field "$out" remaining)
    progressed=$(field "$out" "$done_field")
    if [ "${remaining:-1}" = "0" ]; then
      echo "Pronto."
      return 0
    fi
    if [ "${progressed:-0}" = "0" ]; then
      echo "Nada foi feito nesta rodada; parando para tentar mais tarde."
      exit 1
    fi
  done
  echo "Parou depois de 20 rodadas; rode de novo para continuar."
  exit 1
}

echo "1. Moderação (gratuita)"
if ! count=$(call moderation '{"dry_run": true}'); then
  echo "$count"
  echo "A chamada falhou: confira o token e o endereço da API."
  exit 1
fi
echo "$count"
if [ "$(field "$count" remaining)" != "0" ]; then
  rounds moderation '{"limit": 100}' checked
fi
echo

echo "2. Palavras-chave. Estimativa (nada é gasto):"
if ! estimate=$(call keywords '{}'); then
  echo "$estimate"
  echo "A estimativa falhou; rode de novo daqui a pouco."
  exit 1
fi
echo "$estimate"
read -rp "Rodar as palavras-chave agora? (s/N) " answer
[ "$answer" = "s" ] || { echo "Palavras-chave não rodadas."; exit 0; }
rounds keywords '{"dry_run": false, "limit": 100}' processed
