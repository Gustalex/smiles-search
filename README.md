# ✈️ Monitor Smiles

Busca automática das passagens mais baratas **em milhas Smiles** saindo de **Maceió (MCZ)** para as principais capitais do Nordeste, Centro-Oeste e Sudeste. Roda no seu Mac algumas vezes ao dia, publica os resultados numa página web (GitHub Pages) e manda **e-mail** quando encontra preço abaixo do seu limite.

> ⚠️ **Aviso:** este projeto usa a API interna do site da Smiles, que não é oficial. Só faz consultas de preço (sem login, sem mexer na sua conta), mas a Smiles pode mudar a API a qualquer momento — se isso acontecer, veja a seção [Se parar de funcionar](#se-parar-de-funcionar). Os preços não incluem taxa de embarque; confirme sempre no site antes de emitir.

> 💡 **Por que roda no seu Mac e não na nuvem?** A Smiles usa proteção anti-bot (Akamai) que bloqueia IPs de datacenter — GitHub Actions, VPS etc. dão erro 406. IPs residenciais (sua internet de casa) passam normalmente. Por isso a busca roda no seu Mac; a página web continua na nuvem e acessível de qualquer lugar.

## O que tem aqui

| Arquivo | O que faz |
|---|---|
| `buscar.py` | Script que consulta a Smiles, gera os dados e envia o e-mail |
| `config.json` | Origem, destinos, datas, limites de alerta — **edite à vontade** |
| `rodar_local.sh` | Roda a busca e publica os resultados no GitHub |
| `com.gustavo.smilesmonitor.plist` | Agendador do macOS (launchd) |
| `docs/index.html` | A página web (publicada via GitHub Pages) |
| `docs/dados.json` | Resultados da última busca (gerado automaticamente) |

## Atalhos (Makefile)

Todos os comandos do dia a dia estão no `Makefile`. Rode `make` para ver a lista:

| Comando | O que faz |
|---|---|
| `make instalar` | Instala as dependências (Python + Chromium) |
| `make env` | Cria o arquivo de credenciais do e-mail |
| `make testar` | Busca rápida (3 destinos) sem publicar |
| `make rodar` | Busca completa + publica no GitHub + e-mail |
| `make agendar` | Liga o agendamento automático (4x ao dia) |
| `make desagendar` | Desliga o agendamento |
| `make status` | Mostra se o agendamento está ativo |
| `make log` | Últimas linhas do log |

Instalação em 4 comandos: `make instalar` → `make env` (depois edite `~/.smiles-monitor.env` com suas credenciais) → `make testar` → `make agendar`.

## Instalação no seu Mac (uma vez só)

Você já tem o repositório clonado em `~/code/smiles-search`. Falta:

### 1. Instalar as dependências

```bash
cd ~/code/smiles-search
python3 -m pip install -r requirements.txt --break-system-packages
python3 -m playwright install chromium
chmod +x rodar_local.sh
```

### 2. Configurar o e-mail de alerta (Resend)

As credenciais ficam num arquivo **fora do repositório** (`~/.smiles-monitor.env`), então nunca vão parar no GitHub. Crie-o assim (troque pelos seus valores):

```bash
cat > ~/.smiles-monitor.env <<'EOF'
export RESEND_API_KEY="re_sua_chave_aqui"
export MAIL_FROM="noreply@seudominio.com.br"
export MAIL_TO="voce@gmail.com"
EOF
```

> O `MAIL_FROM` precisa usar um domínio verificado no seu [Resend](https://resend.com/domains). O endereço em si pode ser qualquer um (`noreply@`, `alertas@`...), não precisa existir como caixa de entrada.

### 3. Testar

```bash
source ~/.smiles-monitor.env
python3 buscar.py --max-destinos 3 --max-datas 2
```

Se aparecerem preços em milhas (e não erro 406), está funcionando.

### 4. Ativar a página web (GitHub Pages)

No GitHub: **Settings → Pages** → *Deploy from a branch* → branch `main`, pasta `/docs` → **Save**. Em ~2 min a página fica em `https://SEU-USUARIO.github.io/smiles-search/`. Ela atualiza sozinha toda vez que o Mac roda a busca e dá push.

### 5. Agendar para rodar sozinho

Instale o agendador do macOS (launchd), que dispara a busca às 8h, 12h, 16h e 20h — e recupera o horário perdido se o Mac estiver dormindo:

```bash
cp com.gustavo.smilesmonitor.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.gustavo.smilesmonitor.plist
```

Pronto. Para conferir o que aconteceu na última execução, veja o arquivo `monitor.log`. Para **desligar** o agendamento:

```bash
launchctl unload ~/Library/LaunchAgents/com.gustavo.smilesmonitor.plist
```

> **Importante:** a busca só roda com o Mac **ligado e acordado**. Se ele passar o dia inteiro desligado, aquele dia não terá atualização — é a contrapartida de rodar em casa em vez da nuvem.

## Personalizando

Tudo fica no `config.json`:

- **Destinos** — adicione/remova à vontade usando o código do aeroporto:
  `"GYN": { "nome": "Goiânia", "regiao": "nacional" }`
- **Limites de alerta** (em milhas, por trecho) — em `limites_alerta_milhas`, por região. Para um limite específico de um destino, adicione `"alerta": 20000` dentro dele. Só chega e-mail quando algum preço fica **igual ou abaixo** do limite.
- **Datas pesquisadas** — em `datas`: por padrão, dos 90 aos 270 dias à frente (3 a 9 meses), de 7 em 7 dias. Aumentar o `passo_dias` deixa a busca mais rápida; diminuir encontra mais promoções, mas demora mais.
- **Frequência** — no `com.gustavo.smilesmonitor.plist`, no bloco `StartCalendarInterval`. Cada `<dict>` com `Hour`/`Minute` é um horário. Depois de editar, recarregue: `launchctl unload ...` e `launchctl load ...` de novo.

## Se parar de funcionar

Se a página mostrar o aviso de **bloqueio** (ou o log do Actions mostrar erros 401/403/406), a Smiles provavelmente trocou a chave da API. Para pegar a nova:

1. Abra [smiles.com.br](https://www.smiles.com.br) no Chrome e faça uma busca de voo qualquer.
2. Aperte **F12** → aba **Network** → digite `airlines/search` no filtro.
3. Clique na requisição que aparecer → aba **Headers** → em *Request Headers*, copie o valor de **`x-api-key`**.
4. No GitHub, crie/atualize o secret **`SMILES_API_KEY`** com esse valor (ou edite o `x_api_key` no `config.json`).

Outras coisas que já mudaram no passado e valem checar na mesma tela do DevTools:

- **O endereço da API** — já foi `...flightsearch-prd...`, hoje é `...flightsearch-blue...` (e pode virar `-green`). Se o endereço da requisição no navegador for diferente do `url` no `config.json`, atualize-o.
- **Os parâmetros da busca** — compare a URL da requisição com os `params` do `buscar.py`.

Se mesmo assim continuar bloqueado, a Smiles pode ter endurecido a proteção anti-bot (isso já aconteceu em 2025) — aí a saída é adaptar o script, o que pode exigir ajuda de alguém técnico (ou do Claude 🙂).

## Testando no seu computador (opcional)

```bash
pip install -r requirements.txt
playwright install chromium
python buscar.py --max-destinos 3 --max-datas 2   # teste rápido de verdade
python buscar.py --simular tests/resposta_exemplo.json --max-destinos 2 --max-datas 1   # teste offline
```

> O script abre um Chrome invisível (Playwright) para gerar os cookies anti-bot da Smiles — consultas diretas sem navegador são bloqueadas com erro 406.
