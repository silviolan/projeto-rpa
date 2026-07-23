"""
VALIDACAO - quao COERENTE com o real esta a altitude que o codigo produz.

O nucleo do projeto (urbano.py / rural.py) entrega ALTITUDE (cota) a partir de
um DEM gratuito de ~30 m (OpenTopoData 'mapzen' bilinear). Este modulo mede a
CONFIABILIDADE desse dado por CROSS-VALIDACAO entre varias fontes abertas e
independentes: para cada ponto consulta a cota em todas elas, toma o CONSENSO
(mediana) como melhor estimativa livre do terreno e mede o quanto a fonte
ADOTADA pelo codigo se afasta desse consenso.

  Fontes (todas gratuitas, sem cadastro/cartao):
    - OpenTopoData mapzen   (~30 m, bilinear)  <- ADOTADA pelo codigo
    - OpenTopoData srtm30m  (~30 m, NASA SRTM, independente)
    - OpenTopoData aster30m (~30 m, ASTER GDEM, independente)
    - Open-Elevation        (~30 m, SRTM, provedor independente)
    - Open-Meteo            (~90 m, Copernicus GLO-90, provedor independente)

  Metricas (padrao de literatura de DEM, calculadas vs. o consenso):
    - ME   (vies / erro medio)              - tendencia sistematica da fonte
    - MAE  (erro absoluto medio, m)
    - RMSE (raiz do erro quadratico, m)     - metrica-padrao de exatidao de DEM
    - MAPE (erro percentual absoluto medio) - o "%" do criterio <10%
    - LE90 = 1.6449*RMSE (m)                - exatidao vertical a 90% (NSSDA)
    - incerteza empirica = desvio-padrao entre fontes por ponto (banda ~real)
    - COERENCIA (%) = fracao de pontos dentro da tolerancia (criterios % e m)

  IMPORTANTE - o que este numero significa (e o que NAO significa):
    Mede PRECISAO/coerencia entre dados livres (o quanto fontes independentes
    concordam), nao a EXATIDAO contra o terreno verdadeiro. Para exatidao real
    seria preciso verdade de campo (RN do IBGE, GNSS/RTK ou LiDAR). Em terreno
    litoraneo baixo o DEM gratuito tem +-5 a 10 m de erro vertical: a banda de
    incerteza empirica abaixo quantifica exatamente essa margem.

Entrada : pontos do projeto (default 'pontos_rural.csv'; cai p/ 'pontos.csv').
Saida   : relatorio no console + 'validacao.xlsx' (por_ponto / metricas /
          fontes) + 'validacao.png' (cotas das fontes x ponto, com banda de
          consenso, e barras de erro da fonte adotada).

Stack: reaproveita a pilha de urbano.py (mesma consulta de altitude da fonte
adotada -> a validacao reflete o dado REAL do codigo). Sem novas dependencias.

Execucao:  python validacao.py        (ou opcao [3] do menu.py)
"""

import os
import statistics
import time

import pandas as pd

# Reaproveita a pilha ja validada do projeto. A fonte ADOTADA usa exatamente a
# mesma chamada do codigo (OpenTopoData mapzen bilinear) -> a validacao reflete
# o dado que urbano.py/rural.py realmente produzem.
from urbano import (carrega_env, geod, plt, escreve_xlsx_seguro,
                    _altitude_opentopodata, _altitude_open_elevation)

import requests

# ---------- configuracao ----------
ENTRADA = "pontos_rural.csv"        # lista de pontos (lat,lon[,nome])
ENTRADA_FALLBACK = "pontos.csv"     # se faltar: usa origem/destino dos segmentos
SAIDA = "validacao.xlsx"
SAIDA_GRAFICO = "validacao.png"

PASSO_VALID_M = 50.0   # densifica o trajeto entre pontos consecutivos a cada
                       # ~PASSO_VALID_M para validar mais pontos sem estourar o
                       # limite das APIs gratuitas. 0 = so os pontos do CSV.
N_AMOSTRA_MAX = 90     # teto de pontos validados (APIs livres aceitam <=100 por
                       # requisicao); subamostra uniforme se passar disso.

TOL_PCT = 10.0         # criterio do usuario: |erro|/cota <= 10%  -> "coerente"
TOL_M = 5.0            # criterio de engenharia: |erro| <= 5 m (ruido tipico DEM 30 m)
ALVO_COERENCIA = 90.0  # % minimo de pontos coerentes (no criterio %) p/ "PASSA"

PLOTAR = True
PAUSA_FONTE = 1.2      # s entre fontes/lotes (OpenTopoData publico: ~1 req/s)
TENTATIVAS = 4         # retentativas por lote antes de marcar a fonte indisponivel
RETRY_PAUSA = 1.5      # s entre retentativas

# Fontes consultadas. 'adotada' marca a que o codigo realmente usa.
FONTES = [
    {"nome": "OpenTopoData mapzen",  "tag": "mapzen",  "tipo": "otd",
     "dataset": "mapzen",  "res_m": 30, "adotada": True},
    {"nome": "OpenTopoData SRTM30m", "tag": "srtm30m", "tipo": "otd",
     "dataset": "srtm30m", "res_m": 30, "adotada": False},
    {"nome": "OpenTopoData ASTER30m","tag": "aster30m","tipo": "otd",
     "dataset": "aster30m","res_m": 30, "adotada": False},
    {"nome": "Open-Elevation SRTM",  "tag": "openelev","tipo": "oe",
     "res_m": 30, "adotada": False},
    {"nome": "Open-Meteo Copernicus","tag": "openmeteo","tipo": "om",
     "res_m": 90, "adotada": False},
]
OTD_INTERP_VALID = "bilinear"   # mesma interpolacao usada pelo codigo
# ----------------------------------


# ============ leitura dos pontos ============
def _to_float(serie):
    """Converte coluna texto p/ float tolerando virgula decimal (BR)."""
    if serie.dtype == object:
        return serie.astype(str).str.replace(",", ".", regex=False).astype(float)
    return serie.astype(float)


def le_pontos(caminho=ENTRADA, fallback=ENTRADA_FALLBACK):
    """Le os pontos a validar. Retorna lista de dicts {'nome','lat','lon'}.

    Aceita o CSV de lista (rural: latitude,longitude[,nome]) ou, na falta dele,
    o CSV de segmentos (urbano: lat/lon inicial e final) extraindo as pontas.
    """
    alvo = caminho if os.path.exists(caminho) else fallback
    if not os.path.exists(alvo):
        raise FileNotFoundError(
            f"Nao encontrei '{caminho}' nem '{fallback}'. Coloque um CSV de "
            f"pontos (latitude,longitude[,nome]) na pasta."
        )
    df = pd.read_csv(alvo, sep=None, engine="python")
    df = df.rename(columns={c: c.strip().lower() for c in df.columns})

    # caso 1: lista de pontos (rural)
    col_lat = next((c for c in df.columns if c in {"latitude", "lat"}), None)
    col_lon = next((c for c in df.columns
                    if c in {"longitude", "lon", "lng", "long"}), None)
    if col_lat and col_lon:
        col_nome = next((c for c in df.columns
                         if c in {"nome", "id", "ponto"}), None)
        df[col_lat], df[col_lon] = _to_float(df[col_lat]), _to_float(df[col_lon])
        pts = []
        for i, row in df.iterrows():
            nome = (str(row[col_nome]) if col_nome and pd.notna(row[col_nome])
                    else f"P{i + 1}")
            pts.append({"nome": nome, "lat": float(row[col_lat]),
                        "lon": float(row[col_lon])})
        return pts, os.path.basename(alvo)

    # caso 2: segmentos (urbano) -> usa origem e destino de cada linha
    req = ["latitude inicial", "longitude inicial",
           "latitude final", "longitude final"]
    if all(c in df.columns for c in req):
        for c in req:
            df[c] = _to_float(df[c])
        col_nome = next((c for c in df.columns
                         if c in {"nome", "trajeto", "rota", "id"}), None)
        pts = []
        for i, row in df.iterrows():
            base = (str(row[col_nome]) if col_nome and pd.notna(row[col_nome])
                    else f"Seg{i + 1}")
            pts.append({"nome": f"{base}|ini", "lat": float(row[req[0]]),
                        "lon": float(row[req[1]])})
            pts.append({"nome": f"{base}|fim", "lat": float(row[req[2]]),
                        "lon": float(row[req[3]])})
        return pts, os.path.basename(alvo)

    raise ValueError(f"Nao reconheci as colunas de '{alvo}': {list(df.columns)}")


def densifica(pontos, passo_m=PASSO_VALID_M):
    """Insere pontos intermediarios (geodesicos) entre pontos consecutivos.

    Da mais amostras p/ uma estatistica de coerencia mais representativa, sem
    custo de API relevante. Pontos intermediarios recebem nome "" (sao apenas
    amostras de terreno; os pontos do CSV continuam nomeados).
    """
    if passo_m <= 0 or len(pontos) < 2:
        return list(pontos)
    saida = [pontos[0]]
    for a, b in zip(pontos, pontos[1:]):
        _, _, d = geod.inv(a["lon"], a["lat"], b["lon"], b["lat"])
        n = int(d // passo_m)
        if n > 0:
            for lon, lat in geod.npts(a["lon"], a["lat"], b["lon"], b["lat"], n):
                saida.append({"nome": "", "lat": lat, "lon": lon})
        saida.append(b)
    return saida


def subamostra(pontos, teto=N_AMOSTRA_MAX):
    """Reduz uniformemente a no maximo 'teto' pontos (preserva inicio e fim)."""
    if len(pontos) <= teto:
        return pontos
    passo = len(pontos) / teto
    idx = sorted({int(i * passo) for i in range(teto)} | {len(pontos) - 1})
    return [pontos[i] for i in idx]


# ============ consulta das fontes ============
def _altitude_open_meteo(lote):
    """Altitude de um lote via Open-Meteo (Copernicus GLO-90). Sem chave."""
    lats = ",".join(f"{lat:.6f}" for lat, lon in lote)
    lons = ",".join(f"{lon:.6f}" for lat, lon in lote)
    r = requests.get("https://api.open-meteo.com/v1/elevation",
                     params={"latitude": lats, "longitude": lons}, timeout=60)
    r.raise_for_status()
    return r.json()["elevation"]


def _consulta_lote(lote, fonte):
    """Despacha um lote (<=100 pts) para a fonte certa. Pode lancar excecao."""
    if fonte["tipo"] == "otd":
        url = f"https://api.opentopodata.org/v1/{fonte['dataset']}"
        return _altitude_opentopodata(lote, dataset_url=url,
                                      interp=OTD_INTERP_VALID)
    if fonte["tipo"] == "oe":
        return _altitude_open_elevation(lote)
    if fonte["tipo"] == "om":
        return _altitude_open_meteo(lote)
    raise ValueError(f"tipo de fonte desconhecido: {fonte['tipo']}")


def busca_fonte(latlon, fonte):
    """Cotas de TODOS os pontos por uma fonte, em lotes <=100, com retentativas.

    Retorna lista alinhada a 'latlon' (None onde faltar). Se a fonte falhar por
    completo, devolve so None e o chamador a marca como indisponivel.
    """
    out = []
    for i in range(0, len(latlon), 100):
        lote = latlon[i:i + 100]
        alts = None
        for tentativa in range(TENTATIVAS):
            try:
                alts = _consulta_lote(lote, fonte)
                break
            except Exception:
                if tentativa < TENTATIVAS - 1:
                    time.sleep(RETRY_PAUSA)
        out.extend(alts if alts else [None] * len(lote))
        if i + 100 < len(latlon):
            time.sleep(PAUSA_FONTE)
    return out


# ============ metricas ============
def _media(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def _rmse(difs):
    return (sum(d * d for d in difs) / len(difs)) ** 0.5 if difs else float("nan")


def metricas_vs_consenso(linhas):
    """Agrega as metricas da fonte ADOTADA contra o consenso (mediana).

    'linhas' = saida de monta_tabela (uma por ponto, ja com erro calculado).
    Retorna dict pronto p/ a aba 'metricas' e p/ o relatorio.
    """
    val = [l for l in linhas if l["erro_m"] is not None]
    n = len(val)
    if not n:
        return {"n_pontos": 0, "veredito": "SEM DADO"}

    difs = [l["erro_m"] for l in val]
    abss = [abs(d) for d in difs]
    pcts = [l["erro_pct"] for l in val if l["erro_pct"] is not None]
    incs = [l["desvio_fontes_m"] for l in val if l["desvio_fontes_m"] is not None]
    faixas = [l["faixa_fontes_m"] for l in val if l["faixa_fontes_m"] is not None]

    me, mae, rmse = _media(difs), _media(abss), _rmse(difs)
    coer_pct = 100.0 * sum(1 for l in val if l["coerente_pct"]) / n
    coer_m = 100.0 * sum(1 for l in val if l["coerente_m"]) / n
    mape = _media(pcts) if pcts else float("nan")

    passa = (coer_pct >= ALVO_COERENCIA) and (mape < TOL_PCT)
    return {
        "n_pontos": n,
        "vies_ME_m": round(me, 2),
        "MAE_m": round(mae, 2),
        "RMSE_m": round(rmse, 2),
        "LE90_m": round(1.6449 * rmse, 2),
        "MAPE_pct": round(mape, 2),
        "erro_max_abs_m": round(max(abss), 2),
        "incerteza_media_m": round(_media(incs), 2) if incs else None,
        "faixa_media_fontes_m": round(_media(faixas), 2) if faixas else None,
        f"coerentes_ate_{TOL_PCT:.0f}pct_%": round(coer_pct, 1),
        f"coerentes_ate_{TOL_M:.0f}m_%": round(coer_m, 1),
        "veredito": "COERENTE (<10%)" if passa else "ATENCAO (>10%)",
    }


def metricas_por_fonte(fontes_ok, cotas, consenso):
    """MAE e vies de CADA fonte vs. o consenso (mostra qual fonte destoa)."""
    linhas = []
    for f in fontes_ok:
        difs = [cotas[f["tag"]][i] - consenso[i]
                for i in range(len(consenso))
                if cotas[f["tag"]][i] is not None and consenso[i] is not None]
        if not difs:
            continue
        linhas.append({
            "fonte": f["nome"] + (" [ADOTADA]" if f["adotada"] else ""),
            "resolucao_m": f["res_m"],
            "vies_vs_consenso_m": round(_media(difs), 2),
            "MAE_vs_consenso_m": round(_media([abs(d) for d in difs]), 2),
            "RMSE_vs_consenso_m": round(_rmse(difs), 2),
            "n_validos": len(difs),
        })
    return linhas


# ============ orquestracao ============
def monta_tabela(pontos, cotas, fontes_ok, tag_adotada):
    """Uma linha por ponto: cotas das fontes, consenso e erro da fonte adotada.

    O consenso de referencia e a MEDIANA das fontes INDEPENDENTES (exclui a
    adotada) -> o erro mede de fato o quanto o codigo se afasta de testemunhas
    externas, sem se comparar consigo mesmo. A dispersao (incerteza) usa TODAS
    as fontes, pois reflete o espalhamento real do dado livre.
    """
    linhas = []
    consenso = []
    for i, p in enumerate(pontos):
        vals = [cotas[f["tag"]][i] for f in fontes_ok
                if cotas[f["tag"]][i] is not None]
        indep = [cotas[f["tag"]][i] for f in fontes_ok
                 if not f["adotada"] and cotas[f["tag"]][i] is not None]
        # referencia = consenso das independentes; se nao houver, cai p/ todas
        cons = statistics.median(indep) if indep else (
            statistics.median(vals) if vals else None)
        consenso.append(cons)

        desvio = statistics.pstdev(vals) if len(vals) >= 2 else None
        faixa = (max(vals) - min(vals)) if len(vals) >= 2 else None
        adot = cotas[tag_adotada][i]

        erro = erro_abs = erro_pct = None
        coer_pct = coer_m = False
        if adot is not None and cons is not None:
            erro = adot - cons
            erro_abs = abs(erro)
            erro_pct = (erro_abs / abs(cons) * 100) if cons else None
            coer_m = erro_abs <= TOL_M
            coer_pct = (erro_pct is not None) and (erro_pct <= TOL_PCT)

        linha = {
            "ponto": p["nome"] or f"amostra_{i + 1}",
            "latitude": round(p["lat"], 6),
            "longitude": round(p["lon"], 6),
        }
        for f in fontes_ok:
            v = cotas[f["tag"]][i]
            linha[f["tag"]] = round(v, 2) if v is not None else None
        linha.update({
            "consenso_indep_m": round(cons, 2) if cons is not None else None,
            "desvio_fontes_m": round(desvio, 2) if desvio is not None else None,
            "faixa_fontes_m": round(faixa, 2) if faixa is not None else None,
            "adotada_m": round(adot, 2) if adot is not None else None,
            "erro_m": round(erro, 2) if erro is not None else None,
            "erro_pct": round(erro_pct, 2) if erro_pct is not None else None,
            "coerente_pct": coer_pct,
            "coerente_m": coer_m,
        })
        linhas.append(linha)
    return linhas, consenso


def plota(linhas, fontes_ok, salvar=SAIDA_GRAFICO, mostrar=True):
    """Dois paineis: (1) cota de cada fonte por ponto + banda de consenso;
    (2) erro (m) da fonte adotada vs consenso, com a faixa de tolerancia +-TOL_M."""
    if not linhas:
        return
    x = list(range(len(linhas)))
    rotulos = [l["ponto"] for l in linhas]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7),
                                   gridspec_kw={"height_ratios": [2, 1]})

    # painel 1: cotas das fontes
    cons = [l["consenso_indep_m"] for l in linhas]
    desv = [l["desvio_fontes_m"] or 0 for l in linhas]
    banda_sup = [c + d if c is not None else None for c, d in zip(cons, desv)]
    banda_inf = [c - d if c is not None else None for c, d in zip(cons, desv)]
    xs_ok = [xi for xi, c in zip(x, cons) if c is not None]
    if xs_ok:
        ax1.fill_between([x[i] for i in xs_ok],
                         [banda_inf[i] for i in xs_ok],
                         [banda_sup[i] for i in xs_ok],
                         color="#9e9e9e", alpha=0.25,
                         label="consenso +-1 desvio (incerteza)")
    cores = ["#1f6feb", "#2e7d32", "#c1440e", "#6f42c1", "#b08800"]
    for f, cor in zip(fontes_ok, cores):
        y = [l[f["tag"]] for l in linhas]
        ax1.plot(x, y, marker="o", ms=3, lw=1.2, color=cor,
                 label=f["nome"] + (" [ADOTADA]" if f["adotada"] else ""))
    ax1.plot(x, cons, color="#111", lw=1.8, ls="--",
             label="consenso (mediana das independentes)")
    ax1.set_ylabel("Altitude (m)")
    ax1.set_title("Cotas das fontes livres por ponto + banda de consenso",
                  fontsize=12, fontweight="bold")
    ax1.grid(True, alpha=0.3)
    ax1.legend(fontsize=7, ncol=2, loc="best")

    # painel 2: erro da fonte adotada vs consenso
    erro = [l["erro_m"] if l["erro_m"] is not None else 0 for l in linhas]
    cores_b = ["#2e7d32" if l["coerente_pct"] else "#c62828" for l in linhas]
    ax2.bar(x, erro, color=cores_b, alpha=0.85)
    ax2.axhspan(-TOL_M, TOL_M, color="#2e7d32", alpha=0.12,
                label=f"tolerancia +-{TOL_M:.0f} m")
    ax2.axhline(0, color="#111", lw=0.8)
    ax2.set_ylabel("Erro adotada\n- consenso (m)")
    ax2.set_title("Desvio da fonte adotada (verde = coerente <10%; "
                  "vermelho = fora)", fontsize=11, fontweight="bold")
    ax2.grid(True, alpha=0.3, axis="y")
    ax2.legend(fontsize=8, loc="best")

    n = len(rotulos)
    passo_rot = max(1, n // 25)
    ax2.set_xticks(x[::passo_rot])
    ax2.set_xticklabels(rotulos[::passo_rot], rotation=45, ha="right", fontsize=7)
    ax1.set_xticks(x[::passo_rot])
    ax1.set_xticklabels([])

    fig.suptitle("VALIDACAO - coerencia da altitude entre fontes livres",
                 fontsize=13, fontweight="bold")
    fig.tight_layout()
    if salvar:
        fig.savefig(salvar, dpi=120)
        print(f"Grafico -> {salvar}")
    if mostrar:
        plt.show()
    plt.close(fig)


def relatorio(met, met_fontes, entrada_nome, n_total, fontes_ok):
    """Imprime o resumo da validacao no console."""
    print("\n" + "=" * 64)
    print("  RELATORIO DE VALIDACAO (coerencia entre fontes livres)")
    print("=" * 64)
    print(f"  Entrada            : {entrada_nome}")
    print(f"  Pontos validados   : {met['n_pontos']} (de {n_total})")
    print(f"  Fontes consultadas : {len(fontes_ok)}")
    for f in fontes_ok:
        print(f"       - {f['nome']} (~{f['res_m']} m)"
              + ("  [ADOTADA pelo codigo]" if f["adotada"] else ""))
    if met["n_pontos"] == 0:
        print("\n  ! Nenhuma fonte respondeu. Verifique a conexao/limites de API.")
        print("=" * 64)
        return
    print("-" * 64)
    print("  FONTE ADOTADA vs CONSENSO das fontes independentes:")
    print(f"     Vies (ME)            : {met['vies_ME_m']:+.2f} m")
    print(f"     MAE                  : {met['MAE_m']:.2f} m")
    print(f"     RMSE                 : {met['RMSE_m']:.2f} m")
    print(f"     LE90 (90% NSSDA)     : {met['LE90_m']:.2f} m")
    print(f"     MAPE (erro % medio)  : {met['MAPE_pct']:.2f} %")
    print(f"     Erro maximo          : {met['erro_max_abs_m']:.2f} m")
    print("-" * 64)
    print("  INCERTEZA do dado livre (dispersao entre as fontes):")
    print(f"     Desvio-padrao medio  : +-{met['incerteza_media_m']} m")
    print(f"     Faixa media (max-min): {met['faixa_media_fontes_m']} m")
    print("-" * 64)
    print("  COERENCIA (quanto da fonte adotada bate com o real estimado):")
    print(f"     Dentro de <{TOL_PCT:.0f}% : "
          f"{met[f'coerentes_ate_{TOL_PCT:.0f}pct_%']:.1f}% dos pontos")
    print(f"     Dentro de +-{TOL_M:.0f} m : "
          f"{met[f'coerentes_ate_{TOL_M:.0f}m_%']:.1f}% dos pontos")
    print("-" * 64)
    print("  POR FONTE (vies / MAE vs consenso):")
    for r in met_fontes:
        print(f"     {r['fonte']:<32} vies {r['vies_vs_consenso_m']:+6.2f} m | "
              f"MAE {r['MAE_vs_consenso_m']:5.2f} m")
    print("=" * 64)
    print(f"  VEREDITO: {met['veredito']}")
    print("           (criterio: >= {:.0f}% dos pontos com erro <{:.0f}% E "
          "MAPE <{:.0f}%)".format(ALVO_COERENCIA, TOL_PCT, TOL_PCT))
    print("  OBS: mede COERENCIA entre dados livres (precisao), nao exatidao")
    print("       vs. terreno real. Para exatidao: RN do IBGE / GNSS-RTK / LiDAR.")
    print("=" * 64 + "\n")


def main(entrada=None, saida=SAIDA, saida_grafico=SAIDA_GRAFICO,
         plotar=PLOTAR, mostrar=True):
    """Valida a coerencia da altitude entre fontes livres e gera XLSX + PNG.

    Os caminhos sao parametros (com os defaults do modulo) para permitir que a
    interface grafica reaproveite a mesma logica apontando para outros arquivos.
    Se 'entrada' for None, usa ENTRADA/ENTRADA_FALLBACK do modulo.
    Retorna um dict com os caminhos gravados, o veredito e estatisticas.
    """
    carrega_env()
    if entrada:
        pontos, entrada_nome = le_pontos(caminho=entrada, fallback=entrada)
    else:
        pontos, entrada_nome = le_pontos()
    pontos = densifica(pontos)
    pontos = subamostra(pontos)
    n_total = len(pontos)
    latlon = [(p["lat"], p["lon"]) for p in pontos]
    print(f"  -> {n_total} pontos a validar (origem: {entrada_nome})")

    # consulta cada fonte; descarta as que falharem por completo
    cotas, fontes_ok = {}, []
    for f in FONTES:
        print(f"     consultando {f['nome']} ...", end=" ", flush=True)
        vals = busca_fonte(latlon, f)
        ok = sum(1 for v in vals if v is not None)
        print(f"{ok}/{n_total} pontos")
        if ok > 0:
            cotas[f["tag"]] = vals
            fontes_ok.append(f)
        time.sleep(PAUSA_FONTE)

    if len(fontes_ok) < 2:
        print("\n  ! Preciso de pelo menos 2 fontes respondendo para comparar. "
              "Tente novamente (limites de API costumam ser temporarios).")
        return None

    # a fonte adotada precisa ter respondido; senao usa a 1a disponivel como ref
    adotada = next((f for f in fontes_ok if f["adotada"]), fontes_ok[0])
    tag_adotada = adotada["tag"]

    linhas, consenso = monta_tabela(pontos, cotas, fontes_ok, tag_adotada)
    met = metricas_vs_consenso(linhas)
    met_fontes = metricas_por_fonte(fontes_ok, cotas, consenso)

    relatorio(met, met_fontes, entrada_nome, n_total, fontes_ok)

    saida_real = escreve_xlsx_seguro(saida, {
        "metricas": pd.DataFrame([met]),
        "fontes": pd.DataFrame(met_fontes),
        "por_ponto": pd.DataFrame(linhas),
    })
    print(f"OK -> {saida_real}")

    grafico_real = None
    if plotar and linhas:
        plota(linhas, fontes_ok, salvar=saida_grafico, mostrar=mostrar)
        grafico_real = saida_grafico

    return {"xlsx": saida_real, "png": grafico_real,
            "veredito": met.get("veredito"), "n_pontos": met.get("n_pontos")}


if __name__ == "__main__":
    main()
