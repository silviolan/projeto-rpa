"""
VALIDACAO DA VEGETACAO - afere o modelo de mata (NDVI) contra uma referencia
independente e gratuita (ESA WorldCover 10 m).

O vegetacao.py marca "mata" onde NDVI >= LIMIAR. Este script mede o quanto essa
marcacao concorda com um mapa de cobertura do solo INDEPENDENTE e validado - o
ESA WorldCover (10 m, classe 10 = cobertura arborea) - na mesma filosofia de
cross-validacao entre fontes livres do validacao.py (que faz isso para altitude).

O que faz:
  1) roda o modelo de mata do vegetacao.py para o quadrante (mesma cena Sentinel-2);
  2) le o ESA WorldCover sobre o quadrante e reamostra para a grade do Sentinel-2;
  3) monta a matriz de confusao mata(modelo) x arborea(WorldCover) e calcula
     acuracia, precisao (user), recall (producer), F1, IoU e kappa de Cohen;
  4) VARRE varios limiares de NDVI e mostra qual maximiza a concordancia (F1/IoU)
     - serve para calibrar o LIMIAR_NDVI de forma objetiva;
  5) gera XLSX (resumo/metricas + matriz + tabela por limiar) e um PNG com o mapa
     de concordancia (acerto/falso-positivo/falso-negativo), o WorldCover e a
     curva de precisao/recall x limiar.

RESSALVAS (a validacao mede CONCORDANCIA, nao verdade absoluta):
  - o WorldCover e de um ANO fixo (v200=2021, v100=2020); se a cena Sentinel-2 for
    de outro ano, parte da discordancia e mudanca real de uso do solo, nao erro;
  - "arborea" no WorldCover exige dossel de arvores (~>=5 m); o NDVI nao distingue
    arvore de vegetacao densa baixa (cana/pasto viçoso), entao superestimar mata
    em area agricola/pastagem e esperado.

Fonte da referencia: ESA WorldCover (bucket publico AWS 'esa-worldcover', SEM
chave). Le so a janela do quadrante via rasterio (mesma tecnica do vegetacao.py).

Entrada : quadrante.csv (o mesmo do estradas.py / vegetacao.py).
Saida   : validacao_vegetacao.xlsx + validacao_vegetacao.png.

Uso:
  python validacao_vegetacao.py       # usa quadrante.csv
  ou pela opcao [6] do menu.py
"""

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from matplotlib.colors import BoundaryNorm, ListedColormap
from rasterio.warp import Resampling, reproject
from rasterio.windows import Window, from_bounds

# reaproveita todo o pipeline do modelo de mata e o gravador de xlsx
import vegetacao as vg
from estradas import le_quadrante
from urbano import escreve_xlsx_seguro

# ---------- configuracao ----------
ENTRADA = "quadrante.csv"
SAIDA = "validacao_vegetacao.xlsx"
SAIDA_GRAFICO = "validacao_vegetacao.png"
PLOTAR = True

# ESA WorldCover: v200 = 2021, v100 = 2020. Tiles de 3x3 graus nomeados pelo
# canto SW; bucket publico (sem chave), lido por janela como os COGs do Sentinel.
WC_VERSAO = "v200"
WC_ANO = "2021"
WC_BASE = "https://esa-worldcover.s3.eu-central-1.amazonaws.com"
WC_CLASSE_ARBOREA = 10           # 10 = Tree cover (cobertura arborea)

# Limiares de NDVI varridos para a curva de concordancia (calibracao do LIMIAR).
LIMIARES_SWEEP = [round(0.55 + 0.05 * i, 2) for i in range(9)]   # 0.55..0.95

# Nomes/cores das classes do WorldCover para o mapa de referencia.
WC_CLASSES = {
    10: ("cobertura arborea", "#009900"), 20: ("arbustos", "#ffbb22"),
    30: ("pastagem/herbacea", "#ffff4c"), 40: ("agricultura", "#f096ff"),
    50: ("construido", "#fa0000"), 60: ("solo exposto", "#b4b4b4"),
    70: ("neve/gelo", "#f0f0f0"), 80: ("agua", "#0064c8"),
    90: ("zona umida", "#0096a0"), 95: ("mangue", "#00cf75"),
    100: ("musgo/liquen", "#fae6a0"),
}
# ----------------------------------


def tile_worldcover(lat, lon):
    """Nome do tile WorldCover (3x3 graus) que contem (lat, lon). Ex.: S09W036."""
    la = int(np.floor(lat / 3.0) * 3)
    lo = int(np.floor(lon / 3.0) * 3)
    ns = f"N{la:02d}" if la >= 0 else f"S{abs(la):02d}"
    ew = f"E{lo:03d}" if lo >= 0 else f"W{abs(lo):03d}"
    return f"{ns}{ew}"


def _href_worldcover(tile):
    return (f"{WC_BASE}/{WC_VERSAO}/{WC_ANO}/map/"
            f"ESA_WorldCover_10m_{WC_ANO}_{WC_VERSAO}_{tile}_Map.tif")


def le_worldcover(bbox, tr, crs, shape):
    """Le o ESA WorldCover sobre o quadrante e reamostra para a grade do Sentinel.

    Reprojeta (EPSG:4326 -> UTM da cena) so a janela do quadrante para a grade
    (tr/crs/shape) do NDVI, permitindo a comparacao pixel a pixel. Cobre o caso
    raro de o quadrante cruzar dois tiles de 3 graus.
    """
    dst = np.zeros(shape, dtype="uint8")
    cantos = [(bbox["sul"], bbox["oeste"]), (bbox["sul"], bbox["leste"]),
              (bbox["norte"], bbox["oeste"]), (bbox["norte"], bbox["leste"])]
    tiles = {tile_worldcover(la, lo) for la, lo in cantos}
    leu = False
    for tile in tiles:
        href = _href_worldcover(tile)
        try:
            with rasterio.Env(**vg.GDAL_ENV), rasterio.open(href) as ds:
                win = from_bounds(bbox["oeste"], bbox["sul"],
                                  bbox["leste"], bbox["norte"], ds.transform)
                win = (win.round_offsets().round_lengths()
                       .intersection(Window(0, 0, ds.width, ds.height)))
                src = ds.read(1, window=win)
                if src.size == 0:
                    continue
                tmp = np.zeros(shape, dtype="uint8")
                reproject(src, tmp, src_transform=ds.window_transform(win),
                          src_crs=ds.crs, dst_transform=tr, dst_crs=crs,
                          resampling=Resampling.nearest)
                preenche = (dst == 0) & (tmp != 0)
                dst[preenche] = tmp[preenche]
                leu = True
        except Exception as e:
            print(f"  ! WorldCover tile {tile} falhou: {e}")
    if not leu:
        raise RuntimeError("Nao consegui ler o ESA WorldCover para o quadrante.")
    return dst


def matriz(pred, ref, aval):
    """Matriz de confusao e metricas de pred(bool) vs ref(bool) onde aval=True.

    pred = pixels marcados como mata pelo modelo; ref = cobertura arborea da
    referencia; aval = pixels validos para avaliacao (limpo e com referencia).
    """
    p = pred & aval
    r = ref & aval
    tp = int((p & r).sum())
    fp = int((p & ~r).sum())
    fn = int((~p & r).sum())
    tn = int((aval & ~p & ~r).sum())
    n = tp + fp + fn + tn
    acc = (tp + tn) / n if n else 0.0
    prec = tp / (tp + fp) if (tp + fp) else 0.0        # user's accuracy
    rec = tp / (tp + fn) if (tp + fn) else 0.0         # producer's accuracy
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    iou = tp / (tp + fp + fn) if (tp + fp + fn) else 0.0
    po = acc
    pe = (((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / n ** 2) if n else 0.0
    kappa = (po - pe) / (1 - pe) if (1 - pe) else 0.0
    return {"TP": tp, "FP": fp, "FN": fn, "TN": tn, "N": n,
            "acuracia": acc, "precisao": prec, "recall": rec, "f1": f1,
            "iou": iou, "kappa": kappa}


def varre_limiares(ndvi, aval, tree, limiares):
    """Metricas por limiar de NDVI. Retorna (DataFrame, melhor_limiar_por_f1)."""
    linhas = []
    for t in limiares:
        m = matriz(ndvi >= t, tree, aval)
        area_ha = m["TP"] + m["FP"]                    # pixels de mata
        linhas.append({"limiar": t, **{k: m[k] for k in
                       ("TP", "FP", "FN", "TN")},
                       "precisao": round(m["precisao"], 3),
                       "recall": round(m["recall"], 3),
                       "f1": round(m["f1"], 3), "iou": round(m["iou"], 3),
                       "kappa": round(m["kappa"], 3),
                       "area_mata_ha": round(area_ha * 0.01, 2)})
    df = pd.DataFrame(linhas)
    melhor = float(df.loc[df["f1"].idxmax(), "limiar"]) if not df.empty else None
    return df, melhor


def plota(dados, wc, aval, tree, mata, df_sweep, limiar, melhor, salvar,
          bbox, mostrar=False):
    """Mapa de concordancia + WorldCover + curva de metricas x limiar."""
    tr, crs, shape = dados["tr"], dados["crs"], dados["shape"]
    h, w = shape
    x0, y0 = tr.c, tr.f
    x1, y1 = x0 + w * tr.a, y0 + h * tr.e
    extent = [x0, x1, y1, y0]

    fig, axs = plt.subplots(1, 3, figsize=(19, 6.6))

    # painel 1: concordancia (TN / TP / FP / FN)
    code = np.full(shape, -1, dtype="int8")
    code[aval & ~mata & ~tree] = 0        # concordam: nao-mata
    code[aval & mata & tree] = 1          # acerto (TP)
    code[aval & mata & ~tree] = 2         # falso positivo (modelo diz mata)
    code[aval & ~mata & tree] = 3         # falso negativo (WC arborea, modelo nao)
    cmap1 = ListedColormap(["#eeeeee", "#1a9850", "#fd8d3c", "#d73027"])
    cmap1.set_bad("#9e9e9e")
    im1 = axs[0].imshow(np.ma.masked_less(code, 0), extent=extent, origin="upper",
                        cmap=cmap1, norm=BoundaryNorm([-.5, .5, 1.5, 2.5, 3.5], 4))
    cb1 = fig.colorbar(im1, ax=axs[0], shrink=0.7, ticks=[0, 1, 2, 3])
    cb1.ax.set_yticklabels(["concordam (nao-mata)", "acerto (mata)",
                            "falso +", "falso -"], fontsize=8)
    axs[0].set_title("Concordancia modelo x WorldCover")

    # painel 2: WorldCover (classes)
    presentes = sorted(int(c) for c in np.unique(wc[aval]) if c in WC_CLASSES)
    cores = [WC_CLASSES[c][1] for c in presentes]
    cmap2 = ListedColormap(cores)
    idx = np.full(shape, -1, dtype="int16")
    for i, c in enumerate(presentes):
        idx[wc == c] = i
    im2 = axs[1].imshow(np.ma.masked_less(idx, 0), extent=extent, origin="upper",
                        cmap=cmap2, vmin=-0.5, vmax=len(presentes) - 0.5)
    cb2 = fig.colorbar(im2, ax=axs[1], shrink=0.7, ticks=range(len(presentes)))
    cb2.ax.set_yticklabels([WC_CLASSES[c][0] for c in presentes], fontsize=8)
    axs[1].set_title(f"ESA WorldCover {WC_ANO} (referencia)")

    for a in (axs[0], axs[1]):
        a.set_xlabel("Easting (m)")
        a.set_aspect("equal")
    axs[0].set_ylabel("Northing (m)")

    # painel 3: metricas x limiar
    axs[2].plot(df_sweep["limiar"], df_sweep["precisao"], "-o", label="precisao")
    axs[2].plot(df_sweep["limiar"], df_sweep["recall"], "-o", label="recall")
    axs[2].plot(df_sweep["limiar"], df_sweep["f1"], "-o", label="F1")
    axs[2].plot(df_sweep["limiar"], df_sweep["iou"], "-o", label="IoU")
    axs[2].axvline(limiar, color="k", ls="--", lw=1, label=f"limiar atual ({limiar:g})")
    if melhor is not None:
        axs[2].axvline(melhor, color="green", ls=":", lw=1.5,
                       label=f"melhor F1 ({melhor:g})")
    axs[2].set_xlabel("limiar de NDVI")
    axs[2].set_ylabel("metrica")
    axs[2].set_ylim(0, 1)
    axs[2].set_title("Concordancia x limiar")
    axs[2].grid(True, alpha=0.3)
    axs[2].legend(fontsize=8, loc="best")

    it = dados["item"]["properties"]
    fig.suptitle(f"Validacao da mata (NDVI) x ESA WorldCover {WC_ANO}  |  "
                 f"cena {dados['item']['id']} {it.get('datetime','')[:10]}",
                 fontsize=11)
    fig.tight_layout()
    fig.savefig(salvar, dpi=150)
    if mostrar:
        plt.show()
    plt.close(fig)


def main(entrada=ENTRADA, saida=SAIDA, saida_grafico=SAIDA_GRAFICO,
         plotar=PLOTAR, mostrar=True, limiar=vg.LIMIAR_NDVI):
    """Valida o modelo de mata contra o ESA WorldCover e gera XLSX + PNG."""
    bbox, _ = le_quadrante(entrada)
    print(f"  Quadrante: sul={bbox['sul']:.6f} oeste={bbox['oeste']:.6f} "
          f"norte={bbox['norte']:.6f} leste={bbox['leste']:.6f}")

    print("  -> rodando o modelo de mata (Sentinel-2 / vegetacao.py)...")
    dados = vg.escolhe_cena(bbox, limiar)
    tr, crs, shape = dados["tr"], dados["crs"], dados["shape"]
    mata, limpo, ndvi = dados["mata"], dados["limpo"], dados["ndvi"]

    print(f"  -> lendo referencia ESA WorldCover {WC_ANO} (sem chave)...")
    wc = le_worldcover(bbox, tr, crs, shape)
    aval = limpo & (wc != 0)                # avalia so onde ha dado e referencia
    tree = (wc == WC_CLASSE_ARBOREA)

    m = matriz(mata, tree, aval)
    df_sweep, melhor = varre_limiares(ndvi, aval, tree, LIMIARES_SWEEP)

    ano_cena = dados["item"]["properties"].get("datetime", "")[:4]
    aviso = ("" if ano_cena == WC_ANO else
             f"cena {ano_cena} x referencia {WC_ANO}: parte da discordancia pode "
             f"ser mudanca real de uso do solo, nao erro do modelo.")

    pct_arb = 100 * tree[aval].mean() if aval.any() else 0.0
    pct_mata = 100 * mata[aval].mean() if aval.any() else 0.0
    print(f"\n==> Referencia: {pct_arb:.1f}% do quadrante e cobertura arborea "
          f"(WorldCover). Modelo marca {pct_mata:.1f}% como mata (NDVI>={limiar:g}).")
    print(f"    Matriz: TP={m['TP']} FP={m['FP']} FN={m['FN']} TN={m['TN']} "
          f"(N={m['N']})")
    print(f"    Acuracia={m['acuracia']*100:.1f}%  Precisao={m['precisao']*100:.1f}%  "
          f"Recall={m['recall']*100:.1f}%  F1={m['f1']:.2f}  IoU={m['iou']*100:.1f}%  "
          f"Kappa={m['kappa']:.2f}")
    if melhor is not None:
        lm = df_sweep.loc[df_sweep["limiar"] == melhor].iloc[0]
        print(f"    Limiar de maior concordancia (F1): {melhor:g} "
              f"(F1={lm['f1']:.2f}, precisao={lm['precisao']:.2f}, "
              f"recall={lm['recall']:.2f}). Limiar atual: {limiar:g}.")
    if aviso:
        print(f"    ! {aviso}")

    df_resumo = pd.DataFrame([{
        "cena_id": dados["item"]["id"],
        "data_cena": dados["item"]["properties"].get("datetime", "")[:10],
        "referencia": f"ESA WorldCover {WC_ANO} ({WC_VERSAO})",
        "limiar_ndvi": limiar,
        "pixels_avaliados": m["N"],
        "perc_arborea_ref": round(pct_arb, 1),
        "perc_mata_modelo": round(pct_mata, 1),
        "acuracia": round(m["acuracia"], 3),
        "precisao_user": round(m["precisao"], 3),
        "recall_producer": round(m["recall"], 3),
        "f1": round(m["f1"], 3), "iou": round(m["iou"], 3),
        "kappa": round(m["kappa"], 3),
        "limiar_melhor_f1": melhor,
        "ressalva": aviso or "cena e referencia do mesmo ano",
    }])
    df_conf = pd.DataFrame([
        {"": "modelo: MATA", "ref: arborea": m["TP"], "ref: nao-arborea": m["FP"]},
        {"": "modelo: NAO-mata", "ref: arborea": m["FN"], "ref: nao-arborea": m["TN"]},
    ])

    saida_real = escreve_xlsx_seguro(saida, {
        "resumo": df_resumo, "matriz_confusao": df_conf, "por_limiar": df_sweep,
    })
    print(f"OK -> {saida_real}")

    grafico_real = None
    if plotar:
        plota(dados, wc, aval, tree, mata, df_sweep, limiar, melhor,
              salvar=saida_grafico, bbox=bbox, mostrar=mostrar)
        grafico_real = saida_grafico
        print(f"OK -> {grafico_real}")

    return {"acuracia": m["acuracia"], "precisao": m["precisao"],
            "recall": m["recall"], "f1": m["f1"], "iou": m["iou"],
            "kappa": m["kappa"], "limiar_melhor_f1": melhor,
            "xlsx": saida_real, "png": grafico_real, "cena": dados["item"]["id"]}


if __name__ == "__main__":
    main()
