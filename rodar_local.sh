#!/bin/bash
# Executa o monitor Smiles no seu Mac e publica os resultados no GitHub.
# É chamado automaticamente pelo launchd (ver com.gustavo.smilesmonitor.plist),
# mas você também pode rodar na mão:  ./rodar_local.sh
#
# O launchd roda com um ambiente enxuto, então garantimos o PATH do Homebrew.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

cd "$(dirname "$0")" || exit 1

# Credenciais do e-mail (arquivo FORA do repositório, para não vazar no GitHub)
if [ -f "$HOME/.smiles-monitor.env" ]; then
  # shellcheck disable=SC1090
  source "$HOME/.smiles-monitor.env"
fi

echo "===================== $(date) ====================="

# Traz eventuais mudanças do GitHub antes de rodar
git pull --rebase --autostash 2>&1 || true

# Faz a busca (gera docs/dados.json, docs/historico.json e, se houver, envia e-mail)
python3 buscar.py

# Publica os resultados
git add docs/dados.json docs/historico.json
git commit -m "Atualiza resultados (local) [skip ci]" 2>&1 || echo "Nada novo para commitar."
git push 2>&1 || echo "Falha no push (verifique sua conexão / chave SSH)."

echo "===================== fim $(date) ================="
