#!/usr/bin/env python3
"""
Monitor de passagens em milhas da Smiles.

Varre os destinos definidos em config.json a partir do aeroporto de origem,
amostrando datas futuras, e salva as passagens mais baratas (em milhas) em
docs/dados.json — que alimenta a página web. Quando encontra preço abaixo do
limite de alerta, gera alertas.md (usado pelo GitHub Actions para enviar e-mail).

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
from zoneinfo import ZoneInfo

# curl_cffi imita a "impressão digital" de conexão de um navegador real,
# o que evita bloqueios da proteção anti-bot (Akamai). Se não estiver
# instalado, cai para o requests comum.
try:
    from curl_cffi import requests

    USANDO_CURL_CFFI = True
except ImportError:
    import requests

    USANDO_CURL_CFFI = False

RAIZ = Path(__file__).resolve().parent
ARQ_CONFIG = RAIZ / "config.json"
ARQ_DADOS = RAIZ / "docs" / "dados.json"
ARQ_HISTORICO = RAIZ / "docs" / "historico.json"
ARQ_ALERTAS = RAIZ / "alertas.md"

FUSO = ZoneInfo("America/Maceio")
STATUS_BLOQUEIO = {401, 403, 406, 429}
MAX_ENTRADAS_HISTORICO = 120


def carregar_json(caminho, padrao):
    try:
        return json.loads(Path(caminho).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return padrao


def montar_headers(cfg):
    api = cfg["api"]
    headers = {
        "x-api-key": environ.get("SMILES_API_KEY") or api["x_api_key"],
        "Channel": "WEB",
        "Origin": "https://www.smiles.com.br",
        "Referer": "https://www.smiles.com.br/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "User-Agent": api.get("user_agent", "Mozilla/5.0"),
    }
    headers.update(api.get("headers_extras", {}))
    return headers


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


def consultar(sessao, cfg, headers, destino, data):
    """Faz uma consulta. Retorna (codigo_http, payload_ou_None)."""
    params = {
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
    for tentativa in (1, 2):
        try:
            resp = sessao.get(
                cfg["api"]["url"], params=params, headers=headers, timeout=25
            )
        except Exception:
            if tentativa == 1:
                time.sleep(5)
                continue
            return (0, None)
        if resp.status_code >= 500 and tentativa == 1:
            time.sleep(5)
            continue
        if resp.status_code != 200:
            return (resp.status_code, None)
        try:
            return (200, resp.json())
        except ValueError:
            return (200, None)
    return (0, None)


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


def executar(args):
    cfg = carregar_json(ARQ_CONFIG, None)
    if cfg is None:
        print("ERRO: config.json não encontrado ou inválido.")
        return 1

    headers = montar_headers(cfg)
    datas = datas_para_buscar(cfg)[: args.max_datas or None]
    destinos = list(cfg["destinos"].items())[: args.max_destinos or None]
    pausa = cfg.get("pausa_entre_consultas_seg", 1.5)
    limite_bloqueios = cfg.get("bloqueios_consecutivos_para_abortar", 6)

    simulado = None
    if args.simular:
        simulado = carregar_json(args.simular, None)
        if simulado is None:
            print(f"ERRO: não consegui ler {args.simular}")
            return 1

    dados_anteriores = carregar_json(ARQ_DADOS, {})
    milhas_anteriores = {
        r["destino"]: r.get("milhas")
        for r in dados_anteriores.get("resultados", [])
        if r.get("milhas")
    }

    if USANDO_CURL_CFFI:
        sessao = requests.Session(impersonate=cfg["api"].get("impersonate", "chrome"))
    else:
        sessao = requests.Session()
        print("AVISO: curl_cffi não instalado — usando requests (maior chance de bloqueio).")
    stats = {"consultas": 0, "com_voos": 0, "sem_voos": 0, "bloqueadas": 0, "erros": 0}
    bloqueios_consecutivos = 0
    abortado = False
    resultados = []

    for codigo, info in destinos:
        if abortado:
            break
        por_data = []
        for data_voo in datas:
            if simulado is not None:
                status, payload = 200, simulado
            else:
                time.sleep(pausa + random.uniform(0, 1))
                status, payload = consultar(sessao, cfg, headers, codigo, data_voo)
            stats["consultas"] += 1

            if status in STATUS_BLOQUEIO:
                stats["bloqueadas"] += 1
                bloqueios_consecutivos += 1
                print(f"  {cfg['origem']}->{codigo} {data_voo}: HTTP {status} (bloqueio?)")
                if bloqueios_consecutivos >= limite_bloqueios:
                    print("Muitos bloqueios consecutivos — abortando para não insistir.")
                    abortado = True
                    break
                continue
            if payload is None:
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
        resultado = {
            "destino": codigo,
            "nome": info["nome"],
            "regiao": info.get("regiao", ""),
            "limite_alerta": limite_alerta(cfg, codigo, info),
            "milhas": melhor["milhas"] if melhor else None,
            "detalhes": melhor,
            "variacao": (melhor["milhas"] - anterior) if (melhor and anterior) else None,
            "link": link_emissao(cfg["origem"], codigo, melhor["data"]) if melhor else None,
            "datas": sorted(por_data, key=lambda v: v["data"]),
        }
        resultados.append(resultado)
        if melhor:
            print(f"{cfg['origem']}->{codigo}: {melhor['milhas']} milhas em {melhor['data']}")
        else:
            print(f"{cfg['origem']}->{codigo}: nenhum voo encontrado")

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
