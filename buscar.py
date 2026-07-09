#!/usr/bin/env python3
"""
Monitor de passagens em milhas da Smiles.

Varre os destinos definidos em config.json a partir do aeroporto de origem,
amostrando datas futuras, e salva as passagens mais baratas (em milhas) em
docs/dados.json — que alimenta a página web. Quando encontra preço abaixo do
limite de alerta, gera alertas.md (usado pelo GitHub Actions para enviar e-mail).

A Smiles usa proteção anti-bot (Akamai) que exige cookies gerados pelo
JavaScript do site. Por isso o modo padrão ("navegador") abre um Chrome
invisível via Playwright, visita o site para gerar os cookies e faz as
consultas de dentro do navegador. O modo "http" (curl_cffi/requests) existe
como alternativa leve, mas costuma ser bloqueado.

Uso:
    python buscar.py                     # execução normal
    python buscar.py --max-destinos 3    # teste rápido com poucos destinos
    python buscar.py --simular arquivo   # usa uma resposta salva (sem internet)

AVISO: usa a API interna do site da Smiles (não oficial). Pode parar de
funcionar se a Smiles mudar a chave/proteções — veja o README para atualizar.
"""

import argparse
import json
import random
import sys
import time
from datetime import date, datetime, timedelta
from os import environ
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).resolve().parent
ARQ_CONFIG = RAIZ / "config.json"
ARQ_DADOS = RAIZ / "docs" / "dados.json"
ARQ_HISTORICO = RAIZ / "docs" / "historico.json"
ARQ_ALERTAS = RAIZ / "alertas.md"

FUSO = ZoneInfo("America/Maceio")
STATUS_BLOQUEIO = {0, 401, 403, 406, 429}
MAX_ENTRADAS_HISTORICO = 120


def carregar_json(caminho, padrao):
    try:
        return json.loads(Path(caminho).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return padrao


def api_key(cfg):
    return environ.get("SMILES_API_KEY") or cfg["api"]["x_api_key"]


# ---------------------------------------------------------------------------
# Clientes de consulta: navegador (Playwright), http (curl_cffi) e simulado
# ---------------------------------------------------------------------------

class ClienteNavegador:
    """Faz as consultas de dentro de um Chrome invisível (driblando a Akamai).

    Estratégia: abre a página de emissão (isso faz o sensor da Akamai validar
    a sessão e gerar os cookies _abck/bm_*) e consulta a API via fetch() de
    dentro da página. O bloqueio é intermitente, então cada consulta tem
    retentativas com pausa e recarga da página para renovar os cookies.
    """

    _JS_FETCH = """async ({ url, headers }) => {
        try {
            const r = await fetch(url, { headers });
            return { status: r.status, body: await r.text() };
        } catch (e) {
            return { status: 0, body: String(e) };
        }
    }"""

    def __init__(self, cfg):
        from playwright.sync_api import sync_playwright

        self._cfg = cfg
        self._headers = {"x-api-key": api_key(cfg), "Channel": "WEB"}
        self._pw = sync_playwright().start()
        try:
            # channel="chromium" usa o headless "novo", bem mais parecido
            # com um Chrome real do que o headless-shell padrão.
            self._browser = self._pw.chromium.launch(
                headless=True,
                channel="chromium",
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )

        # Remove o "HeadlessChrome" do User-Agent, que entrega o robô
        temp = self._browser.new_context()
        pagina_temp = temp.new_page()
        ua = pagina_temp.evaluate("navigator.userAgent")
        temp.close()
        contexto = self._browser.new_context(
            user_agent=ua.replace("HeadlessChrome", "Chrome"),
            locale="pt-BR",
            timezone_id="America/Maceio",
            viewport={"width": 1366, "height": 850},
        )
        self._page = contexto.new_page()

        # "Esquenta" a sessão abrindo a página de busca: é isso que faz o
        # sensor da Akamai liberar os cookies. Nem sempre valida de primeira,
        # então insistimos algumas vezes.
        print("Abrindo a busca da Smiles para validar a sessão (anti-bot)...")
        self._revalidar()

    def _revalidar(self):
        """(Re)abre a página de busca para renovar os cookies da Akamai."""
        primeira_data = (date.today() + timedelta(days=30)).isoformat()
        url = link_emissao(self._cfg["origem"], "GRU", primeira_data)
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"  aviso: recarga da página falhou ({type(e).__name__})")
            return
        for x, y in ((400, 300), (800, 500)):
            self._page.mouse.move(x, y, steps=10)
            self._page.wait_for_timeout(500)
        self._page.wait_for_timeout(3_000)

    def get(self, url, params):
        """Consulta via fetch() de dentro da página, com retentativas.

        O bloqueio da Akamai é intermitente: uma consulta pode falhar e a
        seguinte passar. Então insistimos com pausas e, na segunda falha,
        recarregamos a página de busca para renovar os cookies.
        """
        alvo = f"{url}?{urlencode(params)}"
        ultimo = {"status": 0, "body": ""}
        for tentativa in range(3):
            resultado = self._page.evaluate(
                self._JS_FETCH, {"url": alvo, "headers": self._headers}
            )
            if resultado["status"] not in STATUS_BLOQUEIO:
                return resultado["status"], resultado["body"]
            ultimo = resultado
            self._page.wait_for_timeout(int(2_500 + random.uniform(0, 2_000)))
            if tentativa == 1:
                self._revalidar()
        return ultimo["status"], (ultimo["body"] or "")[:120]

    def close(self):
        try:
            self._browser.close()
            self._pw.stop()
        except Exception:
            pass


class ClienteHttp:
    """Consulta direta via curl_cffi (ou requests). Alternativa leve ao navegador."""

    def __init__(self, cfg):
        self._cfg = cfg
        self._headers = {
            "x-api-key": api_key(cfg),
            "Channel": "WEB",
            "Origin": "https://www.smiles.com.br",
            "Referer": "https://www.smiles.com.br/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9",
            "User-Agent": cfg["api"].get("user_agent", "Mozilla/5.0"),
        }
        self._headers.update(cfg["api"].get("headers_extras", {}))
        try:
            from curl_cffi import requests as creq

            self._sessao = creq.Session(
                impersonate=cfg["api"].get("impersonate", "chrome")
            )
        except ImportError:
            import requests

            print("AVISO: curl_cffi não instalado — usando requests (maior chance de bloqueio).")
            self._sessao = requests.Session()

    def get(self, url, params):
        try:
            resp = self._sessao.get(url, params=params, headers=self._headers, timeout=25)
        except Exception as e:
            return 0, str(e)
        return resp.status_code, resp.text

    def close(self):
        pass


class ClienteSimulado:
    """Devolve sempre a mesma resposta salva em arquivo (testes offline)."""

    def __init__(self, caminho):
        self._corpo = Path(caminho).read_text(encoding="utf-8")

    def get(self, url, params):
        return 200, self._corpo

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Lógica de busca
# ---------------------------------------------------------------------------

def datas_para_buscar(cfg):
    d = cfg["datas"]
    hoje = date.today()
    atual = hoje + timedelta(days=d["antecedencia_min_dias"])
    fim = hoje + timedelta(days=d["antecedencia_max_dias"])
    datas = []
    while atual <= fim:
        datas.append(atual.isoformat())
        atual += timedelta(days=d["passo_dias"])
    return datas


def link_emissao(origem, destino, data):
    """Monta o link da página de resultados da Smiles (data = timestamp em ms)."""
    ano, mes, dia = int(data[:4]), int(data[5:7]), int(data[8:10])
    ts_ms = int(datetime(ano, mes, dia, tzinfo=FUSO).timestamp() * 1000)
    return (
        "https://www.smiles.com.br/mfe/emissao-passagem/?adults=1&cabin=ECONOMIC"
        f"&children=0&departureDate={ts_ms}&infants=0&isElegible=false"
        "&isFlexibleDateChecked=false&returnDate=&searchType=g3&segments=1"
        f"&tripType=2&originAirport={origem}&originCity=&originCountry="
        f"&originAirportIsAny=false&destinationAirport={destino}&destinCity="
        "&destinCountry=&destinAirportIsAny=false&novo-resultado-voos=true"
    )


def montar_params(cfg, destino, data):
    return {
        "cabin": cfg["api"].get("cabin", "ECONOMIC"),
        "originAirportCode": cfg["origem"],
        "destinationAirportCode": destino,
        "departureDate": data,
        "memberNumber": "",
        "adults": "1",
        "children": "0",
        "infants": "0",
        "forceCongener": "false",
        "cookies": "_gid=undefined;",
    }


def consultar(cliente, cfg, destino, data):
    """Retorna (status_http, payload_ou_None). Uma retentativa em erro 5xx."""
    params = montar_params(cfg, destino, data)
    for tentativa in (1, 2):
        status, corpo = cliente.get(cfg["api"]["url"], params)
        if status >= 500 and tentativa == 1:
            time.sleep(5)
            continue
        if status != 200:
            return status, corpo
        try:
            return 200, json.loads(corpo)
        except (ValueError, TypeError):
            return 200, None
    return 0, None


def melhor_voo(payload, tarifas_aceitas):
    """Extrai do payload o voo com menor preço em milhas (tarifas aceitas)."""
    melhor = None
    for segmento in payload.get("requestedFlightSegmentList") or []:
        for voo in segmento.get("flightList") or []:
            for tarifa in voo.get("fareList") or []:
                tipo = tarifa.get("type")
                milhas = tarifa.get("miles")
                if tipo not in tarifas_aceitas or not milhas or milhas <= 0:
                    continue
                if melhor is None or milhas < melhor["milhas"]:
                    partida = (voo.get("departure") or {}).get("date") or ""
                    duracao = voo.get("duration") or {}
                    melhor = {
                        "milhas": int(milhas),
                        "tipo_tarifa": tipo,
                        "cia": (voo.get("airline") or {}).get("name") or "?",
                        "paradas": voo.get("stops"),
                        "horario": partida[11:16] if len(partida) >= 16 else None,
                        "duracao_h": duracao.get("hours"),
                    }
    return melhor


def limite_alerta(cfg, codigo, info):
    if "alerta" in info:
        return info["alerta"]
    return cfg["limites_alerta_milhas"].get(info.get("regiao", ""), 0)


def enviar_email_resend(achados, origem):
    """Envia o alerta por e-mail via API do Resend, se as credenciais existirem.

    Lê RESEND_API_KEY, MAIL_FROM e MAIL_TO do ambiente (definidos no
    ~/.smiles-monitor.env quando roda localmente). Sem elas, não faz nada.
    """
    chave = environ.get("RESEND_API_KEY")
    remetente = environ.get("MAIL_FROM")
    destino = environ.get("MAIL_TO")
    if not (chave and remetente and destino and achados):
        return

    linhas = ""
    for r in achados:
        d = r["detalhes"]
        milhas = f"{r['milhas']:,}".replace(",", ".")
        linhas += (
            f"<tr><td style='padding:6px 10px'>{r['nome']} ({r['destino']})</td>"
            f"<td style='padding:6px 10px'><b>{milhas}</b> milhas</td>"
            f"<td style='padding:6px 10px'>{d['data']}</td>"
            f"<td style='padding:6px 10px'>{d['cia']}</td>"
            f"<td style='padding:6px 10px'><a href=\"{r['link']}\">abrir no site</a></td></tr>"
        )
    html = (
        f"<h2>✈️ Passagens baratas na Smiles — saindo de {origem}</h2>"
        "<table style='border-collapse:collapse;font-family:sans-serif' border='1'>"
        "<tr style='background:#f2f2f2'><th style='padding:6px 10px'>Destino</th>"
        "<th style='padding:6px 10px'>Milhas</th><th style='padding:6px 10px'>Data</th>"
        "<th style='padding:6px 10px'>Cia</th><th style='padding:6px 10px'>Reservar</th></tr>"
        f"{linhas}</table>"
        "<p style='color:#666;font-family:sans-serif;font-size:13px'>Preços por trecho "
        "(só ida), sem taxa de embarque. Os valores mudam rápido — confirme no site "
        "antes de emitir.</p>"
    )
    corpo = json.dumps({
        "from": remetente,
        "to": [destino],
        "subject": f"✈️ Smiles: passagem barata saindo de {origem}!",
        "html": html,
    }).encode("utf-8")
    req = Request(
        "https://api.resend.com/emails",
        data=corpo,
        headers={"Authorization": f"Bearer {chave}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=20) as resp:
            resp.read()
        print(f"E-mail de alerta enviado para {destino}.")
    except Exception as e:
        print(f"Falha ao enviar e-mail: {e}")


def criar_cliente(cfg, args):
    if args.simular:
        return ClienteSimulado(args.simular)
    modo = cfg["api"].get("modo", "navegador")
    if modo == "navegador":
        return ClienteNavegador(cfg)
    return ClienteHttp(cfg)


def executar(args):
    cfg = carregar_json(ARQ_CONFIG, None)
    if cfg is None:
        print("ERRO: config.json não encontrado ou inválido.")
        return 1

    datas = datas_para_buscar(cfg)[: args.max_datas or None]
    destinos = list(cfg["destinos"].items())[: args.max_destinos or None]
    pausa = cfg.get("pausa_entre_consultas_seg", 1.5)
    limite_bloqueios = cfg.get("bloqueios_consecutivos_para_abortar", 6)

    dados_anteriores = carregar_json(ARQ_DADOS, {})
    milhas_anteriores = {
        r["destino"]: r.get("milhas")
        for r in dados_anteriores.get("resultados", [])
        if r.get("milhas")
    }

    try:
        cliente = criar_cliente(cfg, args)
    except Exception as e:
        print(f"ERRO ao iniciar o cliente de consultas: {e}")
        return 1

    stats = {"consultas": 0, "com_voos": 0, "sem_voos": 0, "bloqueadas": 0, "erros": 0}
    bloqueios_consecutivos = 0
    abortado = False
    resultados = []

    try:
        for codigo, info in destinos:
            if abortado:
                break
            por_data = []
            for data_voo in datas:
                if not args.simular:
                    time.sleep(pausa + random.uniform(0, 1))
                status, payload = consultar(cliente, cfg, codigo, data_voo)
                stats["consultas"] += 1

                if status in STATUS_BLOQUEIO:
                    stats["bloqueadas"] += 1
                    bloqueios_consecutivos += 1
                    trecho = (payload or "")[:90] if isinstance(payload, str) else ""
                    print(f"  {cfg['origem']}->{codigo} {data_voo}: HTTP {status} (bloqueio?) {trecho}")
                    if bloqueios_consecutivos >= limite_bloqueios:
                        print("Muitos bloqueios consecutivos — abortando para não insistir.")
                        abortado = True
                        break
                    continue
                if payload is None or isinstance(payload, str):
                    stats["erros"] += 1
                    bloqueios_consecutivos = 0
                    continue

                bloqueios_consecutivos = 0
                voo = melhor_voo(payload, cfg["tarifas_aceitas"])
                if voo is None:
                    stats["sem_voos"] += 1
                    continue
                stats["com_voos"] += 1
                por_data.append({"data": data_voo, **voo})

            melhor = min(por_data, key=lambda v: v["milhas"]) if por_data else None
            anterior = milhas_anteriores.get(codigo)
            resultados.append({
                "destino": codigo,
                "nome": info["nome"],
                "regiao": info.get("regiao", ""),
                "limite_alerta": limite_alerta(cfg, codigo, info),
                "milhas": melhor["milhas"] if melhor else None,
                "detalhes": melhor,
                "variacao": (melhor["milhas"] - anterior) if (melhor and anterior) else None,
                "link": link_emissao(cfg["origem"], codigo, melhor["data"]) if melhor else None,
                "datas": sorted(por_data, key=lambda v: v["data"]),
            })
            if melhor:
                print(f"{cfg['origem']}->{codigo}: {melhor['milhas']} milhas em {melhor['data']}")
            else:
                print(f"{cfg['origem']}->{codigo}: nenhum voo encontrado")
    finally:
        cliente.close()

    resultados.sort(key=lambda r: (r["milhas"] is None, r["milhas"] or 0))

    if abortado and stats["com_voos"] == 0:
        status_geral = "bloqueado"
    elif abortado or stats["bloqueadas"] > 0:
        status_geral = "parcial"
    else:
        status_geral = "ok"

    # Top ofertas: as N combinações (destino, data) mais baratas da varredura
    ofertas = [
        {
            "destino": r["destino"],
            "nome": r["nome"],
            "regiao": r["regiao"],
            "data": v["data"],
            "milhas": v["milhas"],
            "cia": v["cia"],
            "tipo_tarifa": v.get("tipo_tarifa"),
            "link": link_emissao(cfg["origem"], r["destino"], v["data"]),
        }
        for r in resultados
        for v in r["datas"]
    ]
    ofertas.sort(key=lambda o: o["milhas"])

    agora = datetime.now(FUSO)
    dados = {
        "gerado_em": agora.isoformat(timespec="seconds"),
        "origem": cfg["origem"],
        "status": status_geral,
        "estatisticas": stats,
        "top_ofertas": ofertas[: cfg.get("top_ofertas", 10)],
        "resultados": resultados,
    }
    ARQ_DADOS.parent.mkdir(parents=True, exist_ok=True)
    ARQ_DADOS.write_text(
        json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # Histórico (mínimo de milhas por destino a cada execução)
    historico = carregar_json(ARQ_HISTORICO, [])
    historico.append({
        "t": agora.isoformat(timespec="seconds"),
        "m": {r["destino"]: r["milhas"] for r in resultados if r["milhas"]},
    })
    ARQ_HISTORICO.write_text(
        json.dumps(historico[-MAX_ENTRADAS_HISTORICO:], ensure_ascii=False),
        encoding="utf-8",
    )

    # Alertas
    achados = [
        r for r in resultados
        if r["milhas"] and r["limite_alerta"] and r["milhas"] <= r["limite_alerta"]
    ]
    if achados:
        linhas = [
            f"# ✈️ Passagens baratas na Smiles — saindo de {cfg['origem']}",
            "",
            "| Destino | Milhas | Data | Cia | Reservar |",
            "|---|---|---|---|---|",
        ]
        for r in achados:
            d = r["detalhes"]
            linhas.append(
                f"| {r['nome']} ({r['destino']}) | **{r['milhas']:,}**".replace(",", ".")
                + f" | {d['data']} | {d['cia']} | [abrir no site]({r['link']}) |"
            )
        linhas += [
            "",
            "_Preços por trecho (só ida), sem taxa de embarque. "
            "Os valores mudam rápido — confirme no site antes de emitir._",
        ]
        ARQ_ALERTAS.write_text("\n".join(linhas), encoding="utf-8")
        print(f"\n{len(achados)} alerta(s) gerado(s) em {ARQ_ALERTAS.name}")
        enviar_email_resend(achados, cfg["origem"])
    elif ARQ_ALERTAS.exists():
        ARQ_ALERTAS.unlink()

    print(f"\nStatus: {status_geral} | {stats}")
    if status_geral == "bloqueado":
        print(
            "A API recusou todas as consultas. Provavelmente a chave/headers "
            "mudaram — veja a seção 'Se parar de funcionar' do README."
        )
    return 0


def main():
    parser = argparse.ArgumentParser(description="Monitor de milhas Smiles")
    parser.add_argument("--simular", help="arquivo JSON com resposta de exemplo (teste offline)")
    parser.add_argument("--max-destinos", type=int, default=0, help="limita destinos (teste)")
    parser.add_argument("--max-datas", type=int, default=0, help="limita datas (teste)")
    sys.exit(executar(parser.parse_args()))


if __name__ == "__main__":
    main()
