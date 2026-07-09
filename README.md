# ✈️ Monitor Smiles

Busca automática das passagens mais baratas **em milhas Smiles** saindo de **Maceió (MCZ)** para 37 destinos no Brasil e no exterior. Roda sozinho 4x por dia no GitHub Actions (de graça), publica os resultados numa página web e manda **e-mail** quando encontra preço abaixo do seu limite.

> ⚠️ **Aviso:** este projeto usa a API interna do site da Smiles, que não é oficial. Só faz consultas de preço (sem login, sem mexer na sua conta), mas a Smiles pode mudar a API a qualquer momento — se isso acontecer, veja a seção [Se parar de funcionar](#se-parar-de-funcionar). Os preços não incluem taxa de embarque; confirme sempre no site antes de emitir.

## O que tem aqui

| Arquivo | O que faz |
|---|---|
| `buscar.py` | Script que consulta a Smiles e gera os dados |
| `config.json` | Origem, destinos, datas, limites de alerta — **edite à vontade** |
| `.github/workflows/monitor.yml` | Agendamento automático + envio de e-mail |
| `docs/index.html` | A página web (publicada via GitHub Pages) |
| `docs/dados.json` | Resultados da última busca (gerado automaticamente) |

## Instalação (uma vez só, ~15 min)

### 1. Crie o repositório no GitHub

1. Crie uma conta em [github.com](https://github.com) se não tiver.
2. Clique em **New repository** → nome `smiles-monitor` → marque **Public** (necessário para o GitHub Pages gratuito) → **Create repository**.

### 2. Suba os arquivos

Na página do repositório novo, clique em **uploading an existing file** e arraste **todo o conteúdo** desta pasta (incluindo as subpastas `docs` e `tests`). Depois clique em **Commit changes**.

> A pasta `.github` às vezes não vai junto no arraste (é oculta). Se isso acontecer: no repositório, clique em **Add file → Create new file**, digite como nome `.github/workflows/monitor.yml` (as barras criam as pastas) e cole o conteúdo do arquivo `monitor.yml` daqui.

### 3. Configure o e-mail de alerta (Resend)

O envio usa o **Resend** com um domínio já verificado na sua conta:

1. Acesse [resend.com/api-keys](https://resend.com/api-keys) → **Create API Key** (permissão *Sending access* basta) → copie a chave (`re_...`).
2. No repositório do GitHub: **Settings → Secrets and variables → Actions → New repository secret**. Crie estes 3 secrets:

| Nome | Valor |
|---|---|
| `RESEND_API_KEY` | a chave `re_...` do Resend |
| `MAIL_FROM` | remetente no seu domínio verificado, ex.: `monitor@seudominio.com` |
| `MAIL_TO` | e-mail que recebe os alertas |

> O `MAIL_FROM` precisa usar exatamente o domínio que está verificado no Resend, senão o envio é recusado. O endereço em si pode ser qualquer um (`monitor@`, `alertas@`...), não precisa existir como caixa de entrada.

### 4. Ative a página web (GitHub Pages)

**Settings → Pages** → em *Build and deployment*, escolha **Deploy from a branch** → branch `main`, pasta `/docs` → **Save**.

Em ~2 minutos sua página estará em: `https://SEU-USUARIO.github.io/smiles-monitor/`

### 5. Rode a primeira busca

Aba **Actions** → se aparecer um botão para habilitar workflows, clique nele → escolha **Monitor Smiles** no menu lateral → **Run workflow** → **Run workflow**.

A execução leva uns 10–20 minutos (o script espera entre uma consulta e outra de propósito, para não sobrecarregar a Smiles). Quando terminar, atualize a página web e os preços estarão lá. Depois disso, roda sozinho 4x por dia.

## Personalizando

Tudo fica no `config.json`:

- **Destinos** — adicione/remova à vontade usando o código do aeroporto:
  `"GYN": { "nome": "Goiânia", "regiao": "nacional" }`
- **Limites de alerta** (em milhas, por trecho) — em `limites_alerta_milhas`, por região. Para um limite específico de um destino, adicione `"alerta": 20000` dentro dele. Só chega e-mail quando algum preço fica **igual ou abaixo** do limite.
- **Datas pesquisadas** — em `datas`: por padrão, dos 15 aos 120 dias à frente, de 14 em 14 dias. Diminuir o `passo_dias` encontra mais promoções, mas faz mais consultas (mais chance de bloqueio).
- **Frequência** — no `monitor.yml`, linha do `cron`. Os horários são em UTC (Maceió = UTC−3). Ex.: `"0 9 * * *"` = 1x por dia às 6h da manhã.

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
