"""
ZONA RURAL - Perfil de elevacao ao longo do trajeto de MENOR DISTANCIA que
liga uma lista de pontos pre-definidos num CSV.

Diferenca para o urbano:
  - Na zona rural normalmente NAO existe malha viaria mapeada ligando os
    pontos (postes em campo aberto). Por isso o trajeto entre os pontos NAO
    usa o OpenRouteService: e a LINHA GEODESICA (menor caminho sobre o
    elipsoide WGS84) entre cada par de pontos consecutivos.
  - A ordem de visita dos pontos e otimizada para MINIMIZAR a distancia total
    do percurso (heuristica vizinho-mais-proximo + melhoria 2-opt, ancorando
    o inicio no primeiro ponto do CSV). Desligue em OTIMIZAR_ORDEM para
    respeitar a ordem do CSV.

Precisao equivalente a do urbano: reaproveita a MESMA reamostragem geodesica
a cada ~30 m e a MESMA consulta de altitude (OpenTopoData bilinear com
fallback Open-Elevation), importadas de urbano.py.

Entrada : CSV com uma linha por ponto, colunas:
          latitude, longitude   (coluna opcional 'nome'/'id' identifica o ponto)
Saida   : XLSX com duas planilhas + grafico PNG:
          - 'perfil'  pontos amostrados a cada ~30 m ao longo do trajeto
                      (distancia acumulada x altitude)
          - 'resumo'  uma linha: distancia total, altitude min/max, desnivel
                      e a ordem de visita dos pontos
          - PNG       altitude x distancia do trajeto

Stack open source / APIs:
  - pyproj                           geodesica: distancia, ordem e reamostragem
  - OpenTopoData (mapzen, bilinear)  altitude ~30 m global   [API publica gratuita]
  - Open-Elevation                   altitude (fallback)     [API publica gratuita]
  - pandas/openpyxl/matplotlib       planilha e grafico

Instalacao:
  pip install pyproj pandas openpyxl requests matplotlib

Observacao: este modulo NAO precisa de chave de API (nao usa OpenRouteService).
"""

import pandas as pd

# Reaproveita a pilha ja validada do urbano -> garante precisao equivalente.
from urbano import (carrega_env, elevacoes, reamostra_trajeto, geod, plt,
                    escreve_xlsx_seguro, suaviza_serie)

# ---------- configuracao ----------
ENTRADA = "pontos_rural.csv"
SAIDA = "perfil_rural.xlsx"
SAIDA_GRAFICO = "perfil_rural.png"
PASSO_M = 10.0          # mesmo passo do urbano -> mesma densidade de pontos.
                        # Menor = curva mais densa (DEM ~30 m -> abaixo disso a
                        # altitude e interpolada). Use 5 m p/ ainda mais pontos.
PLOTAR = True
OTIMIZAR_ORDEM = True   # True: reordena os pontos p/ menor distancia total
                        # False: usa a ordem exata do CSV
# ----------------------------------


def le_pontos(caminho):
    """Le o CSV de pontos (lat, lon, nome opcional), tolerando virgula decimal.

    Retorna lista de dicts {'nome', 'lat', 'lon'} na ordem do arquivo.
    """
    df = pd.read_csv(caminho, sep=None, engine="python")
    df = df.rename(columns={c: c.strip().lower() for c in df.columns})

    # aceita 'latitude'/'lat' e 'longitude'/'lon'/'lng'
    col_lat = next((c for c in df.columns if c in {"latitude", "lat"}), None)
    col_lon = next((c for c in df.columns
                    if c in {"longitude", "lon", "lng", "long"}), None)
    if col_lat is None or col_lon is None:
        raise ValueError(
            "CSV rural precisa das colunas 'latitude' e 'longitude' "
            f"(encontrei: {list(df.columns)})."
        )
    apelidos_nome = {"nome", "id", "ponto", "identificacao", "identificação"}
    col_nome = next((c for c in df.columns if c in apelidos_nome), None)

    for col in (col_lat, col_lon):
        if df[col].dtype == object:
            df[col] = (
                df[col].astype(str).str.replace(",", ".", regex=False).astype(float)
            )

    pontos = []
    for i, row in df.iterrows():
        nome = row[col_nome] if col_nome and pd.notna(row[col_nome]) else f"P{i + 1}"
        pontos.append({"nome": str(nome), "lat": float(row[col_lat]),
                       "lon": float(row[col_lon])})
    return pontos


def _matriz_distancias(pontos):
    """Matriz simetrica de distancias geodesicas (m) entre todos os pontos.

    Calcula geod.inv UMA vez por par (O(n^2)) e reaproveita nos algoritmos de
    ordem, em vez de refazer a geodesica a cada comparacao.
    """
    n = len(pontos)
    dist = [[0.0] * n for _ in range(n)]
    for i in range(n):
        loni, lati = pontos[i]["lon"], pontos[i]["lat"]
        for j in range(i + 1, n):
            _, _, d = geod.inv(loni, lati, pontos[j]["lon"], pontos[j]["lat"])
            dist[i][j] = dist[j][i] = d
    return dist


def _vizinho_mais_proximo(dist, n, inicio=0):
    """Ordem inicial (indices) pela heuristica do vizinho-mais-proximo.

    O ponto 'inicio' fica fixo na primeira posicao. So consulta a matriz de
    distancias ja pronta (nenhuma chamada geodesica dentro do laco).
    """
    visitado = [False] * n
    visitado[inicio] = True
    ordem, atual = [inicio], inicio
    for _ in range(n - 1):
        prox = min((k for k in range(n) if not visitado[k]),
                   key=lambda k: dist[atual][k])
        visitado[prox] = True
        ordem.append(prox)
        atual = prox
    return ordem


def _dois_opt(ordem, dist):
    """Melhora o percurso (lista de indices) com 2-opt incremental.

    Em vez de remontar a rota e somar tudo a cada tentativa (O(n) por troca, o
    que tornava o 2-opt O(n^4) no total), avalia apenas as 2 arestas que mudam
    na inversao -> delta O(1) e 2-opt em O(n^2) por passada, sem nenhuma chamada
    geodesica. Mantem o ponto inicial fixo (i >= 1).
    """
    n = len(ordem)
    melhorou = True
    while melhorou:
        melhorou = False
        for i in range(1, n - 1):            # i>=1 preserva o ponto inicial
            a = ordem[i - 1]
            b = ordem[i]
            for j in range(i + 1, n):
                c = ordem[j]
                removido = dist[a][b]
                adicionado = dist[a][c]
                if j + 1 < n:                # aresta da direita (percurso aberto)
                    d = ordem[j + 1]
                    removido += dist[c][d]
                    adicionado += dist[b][d]
                if adicionado + 1e-6 < removido:
                    ordem[i:j + 1] = ordem[i:j + 1][::-1]
                    b = ordem[i]             # 'b' muda apos inverter o trecho
                    melhorou = True
    return ordem


def ordena_menor_distancia(pontos):
    """Reordena os pontos para minimizar a distancia total do percurso.

    Mantem o primeiro ponto do CSV como inicio do trajeto. Para poucos pontos
    (caso tipico de campo) a heuristica vizinho-mais-proximo + 2-opt chega
    muito perto do otimo, sem dependencia externa. Usa uma matriz de distancias
    pre-calculada -> rapido mesmo com dezenas de pontos.
    """
    if len(pontos) <= 2:
        return list(pontos)
    dist = _matriz_distancias(pontos)
    ordem = _dois_opt(_vizinho_mais_proximo(dist, len(pontos), inicio=0), dist)
    return [pontos[i] for i in ordem]


def processa(pontos):
    """Constroi o trajeto geodesico, amostra altitude e monta perfil/resumo.

    Tambem demarca os PONTOS CONHECIDOS (os do CSV) ao longo da distancia:
    coluna 'ponto_conhecido' na aba perfil e tabela 'pontos_conhecidos'.
    """
    ordem = ordena_menor_distancia(pontos) if OTIMIZAR_ORDEM else list(pontos)

    # coords no formato (lon, lat) esperado por reamostra_trajeto
    coords = [(p["lon"], p["lat"]) for p in ordem]
    amostras, total, vertices = reamostra_trajeto(coords, PASSO_M)
    altitudes = elevacoes([(lat, lon) for _, lat, lon in amostras])

    # cada vertice (ponto original do CSV) cai numa amostra exata -> marca o nome
    marco = [""] * len(amostras)
    for k, idx in enumerate(vertices):
        if k < len(ordem):
            marco[idx] = ordem[k]["nome"]

    perfil = []
    for (dist, lat, lon), alt, nome_marco in zip(amostras, altitudes, marco):
        perfil.append({
            "trajeto_id": 1,
            "trajeto": "Rural",
            "ponto_conhecido": nome_marco,   # P1..Pn so nos pontos do CSV; "" no resto
            "dist_acum_m": round(dist, 1),
            "latitude": lat,
            "longitude": lon,
            "altitude_m": alt,
        })

    # tabela dedicada com a demarcacao dos pontos conhecidos (inicio/fim/intermediario)
    pontos_conhecidos = []
    for k, idx in enumerate(vertices):
        if k >= len(ordem):
            continue
        dist, lat, lon = amostras[idx]
        marco_tipo = ("INICIO" if k == 0
                      else "FIM" if k == len(ordem) - 1
                      else "intermediario")
        pontos_conhecidos.append({
            "ordem": k + 1,
            "ponto": ordem[k]["nome"],
            "marco": marco_tipo,
            "dist_acum_m": round(dist, 1),
            "latitude": lat,
            "longitude": lon,
            "altitude_m": altitudes[idx],
        })

    validas = [a for a in altitudes if a is not None]
    resumo = {
        "trajeto": "Rural",
        "n_pontos": len(ordem),
        "ordem_visita": " -> ".join(p["nome"] for p in ordem),
        "dist_total_m": round(total, 1),
        "n_amostras": len(amostras),
        "altitude_min_m": min(validas) if validas else None,
        "altitude_max_m": max(validas) if validas else None,
        "desnivel_m": (max(validas) - min(validas)) if validas else None,
        "amostras_sem_dado": altitudes.count(None),
    }
    return perfil, resumo, pontos_conhecidos


def plota_perfil(df_perfil, salvar=SAIDA_GRAFICO, mostrar=True):
    """Grafico unico: altitude x distancia ao longo do trajeto rural."""
    if df_perfil.empty:
        return
    x = pd.to_numeric(df_perfil["dist_acum_m"], errors="coerce").to_numpy()
    y = suaviza_serie(pd.to_numeric(df_perfil["altitude_m"], errors="coerce").to_numpy())

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(x, y, color="#2e7d32", linewidth=1.6)
    ax.fill_between(x, y, pd.Series(y).min(), color="#2e7d32", alpha=0.18)

    # demarca os pontos conhecidos (P1..Pn) sobre a curva
    if "ponto_conhecido" in df_perfil.columns:
        mask = df_perfil["ponto_conhecido"].astype(str).to_numpy() != ""
        xk, yk = x[mask], y[mask]
        nomes = df_perfil["ponto_conhecido"].astype(str).to_numpy()[mask]
        ax.scatter(xk, yk, color="#c1440e", s=40, zorder=5)
        for xi, yi, nm in zip(xk, yk, nomes):
            ax.annotate(nm, (xi, yi), textcoords="offset points", xytext=(0, 8),
                        ha="center", fontsize=8, fontweight="bold", color="#7a2a08")

    ax.set_title("Perfil de elevacao do trajeto (RURAL)", fontsize=13,
                 fontweight="bold")
    ax.set_xlabel("Distancia ao longo do trajeto (m)")
    ax.set_ylabel("Altitude (m)")
    ax.grid(True, alpha=0.3)
    ax.margins(x=0)
    fig.tight_layout()
    if salvar:
        fig.savefig(salvar, dpi=120)
        print(f"Grafico -> {salvar}")
    if mostrar:
        plt.show()
    plt.close(fig)


def main(entrada=ENTRADA, saida=SAIDA, saida_grafico=SAIDA_GRAFICO,
         plotar=PLOTAR, mostrar=True):
    """Processa o CSV rural e gera XLSX + PNG.

    Os caminhos sao parametros (com os defaults do modulo) para permitir que a
    interface grafica reaproveite a mesma logica apontando para outros arquivos.
    Retorna um dict com os caminhos efetivamente gravados e estatisticas.
    """
    carrega_env()   # carrega GOOGLE_MAPS_API_KEY (e demais chaves) do .env
    pontos = le_pontos(entrada)
    print(f"  -> {len(pontos)} pontos lidos de {entrada}")
    if len(pontos) < 2:
        print("  ! sao necessarios ao menos 2 pontos para formar um trajeto.")
        return None

    perfil, resumo, pontos_conhecidos = processa(pontos)
    print(f"  -> ordem de visita: {resumo['ordem_visita']}")
    print(f"  -> distancia total: {resumo['dist_total_m']} m")
    print("  -> pontos conhecidos demarcados:")
    for pc in pontos_conhecidos:
        print(f"       {pc['ponto']:>4} ({pc['marco']:^13}) em {pc['dist_acum_m']:>7} m "
              f"-> {pc['altitude_m']} m")

    df_perfil = pd.DataFrame(perfil)
    saida_real = escreve_xlsx_seguro(saida, {
        "resumo": pd.DataFrame([resumo]),
        "pontos": pd.DataFrame(pontos_conhecidos),
        "perfil": df_perfil,
    })

    print(f"OK -> {saida_real} (1 trajeto, {len(perfil)} pontos)")

    grafico_real = None
    if plotar and not df_perfil.empty:
        plota_perfil(df_perfil, salvar=saida_grafico, mostrar=mostrar)
        grafico_real = saida_grafico

    return {"xlsx": saida_real, "png": grafico_real,
            "pontos": len(perfil), "pontos_csv": len(pontos)}


if __name__ == "__main__":
    main()
