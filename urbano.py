"""
ZONA URBANA - Perfil de elevacao por segmento, ao longo do CAMINHO REAL A PE.

Para cada par origem->destino do CSV, o trajeto NAO e uma linha reta: segue o
caminho real de um pedestre (ruas/calcadas/travessias) obtido via
OpenRouteService (perfil 'foot-walking'). A altitude e consultada com
INTERPOLACAO BILINEAR (mais precisa que o vizinho-mais-proximo do SRTM cru)
via OpenTopoData, com fallback automatico para o Open-Elevation se a API
primaria estiver indisponivel.

Entrada : CSV onde cada linha = um segmento, colunas:
          latitude inicial, longitude inicial, latitude final, longitude final
          (coluna opcional 'nome'/'trajeto' identifica cada trajeto)
Saida   : XLSX com duas planilhas + grafico PNG:
          - 'perfil'  pontos amostrados ao longo do caminho real
                      (distancia acumulada x altitude)
          - 'resumo'  uma linha por segmento (distancia total, altitude
                      minima/maxima, desnivel)
          - PNG       altitude x distancia de cada trajeto

Stack open source / APIs:
  - OpenRouteService (foot-walking)  rota real a pe          [precisa de chave gratuita]
  - OpenTopoData (mapzen, bilinear)  altitude ~30 m global   [API publica gratuita]
  - Open-Elevation                   altitude (fallback)     [API publica gratuita]
  - pyproj                           distancia geodesica + reamostragem do trajeto
  - pandas/openpyxl                  leitura e escrita de planilha
  - requests                         chamadas HTTP

Instalacao:
  pip install pyproj pandas openpyxl requests matplotlib

Chave da API (gratuita) em https://openrouteservice.org/dev/#/signup
Defina a variavel de ambiente ORS_API_KEY ou coloque-a num arquivo .env.
  PowerShell:  $env:ORS_API_KEY = "sua_chave"
"""

import os
import time

import matplotlib.pyplot as plt
import pandas as pd
from pyproj import Geod
import requests

# ---------- configuracao ----------
ENTRADA = "pontos.csv"
SAIDA = "perfil_urbano.xlsx"
SAIDA_GRAFICO = "perfil_urbano.png"
PASSO_M = 10.0          # espacamento da reamostragem ao longo do trajeto (m).
                        # Menor = mais pontos / curva mais densa. O DEM e ~30 m,
                        # entao abaixo disso a altitude e interpolada (bilinear),
                        # nao um novo dado real; 10 m enriquece a visualizacao
                        # sem custo de API excessivo. Use 5 m p/ ainda mais pontos.
PLOTAR = True           # gera/exibe o grafico do perfil de elevacao por trajeto
SUAVIZAR_GRAFICO = True  # suaviza a curva no grafico (media movel). NAO altera os
                         # dados: o xlsx continua com as altitudes brutas do DEM.
JANELA_SUAVIZACAO = 5    # nro de pontos da media movel (passo 10 m -> janela ~50 m)

# OpenRouteService (a chave e lida do .env em tempo de execucao, ver rota_a_pe)
ORS_PERFIL = "foot-walking"                       # pedestre: ruas, calcadas, travessias
ORS_URL = f"https://api.openrouteservice.org/v2/directions/{ORS_PERFIL}/geojson"

# Fonte de altitude usada (a outra entra automaticamente como reserva):
#   "opentopodata"   -> Mapzen/SRTM ~30 m com interpolacao BILINEAR, gratuito e
#                       SEM CHAVE. Melhor opcao free: reproduz a descida com
#                       oscilacoes e casa com o fim da referencia. [PADRAO]
#   "open-elevation" -> SRTM gratuito, sem chave; acerta o valor inicial mas
#                       achata a forma (valores inteiros, vizinho-mais-proximo).
#   "google"         -> Google Maps Elevation API (mesma base do Google Earth),
#                       NAO e open source e exige chave/cartao. Disponivel se
#                       voce definir GOOGLE_MAPS_API_KEY no .env.
FONTE_ALTITUDE = "opentopodata"

# Google Maps Elevation API (altitude que casa com o Google Earth)
GOOGLE_URL = "https://maps.googleapis.com/maps/api/elevation/json"
GOOGLE_KEY_ENV = "GOOGLE_MAPS_API_KEY"   # nome da variavel lida do .env/ambiente
GOOGLE_LOTE = 300            # a API aceita ate 512 locations por requisicao

# OpenTopoData (altitude) - reserva
OTD_DATASET = "mapzen"      # global ~30 m. Alternativas: "srtm30m", "aster30m"
OTD_INTERP = "bilinear"     # bilinear melhora a precisao entre celulas do grid
OTD_URL = f"https://api.opentopodata.org/v1/{OTD_DATASET}"
OTD_LOTE = 100              # max. locations por requisicao
OTD_PAUSA = 1.0            # s entre requisicoes (limite de 1 req/s da API publica)
OTD_TENTATIVAS = 5          # retentativas por lote antes de cair p/ o fallback.
                            # A API publica do opentopodata.org cai com SSL/timeout
                            # de forma intermitente; insistir evita usar a fonte
                            # grosseira (Open-Elevation) sem necessidade.
OTD_RETRY_PAUSA = 1.5       # s entre retentativas do mesmo lote

# Open-Elevation (altitude) - fonte de FALLBACK, usada se o OpenTopoData falhar.
# Usa vizinho-mais-proximo do SRTM (menos precisa que a bilinear), mas mantem o
# script funcional quando a API primaria esta indisponivel.
OE_URL = "https://api.open-elevation.com/api/v1/lookup"
OE_LOTE = 100
OE_PAUSA = 0.5
# ----------------------------------

geod = Geod(ellps="WGS84")


def carrega_env(caminho=".env"):
    """Carrega variaveis de um .env simples (CHAVE=valor) para o ambiente.

    Evita precisar definir ORS_API_KEY manualmente a cada sessao do PowerShell
    e mantem a chave fora do codigo-fonte.
    """
    if not os.path.exists(caminho):
        return
    with open(caminho, encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, _, valor = linha.partition("=")
            os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))


def normaliza_colunas(df):
    """Aceita as colunas esperadas independente de espacos/maiusculas.

    Cada linha do CSV = 1 trajeto (origem -> destino). Uma coluna opcional de
    nome ('nome', 'trajeto', 'id', 'rota'...) identifica cada trajeto na saida.
    """
    df = df.rename(columns={c: c.strip().lower() for c in df.columns})
    esperadas = {
        "lat_ini": "latitude inicial",
        "lon_ini": "longitude inicial",
        "lat_fim": "latitude final",
        "lon_fim": "longitude final",
    }
    faltando = [v for v in esperadas.values() if v not in df.columns]
    if faltando:
        raise ValueError(f"Colunas ausentes no CSV: {faltando}")

    apelidos_nome = {"nome", "trajeto", "identificacao", "identificação",
                     "id", "rota"}
    col_nome = next((c for c in df.columns if c in apelidos_nome), None)

    df = df.rename(columns={v: k for k, v in esperadas.items()})
    if col_nome:
        df = df.rename(columns={col_nome: "nome"})
    return df


def le_csv(caminho):
    """Le o CSV detectando o separador e tolerando virgula decimal (BR)."""
    df = pd.read_csv(caminho, sep=None, engine="python")
    df = normaliza_colunas(df)
    for col in ("lat_ini", "lon_ini", "lat_fim", "lon_fim"):
        if df[col].dtype == object:
            df[col] = (
                df[col].astype(str).str.replace(",", ".", regex=False).astype(float)
            )
    return df


def rota_a_pe(lon1, lat1, lon2, lat2):
    """Geometria do caminho real a pe: lista de (lon, lat) seguindo as vias."""
    api_key = os.environ.get("ORS_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "Chave do OpenRouteService ausente. Crie um arquivo .env com a linha "
            "ORS_API_KEY=sua_chave (ou defina a variavel de ambiente)."
        )
    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    body = {"coordinates": [[lon1, lat1], [lon2, lat2]]}
    r = requests.post(ORS_URL, json=body, headers=headers, timeout=60)
    r.raise_for_status()
    coords = r.json()["features"][0]["geometry"]["coordinates"]
    return [(c[0], c[1]) for c in coords]


def reamostra_trajeto(coords, passo_m):
    """Densifica a polilinha da rota para passos ~passo_m.

    Retorna (pontos, distancia_total_m, indices_vertices):
      - pontos = lista de (dist_acumulada_m, lat, lon);
      - indices_vertices = posicoes em 'pontos' que correspondem aos pontos
        ORIGINAIS de 'coords' (uteis p/ demarcar os pontos conhecidos na saida).
    """
    pontos, acum, vertices = [], 0.0, []
    if not coords:
        return pontos, acum, vertices

    lon0, lat0 = coords[0]
    pontos.append((0.0, lat0, lon0))
    vertices.append(0)

    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        _, _, d = geod.inv(lon1, lat1, lon2, lat2)
        n = int(d // passo_m)
        if n > 0:
            sub = d / (n + 1)
            for k, (lon, lat) in enumerate(geod.npts(lon1, lat1, lon2, lat2, n), 1):
                pontos.append((acum + sub * k, lat, lon))
        acum += d
        pontos.append((acum, lat2, lon2))
        vertices.append(len(pontos) - 1)

    return pontos, acum, vertices


def _altitude_opentopodata(lote, dataset_url=OTD_URL, interp=OTD_INTERP):
    """Altitude de um lote via OpenTopoData (bilinear). Pode lancar excecao."""
    locs = "|".join(f"{lat},{lon}" for lat, lon in lote)
    r = requests.get(
        dataset_url,
        params={"locations": locs, "interpolation": interp},
        timeout=60,
    )
    r.raise_for_status()
    return [res["elevation"] for res in r.json()["results"]]


def _altitude_open_elevation(lote):
    """Altitude de um lote via Open-Elevation (fallback). Pode lancar excecao."""
    payload = {"locations": [{"latitude": lat, "longitude": lon}
                             for lat, lon in lote]}
    r = requests.post(OE_URL, json=payload, timeout=60)
    r.raise_for_status()
    return [res["elevation"] for res in r.json()["results"]]


def _altitude_google(lote):
    """Altitude de um lote via Google Maps Elevation API (base do Google Earth).

    Le a chave de GOOGLE_MAPS_API_KEY (ambiente/.env). Retorna valores decimais.
    """
    api_key = os.environ.get(GOOGLE_KEY_ENV, "")
    if not api_key:
        raise RuntimeError(
            f"Chave do Google ausente. Crie/edite o .env com a linha "
            f"{GOOGLE_KEY_ENV}=sua_chave (com a Elevation API habilitada)."
        )
    locs = "|".join(f"{lat},{lon}" for lat, lon in lote)
    r = requests.get(GOOGLE_URL, params={"locations": locs, "key": api_key},
                     timeout=60)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "OK":
        raise RuntimeError(
            f"Google Elevation API status={data.get('status')}: "
            f"{data.get('error_message', '')}"
        )
    return [res["elevation"] for res in data["results"]]


_FONTES = {
    "google": _altitude_google,
    "open-elevation": _altitude_open_elevation,
    "opentopodata": _altitude_opentopodata,
}


def _busca_lote(lote, fonte, tentativas=OTD_TENTATIVAS):
    """Altitude de um lote pela 'fonte' dada, com retentativas. Lanca se falhar."""
    fn = _FONTES.get(fonte, _altitude_opentopodata)
    for tentativa in range(tentativas):
        try:
            return fn(lote)
        except Exception:
            if tentativa < tentativas - 1:
                time.sleep(OTD_RETRY_PAUSA)
    raise RuntimeError(f"fonte '{fonte}' falhou apos {tentativas} tentativas")


def elevacoes(latlon):
    """Altitude (m) para uma lista de (lat, lon). None onde faltar dado.

    Usa a fonte definida em FONTE_ALTITUDE como primaria (com retentativas) e,
    se ela falhar num lote, recorre automaticamente a outra fonte como reserva.
    Avisa uma unica vez quando a reserva e acionada.
    """
    primaria = FONTE_ALTITUDE
    reserva = "open-elevation" if primaria != "open-elevation" else "opentopodata"
    out, reserva_avisada = [], False
    for i in range(0, len(latlon), OTD_LOTE):
        lote = latlon[i:i + OTD_LOTE]
        try:
            alts = _busca_lote(lote, primaria)
        except Exception as e:
            if not reserva_avisada:
                print(f"     (altitude) '{primaria}' indisponivel: {str(e)[:140]}\n"
                      f"     -> usando '{reserva}' como reserva.")
                reserva_avisada = True
            alts = _busca_lote(lote, reserva)
        out.extend(alts)
        if i + OTD_LOTE < len(latlon):
            time.sleep(OTD_PAUSA)
    return out


def processa_trajeto(numero, nome, row):
    """Processa 1 trajeto (origem -> destino). Retorna (linhas_perfil, resumo).

    Foco: distancia ao longo do caminho real a pe, altitude por ponto e
    desnivel total. A inclinacao (rampa %) foi removida: o DEM global (~30 m)
    e ruidoso na escala do passo de amostragem e gerava rampas irreais.
    """
    coords = rota_a_pe(row.lon_ini, row.lat_ini, row.lon_fim, row.lat_fim)
    amostras, total, _ = reamostra_trajeto(coords, PASSO_M)
    altitudes = elevacoes([(lat, lon) for _, lat, lon in amostras])

    perfil = []
    for (dist, lat, lon), alt in zip(amostras, altitudes):
        perfil.append({
            "trajeto_id": numero,
            "trajeto": nome,
            "dist_acum_m": round(dist, 1),
            "latitude": lat,
            "longitude": lon,
            "altitude_m": alt,
        })

    validas = [a for a in altitudes if a is not None]
    resumo = {
        "trajeto_id": numero,
        "trajeto": nome,
        "dist_total_m": round(total, 1),
        "n_amostras": len(amostras),
        "altitude_min_m": min(validas) if validas else None,
        "altitude_max_m": max(validas) if validas else None,
        "desnivel_m": (max(validas) - min(validas)) if validas else None,
        "amostras_sem_dado": altitudes.count(None),
    }
    return perfil, resumo


def suaviza_serie(y, janela=JANELA_SUAVIZACAO):
    """Media movel centrada para suavizar a curva (efeito apenas visual).

    Atenua os 'degraus' do DEM de 30 m sem deslocar a tendencia. Retorna a
    serie original se a suavizacao estiver desligada ou a janela for <= 1.
    """
    if not SUAVIZAR_GRAFICO or janela <= 1 or len(y) < 3:
        return y
    s = (pd.Series(y)
         .rolling(janela, center=True, min_periods=1)
         .mean()
         .to_numpy())
    # ancora as pontas no valor real -> inicio/fim do grafico = cota verdadeira
    s[0], s[-1] = y[0], y[-1]
    return s


def plota_perfis(df_perfil, salvar=SAIDA_GRAFICO, mostrar=True):
    """Desenha o perfil de elevacao (distancia x altitude) de cada trajeto.

    Um subplot por trajeto, area sombreada sob a curva. Salva em PNG e exibe.
    A curva e suavizada (media movel) quando SUAVIZAR_GRAFICO=True; os dados
    da planilha permanecem brutos.
    """
    grupos = list(df_perfil.groupby("trajeto_id"))
    if not grupos:
        return

    fig, axes = plt.subplots(
        len(grupos), 1, figsize=(10, 3.2 * len(grupos)), squeeze=False
    )
    for ax, (_, g) in zip(axes[:, 0], grupos):
        nome = g["trajeto"].iloc[0]
        x = pd.to_numeric(g["dist_acum_m"], errors="coerce").to_numpy()
        y = suaviza_serie(pd.to_numeric(g["altitude_m"], errors="coerce").to_numpy())

        ax.plot(x, y, color="#1f6feb", linewidth=1.6)
        base = pd.Series(y).min()
        ax.fill_between(x, y, base, color="#1f6feb", alpha=0.18)
        ax.set_title(str(nome), fontsize=11, fontweight="bold")
        ax.set_xlabel("Distancia ao longo do trajeto (m)")
        ax.set_ylabel("Altitude (m)")
        ax.grid(True, alpha=0.3)
        ax.margins(x=0)

    fig.suptitle("Perfil de elevacao por trajeto (URBANO)", fontsize=13, fontweight="bold")
    fig.tight_layout()
    if salvar:
        fig.savefig(salvar, dpi=120)
        print(f"Grafico -> {salvar}")
    if mostrar:
        plt.show()
    plt.close(fig)


def escreve_xlsx_seguro(caminho, abas):
    """Escreve {nome_aba: DataFrame} em xlsx sem derrubar a execucao.

    Se o arquivo estiver aberto no Excel (bloqueio de permissao no Windows),
    grava numa copia com sufixo de horario e avisa, em vez de lancar erro.
    Retorna o caminho efetivamente gravado.
    """
    destinos = [caminho]
    base, ext = os.path.splitext(caminho)
    destinos.append(f"{base}_{time.strftime('%H%M%S')}{ext}")
    ultimo_erro = None
    for destino in destinos:
        try:
            with pd.ExcelWriter(destino, engine="openpyxl") as xls:
                for nome, df in abas.items():
                    df.to_excel(xls, sheet_name=nome, index=False)
            if destino != caminho:
                print(f"     ! '{caminho}' estava aberto/bloqueado; "
                      f"salvei em '{destino}'. Feche o arquivo no Excel.")
            return destino
        except PermissionError as e:
            ultimo_erro = e
    raise ultimo_erro


def main(entrada=ENTRADA, saida=SAIDA, saida_grafico=SAIDA_GRAFICO,
         plotar=PLOTAR, mostrar=True):
    """Processa o CSV urbano e gera XLSX + PNG.

    Os caminhos sao parametros (com os defaults do modulo) para permitir que a
    interface grafica reaproveite a mesma logica apontando para outros arquivos.
    Retorna um dict com os caminhos efetivamente gravados e estatisticas.
    """
    carrega_env()
    df = le_csv(entrada)
    tem_nome = "nome" in df.columns

    linhas_perfil, linhas_resumo = [], []

    # Cada linha do CSV = 1 trajeto. Processa os N trajetos em sequencia.
    for seg_id, row in df.iterrows():
        numero = seg_id + 1
        nome = row.nome if tem_nome and pd.notna(row.nome) else f"Trajeto {numero}"
        print(f"  -> trajeto {numero}/{len(df)}: {nome}")
        try:
            perfil, resumo = processa_trajeto(numero, nome, row)
        except Exception as e:
            # Um trajeto com erro (sem rota, ponto longe de via, falha de API)
            # nao derruba os demais; fica registrado no resumo.
            print(f"     ! falhou: {e}")
            linhas_resumo.append({"trajeto_id": numero, "trajeto": nome,
                                  "erro": str(e)})
            continue
        linhas_perfil.extend(perfil)
        linhas_resumo.append(resumo)

    df_perfil = pd.DataFrame(linhas_perfil)
    saida_real = escreve_xlsx_seguro(saida, {
        "resumo": pd.DataFrame(linhas_resumo),
        "perfil": df_perfil,
    })

    print(f"OK -> {saida_real} ({len(df)} trajetos, {len(linhas_perfil)} pontos)")

    grafico_real = None
    if plotar and not df_perfil.empty:
        plota_perfis(df_perfil, salvar=saida_grafico, mostrar=mostrar)
        grafico_real = saida_grafico

    return {"xlsx": saida_real, "png": grafico_real,
            "trajetos": len(df), "pontos": len(linhas_perfil)}


if __name__ == "__main__":
    main()
