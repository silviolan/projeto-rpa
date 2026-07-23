"""
PRINCIPAL - ponto de entrada unico do Projeto RPA.

Junta num so lugar os quatro processos de campo do projeto, deixando o usuario
escolher a acao ANTES de qualquer processamento pesado:

  [1] URBANO     -> urbano.py    : perfil de elevacao do caminho real a pe
                                   (ruas/calcadas) entre pares origem->destino.
  [2] RURAL      -> rural.py     : perfil de elevacao pela menor distancia
                                   (linha geodesica) ligando N pontos.
  [3] ESTRADAS   -> estradas.py  : estradas e proto-estradas dentro de um
                                   quadrante, via OpenStreetMap/Overpass.
  [4] NDVI RURAL -> vegetacao.py : zonas de mata (NDVI >= 0.85) no quadrante,
                                   via Sentinel-2, e quantos metros de via
                                   passam dentro de mata.

Este modulo NAO reimplementa nada: ele apenas orquestra. Toda a regra de
negocio continua nos modulos originais, que seguem funcionando sozinhos
(`python urbano.py`, `python estradas.py`, ...). O que ele acrescenta:

  1. um menu unico com as quatro acoes;
  2. PRE-CHECAGEM antes de rodar - confere arquivo de entrada, pacotes Python
     e chave de API exigidos por AQUELA acao, e explica o que falta em vez de
     estourar um traceback no meio do processamento;
  3. modo diagnostico [D], que roda a pre-checagem das quatro acoes sem
     executar nenhuma (util para saber o que falta preparar);
  4. uso por linha de comando, para automacao/RPA sem menu interativo.

As dependencias pesadas (matplotlib, pandas, pyproj, rasterio) so sao
importadas quando a acao escolhida precisa delas - o menu abre na hora.

Uso interativo:
  python principal.py

Uso direto (sem menu):
  python principal.py urbano
  python principal.py estradas --entrada quadrante_norte.csv
  python principal.py ndvi-rural --limiar 0.80 --sem-janela
  python principal.py --diagnostico
"""

import argparse
import importlib
import importlib.util
import os
import sys
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Registro dos processos
# ---------------------------------------------------------------------------
# Optei por um REGISTRO (dict de dataclasses) no lugar do switch/match sugerido.
# O switch so resolveria o desvio; aqui a mesma estrutura serve para tres coisas
# ao mesmo tempo - desenhar o menu, rodar a pre-checagem e despachar a chamada -
# entao acao nova = uma entrada nova, sem mexer em mais nenhum ponto do arquivo.

@dataclass(frozen=True)
class Processo:
    """Descreve uma acao do projeto e tudo que ela exige para rodar."""

    chave: str                  # nome curto p/ a linha de comando (ex.: "urbano")
    titulo: str                 # rotulo no menu
    modulo: str                 # modulo Python que contem o main()
    descricao: str              # o que a acao faz, em uma linha
    entrada_padrao: str         # CSV lido por padrao
    formato_entrada: str        # como o CSV precisa estar
    saidas: tuple               # arquivos gerados
    pacotes: tuple              # pacotes de terceiros exigidos
    env_obrigatorio: tuple = ()  # variaveis de ambiente sem as quais nao roda
    servicos: tuple = ()        # servicos de rede consultados
    observacoes: tuple = field(default_factory=tuple)  # restricoes conhecidas


PROCESSOS = {
    "1": Processo(
        chave="urbano",
        titulo="URBANO      (perfil de elevacao a pe pelas vias)",
        modulo="urbano",
        descricao=("Roteia cada par origem->destino a pe pelo OpenRouteService, "
                   "reamostra a cada 10 m e busca a altitude de cada ponto."),
        entrada_padrao="pontos.csv",
        formato_entrada=("uma linha por trajeto, com as colunas 'latitude inicial', "
                         "'longitude inicial', 'latitude final', 'longitude final' "
                         "(coluna 'nome' opcional). Aceita virgula decimal."),
        saidas=("perfil_urbano.xlsx", "perfil_urbano.png"),
        pacotes=("requests", "pandas", "pyproj", "matplotlib", "openpyxl"),
        env_obrigatorio=("ORS_API_KEY",),
        servicos=("OpenRouteService (rota a pe)", "OpenTopoData (altitude)"),
        observacoes=(
            "Cada ponto precisa estar perto de uma via mapeada; senao o ORS nao "
            "acha rota e o trajeto entra no resumo como erro (os demais seguem).",
            "OpenTopoData e limitado a 1 requisicao/s: trajetos longos demoram.",
        ),
    ),
    "2": Processo(
        chave="rural",
        titulo="RURAL       (menor distancia entre pontos)",
        modulo="rural",
        descricao=("Ordena os pontos para a menor distancia total (vizinho mais "
                   "proximo + 2-opt) e levanta o perfil pela linha geodesica."),
        entrada_padrao="pontos_rural.csv",
        formato_entrada=("uma linha por ponto, com as colunas 'latitude' e "
                         "'longitude' (coluna 'nome' opcional). Aceita virgula "
                         "decimal."),
        saidas=("perfil_rural.xlsx", "perfil_rural.png"),
        pacotes=("requests", "pandas", "pyproj", "matplotlib", "openpyxl"),
        servicos=("OpenTopoData (altitude)",),
        observacoes=(
            "Exige ao menos 2 pontos no CSV; com menos que isso o modulo aborta.",
            "Nao usa o OpenRouteService: ignora vias e corta em linha reta.",
        ),
    ),
    "3": Processo(
        chave="estradas",
        titulo="ESTRADAS    (vias dentro de um quadrante)",
        modulo="estradas",
        descricao=("Monta o quadrante a partir de 2 cantos diagonais e busca "
                   "estradas/proto-estradas dentro dele no OpenStreetMap."),
        entrada_padrao="quadrante.csv",
        formato_entrada=("os cantos diagonais do quadrante. Aceita 3 formatos: "
                         "colunas 'latitude'/'longitude' (>= 2 linhas), colunas "
                         "inicial/final numa linha so, ou sem cabecalho (lat,lon)."),
        saidas=("estradas.xlsx", "estradas.png"),
        pacotes=("requests", "pandas", "pyproj", "matplotlib", "openpyxl"),
        servicos=("Overpass / OpenStreetMap (sem chave)",),
        observacoes=(
            "Os 2 cantos nao podem partilhar latitude ou longitude - senao o "
            "retangulo degenera e o modulo aborta.",
            "O Overpass e publico e intermitente: o modulo ja tenta espelhos "
            "alternativos, mas em hora de pico pode falhar mesmo assim.",
        ),
    ),
    "4": Processo(
        chave="ndvi-rural",
        titulo="NDVI RURAL  (zonas de mata no quadrante + vias)",
        modulo="vegetacao",
        descricao=("Calcula o NDVI por Sentinel-2 no MESMO quadrante do [3], "
                   "delimita a mata (NDVI >= 0.85) e mede quantos metros de "
                   "via passam dentro dela."),
        entrada_padrao="quadrante.csv",
        formato_entrada="o mesmo quadrante.csv da acao [3].",
        saidas=("vegetacao.xlsx", "vegetacao.png", "vegetacao_zonas.geojson"),
        pacotes=("requests", "pandas", "numpy", "pyproj", "matplotlib",
                 "rasterio", "shapely", "openpyxl"),
        servicos=("Earth Search / STAC (Sentinel-2, sem chave)",
                  "Overpass / OpenStreetMap (vias)"),
        observacoes=(
            "Se nao houver mata acima do limiar, grava o XLSX informando isso e "
            "NAO desenha o mapa. Em zona urbana use o ndvi_urbano.py.",
            "O NDVI mede vigor, nao porte: pasto vicoso e cana podem entrar como "
            "mata. Ver DOCUMENTACAO_NDVI.md, secao 9.2.",
            "Baixa janelas de imagem de satelite: e a acao mais lenta e a que "
            "mais consome banda.",
        ),
    ),
}

# atalho: aceita tanto o numero do menu ("3") quanto o nome ("estradas")
POR_CHAVE = {p.chave: p for p in PROCESSOS.values()}


# ---------------------------------------------------------------------------
# Pre-checagem
# ---------------------------------------------------------------------------

def carrega_env(caminho=".env"):
    """Carrega um .env simples (CHAVE=valor) para o ambiente.

    Espelha urbano.carrega_env de proposito: assim a pre-checagem consegue
    conferir a ORS_API_KEY sem ter de importar o urbano (e, com ele, todo o
    matplotlib/pandas) antes de o usuario sequer escolher a acao.
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


def pacotes_faltando(processo):
    """Pacotes de terceiros exigidos pela acao que nao estao instalados."""
    faltando = []
    for pacote in processo.pacotes:
        # find_spec so procura o modulo, nao executa o import (barato).
        try:
            if importlib.util.find_spec(pacote) is None:
                faltando.append(pacote)
        except (ImportError, ValueError):
            faltando.append(pacote)
    return faltando


def checa(processo, entrada):
    """Confere as condicoes de funcionamento da acao ANTES de rodar.

    Retorna a lista de impedimentos (vazia = pode rodar). Cada item ja vem
    escrito como orientacao para o usuario, nao como mensagem de erro tecnica.
    """
    problemas = []

    if not os.path.exists(entrada):
        problemas.append(
            f"arquivo de entrada nao encontrado: {entrada}\n"
            f"       formato esperado: {processo.formato_entrada}"
        )

    faltando = pacotes_faltando(processo)
    if faltando:
        problemas.append(
            f"pacotes Python ausentes: {', '.join(faltando)}\n"
            f"       instale com: pip install {' '.join(faltando)}"
        )

    for var in processo.env_obrigatorio:
        if not os.environ.get(var):
            problemas.append(
                f"variavel {var} nao definida\n"
                f"       crie um arquivo .env na pasta com a linha {var}=sua_chave"
            )

    return problemas


def imprime_problemas(problemas):
    for p in problemas:
        print(f"  [!] {p}")


# ---------------------------------------------------------------------------
# Execucao
# ---------------------------------------------------------------------------

def caminhos_de_saida(processo, entrada):
    """Resolve os arquivos de saida para a MESMA pasta do CSV de entrada.

    Com a entrada no diretorio atual (o caso normal) o resultado e identico ao
    de rodar o modulo direto. Escolhendo um CSV de outra pasta, as saidas ficam
    junto dele em vez de se espalharem pelo diretorio de trabalho - mesma regra
    que o app.py ja adota.
    """
    pasta = os.path.dirname(os.path.abspath(entrada))
    return {nome: os.path.join(pasta, os.path.basename(nome))
            for nome in processo.saidas}


def executa(processo, entrada=None, plotar=True, mostrar=True, limiar=None):
    """Roda a acao escolhida delegando ao main() do modulo original."""
    entrada = entrada or processo.entrada_padrao

    print()
    print("=" * 62)
    print(f"  {processo.titulo.split('(')[0].strip()}  ->  {processo.modulo}.py")
    print("=" * 62)
    print(f"  entrada : {entrada}")
    if processo.servicos:
        print(f"  servicos: {', '.join(processo.servicos)}")
    print()

    problemas = checa(processo, entrada)
    if problemas:
        print("  Nao da para rodar ainda:")
        imprime_problemas(problemas)
        return None

    # import tardio: so agora as bibliotecas pesadas entram na memoria.
    print(f"  -> carregando {processo.modulo}.py ...")
    try:
        modulo = importlib.import_module(processo.modulo)
    except Exception as e:
        print(f"  [!] falha ao importar {processo.modulo}.py: {e}")
        return None

    saidas = caminhos_de_saida(processo, entrada)
    argumentos = {
        "entrada": entrada,
        "saida": saidas[processo.saidas[0]],
        "saida_grafico": saidas[processo.saidas[1]],
        "plotar": plotar,
        "mostrar": mostrar,
    }
    # o vegetacao.py tem dois parametros a mais que os demais main()
    if processo.modulo == "vegetacao":
        argumentos["saida_geojson"] = saidas[processo.saidas[2]]
        if limiar is not None:
            argumentos["limiar"] = limiar

    print()
    try:
        resultado = modulo.main(**argumentos)
    except KeyboardInterrupt:
        print("\n  Interrompido pelo usuario.")
        return None
    except FileNotFoundError as e:
        print(f"\n  [!] arquivo nao encontrado: {e}")
        return None
    except ValueError as e:
        # entrada mal formada (colunas erradas, quadrante degenerado, ...)
        print(f"\n  [!] entrada invalida: {e}")
        return None
    except Exception as e:
        # rede fora do ar, API sem chave, servico intermitente...
        print(f"\n  [!] {type(e).__name__}: {e}")
        return None

    print()
    print("-" * 62)
    print(f"  Concluido: {processo.chave}")
    for rotulo, caminho in (resultado or {}).items():
        if rotulo in {"xlsx", "png", "geojson"} and caminho:
            print(f"    {rotulo:<8} {caminho}")
    print("-" * 62)
    return resultado


# ---------------------------------------------------------------------------
# Menu interativo
# ---------------------------------------------------------------------------

def mostra_menu():
    print()
    print("=" * 62)
    print("  PROJETO RPA - perfil de elevacao e analise de quadrante")
    print("=" * 62)
    for numero, p in PROCESSOS.items():
        print(f"  [{numero}] {p.titulo}")
        print(f"      entrada: {p.entrada_padrao:<22} saida: {p.saidas[0]}")
    print("  [D] Diagnostico  (confere o que falta para cada acao)")
    print("  [0] Sair")
    print("-" * 62)


def mostra_detalhe(processo):
    """Restricoes e condicoes de funcionamento da acao, antes de confirmar."""
    print()
    print(f"  {processo.descricao}")
    print(f"  Entrada: {processo.formato_entrada}")
    print(f"  Saidas : {', '.join(processo.saidas)}")
    if processo.observacoes:
        print("  Atencao:")
        for obs in processo.observacoes:
            print(f"    - {obs}")


def diagnostico():
    """Pre-checagem das quatro acoes, sem rodar nenhuma."""
    print()
    print("=" * 62)
    print("  DIAGNOSTICO - condicoes de funcionamento de cada acao")
    print("=" * 62)
    for numero, p in PROCESSOS.items():
        problemas = checa(p, p.entrada_padrao)
        marca = "OK " if not problemas else "-- "
        print(f"\n  [{marca}] [{numero}] {p.chave}")
        if problemas:
            imprime_problemas(problemas)
        else:
            print(f"      pronto para rodar com {p.entrada_padrao}")
    print()
    print("-" * 62)


def pergunta_entrada(processo):
    """Deixa apontar outro CSV; Enter aceita o padrao do modulo."""
    resposta = input(
        f"  Arquivo de entrada [{processo.entrada_padrao}]: "
    ).strip().strip('"')
    return resposta or processo.entrada_padrao


def menu():
    while True:
        mostra_menu()
        opcao = input("  Escolha [1/2/3/4/D/0]: ").strip().lower()

        if opcao in {"0", "q", "sair", "exit"}:
            print("  Encerrado.")
            return 0

        if opcao == "d":
            diagnostico()
            continue

        processo = PROCESSOS.get(opcao) or POR_CHAVE.get(opcao)
        if processo is None:
            print("  Opcao invalida. Digite 1, 2, 3, 4, D ou 0.")
            continue

        mostra_detalhe(processo)
        entrada = pergunta_entrada(processo)
        executa(processo, entrada=entrada, plotar=True, mostrar=True)

        if input("\n  Outra acao? [s/N]: ").strip().lower() not in {"s", "sim", "y"}:
            print("  Encerrado.")
            return 0


# ---------------------------------------------------------------------------
# Linha de comando
# ---------------------------------------------------------------------------

def monta_parser():
    nomes = ", ".join(f"{n}={p.chave}" for n, p in PROCESSOS.items())
    parser = argparse.ArgumentParser(
        prog="principal.py",
        description="Ponto de entrada unico do Projeto RPA.",
        epilog=f"acoes: {nomes}. Sem argumentos, abre o menu interativo.",
    )
    parser.add_argument("acao", nargs="?",
                        help="numero do menu ou nome da acao")
    parser.add_argument("--entrada", help="CSV de entrada (sobrepoe o padrao)")
    parser.add_argument("--limiar", type=float,
                        help="limiar de NDVI da acao ndvi-rural (padrao 0.85)")
    parser.add_argument("--sem-grafico", action="store_true",
                        help="nao gera o PNG")
    parser.add_argument("--sem-janela", action="store_true",
                        help="gera o PNG mas nao abre a janela do grafico "
                             "(use em automacao/RPA: sem isso o script fica "
                             "travado esperando fechar a janela)")
    parser.add_argument("--diagnostico", action="store_true",
                        help="so confere as condicoes de cada acao e sai")
    return parser


def main(argv=None):
    carrega_env()
    args = monta_parser().parse_args(argv)

    if args.diagnostico:
        diagnostico()
        return 0

    if args.acao is None:
        return menu()

    processo = PROCESSOS.get(args.acao) or POR_CHAVE.get(args.acao.lower())
    if processo is None:
        validas = ", ".join(p.chave for p in PROCESSOS.values())
        print(f"Acao desconhecida: {args.acao}. Validas: {validas}.")
        return 2

    if args.sem_janela:
        # precisa valer ANTES de o modulo importar o matplotlib; como o import
        # e tardio (dentro de executa), definir aqui ainda pega a tempo.
        os.environ["MPLBACKEND"] = "Agg"

    resultado = executa(
        processo,
        entrada=args.entrada,
        plotar=not args.sem_grafico,
        mostrar=not args.sem_janela,
        limiar=args.limiar,
    )
    return 0 if resultado is not None else 1


if __name__ == "__main__":
    sys.exit(main())
