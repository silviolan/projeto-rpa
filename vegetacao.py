"""
VEGETACAO - mapeamento de zonas de mata (NDVI) dentro de um quadrante.

Dado o MESMO quadrante do estradas.py (dois pontos diagonais num CSV), o script
identifica e "rastreia" as zonas de mata densa dentro dele a partir de imagem de
satelite:
  1) delimita o quadrante (bounding box: sul, oeste, norte, leste);
  2) busca no catalogo Sentinel-2 L2A (Earth Search / STAC, SEM chave) a cena
     recente com menos nuvem SOBRE o quadrante;
  3) le so a janela do quadrante das bandas do vermelho (B04) e do
     infravermelho-proximo (B08), 10 m, direto do COG (leitura por janela);
  4) calcula o NDVI = (NIR - VERMELHO) / (NIR + VERMELHO) e mascara nuvem/sombra
     pela camada SCL;
  5) marca como MATA os pixels com NDVI >= LIMIAR_NDVI (0.8 -> vegetacao densa /
     mata fechada) e os vetoriza em zonas (poligonos) com area e centroide;
  6) cruza com as ESTRADAS/PROTO-ESTRADAS do estradas.py: para cada trecho de via
     dentro do quadrante, mede quantos metros passam DENTRO de mata (util p/ o
     esforco de instalacao de postes).

NDVI: indice de -1 a 1. Vegetacao densa e saudavel fica ~0.7-0.9; pastagem/gramado
~0.3-0.6; solo exposto/agua/telhado abaixo disso. Por isso o limiar 0.8 isola a
"mata". O limiar e configuravel (LIMIAR_NDVI / parametro `limiar`).

Fonte: Sentinel-2 L2A via Earth Search (Element84/AWS), catalogo STAC publico e
COGs no bucket 'sentinel-cogs' - gratuito e SEM chave (mesma filosofia do resto
do projeto). Usa 'requests' para o STAC e 'rasterio' para ler a janela do COG.

Entrada : quadrante.csv (o mesmo do estradas.py; ver le_quadrante em estradas.py).
Saida   : XLSX com quatro planilhas + grafico PNG:
            - 'resumo'      : cena usada, data, nuvem, limiar, area de mata, % do
                              quadrante em mata, n de zonas;
            - 'zonas'       : uma linha por zona de mata (id, area_ha, n_pixels,
                              centro_lat/lon, ndvi_medio, ndvi_max);
            - 'vias_x_mata' : uma linha por trecho de via (comprimento, metros
                              dentro de mata, fracao);
            - 'zonas_geom'  : vertices (lat/lon) do contorno de cada zona, com o
                              NDVI (medio/max) e a area da zona em cada linha;
            - GeoJSON       : contorno (poligono) de cada zona de mata em lat/lon,
                              com o NDVI nas propriedades - abre no QGIS/Google Earth;
            - PNG           : mapa do quadrante em dois paineis - NDVI em faixas
                              discretas de densidade (mata != vegetacao media) e
                              a imagem de cor real (TCI) para conferencia - ambos
                              com as zonas de mata e as vias destacadas.

Instalacao (duas dependencias novas alem do estradas.py):
  pip install rasterio shapely
  (ja usa pandas, pyproj, openpyxl, requests, matplotlib do projeto)

Uso:
  python vegetacao.py           # usa quadrante.csv
  ou pela opcao [5] do menu.py
"""

import datetime
import json
import time

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
import requests
from matplotlib.colors import BoundaryNorm
from pyproj import Geod, Transformer
from rasterio.enums import Resampling
from rasterio.features import geometry_mask, shapes
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds
from shapely.geometry import LineString, mapping
from shapely.geometry import shape as shp_shape
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union

# reaproveita a leitura do quadrante e a deteccao de vias do estradas.py e o
# gravador de xlsx tolerante a arquivo aberto do urbano.py
from estradas import consulta_overpass, extrai_vias, le_quadrante, recorta_vias
from urbano import escreve_xlsx_seguro

# ---------- configuracao ----------
ENTRADA = "quadrante.csv"
SAIDA = "vegetacao.xlsx"
SAIDA_GRAFICO = "vegetacao.png"
# GeoJSON com o contorno (poligono) de cada zona de mata + o NDVI nas propriedades,
# pronto para abrir no QGIS / Google Earth. Coordenadas em lat/lon (WGS84).
SAIDA_GEOJSON = "vegetacao_zonas.geojson"
PLOTAR = True

# Limiar do NDVI para considerar "mata densa". Subimos de 0.8 -> 0.85 para exigir
# vegetacao mais fechada e descartar vegetacao apenas vigorosa (cana/pasto/capoeira),
# que costuma cair na faixa 0.8-0.85. Baixe para 0.8 se quiser incluir mata mais rala.
LIMIAR_NDVI = 0.85
# Area minima de uma zona: descarta manchas pequenas/fragmentadas (ruido). 1 pixel
# = 10x10 = 100 m2; 2000 m2 = 20 pixels = 0.2 ha. Suba para pegar so os macicos.
AREA_MIN_M2 = 2000.0

# Faixas (classes) de NDVI para o mapa. O fundo e desenhado em faixas DISCRETAS
# (nao um degrade continuo) para que vegetacao media (~0.5-0.7) NAO fique com a
# mesma cor da mata densa (>=0.8) - evita "achatar" tudo em verde-escuro. Cada
# faixa vira uma cor da paleta RdYlGn (vermelho=baixo -> verde=alto).
NDVI_BORDAS = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
# Desenha tambem o painel de cor real (Sentinel-2 TCI) ao lado, para conferencia.
MOSTRAR_COR_REAL = True

# Busca da cena (Sentinel-2 L2A via Earth Search / STAC, sem chave)
STAC_URL = "https://earth-search.aws.element84.com/v1/search"
COLECAO = "sentinel-2-l2a"
MESES_BUSCA = 18          # janela temporal para achar cena recente sem nuvem
CLOUD_MAX = 40            # % de nuvem maxima da cena inteira (filtro do STAC)
FRAC_LIMPA_MIN = 0.90     # fracao minima de pixels limpos DENTRO do quadrante
MAX_CANDIDATAS = 15       # cenas a inspecionar antes de aceitar a melhor
STAC_TIMEOUT = 60
STAC_TENTATIVAS = 3
HEADERS = {"User-Agent": "ProjetoRPA-vegetacao/1.0 (uso academico)"}

# Classes da SCL (Scene Classification) do Sentinel-2 tratadas como INVALIDAS
# (sem dado / nuvem / sombra / cirrus / neve). Ver wiki do Sentinel-2 L2A.
#   0 sem dado | 1 saturado/defeito | 3 sombra de nuvem | 8 nuvem media |
#   9 nuvem alta | 10 cirrus fino | 11 neve/gelo
SCL_INVALIDA = (0, 1, 3, 8, 9, 10, 11)

# Config do GDAL para leitura eficiente de COG por HTTP (bucket publico, sem
# assinatura). Evita listar o diretorio e restringe a extensao .tif.
GDAL_ENV = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    GDAL_HTTP_MULTIPLEX="YES",
    GDAL_HTTP_VERSION="2",
    VSI_CACHE="TRUE",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
    AWS_NO_SIGN_REQUEST="YES",
)
# ----------------------------------

geod = Geod(ellps="WGS84")


def busca_candidatas(bbox):
    """Consulta o STAC e devolve as cenas candidatas, da menos p/ a mais nublada.

    Filtra por nuvem da cena < CLOUD_MAX e pelos ultimos MESES_BUSCA meses.
    """
    hoje = datetime.date.today()
    inicio = (hoje - datetime.timedelta(days=30 * MESES_BUSCA)).isoformat()
    corpo = {
        "collections": [COLECAO],
        "bbox": [bbox["oeste"], bbox["sul"], bbox["leste"], bbox["norte"]],
        "datetime": f"{inicio}T00:00:00Z/{hoje.isoformat()}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": CLOUD_MAX}},
        "limit": 100,
        "sortby": [{"field": "properties.eo:cloud_cover", "direction": "asc"}],
    }
    ultimo_erro = None
    for tentativa in range(STAC_TENTATIVAS):
        try:
            r = requests.post(STAC_URL, json=corpo, headers=HEADERS,
                              timeout=STAC_TIMEOUT)
            r.raise_for_status()
            feats = r.json().get("features", [])
            feats.sort(key=lambda f: f["properties"].get("eo:cloud_cover", 100))
            return feats
        except Exception as e:                     # rede/timeout/JSON invalido
            ultimo_erro = e
            print(f"     ! STAC falhou (tentativa {tentativa + 1}/"
                  f"{STAC_TENTATIVAS}): {e}")
            time.sleep(2.0)
    raise RuntimeError(f"Earth Search (STAC) nao respondeu. Ultimo erro: {ultimo_erro}")


def _janela(ds, bbox):
    """Janela (em pixels) do dataset que cobre o quadrante, e seu transform.

    Converte o bbox (graus, EPSG:4326) para o CRS nativo do COG (UTM) e recorta
    a janela aos limites do raster.
    """
    l, b, r, t = transform_bounds("EPSG:4326", ds.crs,
                                  bbox["oeste"], bbox["sul"],
                                  bbox["leste"], bbox["norte"])
    win = from_bounds(l, b, r, t, ds.transform).round_offsets().round_lengths()
    win = win.intersection(Window(0, 0, ds.width, ds.height))
    return win, ds.window_transform(win)


def frac_limpa_scl(item, bbox):
    """Fracao de pixels limpos (nao nuvem/sombra/sem-dado) do quadrante na SCL.

    Le so a SCL (20 m) sobre a janela - barato - para triar as candidatas antes
    de baixar as bandas de 10 m.
    """
    href = item["assets"]["scl"]["href"]
    with rasterio.Env(**GDAL_ENV), rasterio.open(href) as ds:
        win, _ = _janela(ds, bbox)
        scl = ds.read(1, window=win)
    if scl.size == 0:
        return 0.0
    return float((~np.isin(scl, SCL_INVALIDA)).mean())


def le_cena(item, bbox, limiar):
    """Le vermelho/NIR/SCL do quadrante e calcula NDVI e a mascara de mata.

    Retorna um dict com o array NDVI, as mascaras (limpo/mata), o transform e o
    CRS (UTM) da janela, alem dos metadados da cena.
    """
    a = item["assets"]
    with rasterio.Env(**GDAL_ENV):
        with rasterio.open(a["red"]["href"]) as ds:       # B04 (vermelho), 10 m
            crs = ds.crs
            win, tr = _janela(ds, bbox)
            red = ds.read(1, window=win).astype("float64")
            shape = red.shape
        with rasterio.open(a["nir"]["href"]) as d2:        # B08 (NIR), 10 m
            win2, _ = _janela(d2, bbox)
            nir = d2.read(1, window=win2).astype("float64")[:shape[0], :shape[1]]
        with rasterio.open(a["scl"]["href"]) as d3:        # SCL, 20 m -> reamostra
            win3, _ = _janela(d3, bbox)
            scl = d3.read(1, window=win3, out_shape=shape,
                          resampling=Resampling.nearest)
        rgb = None                                          # cor real (TCI), p/ plot
        va = a.get("visual")
        if va:
            try:
                with rasterio.open(va["href"]) as d4:
                    win4, _ = _janela(d4, bbox)
                    rgb = np.transpose(
                        d4.read([1, 2, 3], window=win4, out_shape=(3, *shape),
                                resampling=Resampling.bilinear), (1, 2, 0))
            except Exception as e:
                print(f"     ! nao consegui ler a imagem de cor real: {e}")

    # DN -> reflectancia. A escala/offset vem da metadata (raster:bands). O bucket
    # 'sentinel-cogs' guarda os DN SEM o deslocamento +1000 do baseline >=04.00,
    # embora a metadata as vezes declare offset=-0.1; detecta pela magnitude do DN:
    # se os pixels escuros ja estao ~>=1000, o deslocamento esta aplicado.
    rb = (a["red"].get("raster:bands") or [{}])[0]
    escala = rb.get("scale") or 1e-4
    off_meta = rb.get("offset") or 0.0
    dn_positivos = red[red > 0]
    p1 = np.percentile(dn_positivos, 1) if dn_positivos.size else 0.0
    offset = off_meta if p1 >= 1000 else 0.0

    red_r = np.clip(red * escala + offset, 0, None)
    nir_r = np.clip(nir * escala + offset, 0, None)
    den = nir_r + red_r
    valido = (red > 0) & (nir > 0) & (den > 1e-6)      # DN==0 = sem dado
    nuvem = np.isin(scl, SCL_INVALIDA)
    limpo = valido & ~nuvem

    ndvi = np.full(shape, np.nan, dtype="float32")
    ndvi[valido] = ((nir_r - red_r) / np.where(den > 0, den, 1.0))[valido]
    mata = limpo & (ndvi >= limiar)

    return {
        "item": item, "crs": crs, "tr": tr, "shape": shape,
        "ndvi": ndvi, "limpo": limpo, "mata": mata, "rgb": rgb,
        "frac_limpa": float(limpo.mean()) if limpo.size else 0.0,
    }


def escolhe_cena(bbox, limiar):
    """Escolhe a melhor cena: a mais limpa SOBRE o quadrante entre as candidatas.

    Tria as candidatas pela SCL (barato) e para na primeira com fracao limpa
    >= FRAC_LIMPA_MIN. Se nenhuma atingir, usa a de maior fracao limpa.
    """
    cands = busca_candidatas(bbox)
    if not cands:
        raise RuntimeError(
            "Nenhuma cena Sentinel-2 encontrada para o quadrante. "
            "Aumente MESES_BUSCA ou CLOUD_MAX."
        )
    print(f"  {len(cands)} cena(s) candidata(s) (nuvem da cena < {CLOUD_MAX}%). "
          f"Triando pela nuvem sobre o quadrante...")
    melhor = None
    for item in cands[:MAX_CANDIDATAS]:
        data = item["properties"].get("datetime", "")[:10]
        nuv = item["properties"].get("eo:cloud_cover", 0.0)
        try:
            frac = frac_limpa_scl(item, bbox)
        except Exception as e:                         # cena nao cobre / erro
            print(f"    - {item['id']} {data}: falha ao ler SCL ({e}); pulando.")
            continue
        print(f"    - {item['id']} {data}  nuvem_cena={nuv:.0f}%  "
              f"limpo_no_quadrante={frac * 100:.0f}%")
        if frac >= FRAC_LIMPA_MIN:
            print("      -> escolhida.")
            return le_cena(item, bbox, limiar)
        if melhor is None or frac > melhor[0]:
            melhor = (frac, item)
    if melhor:
        print(f"  Nenhuma cena >= {FRAC_LIMPA_MIN * 100:.0f}% limpa; usando a "
              f"melhor disponivel ({melhor[0] * 100:.0f}% limpa sobre o quadrante).")
        return le_cena(melhor[1], bbox, limiar)
    raise RuntimeError("Nao foi possivel ler nenhuma cena candidata.")


def extrai_zonas(dados):
    """Vetoriza a mascara de mata em zonas (poligonos) com area e estatisticas.

    Cada zona = dict(id, poly_utm, poly_ll, area_ha, area_m2, n_pixels,
    centro_lat, centro_lon, ndvi_medio, ndvi_max). Poligonos em UTM (metros) para
    area/interseccao e em lat/lon para saida/plotagem.
    """
    tr, crs = dados["tr"], dados["crs"]
    ndvi, mata, shape = dados["ndvi"], dados["mata"], dados["shape"]
    para_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform

    zonas = []
    for geo, _ in shapes(mata.astype("uint8"), mask=mata, transform=tr):
        poly = shp_shape(geo)                     # poligono em UTM (metros)
        if poly.area < AREA_MIN_M2:
            continue
        dentro = geometry_mask([geo], out_shape=shape, transform=tr,
                               invert=True) & mata
        vals = ndvi[dentro]
        poly_ll = shp_transform(para_ll, poly)
        c = poly_ll.centroid
        zonas.append({
            "poly_utm": poly, "poly_ll": poly_ll,
            "area_m2": poly.area, "area_ha": poly.area / 1e4,
            "n_pixels": int(dentro.sum()),
            "centro_lat": c.y, "centro_lon": c.x,
            "ndvi_medio": float(vals.mean()) if vals.size else float("nan"),
            "ndvi_max": float(vals.max()) if vals.size else float("nan"),
        })
    zonas.sort(key=lambda z: z["area_m2"], reverse=True)
    for i, z in enumerate(zonas, 1):
        z["id"] = i
    return zonas


def vias_x_mata(bbox, zonas, crs):
    """Cruza as vias do quadrante com as zonas de mata.

    Para cada trecho de via (estradas.recorta_vias), mede quantos metros passam
    DENTRO da uniao das zonas de mata. Retorna (lista_de_trechos, forest_utm),
    onde forest_utm e a geometria (UTM) das zonas para reuso no plot. Se o
    Overpass falhar, retorna (None, forest_utm) e o cruzamento e omitido.
    """
    forest = unary_union([z["poly_utm"] for z in zonas]) if zonas else None
    if forest is None or forest.is_empty:
        return [], forest
    para_utm = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    try:
        ways = consulta_overpass(bbox)
    except Exception as e:
        print(f"  ! Overpass falhou ({e}); cruzamento vias x mata omitido.")
        return None, forest

    recortadas = recorta_vias(extrai_vias(ways), bbox)
    linhas = []
    for v in recortadas:
        pts = [para_utm(lon, lat) for lat, lon in v["geom"]]
        if len(pts) < 2:
            continue
        linha = LineString(pts)
        comp = linha.length
        inter = linha.intersection(forest)
        metros = 0.0 if inter.is_empty else inter.length
        linhas.append({
            "via_id": v["id"], "trecho": v["trecho"], "classe": v["classe"],
            "tipo_osm": v["tipo_osm"], "nome": v["nome"],
            "comprimento_m": round(comp, 1),
            "metros_em_mata": round(metros, 1),
            "frac_em_mata": round(metros / comp, 3) if comp > 0 else 0.0,
            "xy": pts, "inter": inter,
        })
    linhas.sort(key=lambda d: d["metros_em_mata"], reverse=True)
    return linhas, forest


def _iter_linhas(geom):
    """Itera as LineStrings de uma geometria (Line/MultiLine/Collection)."""
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "LineString":
        yield geom
    elif geom.geom_type in ("MultiLineString", "GeometryCollection"):
        for g in geom.geoms:
            yield from _iter_linhas(g)


def _desenha_overlays(ax, zonas, vias, bbox, pontos, para_utm, cor_zona,
                      legenda=True, limiar=LIMIAR_NDVI):
    """Desenha zonas de mata, vias, quadrante e pontos sobre um eixo (em UTM).

    Usado nos dois paineis (NDVI classificado e cor real). 'cor_zona' e a cor do
    contorno das zonas (escolhida para contrastar com o fundo de cada painel).
    """
    for z in zonas:                               # contorno das zonas de mata
        xs, ys = z["poly_utm"].exterior.xy
        ax.plot(xs, ys, color=cor_zona, lw=1.3, zorder=4)
    if zonas and legenda:
        ax.plot([], [], color=cor_zona, lw=1.3,
                label=f"zona de mata (NDVI>={limiar:g})")

    cores = {"ESTRADA": "#1f77b4", "PROTO-ESTRADA": "#d62728", "OUTRO": "#7f7f7f"}
    ja = set()
    for v in (vias or []):                        # vias por classe + realce na mata
        xs, ys = zip(*v["xy"])
        classe = v["classe"]
        rot = classe if (legenda and classe not in ja) else None
        ja.add(classe)
        ax.plot(xs, ys, color=cores.get(classe, "#7f7f7f"), lw=1.5,
                zorder=5, label=rot)
        for seg in _iter_linhas(v["inter"]):
            sx, sy = seg.xy
            ax.plot(sx, sy, color="#ff7f0e", lw=3.0, zorder=6)
    if legenda and any(v["metros_em_mata"] > 0 for v in (vias or [])):
        ax.plot([], [], color="#ff7f0e", lw=3.0, label="via dentro da mata")

    cantos = [(bbox["oeste"], bbox["sul"]), (bbox["leste"], bbox["sul"]),
              (bbox["leste"], bbox["norte"]), (bbox["oeste"], bbox["norte"]),
              (bbox["oeste"], bbox["sul"])]
    qx, qy = zip(*[para_utm(lo, la) for lo, la in cantos])
    ax.plot(qx, qy, "k--", lw=1.2, zorder=7,
            label="quadrante" if legenda else None)
    px, py = zip(*[para_utm(lo, la) for la, lo in pontos])
    ax.scatter(px, py, c="black", marker="x", s=55, zorder=8,
               label="pontos informados" if legenda else None)


def plota_mapa(dados, zonas, vias, bbox, pontos, salvar, mostrar=False,
               limiar=LIMIAR_NDVI):
    """Desenha o NDVI classificado (e a cor real ao lado) e salva em PNG.

    Painel 1: NDVI em FAIXAS discretas (vegetacao media != mata densa), com as
    zonas de mata contornadas e as vias destacadas. Painel 2 (se ha imagem de
    cor real): o TCI do Sentinel-2 com as mesmas zonas, para conferir a olho.
    """
    tr, crs, shape = dados["tr"], dados["crs"], dados["shape"]
    h, w = shape
    x0, y0 = tr.c, tr.f                          # canto superior-esquerdo (UTM)
    x1, y1 = x0 + w * tr.a, y0 + h * tr.e        # tr.e < 0 -> y1 (base) < y0
    extent = [x0, x1, y1, y0]                    # left, right, bottom, top
    epsg = crs.to_epsg()
    para_utm = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    rgb = dados.get("rgb")
    tem_rgb = MOSTRAR_COR_REAL and rgb is not None

    if tem_rgb:
        fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(17, 7.5),
                                       sharex=True, sharey=True)
    else:
        fig, ax0 = plt.subplots(figsize=(10, 8))
        ax1 = None

    # --- painel 1: NDVI classificado em faixas discretas ---
    disp = np.where(dados["limpo"], dados["ndvi"], np.nan)   # nuvem/sem-dado = nan
    cmap = plt.get_cmap("RdYlGn", len(NDVI_BORDAS) - 1).copy()
    cmap.set_bad("#cccccc")                     # nuvem/sem-dado -> cinza
    cmap.set_under("#7a3b12")                   # NDVI < primeira borda -> marrom
    norm = BoundaryNorm(NDVI_BORDAS, cmap.N)
    im = ax0.imshow(np.ma.masked_invalid(disp), extent=extent, origin="upper",
                    cmap=cmap, norm=norm, zorder=1)
    fig.colorbar(im, ax=ax0, shrink=0.82, ticks=NDVI_BORDAS, extend="min",
                 label="NDVI (faixas de densidade)")
    _desenha_overlays(ax0, zonas, vias, bbox, pontos, para_utm,
                      cor_zona="#08306b", legenda=True, limiar=limiar)
    ax0.set_title("NDVI classificado")
    ax0.set_xlabel(f"Easting (m) - UTM EPSG:{epsg}")
    ax0.set_ylabel("Northing (m)")
    ax0.set_aspect("equal")
    ax0.grid(True, alpha=0.3)
    ax0.legend(loc="best", fontsize=8)

    # --- painel 2: cor real (TCI) para conferencia ---
    if tem_rgb:
        ax1.imshow(rgb, extent=extent, origin="upper", zorder=1)
        _desenha_overlays(ax1, zonas, vias, bbox, pontos, para_utm,
                          cor_zona="#00e5ff", legenda=False, limiar=limiar)
        ax1.set_title("Cor real (Sentinel-2 TCI) - conferencia")
        ax1.set_xlabel(f"Easting (m) - UTM EPSG:{epsg}")
        ax1.set_aspect("equal")

    it = dados["item"]["properties"]
    fig.suptitle(f"Zonas de mata (NDVI >= {limiar:g}) no quadrante   |   "
                 f"{dados['item']['id']}  {it.get('datetime', '')[:10]}  "
                 f"nuvem {it.get('eo:cloud_cover', 0):.0f}%", fontsize=11)
    fig.tight_layout()
    fig.savefig(salvar, dpi=150)
    if mostrar:
        plt.show()
    plt.close(fig)


def monta_tabelas(dados, zonas, vias, bbox, limiar):
    """Constroi os DataFrames resumo/zonas/vias_x_mata/zonas_geom."""
    largura_m = geod.line_length([bbox["oeste"], bbox["leste"]],
                                 [bbox["sul"], bbox["sul"]])
    altura_m = geod.line_length([bbox["oeste"], bbox["oeste"]],
                                [bbox["sul"], bbox["norte"]])
    area_quad_ha = largura_m * altura_m / 1e4
    area_mata_ha = sum(z["area_ha"] for z in zonas)
    ndvi_lim = dados["ndvi"][dados["limpo"]]
    it = dados["item"]["properties"]

    df_resumo = pd.DataFrame([{
        "cena_id": dados["item"]["id"],
        "data": it.get("datetime", "")[:10],
        "nuvem_cena_%": round(it.get("eo:cloud_cover", 0.0), 1),
        "limpo_no_quadrante_%": round(dados["frac_limpa"] * 100, 1),
        "limiar_ndvi": limiar,
        "ndvi_medio_quadrante": round(float(np.nanmean(ndvi_lim)), 3)
        if ndvi_lim.size else float("nan"),
        "area_quadrante_ha": round(area_quad_ha, 2),
        "area_mata_ha": round(area_mata_ha, 2),
        "perc_quadrante_em_mata": round(100 * area_mata_ha / area_quad_ha, 1)
        if area_quad_ha else 0.0,
        "n_zonas": len(zonas),
        "utm_epsg": dados["crs"].to_epsg(),
    }])

    df_zonas = pd.DataFrame([{
        "zona_id": z["id"], "area_ha": round(z["area_ha"], 3),
        "area_m2": round(z["area_m2"], 1), "n_pixels": z["n_pixels"],
        "centro_lat": round(z["centro_lat"], 6),
        "centro_lon": round(z["centro_lon"], 6),
        "ndvi_medio": round(z["ndvi_medio"], 3),
        "ndvi_max": round(z["ndvi_max"], 3),
    } for z in zonas])

    if vias:
        df_vias = pd.DataFrame([{k: d[k] for k in
                                 ("via_id", "trecho", "classe", "tipo_osm", "nome",
                                  "comprimento_m", "metros_em_mata", "frac_em_mata")}
                                for d in vias])
    else:
        motivo = ("Overpass indisponivel" if vias is None
                  else "sem vias no quadrante")
        df_vias = pd.DataFrame([{"resultado": f"cruzamento nao gerado ({motivo})"}])

    # contorno de cada zona (vertices lat/lon), carregando o NDVI da zona em cada
    # linha para que o contorno seja auto-descritivo (a zona "consta o NDVI").
    linhas_geom = []
    for z in zonas:
        for ordem, (lon, lat) in enumerate(z["poly_ll"].exterior.coords, 1):
            linhas_geom.append({
                "zona_id": z["id"], "ordem": ordem,
                "latitude": round(lat, 6), "longitude": round(lon, 6),
                "ndvi_medio": round(z["ndvi_medio"], 3),
                "ndvi_max": round(z["ndvi_max"], 3),
                "area_ha": round(z["area_ha"], 3),
            })
    df_geom = pd.DataFrame(linhas_geom)
    return df_resumo, df_zonas, df_vias, df_geom


def escreve_geojson(caminho, zonas, limiar):
    """Grava o contorno (poligono) de cada zona de mata em GeoJSON (WGS84).

    Cada zona vira uma Feature com o poligono em lat/lon e o NDVI (medio/max),
    area e centroide nas propriedades. Se o arquivo estiver bloqueado, grava
    numa copia com sufixo de horario (mesma tolerancia do escreve_xlsx_seguro).
    """
    fc = {
        "type": "FeatureCollection",
        # CRS84 = WGS84 em ordem lon,lat (padrao do GeoJSON / QGIS / Google Earth)
        "crs": {"type": "name",
                "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": [{
            "type": "Feature",
            "properties": {
                "zona_id": z["id"], "limiar_ndvi": limiar,
                "ndvi_medio": round(z["ndvi_medio"], 3),
                "ndvi_max": round(z["ndvi_max"], 3),
                "area_ha": round(z["area_ha"], 3),
                "area_m2": round(z["area_m2"], 1), "n_pixels": z["n_pixels"],
                "centro_lat": round(z["centro_lat"], 6),
                "centro_lon": round(z["centro_lon"], 6),
            },
            "geometry": mapping(z["poly_ll"]),   # poligono em lon,lat
        } for z in zonas],
    }
    import os
    destinos = [caminho,
                f"{os.path.splitext(caminho)[0]}_{time.strftime('%H%M%S')}.geojson"]
    ultimo_erro = None
    for destino in destinos:
        try:
            with open(destino, "w", encoding="utf-8") as f:
                json.dump(fc, f, ensure_ascii=False)
            if destino != caminho:
                print(f"     ! '{caminho}' estava bloqueado; salvei em '{destino}'.")
            return destino
        except PermissionError as e:
            ultimo_erro = e
    raise ultimo_erro


def main(entrada=ENTRADA, saida=SAIDA, saida_grafico=SAIDA_GRAFICO,
         saida_geojson=SAIDA_GEOJSON,
         plotar=PLOTAR, mostrar=True, limiar=LIMIAR_NDVI):
    """Le o quadrante, calcula o NDVI, rastreia a mata e gera XLSX + PNG.

    Retorna um dict com o resultado (compativel com a interface do projeto).
    """
    bbox, pontos = le_quadrante(entrada)
    print(f"  Quadrante: sul={bbox['sul']:.6f} oeste={bbox['oeste']:.6f} "
          f"norte={bbox['norte']:.6f} leste={bbox['leste']:.6f}")

    print("  -> buscando cena Sentinel-2 (Earth Search / STAC, sem chave)...")
    dados = escolhe_cena(bbox, limiar)
    zonas = extrai_zonas(dados)

    if not zonas:
        print(f"\n==> NAO ha mata (NDVI >= {limiar:g}) dentro do quadrante.")
        saida_real = escreve_xlsx_seguro(saida, {
            "resumo": pd.DataFrame([{
                "cena_id": dados["item"]["id"],
                "data": dados["item"]["properties"].get("datetime", "")[:10],
                "limiar_ndvi": limiar,
                "resultado": "nenhuma zona de mata encontrada",
            }]),
            "zonas": pd.DataFrame(), "vias_x_mata": pd.DataFrame(),
            "zonas_geom": pd.DataFrame(),
        })
        print(f"OK -> {saida_real}")
        return {"tem_mata": False, "zonas": 0, "xlsx": saida_real,
                "png": None, "bbox": bbox}

    area_mata_ha = sum(z["area_ha"] for z in zonas)
    print(f"\n==> SIM: {len(zonas)} zona(s) de mata (NDVI >= {limiar:g}); "
          f"{area_mata_ha:.2f} ha no total.")
    for z in zonas[:10]:
        print(f"    - zona {z['id']}: {z['area_ha']:.2f} ha, "
              f"NDVI medio {z['ndvi_medio']:.2f}, "
              f"centro=({z['centro_lat']:.5f}, {z['centro_lon']:.5f})")
    if len(zonas) > 10:
        print(f"    ... e mais {len(zonas) - 10} zona(s) (ver planilha).")

    print("  -> cruzando com estradas/proto-estradas (Overpass)...")
    vias, _ = vias_x_mata(bbox, zonas, dados["crs"])
    if vias:
        na_mata = [v for v in vias if v["metros_em_mata"] > 0]
        tot = sum(v["metros_em_mata"] for v in vias)
        print(f"     {len(na_mata)} de {len(vias)} trecho(s) de via cruzam mata "
              f"({tot:.0f} m de via dentro de mata).")
        for v in na_mata[:8]:
            nome = v["nome"] or "(sem nome)"
            print(f"       - #{v['via_id']} t{v['trecho']} [{v['classe']}] {nome}: "
                  f"{v['metros_em_mata']:.0f} m em mata "
                  f"({v['frac_em_mata'] * 100:.0f}% do trecho)")

    df_resumo, df_zonas, df_vias, df_geom = monta_tabelas(
        dados, zonas, vias, bbox, limiar)
    saida_real = escreve_xlsx_seguro(saida, {
        "resumo": df_resumo, "zonas": df_zonas,
        "vias_x_mata": df_vias, "zonas_geom": df_geom,
    })
    print(f"OK -> {saida_real} ({len(zonas)} zona(s), {area_mata_ha:.1f} ha)")

    # contorno de cada zona (poligono lat/lon + NDVI) em GeoJSON para GIS
    geojson_real = escreve_geojson(saida_geojson, zonas, limiar)
    print(f"OK -> {geojson_real} ({len(zonas)} poligono(s) de contorno)")

    grafico_real = None
    if plotar:
        plota_mapa(dados, zonas, vias, bbox, pontos,
                   salvar=saida_grafico, mostrar=mostrar, limiar=limiar)
        grafico_real = saida_grafico
        print(f"OK -> {grafico_real}")

    return {"tem_mata": True, "zonas": len(zonas), "area_mata_ha": area_mata_ha,
            "vias_na_mata": len([v for v in (vias or []) if v["metros_em_mata"] > 0]),
            "xlsx": saida_real, "geojson": geojson_real, "png": grafico_real,
            "bbox": bbox, "cena": dados["item"]["id"]}


if __name__ == "__main__":
    main()
