# Projeto RPA — Perfil de elevação e análise de quadrante

Conjunto de ferramentas para apoiar o esforço de instalação de postes: calcula o
**perfil de elevação** ao longo de trajetos, procura **estradas** dentro de um
quadrante e mapeia **zonas de mata (NDVI)** — tudo a partir de fontes de dados
**abertas e gratuitas** (OpenRouteService, OpenTopoData, OpenStreetMap/Overpass,
Sentinel-2, ESA WorldCover).

Cada ação lê um `.csv` de coordenadas e gera uma planilha `.xlsx` + um gráfico
`.png`, salvos **na mesma pasta do CSV**.

## Interface gráfica (recomendado)

A forma mais simples de usar. A janela abre na hora e tem **um único botão de
configuração** (selecionar o arquivo `.csv`); cada ação do menu é um botão:

```bash
python app.py
```

1. clique em **Selecionar arquivo...** e escolha o `.csv` com as coordenadas;
2. clique na ação desejada — **URBANO, RURAL, VALIDAÇÃO, ESTRADAS, VEGETAÇÃO**
   ou **VALIDA MATA**.

A interface roda a mesma lógica dos módulos e mostra o gráfico, além de botões
para abrir a planilha, o gráfico e a pasta de saída. Antes de rodar, ela faz uma
pré-checagem (arquivo, pacotes e chave de API) e explica o que falta, em vez de
estourar um erro técnico.

## Interface no navegador (versão web)

Mesma lógica, mas exibida no navegador. Sobe um servidor **local** (só a
biblioteca padrão do Python — não instala nada) e abre a página sozinho:

```bash
python web.py
```

A página abre em `http://127.0.0.1:8000`. Você escolhe o `.csv`, clica na ação
e o gráfico + os botões de download aparecem na própria página. Os arquivos
gerados ficam em `saidas_web/<data-hora>/`.

> Importante: o cálculo roda em Python no seu computador. Uma página estática
> (por exemplo, GitHub Pages) **não** consegue fazer os cálculos sozinha — ela
> só mostraria a interface. Para calcular de verdade é preciso rodar o `web.py`
> localmente, ou publicar o `web.py` em um serviço de hospedagem Python.

## Uso por linha de comando

Além da GUI, há dois pontos de entrada em modo texto:

```bash
python menu.py         # menu simples com as 6 ações
python principal.py    # menu com pré-checagem, diagnóstico e modo automação (RPA)
```

Cada módulo também roda sozinho: `python urbano.py`, `python estradas.py`, etc.

O `principal.py` aceita uso direto, útil para automação:

```bash
python principal.py urbano --entrada exemplos/pontos.csv
python principal.py estradas --entrada exemplos/quadrante.csv --sem-janela
python principal.py --diagnostico
```

## Ações

| Ação | Módulo | Entrada | Saídas |
|------|--------|---------|--------|
| **URBANO** | `urbano.py` | trajetos origem→destino | `perfil_urbano.xlsx` + `.png` |
| **RURAL** | `rural.py` | lista de pontos | `perfil_rural.xlsx` + `.png` |
| **VALIDAÇÃO** | `validacao.py` | mesmo CSV de pontos/segmentos | `validacao.xlsx` + `.png` |
| **ESTRADAS** | `estradas.py` | 2 cantos do quadrante | `estradas.xlsx` + `.png` |
| **VEGETAÇÃO** | `vegetacao.py` | mesmo quadrante | `vegetacao.xlsx` + `.png` + `.geojson` |
| **VALIDA MATA** | `validacao_vegetacao.py` | mesmo quadrante | `validacao_vegetacao.xlsx` + `.png` |

> Há ainda o `ndvi_urbano.py`, variante do NDVI para zona urbana (executável
> diretamente por `python ndvi_urbano.py`).

## Formato do CSV de entrada

A leitura detecta o separador automaticamente e aceita vírgula decimal.

- **URBANO** — uma linha por trajeto:
  `latitude inicial, longitude inicial, latitude final, longitude final` (coluna `nome` opcional).
- **RURAL / VALIDAÇÃO** — uma linha por ponto: `latitude, longitude` (coluna `nome` opcional; mínimo 2 pontos).
- **ESTRADAS / VEGETAÇÃO / VALIDA MATA** — os 2 cantos diagonais do quadrante
  (`latitude, longitude`); os cantos não podem partilhar latitude ou longitude.

Exemplos prontos na pasta [`exemplos/`](exemplos/).

## Instalação

Requer **Python 3.10+**. A interface usa apenas `tkinter` (já incluso no Python
no Windows). Os módulos de cálculo precisam dos pacotes do `requirements.txt`:

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows  (no Linux/Mac: source .venv/bin/activate)
pip install -r requirements.txt
```

## Chave de API (apenas para a ação URBANO)

A ação **URBANO** usa o OpenRouteService, que exige uma chave gratuita. As demais
ações **não precisam de chave**.

1. copie `.env.example` para `.env`;
2. preencha `ORS_API_KEY` com a sua chave (crie em
   <https://openrouteservice.org/dev/#/signup>).

> ⚠️ O arquivo `.env` está no `.gitignore` e **nunca deve ser enviado ao
> GitHub** — ele contém um segredo. Use sempre o `.env.example` como modelo.

## Precisão

O objetivo do perfil de elevação é ficar dentro de **5–10%** de erro. A ação
**VALIDAÇÃO** cruza várias fontes livres de altitude e reporta o erro %, RMSE e
um veredito de coerência. Detalhes do NDVI em
[`DOCUMENTACAO_NDVI.md`](DOCUMENTACAO_NDVI.md).

## Estrutura

```
app.py                    interface gráfica (6 ações + seletor de arquivo)
menu.py                   menu de texto simples
principal.py              menu com pré-checagem, diagnóstico e modo automação
urbano.py rural.py        perfil de elevação (vias / linha reta)
validacao.py              coerência da altitude entre fontes livres
estradas.py               estradas/proto-estradas num quadrante (OpenStreetMap)
vegetacao.py ndvi_urbano.py   zonas de mata por NDVI (Sentinel-2)
validacao_vegetacao.py    valida a mata do NDVI contra o ESA WorldCover
exemplos/                 CSVs de exemplo
requirements.txt          dependências
```
