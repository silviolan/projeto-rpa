"""
QUADRANTE - deteccao de estradas / proto-estradas dentro de uma area.

A partir de um arquivo com as coordenadas dos DOIS pontos diagonais de um
quadrante (retangulo), o script:
  1) delimita o quadrante (bounding box: sul, oeste, norte, leste);
  2) consulta o OpenStreetMap (Overpass API) por todas as vias (`highway`)
     que passam dentro dessa area;
  3) classifica cada via em:
       - ESTRADA       : vias formais (asfalto/leito definido): motorway, trunk,
                         primary/secondary/tertiary, unclassified, residential,
                         living_street, service...
       - PROTO-ESTRADA : leito precario / informal: track (estrada de terra/
                         carrocavel), path (trilha), road (classificacao
                         desconhecida), bridleway, cycleway, footway...
  4) devolve as coordenadas (latitude, longitude) de TODOS os pontos que
     tracejam cada via encontrada. Se nao houver nenhuma, apenas informa.

Fonte: OpenStreetMap via Overpass API - gratuita, SEM CHAVE (mesma filosofia
open source do resto do projeto; usa apenas `requests`, ja instalado).

Entrada : arquivo (CSV/TXT) com os pontos diagonais do quadrante. Formatos
          aceitos (o separador e detectado automaticamente):
            a) duas linhas com colunas 'latitude,longitude'
               (cabecalho opcional; coluna 'nome' opcional):
                   latitude,longitude
                   -7.2110,-34.9640
                   -7.2180,-34.9585
            b) uma linha com 'latitude inicial,longitude inicial,
               latitude final,longitude final'
            c) sem cabecalho: duas linhas 'lat,lon'
          Os pontos NAO precisam estar em nenhum canto especifico: o quadrante
          e o retangulo que os contem (min/max de lat e lon).
Saida   : XLSX com duas planilhas + grafico PNG:
            - 'resumo' : uma linha por via (id, classe, tipo OSM, nome,
                         superficie, n_pontos, comprimento_m);
            - 'pontos' : uma linha por vertice (via_id, classe, tipo, nome,
                         ordem, latitude, longitude) -> os pontos pedidos;
            - PNG      : mapa do quadrante com as vias encontradas.

Instalacao (sem dependencias novas):
  pip install pandas pyproj openpyxl requests matplotlib

Uso:
  python estradas.py            # usa quadrante.csv
  ou pela opcao [4] do menu.py
"""

import os
import time

import matplotlib.pyplot as plt
import pandas as pd
from pyproj import Geod
import requests

# reaproveita o gravador de xlsx tolerante a arquivo aberto (mesmo do urbano)
from urbano import escreve_xlsx_seguro

# ---------- configuracao ----------
ENTRADA = "quadrante.csv"
SAIDA = "estradas.xlsx"
SAIDA_GRAFICO = "estradas.png"
PLOTAR = True
# True  -> as coordenadas de saida sao APENAS a zona de rodagem DENTRO do
#          quadrante (as vias sao cortadas na borda; pontos de interseccao com
#          o limite sao inseridos). Uma via que entra e sai vira varios trechos.
# False -> geometria COMPLETA de cada via que cruza o quadrante (nao corta).
# O mapa PNG sempre mostra a via inteira (contexto) e destaca a parte interna.
RECORTAR_NO_QUADRANTE = True
# Densificacao: o OSM so guarda os vertices que definem o tracado (um trecho reto
# de 500 m tem so 2 pontos). Com DENSIFICAR=True, insere pontos intermediarios a
# cada ~PASSO_M metros ao longo da rodagem (preservando os vertices originais),
# cobrindo toda a extensao. Mesmo criterio do perfil de elevacao urbano/rural.
DENSIFICAR = True
PASSO_M = 10.0          # espacamento alvo entre pontos densificados (metros)

# Overpass API (OpenStreetMap) - gratuita e sem chave. Varios espelhos como
# reserva: a instancia publica cai/limita de forma intermitente, entao tenta o
# proximo espelho automaticamente (mesma logica de retentativa do urbano.py).
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
OVERPASS_TIMEOUT = 90       # s: timeout da requisicao HTTP e da query (server-side)
OVERPASS_TENTATIVAS = 2     # tentativas POR espelho antes de passar ao proximo
OVERPASS_RETRY_PAUSA = 2.0  # s entre retentativas do mesmo espelho
# alguns espelhos (ex.: overpass-api.de) recusam requisicoes sem User-Agent (HTTP 406)
OVERPASS_HEADERS = {"User-Agent": "ProjetoRPA-estradas/1.0 (uso academico)"}

# Classificacao dos valores de `highway` do OSM.
#   ESTRADA       = via formal, com leito definido.
#   PROTO-ESTRADA = leito precario/informal ou classificacao desconhecida.
# Ver https://wiki.openstreetmap.org/wiki/Key:highway
ESTRADAS = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential",
    "living_street", "service", "busway",
}
PROTO_ESTRADAS = {
    "track",       # estrada de terra / carrocavel (rural) - a "proto-estrada" tipica
    "road",        # via de classificacao desconhecida (ainda nao mapeada)
    "path",        # trilha / caminho
    "footway", "pedestrian", "bridleway", "cycleway", "steps",
    "corridor", "raceway", "escape",
}
# ----------------------------------

geod = Geod(ellps="WGS84")


def _para_float(serie):
    """Converte uma coluna para float tolerando virgula decimal (padrao BR)."""
    if serie.dtype == object:
        serie = serie.astype(str).str.replace(",", ".", regex=False)
    return serie.astype(float)


def _acha_coluna(colunas, apelidos):
    """Primeira coluna cujo nome (sem espacos/maiusculas) esta em 'apelidos'."""
    return next((c for c in colunas if str(c).strip().lower() in apelidos), None)


def le_quadrante(caminho):
    """Le o arquivo de entrada e retorna (bbox, pontos).

    bbox  = dict(sul, oeste, norte, leste)  -> os limites do quadrante.
    pontos = lista de (latitude, longitude) informados (>= 2), para relatorio.

    Aceita os tres formatos descritos no cabecalho do modulo. O quadrante e
    sempre o retangulo que contem os pontos dados (min/max), entao a ordem dos
    pontos diagonais nao importa.
    """
    if not os.path.exists(caminho):
        raise FileNotFoundError(f"Arquivo de entrada nao encontrado: {caminho}")

    df = pd.read_csv(caminho, sep=None, engine="python")
    df.columns = [str(c).strip().lower() for c in df.columns]

    apel_lat = {"latitude", "lat", "y"}
    apel_lon = {"longitude", "long", "lon", "lng", "x"}
    col_lat = _acha_coluna(df.columns, apel_lat)
    col_lon = _acha_coluna(df.columns, apel_lon)

    # formato (b): uma linha com pontos inicial e final
    col_lat_i = _acha_coluna(df.columns, {"latitude inicial", "lat inicial",
                                          "latitude 1", "lat1", "lat_ini"})
    col_lon_i = _acha_coluna(df.columns, {"longitude inicial", "lon inicial",
                                          "longitude 1", "lon1", "lon_ini"})
    col_lat_f = _acha_coluna(df.columns, {"latitude final", "lat final",
                                          "latitude 2", "lat2", "lat_fim"})
    col_lon_f = _acha_coluna(df.columns, {"longitude final", "lon final",
                                          "longitude 2", "lon2", "lon_fim"})

    pontos = []
    if col_lat and col_lon:                      # formato (a): coluna lat/lon
        lats = _para_float(df[col_lat])
        lons = _para_float(df[col_lon])
        pontos = list(zip(lats, lons))
    elif col_lat_i and col_lon_i and col_lat_f and col_lon_f:  # formato (b)
        for _, r in df.iterrows():
            pontos.append((float(str(r[col_lat_i]).replace(",", ".")),
                           float(str(r[col_lon_i]).replace(",", "."))))
            pontos.append((float(str(r[col_lat_f]).replace(",", ".")),
                           float(str(r[col_lon_f]).replace(",", "."))))
    else:                                        # formato (c): sem cabecalho
        bruto = pd.read_csv(caminho, sep=None, engine="python", header=None)
        lats = _para_float(bruto.iloc[:, 0])
        lons = _para_float(bruto.iloc[:, 1])
        pontos = list(zip(lats, lons))

    pontos = [(la, lo) for la, lo in pontos
              if pd.notna(la) and pd.notna(lo)]
    if len(pontos) < 2:
        raise ValueError(
            "Sao necessarios ao menos 2 pontos (os cantos diagonais do "
            f"quadrante). Lidos: {len(pontos)}."
        )

    lats = [p[0] for p in pontos]
    lons = [p[1] for p in pontos]
    bbox = {"sul": min(lats), "oeste": min(lons),
            "norte": max(lats), "leste": max(lons)}
    if bbox["sul"] == bbox["norte"] or bbox["oeste"] == bbox["leste"]:
        raise ValueError(
            "Os pontos nao formam um quadrante (mesma latitude ou longitude)."
        )
    return bbox, pontos


def _monta_query(bbox):
    """Monta a query Overpass QL: todas as vias 'highway' dentro do bbox."""
    sul, oeste, norte, leste = (bbox["sul"], bbox["oeste"],
                                bbox["norte"], bbox["leste"])
    # 'out geom' devolve a geometria (lat/lon de cada no) e as tags de cada via.
    return (
        f"[out:json][timeout:{OVERPASS_TIMEOUT}];"
        f'way["highway"]({sul},{oeste},{norte},{leste});'
        f"out geom tags;"
    )


def consulta_overpass(bbox):
    """Consulta o Overpass e retorna a lista bruta de 'ways' (dict do JSON).

    Tenta cada espelho de OVERPASS_URLS com retentativas. Lanca se todos
    falharem.
    """
    query = _monta_query(bbox)
    ultimo_erro = None
    for url in OVERPASS_URLS:
        for tentativa in range(OVERPASS_TENTATIVAS):
            try:
                r = requests.post(url, data={"data": query},
                                  headers=OVERPASS_HEADERS,
                                  timeout=OVERPASS_TIMEOUT + 30)
                r.raise_for_status()
                elementos = r.json().get("elements", [])
                return [e for e in elementos if e.get("type") == "way"]
            except Exception as e:                # rede/SSL/timeout/JSON invalido
                ultimo_erro = e
                print(f"     ! Overpass {url} falhou "
                      f"(tentativa {tentativa + 1}/{OVERPASS_TENTATIVAS}): {e}")
                time.sleep(OVERPASS_RETRY_PAUSA)
    raise RuntimeError(
        f"Nenhum espelho do Overpass respondeu. Ultimo erro: {ultimo_erro}"
    )


def classifica(tipo_osm):
    """Retorna 'ESTRADA', 'PROTO-ESTRADA' ou 'OUTRO' para um valor de highway."""
    if tipo_osm in ESTRADAS:
        return "ESTRADA"
    if tipo_osm in PROTO_ESTRADAS:
        return "PROTO-ESTRADA"
    return "OUTRO"


def extrai_vias(ways):
    """Converte os 'ways' do Overpass em uma lista de vias normalizadas.

    Cada via = dict(id, tipo_osm, classe, nome, superficie, geom, comprimento_m),
    onde geom = lista de (latitude, longitude) que traceja a via.
    """
    vias = []
    for w in ways:
        geom = w.get("geometry") or []
        if len(geom) < 2:                         # via sem tracado utilizavel
            continue
        tags = w.get("tags", {})
        tipo = tags.get("highway", "")
        pontos = [(g["lat"], g["lon"]) for g in geom]
        lats = [p[0] for p in pontos]
        lons = [p[1] for p in pontos]
        comprimento = geod.line_length(lons, lats)   # metros (geodesica WGS84)
        vias.append({
            "id": w.get("id"),
            "tipo_osm": tipo,
            "classe": classifica(tipo),
            "nome": tags.get("name", ""),
            "superficie": tags.get("surface", ""),
            "geom": pontos,
            "comprimento_m": round(comprimento, 1),
        })
    # maiores/mais relevantes primeiro
    vias.sort(key=lambda v: v["comprimento_m"], reverse=True)
    return vias


def _clip_segmento(lat1, lon1, lat2, lon2, bbox):
    """Recorta um segmento ao retangulo do quadrante (algoritmo Liang-Barsky).

    Retorna (a, b, t0, t1) com a e b em (lat, lon) — os extremos do trecho do
    segmento que fica DENTRO do quadrante — ou None se o segmento nao intercepta.
    t0/t1 in [0,1] sao as fracoes do segmento onde ele entra/sai (t0=0 -> ja
    comeca dentro; t1=1 -> termina dentro).
    """
    x1, y1, x2, y2 = lon1, lat1, lon2, lat2          # x=lon, y=lat
    xmin, xmax = bbox["oeste"], bbox["leste"]
    ymin, ymax = bbox["sul"], bbox["norte"]
    dx, dy = x2 - x1, y2 - y1
    p = (-dx, dx, -dy, dy)
    q = (x1 - xmin, xmax - x1, y1 - ymin, ymax - y1)
    t0, t1 = 0.0, 1.0
    for pi, qi in zip(p, q):
        if pi == 0:
            if qi < 0:
                return None                          # paralelo a borda e fora
        else:
            t = qi / pi
            if pi < 0:                               # entrando: aperta t0
                if t > t1:
                    return None
                if t > t0:
                    t0 = t
            else:                                    # saindo: aperta t1
                if t < t0:
                    return None
                if t < t1:
                    t1 = t
    a = (y1 + t0 * dy, x1 + t0 * dx)                 # (lat, lon)
    b = (y1 + t1 * dy, x1 + t1 * dx)
    return a, b, t0, t1


def recorta_polilinha(pontos, bbox):
    """Recorta a polilinha ao quadrante e retorna as sub-polilinhas internas.

    Cada sub-polilinha (>= 2 pontos) fica inteiramente dentro do quadrante, com
    os pontos de interseccao inseridos exatamente sobre a borda.
    """
    trechos, atual = [], []
    for (lat1, lon1), (lat2, lon2) in zip(pontos, pontos[1:]):
        res = _clip_segmento(lat1, lon1, lat2, lon2, bbox)
        if res is None:                              # segmento todo fora
            if len(atual) >= 2:
                trechos.append(atual)
            atual = []
            continue
        a, b, t0, t1 = res
        # continua o trecho corrente se este segmento comeca onde o anterior
        # terminou (t0 == 0 e o anterior nao havia saido); senao inicia outro.
        if atual and t0 <= 1e-12:
            atual.append(b)
        else:
            if len(atual) >= 2:
                trechos.append(atual)
            atual = [a, b]
        if t1 < 1.0:                                 # o segmento SAIU: fecha aqui
            if len(atual) >= 2:
                trechos.append(atual)
            atual = []
    if len(atual) >= 2:
        trechos.append(atual)
    return trechos


def recorta_vias(vias, bbox):
    """Recorta cada via ao quadrante. Cada trecho interno vira um registro.

    Mantem os metadados da via (id/tipo/classe/nome/superficie) e numera os
    trechos (campo 'trecho'); recalcula o comprimento so da parte interna.
    """
    recortadas = []
    for v in vias:
        for i, geom in enumerate(recorta_polilinha(v["geom"], bbox), 1):
            lats = [p[0] for p in geom]
            lons = [p[1] for p in geom]
            recortadas.append({
                "id": v["id"],
                "trecho": i,
                "tipo_osm": v["tipo_osm"],
                "classe": v["classe"],
                "nome": v["nome"],
                "superficie": v["superficie"],
                "geom": geom,
                "comprimento_m": round(geod.line_length(lons, lats), 1),
            })
    recortadas.sort(key=lambda v: v["comprimento_m"], reverse=True)
    return recortadas


def densifica_polilinha(pontos, passo_m):
    """Insere pontos intermediarios a cada ~passo_m metros ao longo da linha.

    Preserva os vertices originais e adiciona entre eles pontos igualmente
    espacados (geodesica WGS84), como o reamostra_trajeto do urbano.py. Nao
    altera o tracado (os pontos novos ficam exatamente sobre os segmentos), so
    aumenta a densidade de amostragem. Retorna nova lista de (lat, lon).
    """
    if len(pontos) < 2 or passo_m <= 0:
        return list(pontos)
    saida = [pontos[0]]
    for (lat1, lon1), (lat2, lon2) in zip(pontos, pontos[1:]):
        _, _, d = geod.inv(lon1, lat1, lon2, lat2)
        n = int(d // passo_m)                    # nro de pontos a inserir
        if n > 0:
            for lon, lat in geod.npts(lon1, lat1, lon2, lat2, n):
                saida.append((lat, lon))
        saida.append((lat2, lon2))               # mantem o vertice original
    return saida


def monta_tabelas(vias):
    """Constroi os DataFrames 'resumo' e 'pontos' a partir das vias."""
    linhas_resumo, linhas_pontos = [], []
    for v in vias:
        trecho = v.get("trecho", 1)
        linhas_resumo.append({
            "via_id": v["id"],
            "trecho": trecho,
            "classe": v["classe"],
            "tipo_osm": v["tipo_osm"],
            "nome": v["nome"],
            "superficie": v["superficie"],
            "n_pontos": len(v["geom"]),
            "comprimento_m": v["comprimento_m"],
        })
        for ordem, (lat, lon) in enumerate(v["geom"], 1):
            linhas_pontos.append({
                "via_id": v["id"],
                "trecho": trecho,
                "classe": v["classe"],
                "tipo_osm": v["tipo_osm"],
                "nome": v["nome"],
                "ordem": ordem,
                "latitude": lat,
                "longitude": lon,
            })
    return pd.DataFrame(linhas_resumo), pd.DataFrame(linhas_pontos)


def plota_mapa(bbox, pontos, vias_completas, vias_dentro, salvar, mostrar=False):
    """Desenha o quadrante e as vias e salva em PNG.

    'vias_completas' entram como CONTEXTO esmaecido (a via inteira, mesmo o que
    fica fora); 'vias_dentro' (zona de rodagem recortada no quadrante) sao
    desenhadas em destaque, coloridas por classe.
    """
    import math

    cores = {"ESTRADA": "#1f77b4", "PROTO-ESTRADA": "#d62728", "OUTRO": "#7f7f7f"}
    fig, ax = plt.subplots(figsize=(9, 8))

    # contexto: geometria completa das vias (inclui o que fica fora do quadrante)
    for v in vias_completas:
        ax.plot([p[1] for p in v["geom"]], [p[0] for p in v["geom"]],
                color="#b8b8b8", lw=1.0, alpha=0.7, zorder=1)
    ax.plot([], [], color="#b8b8b8", lw=1.0, label="via (fora do quadrante)")

    # retangulo do quadrante
    ax.plot([bbox["oeste"], bbox["leste"], bbox["leste"],
             bbox["oeste"], bbox["oeste"]],
            [bbox["sul"], bbox["sul"], bbox["norte"],
             bbox["norte"], bbox["sul"]],
            "k--", lw=1.2, zorder=3, label="quadrante")

    # pontos diagonais informados
    ax.scatter([p[1] for p in pontos], [p[0] for p in pontos],
               c="black", marker="x", s=60, zorder=6, label="pontos informados")

    # zona de rodagem DENTRO do quadrante (destaque)
    ja_legendado = set()
    for v in vias_dentro:
        lats = [p[0] for p in v["geom"]]
        lons = [p[1] for p in v["geom"]]
        classe = v["classe"]
        rotulo = f"{classe} (no quadrante)" if classe not in ja_legendado else None
        ja_legendado.add(classe)
        ax.plot(lons, lats, color=cores.get(classe, "#7f7f7f"),
                lw=2.4 if classe == "ESTRADA" else 1.8,
                linestyle="-" if classe == "ESTRADA" else "--",
                zorder=4, label=rotulo)

    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_title("Zona de rodagem no quadrante")
    # mantem a escala geografica proporcional (1 grau de lon < 1 grau de lat)
    lat_media = (bbox["sul"] + bbox["norte"]) / 2
    ax.set_aspect(1.0 / max(math.cos(math.radians(lat_media)), 1e-6))
    ax.legend(loc="best", fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(salvar, dpi=150)
    if mostrar:
        plt.show()
    plt.close(fig)


def main(entrada=ENTRADA, saida=SAIDA, saida_grafico=SAIDA_GRAFICO,
         plotar=PLOTAR, mostrar=True):
    """Le o quadrante, procura estradas/proto-estradas e gera XLSX + PNG.

    Os caminhos sao parametros (com os defaults do modulo) para a interface
    grafica reaproveitar a mesma logica. Retorna um dict com o resultado.
    """
    bbox, pontos = le_quadrante(entrada)
    print(f"  Quadrante: sul={bbox['sul']:.6f} oeste={bbox['oeste']:.6f} "
          f"norte={bbox['norte']:.6f} leste={bbox['leste']:.6f}")
    largura_m = geod.line_length([bbox["oeste"], bbox["leste"]],
                                 [bbox["sul"], bbox["sul"]])
    altura_m = geod.line_length([bbox["oeste"], bbox["oeste"]],
                                [bbox["sul"], bbox["norte"]])
    print(f"  Dimensao aproximada: {largura_m:.0f} m (L-O) x {altura_m:.0f} m (N-S)")

    print("  -> consultando OpenStreetMap (Overpass)...")
    ways = consulta_overpass(bbox)
    vias = extrai_vias(ways)                # geometria completa (contexto/plot)

    # Recorta a zona de rodagem para o interior do quadrante (o que o usuario
    # pediu). Com RECORTAR_NO_QUADRANTE=False, mantem a via inteira.
    if RECORTAR_NO_QUADRANTE:
        vias_saida = recorta_vias(vias, bbox)
        escopo = "dentro do quadrante"
    else:
        vias_saida = [dict(v, trecho=1) for v in vias]
        escopo = "geometria completa"

    # densifica cada rodagem (~PASSO_M em PASSO_M) preservando os vertices,
    # para cobrir toda a extensao com pontos, nao so os vertices do OSM.
    if DENSIFICAR:
        for v in vias_saida:
            v["geom"] = densifica_polilinha(v["geom"], PASSO_M)

    estradas = [v for v in vias_saida if v["classe"] == "ESTRADA"]
    protos = [v for v in vias_saida if v["classe"] == "PROTO-ESTRADA"]
    outras = [v for v in vias_saida if v["classe"] == "OUTRO"]
    relevantes = estradas + protos

    if not relevantes:
        print("\n==> NAO ha estrada nem proto-estrada dentro do quadrante.")
        if outras:
            print(f"    (havia {len(outras)} via(s) de outro tipo, ignoradas.)")
        saida_real = escreve_xlsx_seguro(saida, {
            "resumo": pd.DataFrame(
                [{"resultado": "nenhuma estrada/proto-estrada encontrada"}]),
            "pontos": pd.DataFrame(),
        })
        print(f"OK -> {saida_real}")
        return {"tem_estrada": False, "vias": 0, "pontos": 0,
                "xlsx": saida_real, "png": None, "bbox": bbox}

    n_pontos = sum(len(v["geom"]) for v in relevantes)
    n_vias = len({v["id"] for v in relevantes})
    dens = f" densificadas a ~{PASSO_M:.0f} m" if DENSIFICAR else ""
    print(f"\n==> SIM: {n_vias} via(s) com rodagem {escopo} "
          f"({len(estradas)} trecho(s) de estrada, "
          f"{len(protos)} de proto-estrada); "
          f"{n_pontos} coordenadas (lat/lon){dens}.")
    for v in relevantes[:15]:
        nome = v["nome"] or "(sem nome)"
        tr = f" t{v['trecho']}" if v.get("trecho") else ""
        print(f"    - #{v['id']}{tr} [{v['classe']}/{v['tipo_osm']}] {nome}: "
              f"{len(v['geom'])} pts, {v['comprimento_m']:.0f} m")
    if len(relevantes) > 15:
        print(f"    ... e mais {len(relevantes) - 15} trecho(s) (ver planilha).")

    # so a zona de rodagem (estradas + proto-estradas) vai para as tabelas
    df_resumo, df_pontos = monta_tabelas(relevantes)
    saida_real = escreve_xlsx_seguro(saida, {
        "resumo": df_resumo,
        "pontos": df_pontos,
    })
    print(f"OK -> {saida_real} "
          f"({len(relevantes)} trecho(s), {len(df_pontos)} coordenadas)")

    grafico_real = None
    if plotar:
        plota_mapa(bbox, pontos, vias, relevantes,
                   salvar=saida_grafico, mostrar=mostrar)
        grafico_real = saida_grafico
        print(f"OK -> {grafico_real}")

    return {"tem_estrada": True, "vias": n_vias, "trechos": len(relevantes),
            "pontos": n_pontos, "estradas": len(estradas),
            "proto_estradas": len(protos),
            "xlsx": saida_real, "png": grafico_real, "bbox": bbox}


if __name__ == "__main__":
    main()
