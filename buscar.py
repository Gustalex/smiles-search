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

    Estratégia: abre a página de emissão com uma busca real (isso faz o sensor
    da Akamai validar a sessão e gerar os cookies _abck/bm_*). Depois, as
    consultas são feitas via fetch() de dentro da página. Se o fetch for
    bloqueado, cai para o modo "navegação": abre a página de busca para cada
    consulta e intercepta a resposta da API que o próprio site faz (mais lento,
    porém indistinguível de um usuário real).
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
        self._modo_navegacao = False
        self._falhas_fetch = 0

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

        # "Esquenta" a sessão com uma busca real na página de emissão:
        # é isso que faz o sensor da Akamai liberar os cookies.
        primeira_data = (date.today() + timedelta(days=30)).isoformat()
        print("Abrindo a busca da Smiles para validar a sessão (anti-bot)...")
        status, _ = self._consultar_navegando("GRU", primeira_data)
        if status == 200:
            print("Sessão validada pela Akamai.")
        else:
            print(f"Aviso: busca de validação retornou HTTP {status} — seguindo mesmo assim.")

    def _consultar_navegando(self, destino, data):
        """Abre a página de busca e intercepta a resposta da API feita pelo site."""
        url = link_emissao(self._cfg["origem"], destino, data)
        try:
            with self._page.expect_response(
                lambda r: "airlines/search" in r.url, timeout=60_000
            ) as espera:
                self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            resposta = espera.value
            return resposta.status, resposta.text()
        except Exception as e:
            return 0, f"navegacao: {type(e).__name__}"

    def get(self, url, params):
        destino = params["destinationAirportCode"]
        data = params["departureDate"]
        if self._modo_navegacao:
            return self._consultar_navegando(destino, data)

        resultado = self._page.evaluate(
            self._JS_FETCH,
            {"url": f"{url}?{urlencode(params)}", "headers": self._headers},
        )
        if resultado["status"] in STATUS_BLOQUEIO:
            self._falhas_fetch += 1
            if self._falhas_fetch >= 2:
                print("fetch() bloqueado — mudando para o modo navegação (mais lento, mais confiável).")
                self._modo_navegacao = True
            return self._consultar_navegando(destino, data)
        self._falhas_fetch = 0
        return resultado["status"], resultado["body"]

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
    return (
        "https://www.smiles.com.br/mfe/emissao-passagem/?adults=1&cabinType=all"
        f"&children=0&currencyCode=BRL&departureDate={data}"
        f"&destinationAirportCode={destino}&infants=0&isFlexibleDateChecked=false"
        f"&originAirportCode={origem}&tripType=2"
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

    agora = datetime.now(FUSO)
    dados = {
        "gerado_em": agora.isoformat(timespec="seconds"),
        "origem": cfg["origem"],
        "status": status_geral,
        "estatisticas": stats,
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
