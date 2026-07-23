"""
NDVI URBANO - indice de vegetacao (NDVI) de um quadrante em zona urbana.

Aplica o MESMO principio do estradas.py (le um quadrante a partir dos pontos
de canto num CSV) mas, em vez de procurar vias, calcula o NDVI a partir de
imagem de satelite Sentinel-2 (a "versao nova que calcula o NDVI", como o
vegetacao.py) e SEMPRE plota o mapa/grafico do indice NDVI do local.

Diferenca para o vegetacao.py: aquele foi feito para MATA densa (NDVI >= 0.85) e,
se nao acha mata, encerra sem desenhar. Numa zona URBANA quase nunca ha mata
densa, entao aqui o objetivo e outro: mostrar o NDVI do local (agua, area
construida, solo, gramado, arvore...) num mapa com barra de indice, mais uma
classificacao da cobertura por faixa de NDVI. O mapa e sempre gerado.

Como o quadrante urbano costuma ser MINUSCULO (poucos pixels de 10 m), o mapa
seria pobre se recortado nele. Por isso, tal como o estradas.py desenha as vias
PARA ALEM do quadrante (contexto), aqui lemos e desenhamos uma faixa de contexto
de BUFFER_M metros ao redor - a vizinhanca, com pixels reais do satelite - e
marcamos o quadrante por dentro. As estatisticas/tabelas continuam sendo APENAS
dos pixels dentro do quadrante.

  1) delimita o quadrante (bbox: sul, oeste, norte, leste) a partir dos cantos
     informados (reaproveita estradas.le_quadrante) e o expande por BUFFER_M m;
  2) busca no catalogo Sentinel-2 L2A (Earth Search / STAC, SEM chave) a cena
     recente com menos nuvem sobre a area (reaproveita vegetacao.escolhe_cena);
  3) le a janela do vermelho (B04) e do NIR (B08), 10 m, e calcula
     NDVI = (NIR - VERMELHO) / (NIR + VERMELHO), mascarando nuvem/sombra pela SCL;
  4) classifica cada pixel do QUADRANTE por faixa de NDVI (agua/construido/solo,
     vegetacao rala, moderada, densa, mata) e resume a cobertura;
  5) plota o mapa de NDVI da vizinhanca (faixas RdYlGn + barra de indice) ao lado
     da cor real, com o quadrante e seus cantos marcados, eixos em lat/lon.

NDVI: indice de -1 a 1. Agua/sombra < 0; area construida/solo exposto ~0-0.15;
vegetacao rala ~0.15-0.3; gramado/pasto ~0.3-0.5; vegetacao densa ~0.5-0.7;
mata fechada ~0.7-0.9.

Fonte: Sentinel-2 L2A via Earth Search (Element84/AWS), STAC publico e COGs no
bucket 'sentinel-cogs' - gratuito e SEM chave (mesma filosofia do projeto).

Entrada : CSV com os cantos do quadrante (mesmo formato do estradas.py).
Saida   : XLSX (resumo / classes / pixels) + grafico PNG (NDVI + cor real).

Uso:
  python ndvi_urbano.py                    # usa quadrante_urbano.csv
  python ndvi_urbano.py meu_quadrante.csv  # outro arquivo
"""

import math
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import BoundaryNorm
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter, MaxNLocator
from pyproj import Geod, Transformer
from rasterio.warp import transform_bounds

# reaproveita a leitura do quadrante (estradas.py), a busca/leitura da cena
# Sentinel-2 e o calculo do NDVI (vegetacao.py) e o gravador de xlsx (urbano.py)
from estradas import le_quadrante
from vegetacao import escolhe_cena
from urbano import escreve_xlsx_seguro

# ---------- configuracao ----------
ENTRADA = "quadrante_urbano.csv"
SAIDA = "ndvi_urbano.xlsx"
SAIDA_GRAFICO = "ndvi_urbano.png"
PLOTAR = True

# Margem de contexto (metros): o quadrante urbano costuma ser minusculo (poucos
# pixels de 10 m). Para o mapa ter a mesma riqueza do estradas.py/vegetacao.py,
# lemos e desenhamos esta faixa ao redor do quadrante (a vizinhanca, com pixels
# reais do satelite) e marcamos o quadrante por dentro. As estatisticas/tabelas
# continuam sendo so dos pixels DENTRO do quadrante.
BUFFER_M = 150.0

# Faixas (classes) de cobertura por NDVI. Cada faixa = (limite_inferior, nome, cor).
# Usadas na tabela de classes e na cobertura dominante do quadrante. Vao de agua
# (<0) a mata fechada (>=0.7).
CLASSES_NDVI = [
    (-1.0, "agua / sombra",              "#2c7fb8"),
    (0.00, "area construida / solo",     "#a6611a"),
    (0.15, "vegetacao rala",             "#dfc27d"),
    (0.30, "vegetacao moderada (grama)", "#c7e9b4"),
    (0.50, "vegetacao densa",            "#41ab5d"),
    (0.70, "mata / muito densa",         "#005a32"),
]
# Bordas do mapa de NDVI (barra de indice discreta, paleta RdYlGn vermelho->verde,
# mesmo estilo do vegetacao.py). Passo de 0.1 de -0.1 a 1.0: cada faixa vira uma cor.
NDVI_BORDAS = [-0.1, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]

# Desenha o painel de cor real (Sentinel-2 TCI) ao lado, para conferencia.
MOSTRAR_COR_REAL = True
# ----------------------------------

geod = Geod(ellps="WGS84")


def expande_bbox(bbox, metros):
    """Expande o bbox (graus) por 'metros' em cada lado, virando a area de contexto."""
    lat_media = (bbox["sul"] + bbox["norte"]) / 2
    dlat = metros / 111320.0
    dlon = metros / (111320.0 * max(math.cos(math.radians(lat_media)), 1e-6))
    return {"sul": bbox["sul"] - dlat, "norte": bbox["norte"] + dlat,
            "oeste": bbox["oeste"] - dlon, "leste": bbox["leste"] + dlon}


def classe_ndvi(v):
    """Nome da classe de cobertura para um valor de NDVI (None se invalido)."""
    if v is None or np.isnan(v):
        return None
    nome = CLASSES_NDVI[0][1]
    for limite, rot, _ in CLASSES_NDVI:
        if v >= limite:
            nome = rot
    return nome


def indice_classe(v):
    """Indice da faixa de CLASSES_NDVI para um valor (-1 se invalido)."""
    if v is None or np.isnan(v):
        return -1
    idx = 0
    for i, (limite, _, _) in enumerate(CLASSES_NDVI):
        if v >= limite:
            idx = i
    return idx


def classe_dominante(vals):
    """Classe de cobertura com mais pixels (str), ou '-' se nao ha pixel valido."""
    if vals.size == 0:
        return "-"
    idxs = [indice_classe(v) for v in vals]
    dom = max(set(idxs), key=idxs.count)
    return CLASSES_NDVI[dom][1]


def centros_pixels(tr, shape):
    """Coordenadas (x, y) UTM do centro de cada pixel da janela.

    Retorna dois arrays 2D (xs, ys) com o mesmo shape do raster. Usa o affine
    transform da janela (tr): x = c + (col+0.5)*a + (lin+0.5)*b, idem para y.
    """
    h, w = shape
    linhas, colunas = np.mgrid[0:h, 0:w]
    xs = tr.c + (colunas + 0.5) * tr.a + (linhas + 0.5) * tr.b
    ys = tr.f + (colunas + 0.5) * tr.d + (linhas + 0.5) * tr.e
    return xs, ys


def mascara_quadrante(dados, bbox):
    """Booleano (shape do raster): pixels cujo centro cai DENTRO do quadrante.

    Converte os cantos do quadrante para o UTM da cena e testa cada centro de
    pixel. Como a cena e o quadrante estao no mesmo CRS/grade de 10 m, isso
    seleciona exatamente os pixels do quadrante dentro da area de contexto.
    """
    tr, crs, shape = dados["tr"], dados["crs"], dados["shape"]
    para_utm = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    cantos = [(bbox["oeste"], bbox["sul"]), (bbox["leste"], bbox["sul"]),
              (bbox["leste"], bbox["norte"]), (bbox["oeste"], bbox["norte"])]
    xs_q, ys_q = zip(*[para_utm(lo, la) for lo, la in cantos])
    xmin, xmax, ymin, ymax = min(xs_q), max(xs_q), min(ys_q), max(ys_q)
    xs, ys = centros_pixels(tr, shape)
    mask = (xs >= xmin) & (xs <= xmax) & (ys >= ymin) & (ys <= ymax)
    if not mask.any():
        # quadrante sub-pixel (nenhum centro cai dentro): usa o pixel mais
        # proximo do centro do quadrante, para nunca ficar sem amostra.
        cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
        idx = np.unravel_index(np.argmin((xs - cx) ** 2 + (ys - cy) ** 2), shape)
        mask = np.zeros(shape, dtype=bool)
        mask[idx] = True
    return mask


def monta_tabelas(dados, bbox, dentro):
    """DataFrames resumo / classes / pixels, restritos ao interior do quadrante."""
    ndvi, limpo = dados["ndvi"], dados["limpo"]
    tr, crs = dados["tr"], dados["crs"]
    it = dados["item"]["properties"]

    largura_m = geod.line_length([bbox["oeste"], bbox["leste"]],
                                 [bbox["sul"], bbox["sul"]])
    altura_m = geod.line_length([bbox["oeste"], bbox["oeste"]],
                                [bbox["sul"], bbox["norte"]])
    area_pixel = abs(tr.a * tr.e)               # m2 por pixel (10x10 = 100)
    validos = limpo & dentro
    vals = ndvi[validos]
    n_quad = int(dentro.sum())

    df_resumo = pd.DataFrame([{
        "cena_id": dados["item"]["id"],
        "data": it.get("datetime", "")[:10],
        "nuvem_cena_%": round(it.get("eo:cloud_cover", 0.0), 1),
        "limpo_no_quadrante_%": round(100 * vals.size / n_quad, 1) if n_quad else 0.0,
        "n_pixels_quadrante": n_quad,
        "n_pixels_validos": int(vals.size),
        "resolucao_m": round((abs(tr.a) + abs(tr.e)) / 2, 1),
        "quadrante_m": f"{largura_m:.0f} x {altura_m:.0f}",
        "contexto_m": f"{largura_m + 2 * BUFFER_M:.0f} x {altura_m + 2 * BUFFER_M:.0f}",
        "ndvi_medio": round(float(vals.mean()), 3) if vals.size else float("nan"),
        "ndvi_mediano": round(float(np.median(vals)), 3) if vals.size else float("nan"),
        "ndvi_min": round(float(vals.min()), 3) if vals.size else float("nan"),
        "ndvi_max": round(float(vals.max()), 3) if vals.size else float("nan"),
        "classe_dominante": classe_dominante(vals),
        "utm_epsg": crs.to_epsg(),
    }])

    # distribuicao por classe de cobertura. O teto de cada classe e o piso da
    # proxima (1.0 na ultima) - independente das bordas do MAPA (NDVI_BORDAS).
    linhas_cls = []
    for i, (limite, nome, _) in enumerate(CLASSES_NDVI):
        teto = CLASSES_NDVI[i + 1][0] if i + 1 < len(CLASSES_NDVI) else 1.0
        n = int(sum(indice_classe(v) == i for v in vals)) if vals.size else 0
        linhas_cls.append({
            "classe": nome,
            "faixa_ndvi": f"[{limite:.2f}, {teto:.2f})",
            "n_pixels": n,
            "area_m2": round(n * area_pixel, 1),
            "perc_validos": round(100 * n / vals.size, 1) if vals.size else 0.0,
        })
    df_classes = pd.DataFrame(linhas_cls)

    # um pixel do quadrante por linha (lat/lon do centro, NDVI e classe)
    para_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    xs, ys = centros_pixels(tr, dados["shape"])
    linhas_px = []
    for lin, col in zip(*np.where(dentro)):
        v = float(ndvi[lin, col]) if limpo[lin, col] else float("nan")
        lon, lat = para_ll(xs[lin, col], ys[lin, col])
        linhas_px.append({
            "linha": int(lin), "coluna": int(col),
            "centro_lat": round(lat, 6), "centro_lon": round(lon, 6),
            "ndvi": round(v, 3) if not np.isnan(v) else None,
            "classe": classe_ndvi(v) or "nuvem/sem-dado",
            "valido": bool(limpo[lin, col]),
        })
    df_pixels = pd.DataFrame(linhas_px)
    return df_resumo, df_classes, df_pixels


def plota_mapa(dados, bbox, pontos, dentro, salvar, mostrar=False):
    """Plota o mapa de NDVI da vizinhanca e a cor real, e salva em PNG.

    Painel 1: NDVI (faixas RdYlGn + barra de indice) da area de contexto, com o
    quadrante e os cantos marcados e uma caixa com o NDVI do quadrante.
    Painel 2 (se disponivel): cor real (Sentinel-2 TCI). Eixos em lat/lon (graus).
    """
    tr, crs, shape = dados["tr"], dados["crs"], dados["shape"]
    ndvi, limpo = dados["ndvi"], dados["limpo"]
    h, w = shape

    # limites geograficos (lat/lon) da area lida -> eixos em graus
    oeste_utm, norte_utm = tr.c, tr.f
    leste_utm, sul_utm = tr.c + w * tr.a, tr.f + h * tr.e   # tr.e < 0
    lon_min, lat_min, lon_max, lat_max = transform_bounds(
        crs, "EPSG:4326", oeste_utm, sul_utm, leste_utm, norte_utm)
    extent = [lon_min, lon_max, lat_min, lat_max]           # left, right, bottom, top
    lat_media = (lat_min + lat_max) / 2
    aspecto = 1.0 / max(math.cos(math.radians(lat_media)), 1e-6)
    rgb = dados.get("rgb")
    tem_rgb = MOSTRAR_COR_REAL and rgb is not None

    if tem_rgb:
        fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(16, 7.5),
                                       sharex=True, sharey=True)
    else:
        fig, ax0 = plt.subplots(figsize=(9, 8))
        ax1 = None

    # --- painel 1: NDVI da vizinhanca em faixas RdYlGn ---
    disp = np.where(limpo, ndvi, np.nan)          # nuvem/sem-dado = nan
    cmap = plt.get_cmap("RdYlGn", len(NDVI_BORDAS) - 1).copy()
    cmap.set_bad("#cccccc")                       # nuvem/sem-dado -> cinza
    cmap.set_under("#7a3b12")                     # NDVI < primeira borda -> marrom
    norm = BoundaryNorm(NDVI_BORDAS, cmap.N)
    im = ax0.imshow(np.ma.masked_invalid(disp), extent=extent, origin="upper",
                    cmap=cmap, norm=norm, zorder=1, interpolation="nearest")
    cbar = fig.colorbar(im, ax=ax0, shrink=0.82, ticks=NDVI_BORDAS, extend="min",
                        label="NDVI (indice de vegetacao)")
    cbar.ax.tick_params(labelsize=8)

    _desenha_moldura(ax0, bbox, pontos)
    _caixa_stats(ax0, ndvi[limpo & dentro])

    ax0.set_title("NDVI da vizinhanca (quadrante marcado)")
    _eixos_latlon(ax0, aspecto)
    ax0.grid(True, alpha=0.3)
    ax0.legend(loc="upper right", fontsize=8)

    # --- painel 2: cor real (TCI) para conferencia ---
    if tem_rgb:
        ax1.imshow(rgb, extent=extent, origin="upper", zorder=1,
                   interpolation="nearest")
        _desenha_moldura(ax1, bbox, pontos, cor="#00e5ff")
        ax1.set_title("Cor real (Sentinel-2 TCI) - conferencia")
        _eixos_latlon(ax1, aspecto, so_x=True)
        ax1.legend(loc="upper right", fontsize=8)

    it = dados["item"]["properties"]
    vals = ndvi[limpo & dentro]
    ndvi_med = float(vals.mean()) if vals.size else float("nan")
    fig.suptitle(
        f"NDVI do local (quadrante urbano)  |  medio={ndvi_med:.2f} "
        f"({classe_dominante(vals)})  |  {dados['item']['id']}  "
        f"{it.get('datetime', '')[:10]}  nuvem {it.get('eo:cloud_cover', 0):.0f}%",
        fontsize=11)
    fig.tight_layout()
    fig.savefig(salvar, dpi=150)
    if mostrar:
        plt.show()
    plt.close(fig)


def _desenha_moldura(ax, bbox, pontos, cor="black"):
    """Destaca o retangulo do quadrante e marca os cantos informados, em lat/lon."""
    ax.add_patch(Rectangle(
        (bbox["oeste"], bbox["sul"]),
        bbox["leste"] - bbox["oeste"], bbox["norte"] - bbox["sul"],
        fill=False, edgecolor=cor, linestyle="--", linewidth=1.8,
        zorder=7, label="quadrante"))
    ax.scatter([lo for _, lo in pontos], [la for la, _ in pontos],
               c=cor, marker="x", s=60, linewidths=1.6, zorder=8,
               label="cantos informados")


def _caixa_stats(ax, vals):
    """Caixa de texto com o NDVI do quadrante (medio/faixa/classe/n pixels)."""
    if vals.size == 0:
        texto = "Quadrante: sem pixel valido\n(nuvem/sombra)"
    else:
        texto = (f"Quadrante (interior):\n"
                 f"NDVI medio {vals.mean():.2f}  ({vals.min():.2f}-{vals.max():.2f})\n"
                 f"classe: {classe_dominante(vals)}\n"
                 f"{vals.size} pixel(s) de 10 m")
    ax.text(0.02, 0.02, texto, transform=ax.transAxes, fontsize=8,
            va="bottom", ha="left", zorder=10,
            bbox=dict(boxstyle="round,pad=0.4", fc="white", ec="#555555", alpha=0.85))


def _eixos_latlon(ax, aspecto, so_x=False):
    """Rotula os eixos em graus de latitude/longitude e ajusta a proporcao."""
    ax.set_xlabel("longitude (graus)")
    if not so_x:
        ax.set_ylabel("latitude (graus)")
    ax.xaxis.set_major_locator(MaxNLocator(6))
    ax.yaxis.set_major_locator(MaxNLocator(6))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.5f}"))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.5f}"))
    ax.set_aspect(aspecto)
    for rot in ax.get_xticklabels():
        rot.set_rotation(30)
        rot.set_ha("right")


def main(entrada=ENTRADA, saida=SAIDA, saida_grafico=SAIDA_GRAFICO,
         plotar=PLOTAR, mostrar=True):
    """Le o quadrante, calcula o NDVI da cena Sentinel-2 e gera XLSX + PNG.

    Retorna um dict com o resultado (compativel com a interface do projeto).
    """
    bbox, pontos = le_quadrante(entrada)
    print(f"  Quadrante: sul={bbox['sul']:.6f} oeste={bbox['oeste']:.6f} "
          f"norte={bbox['norte']:.6f} leste={bbox['leste']:.6f}")
    largura_m = geod.line_length([bbox["oeste"], bbox["leste"]],
                                 [bbox["sul"], bbox["sul"]])
    altura_m = geod.line_length([bbox["oeste"], bbox["oeste"]],
                                [bbox["sul"], bbox["norte"]])
    print(f"  Dimensao aproximada: {largura_m:.0f} m (L-O) x {altura_m:.0f} m (N-S)")

    bbox_ctx = expande_bbox(bbox, BUFFER_M)
    print(f"  Area de contexto (+{BUFFER_M:.0f} m por lado): "
          f"{largura_m + 2 * BUFFER_M:.0f} m x {altura_m + 2 * BUFFER_M:.0f} m")

    print("  -> buscando cena Sentinel-2 (Earth Search / STAC, sem chave)...")
    # limiar so alimenta a mascara de mata do le_cena (nao usada aqui); NDVI vem
    # do array completo. Passamos 0.85 apenas para satisfazer a assinatura.
    dados = escolhe_cena(bbox_ctx, 0.85)
    dentro = mascara_quadrante(dados, bbox)
    vals = dados["ndvi"][dados["limpo"] & dentro]
    if vals.size == 0:
        print("\n==> Nenhum pixel valido no quadrante (nuvem/sombra). "
              "Tente rodar novamente (outra cena) ou um quadrante maior.")
        return {"ok": False, "bbox": bbox}

    print(f"\n==> NDVI do quadrante: medio={vals.mean():.3f}  "
          f"mediano={np.median(vals):.3f}  min={vals.min():.3f}  max={vals.max():.3f}"
          f"  ({vals.size} de {int(dentro.sum())} pixel(s) de 10 m validos).")
    print(f"    Cobertura dominante: {classe_dominante(vals)}.")

    df_resumo, df_classes, df_pixels = monta_tabelas(dados, bbox, dentro)
    print("    Distribuicao por classe de cobertura (pixels validos do quadrante):")
    for _, r in df_classes.iterrows():
        if r["n_pixels"] > 0:
            print(f"       - {r['classe']:<28} {r['faixa_ndvi']:<14} "
                  f"{r['n_pixels']:>3} px  ({r['perc_validos']:.0f}%)")

    saida_real = escreve_xlsx_seguro(saida, {
        "resumo": df_resumo, "classes": df_classes, "pixels": df_pixels,
    })
    print(f"OK -> {saida_real}")

    grafico_real = None
    if plotar:
        plota_mapa(dados, bbox, pontos, dentro, salvar=saida_grafico, mostrar=mostrar)
        grafico_real = saida_grafico
        print(f"OK -> {grafico_real}")

    return {"ok": True, "ndvi_medio": float(vals.mean()),
            "ndvi_mediano": float(np.median(vals)),
            "classe_dominante": classe_dominante(vals),
            "n_pixels": int(vals.size), "xlsx": saida_real, "png": grafico_real,
            "cena": dados["item"]["id"], "bbox": bbox}


if __name__ == "__main__":
    arq = sys.argv[1] if len(sys.argv) > 1 else ENTRADA
    main(entrada=arq)
