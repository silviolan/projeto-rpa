"""
Menu interativo: escolhe o tipo de calculo de trajeto/perfil de elevacao.

  1) URBANO    -> urbano.py    : caminho real a pe (ruas/calcadas) via OpenRouteService.
                                 Entrada: pontos.csv (pares origem -> destino).
  2) RURAL     -> rural.py     : menor distancia (linha geodesica) por pontos de um CSV.
                                 Entrada: pontos_rural.csv (lista de pontos).
  3) VALIDACAO -> validacao.py : mede a coerencia da altitude por cross-validacao
                                 entre fontes livres (SRTM/ASTER/Copernicus...).
                                 Gera validacao.xlsx + validacao.png.
  4) ESTRADAS  -> estradas.py  : dado um quadrante (2 pontos diagonais num CSV),
                                 procura estradas/proto-estradas dentro dele via
                                 OpenStreetMap. Entrada: quadrante.csv.
                                 Gera estradas.xlsx + estradas.png.
  5) VEGETACAO -> vegetacao.py : no MESMO quadrante, mapeia as zonas de mata
                                 (NDVI >= 0.8) por imagem Sentinel-2 e mede
                                 quantos metros de via passam dentro de mata.
                                 Entrada: quadrante.csv.
                                 Gera vegetacao.xlsx + vegetacao.png.
  6) VALIDA MATA -> validacao_vegetacao.py : afere o modelo de mata do [5] contra
                                 o ESA WorldCover (referencia 10 m, sem chave):
                                 matriz de confusao, acuracia/F1/IoU e varredura
                                 de limiar. Gera validacao_vegetacao.xlsx + .png.

1 e 2 geram XLSX (perfil + resumo) e um grafico PNG de altitude x distancia.
Os modulos tambem podem ser executados diretamente: `python urbano.py` /
`python rural.py` / `python validacao.py` / `python estradas.py` /
`python vegetacao.py` / `python validacao_vegetacao.py`.
"""


def main():
    print("=" * 50)
    print("  PERFIL DE ELEVACAO - calculo de trajeto")
    print("=" * 50)
    print("  [1] Zona URBANA  (caminho a pe pelas vias)")
    print("  [2] Zona RURAL   (menor distancia entre pontos)")
    print("  [3] VALIDACAO    (coerencia da altitude entre fontes livres)")
    print("  [4] ESTRADAS     (procura estradas/proto-estradas num quadrante)")
    print("  [5] VEGETACAO    (zonas de mata NDVI>=0.8 no quadrante + vias)")
    print("  [6] VALIDA MATA  (valida a mata do NDVI vs ESA WorldCover)")
    print("  [0] Sair")
    print("-" * 50)

    while True:
        opcao = input("Escolha uma opcao [1/2/3/4/5/6/0]: ").strip()
        if opcao == "1":
            print("\n>> Modo URBANO\n")
            import urbano
            urbano.main()
            return
        if opcao == "2":
            print("\n>> Modo RURAL\n")
            import rural
            rural.main()
            return
        if opcao == "3":
            print("\n>> Modo VALIDACAO\n")
            import validacao
            validacao.main()
            return
        if opcao == "4":
            print("\n>> Modo ESTRADAS\n")
            import estradas
            estradas.main()
            return
        if opcao == "5":
            print("\n>> Modo VEGETACAO\n")
            import vegetacao
            vegetacao.main()
            return
        if opcao == "6":
            print("\n>> Modo VALIDA MATA\n")
            import validacao_vegetacao
            validacao_vegetacao.main()
            return
        if opcao in {"0", "q", "sair", "exit"}:
            print("Encerrado.")
            return
        print("  Opcao invalida. Digite 1, 2, 3, 4, 5, 6 ou 0.")


if __name__ == "__main__":
    main()
