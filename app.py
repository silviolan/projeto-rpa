"""
Interface grafica do PROJETO RPA  -  identidade visual Grupo Energisa.

A janela abre na hora e reune as MESMAS acoes do menu.py, cada uma como um
botao proprio. O usuario final (sem Python) so precisa de dois passos:

  1. clicar em "Selecionar arquivo..." e escolher o .csv com as coordenadas
     (este e o UNICO botao de configuracao);
  2. clicar na acao desejada  ->  URBANO, RURAL, VALIDACAO, ESTRADAS,
     VEGETACAO ou VALIDA MATA.

Cada acao roda a mesma logica do modulo correspondente (urbano.py, rural.py,
validacao.py, estradas.py, vegetacao.py, validacao_vegetacao.py) e gera os
mesmos arquivos (planilha .xlsx + grafico .png), salvos na MESMA PASTA do CSV.

Pontos de software desta versao:
  - A janela abre na hora: os modulos pesados (pandas/matplotlib/rasterio...)
    so sao importados quando a acao que precisa deles e acionada. Assim a
    interface aparece imediatamente e uma dependencia ausente (ex.: rasterio,
    exigido so pela VEGETACAO) nao impede as demais acoes de rodar.
  - Pre-checagem por acao: antes de rodar, confere o arquivo de entrada, os
    pacotes Python e a chave de API exigidos por AQUELA acao, e explica o que
    falta em vez de estourar um traceback.
  - Acessibilidade: contraste alto (WCAG AA), navegacao por teclado com
    atalhos, foco visivel, fontes ajustaveis (A- / A+) e pistas que nao
    dependem so de cor.

Logo: se houver 'energisa_logo.png' na pasta (ou embarcado no .exe), ele e
usado no cabecalho; caso contrario, desenha-se um wordmark no estilo da marca.
Empacotamento em .exe: ver build_exe.bat.
"""

import os
import sys
import queue
import importlib
import importlib.util
import threading

# Backend do matplotlib SEM janela: a interface (Tk) cuida da exibicao; o
# matplotlib so salva o PNG. Definir por variavel de ambiente evita importar o
# matplotlib na thread principal -> a janela aparece mais rapido.
os.environ.setdefault("MPLBACKEND", "Agg")

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tkinter.font as tkfont

# DPI awareness no Windows: evita a interface "borrada" em telas de alta
# resolucao (e parte da nitidez/acessibilidade visual). Inofensivo em outros SO.
try:
    from ctypes import windll
    try:
        windll.shcore.SetProcessDpiAwareness(2)   # por-monitor (Win 8.1+)
    except Exception:
        windll.user32.SetProcessDPIAware()        # reserva (Win Vista+)
except Exception:
    pass


VERSAO = "3.0"

# Pasta base: em modo .exe (PyInstaller) os dados ficam em sys._MEIPASS;
# em modo script, fica ao lado deste arquivo.
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


# ---------- paleta da marca Energisa (gradiente ciano -> laranja da imagem) ----------
CIANO = "#22BCD6"          # ciano vivo (inicio do gradiente, lado esquerdo)
LARANJA = "#F39200"        # laranja vivo (fim do gradiente, lado direito)
CREME = "#F6F1E8"          # centro claro do gradiente (evita o verde-barro do meio)
TEAL = "#0E7490"           # teal escuro: texto/realce acessivel s/ branco (~6:1)
TEAL_CLARO = "#E0F4F8"     # teal bem claro (fundos sutis)
ACAO = "#C2410C"           # laranja escuro de acao (botao) - branco por cima ~5.9:1
ACAO_HOVER = "#9A3412"     # laranja mais escuro (hover)
ACAO_CLARO = "#FCEAD9"     # creme claro (fundo de botao secundario)
BRANCO = "#FFFFFF"
TXT = "#1A1A1A"            # texto principal (~16:1 sobre branco)
TXT_SUAVE = "#5A5A5A"      # texto secundario (~7:1 sobre branco - passa AA)
BORDA = "#E4DCD2"          # bordas suaves (neutro quente)
LOG_BG = "#15202B"         # log: tema escuro (azulado, casa com o teal)
LOG_FG = "#E6E6E6"


# ---------- .env simples (CHAVE=valor), sem depender do urbano.py ----------
def carrega_env(caminho=None):
    """Carrega um .env simples para o ambiente (nao sobrescreve o que ja existe)."""
    caminho = caminho or os.path.join(BASE_DIR, ".env")
    if not os.path.exists(caminho):
        return
    try:
        with open(caminho, encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha or linha.startswith("#") or "=" not in linha:
                    continue
                chave, _, valor = linha.partition("=")
                os.environ.setdefault(chave.strip(),
                                      valor.strip().strip('"').strip("'"))
    except Exception:
        pass


# ---------- redireciona os print() dos modulos para o log da interface ----------
class _LogStream:
    def __init__(self, fila):
        self._fila = fila

    def write(self, texto):
        if texto:
            self._fila.put(texto)

    def flush(self):
        pass


class _Tooltip:
    """Dica flutuante (alto contraste) ao passar o mouse sobre um widget."""

    def __init__(self, widget, texto, fonte):
        self.widget, self.texto, self.fonte, self.tip = widget, texto, fonte, None
        widget.bind("<Enter>", self._mostra, add="+")
        widget.bind("<Leave>", self._esconde, add="+")
        widget.bind("<ButtonPress>", self._esconde, add="+")

    def _mostra(self, _=None):
        if self.tip or not self.texto:
            return
        x = self.widget.winfo_rootx() + 14
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.texto, font=self.fonte, justify="left",
                 background="#1A1A1A", foreground="#FFFFFF", padx=8, pady=5,
                 wraplength=420).pack()

    def _esconde(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# ---------------------------------------------------------------------------
# Registro das acoes (as mesmas opcoes do menu.py), cada uma vira um botao.
# 'base' define os nomes de saida; 'pacotes'/'env' alimentam a pre-checagem;
# 'geojson' marca a acao que tambem gera .geojson (so a VEGETACAO).
# ---------------------------------------------------------------------------
MODOS = [
    {
        "chave": "urbano", "modname": "urbano", "base": "perfil_urbano",
        "titulo": "URBANO", "mnem": "u",
        "rotulo": "Caminho real a pe pelas vias (origem -> destino por linha)",
        "dica": "Uma linha por trajeto, colunas 'latitude inicial', 'longitude "
                "inicial', 'latitude final', 'longitude final' (coluna 'nome' "
                "opcional). Gera perfil_urbano.xlsx + .png. Exige ORS_API_KEY.",
        "pacotes": ("requests", "pandas", "pyproj", "matplotlib", "openpyxl"),
        "env": ("ORS_API_KEY",),
    },
    {
        "chave": "rural", "modname": "rural", "base": "perfil_rural",
        "titulo": "RURAL", "mnem": "r",
        "rotulo": "Menor distancia (linha geodesica) ligando os pontos",
        "dica": "Uma linha por ponto, colunas 'latitude', 'longitude' (coluna "
                "'nome' opcional). Exige ao menos 2 pontos. Gera "
                "perfil_rural.xlsx + .png.",
        "pacotes": ("requests", "pandas", "pyproj", "matplotlib", "openpyxl"),
        "env": (),
    },
    {
        "chave": "validacao", "modname": "validacao", "base": "validacao",
        "titulo": "VALIDACAO", "mnem": "v",
        "rotulo": "Coerencia da altitude entre fontes livres (qualidade do dado)",
        "dica": "Use o MESMO CSV (pontos rural ou segmentos urbano). Cruza fontes "
                "abertas de altitude (SRTM/ASTER/Copernicus...) e mede o erro %. "
                "Gera validacao.xlsx + .png.",
        "pacotes": ("requests", "pandas", "pyproj", "matplotlib", "openpyxl"),
        "env": (),
    },
    {
        "chave": "estradas", "modname": "estradas", "base": "estradas",
        "titulo": "ESTRADAS", "mnem": "e",
        "rotulo": "Estradas/proto-estradas dentro de um quadrante (OpenStreetMap)",
        "dica": "Os 2 cantos diagonais do quadrante (nao podem partilhar latitude "
                "ou longitude). Gera estradas.xlsx + .png. Usa o Overpass (sem "
                "chave).",
        "pacotes": ("requests", "pandas", "pyproj", "matplotlib", "openpyxl"),
        "env": (),
    },
    {
        "chave": "vegetacao", "modname": "vegetacao", "base": "vegetacao",
        "titulo": "VEGETACAO", "mnem": "g", "geojson": True,
        "rotulo": "Zonas de mata (NDVI >= 0.85) no quadrante + vias dentro da mata",
        "dica": "O MESMO quadrante da acao ESTRADAS. Calcula o NDVI por Sentinel-2 "
                "e mede quantos metros de via passam dentro da mata. Gera "
                "vegetacao.xlsx + .png + .geojson. Acao mais lenta (baixa imagem "
                "de satelite).",
        "pacotes": ("requests", "pandas", "numpy", "pyproj", "matplotlib",
                    "rasterio", "shapely", "openpyxl"),
        "env": (),
    },
    {
        "chave": "validacao_vegetacao", "modname": "validacao_vegetacao",
        "base": "validacao_vegetacao", "titulo": "VALIDA MATA", "mnem": "m",
        "rotulo": "Valida a mata do NDVI contra o ESA WorldCover (referencia 10 m)",
        "dica": "O MESMO quadrante da VEGETACAO. Compara o modelo de mata com o ESA "
                "WorldCover: matriz de confusao, acuracia/F1/IoU. Gera "
                "validacao_vegetacao.xlsx + .png.",
        "pacotes": ("requests", "pandas", "numpy", "pyproj", "matplotlib",
                    "rasterio", "shapely", "openpyxl"),
        "env": (),
    },
]
MODOS_POR_CHAVE = {m["chave"]: m for m in MODOS}


def pacotes_faltando(pacotes):
    """Pacotes de terceiros exigidos que nao estao instalados (import barato)."""
    faltando = []
    for pacote in pacotes:
        try:
            if importlib.util.find_spec(pacote) is None:
                faltando.append(pacote)
        except (ImportError, ValueError):
            faltando.append(pacote)
    return faltando


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Projeto RPA - Grupo Energisa")
        self.geometry("960x860")
        self.minsize(820, 720)
        self.configure(background=BRANCO)

        carrega_env()

        self.csv_path = tk.StringVar(value="")
        self._fila_log = queue.Queue()
        self._preview_img = None       # mantem referencia da imagem (evita GC)
        self._rodando = False
        self._botoes_acao = []         # (botao, meta) p/ habilitar/desabilitar

        self._monta_fontes()
        self._estilo()
        self._monta_interface()
        self._monta_atalhos()

        self.after(100, self._drena_log)

    # ---------- fontes nomeadas (permitem o ajuste global A- / A+) ----------
    def _monta_fontes(self):
        self._escala = 1.0
        self._fontes = {}   # nome -> (objeto Font, tamanho base)

        def nova(nome, familia, tam, peso="normal"):
            f = tkfont.Font(family=familia, size=tam, weight=peso)
            self._fontes[nome] = (f, tam)
            return f

        self.f_titulo = nova("titulo", "Segoe UI", 17, "bold")
        self.f_sub = nova("sub", "Segoe UI", 9)
        self.f_secao = nova("secao", "Segoe UI", 10, "bold")
        self.f_corpo = nova("corpo", "Segoe UI", 10)
        self.f_corpo_b = nova("corpo_b", "Segoe UI", 10, "bold")
        self.f_botao = nova("botao", "Segoe UI", 11, "bold")
        self.f_acao = nova("acao", "Segoe UI", 11, "bold")
        self.f_acao_sub = nova("acao_sub", "Segoe UI", 8)
        self.f_peq = nova("peq", "Segoe UI", 9)
        self.f_ctl = nova("ctl", "Segoe UI", 11, "bold")
        self.f_log = nova("log", "Consolas", 9)

    def _aplica_escala(self):
        for f, base in self._fontes.values():
            f.configure(size=max(7, int(round(base * self._escala))))

    def _muda_fonte(self, passo):
        self._escala = min(1.6, max(0.8, round(self._escala + passo, 2)))
        self._aplica_escala()
        self._status(f"Tamanho da fonte: {int(self._escala * 100)}%")

    # ---------- estilo ttk ----------
    def _estilo(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")    # permite colorir os widgets na marca
        except tk.TclError:
            pass
        st.configure(".", background=BRANCO, foreground=TXT, font=self.f_corpo)
        st.configure("TFrame", background=BRANCO)
        st.configure("TLabel", background=BRANCO, foreground=TXT, font=self.f_corpo)
        st.configure("Titulo.TLabel", foreground=TEAL, font=self.f_titulo)
        st.configure("Sub.TLabel", foreground=TXT_SUAVE, font=self.f_peq)
        st.configure("Dica.TLabel", foreground=TEAL, font=self.f_peq)
        st.configure("Card.TLabelframe", background=BRANCO, bordercolor=BORDA,
                     relief="solid", borderwidth=1)
        st.configure("Card.TLabelframe.Label", background=BRANCO,
                     foreground=TEAL, font=self.f_secao)
        st.configure("Energisa.Horizontal.TProgressbar",
                     background=CIANO, troughcolor=TEAL_CLARO, bordercolor=BORDA)

    # ---------- helpers de botao acessivel (foco visivel + teclado) ----------
    def _botao(self, parent, texto, comando, primario=False, mnem=0, dica=""):
        if primario:
            bg, fg, hover = ACAO, BRANCO, ACAO_HOVER
        else:
            bg, fg, hover = ACAO_CLARO, ACAO_HOVER, ACAO
        b = tk.Button(parent, text=texto, command=comando,
                      bg=bg, fg=fg, activebackground=hover,
                      activeforeground=BRANCO, disabledforeground="#9A9A9A",
                      font=self.f_botao if primario else self.f_corpo_b,
                      relief="flat", bd=0, cursor="hand2",
                      underline=mnem, takefocus=1,
                      highlightthickness=2, highlightbackground=BRANCO,
                      highlightcolor=TEAL,
                      padx=14, pady=(9 if primario else 4))
        b.bind("<Return>", lambda e: (b.invoke(), "break"))
        if dica:
            _Tooltip(b, dica, self.f_peq)
        return b

    def _botao_acao(self, parent, meta):
        """Botao grande de uma acao do menu: titulo em cima, resumo embaixo."""
        quadro = tk.Frame(parent, background=ACAO, cursor="hand2",
                          highlightthickness=2, highlightbackground=BRANCO,
                          highlightcolor=TEAL)
        tk.Label(quadro, text=meta["titulo"], background=ACAO, foreground=BRANCO,
                 font=self.f_acao).pack(anchor="w", padx=12, pady=(8, 0))
        tk.Label(quadro, text=meta["rotulo"], background=ACAO, foreground="#FFE7D3",
                 font=self.f_acao_sub, wraplength=390, justify="left").pack(
                     anchor="w", padx=12, pady=(0, 8))

        def dispara(_=None):
            self._executa(meta)

        def entra(_=None):
            if str(quadro.cget("cursor")) == "hand2":
                self._pinta_acao(quadro, ACAO_HOVER)

        def sai(_=None):
            if str(quadro.cget("cursor")) == "hand2":
                self._pinta_acao(quadro, ACAO)

        for w in (quadro, *quadro.winfo_children()):
            w.bind("<Button-1>", dispara)
        quadro.bind("<Enter>", entra)
        quadro.bind("<Leave>", sai)
        quadro.bind("<Return>", dispara)
        quadro.bind("<space>", dispara)
        quadro.configure(takefocus=1)
        _Tooltip(quadro, meta["dica"], self.f_peq)
        return quadro

    @staticmethod
    def _pinta_acao(quadro, cor):
        quadro.configure(background=cor)
        for w in quadro.winfo_children():
            w.configure(background=cor)

    # ---------- construcao da interface ----------
    def _monta_interface(self):
        self._monta_cabecalho()

        corpo = ttk.Frame(self)
        corpo.pack(fill="both", expand=True, padx=16)

        # ---- Passo 1: arquivo (UNICO botao de configuracao) ----
        box1 = ttk.LabelFrame(corpo, text="  1. Arquivo de coordenadas (.csv)  ",
                              style="Card.TLabelframe")
        box1.pack(fill="x", pady=(12, 6))
        linha = ttk.Frame(box1)
        linha.pack(fill="x", padx=10, pady=8)
        self.lbl_arquivo = ttk.Label(linha, text="Nenhum arquivo selecionado",
                                     style="Sub.TLabel")
        self.lbl_arquivo.pack(side="left", fill="x", expand=True)
        self.btn_sel = self._botao(linha, "Selecionar arquivo...",
                                   self._escolhe_csv, mnem=0,
                                   dica="Atalho: Alt+S ou Ctrl+O")
        self.btn_sel.pack(side="right")

        # ---- Passo 2: acoes (uma por opcao do menu) ----
        box2 = ttk.LabelFrame(corpo, text="  2. Escolha a acao  ",
                              style="Card.TLabelframe")
        box2.pack(fill="x", pady=6)
        grade = ttk.Frame(box2)
        grade.pack(fill="x", padx=8, pady=8)
        for i, meta in enumerate(MODOS):
            b = self._botao_acao(grade, meta)
            b.grid(row=i // 2, column=i % 2, sticky="nsew", padx=4, pady=4)
            self._botoes_acao.append((b, meta))
        grade.columnconfigure(0, weight=1, uniform="acao")
        grade.columnconfigure(1, weight=1, uniform="acao")

        self.barra = ttk.Progressbar(corpo, mode="indeterminate",
                                     style="Energisa.Horizontal.TProgressbar")
        self.barra.pack(fill="x", pady=(2, 0))

        # ---- Log ----
        box3 = ttk.LabelFrame(corpo, text="  Progresso  ", style="Card.TLabelframe")
        box3.pack(fill="x", pady=6)
        self.txt_log = tk.Text(box3, height=6, wrap="word", font=self.f_log,
                               state="disabled", background=LOG_BG,
                               foreground=LOG_FG, insertbackground=LOG_FG,
                               relief="flat", padx=8, pady=6)
        self.txt_log.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb = ttk.Scrollbar(box3, command=self.txt_log.yview)
        sb.pack(side="right", fill="y", pady=8, padx=(0, 8))
        self.txt_log.configure(yscrollcommand=sb.set)

        # ---- Resultado: botoes + grafico com rolagem ----
        box4 = ttk.LabelFrame(corpo, text="  Resultado / Grafico  ",
                              style="Card.TLabelframe")
        box4.pack(fill="both", expand=True, pady=(6, 6))

        self.box_botoes = ttk.Frame(box4)
        self.box_botoes.pack(fill="x", padx=8, pady=(8, 4))

        area = ttk.Frame(box4)
        area.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.canvas_img = tk.Canvas(area, background="#F6F4F1",
                                    highlightthickness=1, highlightbackground=BORDA)
        sv = ttk.Scrollbar(area, orient="vertical", command=self.canvas_img.yview)
        sh = ttk.Scrollbar(area, orient="horizontal", command=self.canvas_img.xview)
        self.canvas_img.configure(yscrollcommand=sv.set, xscrollcommand=sh.set)
        self.canvas_img.grid(row=0, column=0, sticky="nsew")
        sv.grid(row=0, column=1, sticky="ns")
        sh.grid(row=1, column=0, sticky="ew")
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        self._mostra_msg_canvas("O grafico aparecera aqui apos o calculo.")
        self.canvas_img.bind("<Enter>", lambda e: self._liga_roda(True))
        self.canvas_img.bind("<Leave>", lambda e: self._liga_roda(False))

        # ---- rodape: fonte do dado + versao + atalhos ----
        self.lbl_status = ttk.Label(
            self, anchor="w", style="Sub.TLabel",
            text=("Altitude: OpenTopoData ~30 m  |  Energisa  |  "
                  f"v{VERSAO}   -   Selecione o .csv e clique numa acao."))
        self.lbl_status.pack(fill="x", side="bottom", padx=16, pady=(0, 8))

    def _monta_cabecalho(self):
        self.cab = tk.Canvas(self, height=84, highlightthickness=0, bd=0)
        self.cab.pack(fill="x")

        ctrl = tk.Frame(self.cab, background=LARANJA)
        tk.Label(ctrl, text="Fonte", background=LARANJA, foreground=BRANCO,
                 font=self.f_peq).pack(side="left", padx=(0, 6))
        for txt, passo, dica in (("A-", -0.1, "Diminuir o tamanho do texto"),
                                 ("A+", +0.1, "Aumentar o tamanho do texto")):
            bb = tk.Button(ctrl, text=txt,
                           command=lambda p=passo: self._muda_fonte(p),
                           bg=BRANCO, fg=TEAL, activebackground=TEAL_CLARO,
                           activeforeground=TEAL, font=self.f_ctl, relief="flat",
                           bd=0, cursor="hand2", takefocus=1, highlightthickness=2,
                           highlightbackground=LARANJA, highlightcolor=BRANCO,
                           width=3)
            bb.pack(side="left", padx=2)
            _Tooltip(bb, dica, self.f_peq)
        self._ctrl_win = self.cab.create_window(0, 0, anchor="e", window=ctrl)
        self.cab.bind("<Configure>", self._redesenha_cabecalho)
        self.after(60, self._redesenha_cabecalho)

        barra = tk.Frame(self, background=BRANCO)
        barra.pack(fill="x")
        bloco = ttk.Frame(barra)
        bloco.pack(side="left", padx=16, pady=(8, 8))
        ttk.Label(bloco, text="Projeto RPA",
                  style="Titulo.TLabel").pack(anchor="w")
        ttk.Label(bloco,
                  text="Perfil de elevacao e analise de quadrante - Grupo Energisa",
                  style="Sub.TLabel").pack(anchor="w")
        tk.Frame(self, background=BORDA, height=1).pack(fill="x")

    @staticmethod
    def _mistura(c1, c2, t):
        a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
        b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
        r, g, bl = (round(a[k] + (b[k] - a[k]) * t) for k in range(3))
        return f"#{r:02x}{g:02x}{bl:02x}"

    def _logo_imagem(self):
        if hasattr(self, "_logo_cache"):
            return self._logo_cache
        self._logo_cache = None
        for nome in ("energisa_logo.png", "logo.png", "energisa.png"):
            caminho = os.path.join(BASE_DIR, nome)
            if os.path.exists(caminho):
                try:
                    img = tk.PhotoImage(file=caminho)
                    fator = max(1, img.height() // 56)
                    self._logo_cache = img.subsample(fator, fator)
                except Exception:
                    self._logo_cache = None
                break
        return self._logo_cache

    def _redesenha_cabecalho(self, evento=None):
        c = self.cab
        w = evento.width if evento is not None else c.winfo_width()
        h = int(c["height"])
        if w <= 1:
            return
        c.delete("grad")
        for x in range(0, w, 3):
            t = x / max(1, w - 1)
            cor = (self._mistura(CIANO, CREME, t / 0.5) if t < 0.5
                   else self._mistura(CREME, LARANJA, (t - 0.5) / 0.5))
            c.create_rectangle(x, 0, x + 3, h, outline="", fill=cor, tags="grad")
        c.tag_lower("grad")

        c.delete("marca")
        logo = self._logo_imagem()
        if logo is not None:
            c.create_image(18, h // 2, anchor="w", image=logo, tags="marca")
        else:
            c.create_text(21, h // 2 - 7, anchor="w", text="energisa",
                          fill="#0A3A44", tags="marca",
                          font=("Segoe UI", 23, "bold"))
            c.create_text(20, h // 2 - 8, anchor="w", text="energisa",
                          fill=BRANCO, tags="marca",
                          font=("Segoe UI", 23, "bold"))
            c.create_text(22, h // 2 + 16, anchor="w",
                          text="LIGADA NA SUA ENERGIA", fill=BRANCO, tags="marca",
                          font=("Segoe UI", 8, "bold"))
        c.coords(self._ctrl_win, w - 12, 20)
        c.tag_raise(self._ctrl_win)

    # ---------- atalhos de teclado ----------
    def _monta_atalhos(self):
        self.bind("<Control-o>", lambda e: self._escolhe_csv())
        self.bind("<Alt-s>", lambda e: self._escolhe_csv())
        self.bind("<Alt-S>", lambda e: self._escolhe_csv())
        for meta in MODOS:
            m = meta["mnem"]
            self.bind(f"<Alt-{m}>", lambda e, mt=meta: self._executa(mt))
            self.bind(f"<Alt-{m.upper()}>", lambda e, mt=meta: self._executa(mt))

    # ---------- rolagem com roda do mouse ----------
    def _liga_roda(self, ligar):
        if ligar:
            self.canvas_img.bind_all("<MouseWheel>", self._roda)
            self.canvas_img.bind_all("<Shift-MouseWheel>", self._roda_h)
        else:
            self.canvas_img.unbind_all("<MouseWheel>")
            self.canvas_img.unbind_all("<Shift-MouseWheel>")

    def _roda(self, e):
        self.canvas_img.yview_scroll(int(-e.delta / 120), "units")

    def _roda_h(self, e):
        self.canvas_img.xview_scroll(int(-e.delta / 120), "units")

    # ---------- acoes ----------
    def _escolhe_csv(self):
        caminho = filedialog.askopenfilename(
            title="Selecione o arquivo de coordenadas",
            filetypes=[("Arquivos CSV", "*.csv"), ("Todos os arquivos", "*.*")],
        )
        if caminho:
            self.csv_path.set(caminho)
            self.lbl_arquivo.configure(text=os.path.basename(caminho),
                                       foreground=TXT)
            self._status(f"Arquivo selecionado: {os.path.basename(caminho)}")

    def _pre_checa(self, meta, caminho):
        """Impedimentos ANTES de rodar (arquivo, pacotes, chave de API)."""
        problemas = []
        if not caminho:
            problemas.append("Selecione primeiro o arquivo .csv com as coordenadas.")
            return problemas
        if not os.path.exists(caminho):
            problemas.append("O arquivo selecionado nao foi encontrado.")
            return problemas
        faltando = pacotes_faltando(meta["pacotes"])
        if faltando:
            problemas.append(
                "Pacotes Python ausentes: " + ", ".join(faltando)
                + "\n\nInstale com:\n    pip install " + " ".join(faltando))
        for var in meta.get("env", ()):
            if not os.environ.get(var):
                problemas.append(
                    f"Variavel {var} nao definida.\n\nCrie um arquivo .env na "
                    f"pasta do programa com a linha:\n    {var}=sua_chave")
        return problemas

    def _executa(self, meta):
        if self._rodando:
            return
        caminho = self.csv_path.get()
        problemas = self._pre_checa(meta, caminho)
        if problemas:
            messagebox.showwarning(f"{meta['titulo']} - falta preparar",
                                   "\n\n".join(problemas))
            self._status(f"{meta['titulo']}: {problemas[0].splitlines()[0]}")
            return

        for w in self.box_botoes.winfo_children():
            w.destroy()
        self._preview_img = None
        self._mostra_msg_canvas(f"Calculando ({meta['titulo']})...")
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

        self._rodando = True
        self._trava_acoes(True)
        self.barra.start(12)
        self._status(f"Calculando ({meta['titulo']})... acompanhe o progresso.")

        threading.Thread(target=self._trabalho, args=(meta, caminho),
                         daemon=True).start()

    def _trava_acoes(self, travar):
        for b, _ in self._botoes_acao:
            b.configure(cursor="watch" if travar else "hand2")
            self._pinta_acao(b, "#B98A6E" if travar else ACAO)
        self.btn_sel.configure(state="disabled" if travar else "normal")

    def _trabalho(self, meta, caminho):
        """Executa em thread separada para nao congelar a interface."""
        pasta = os.path.dirname(os.path.abspath(caminho))
        saida_xlsx = os.path.join(pasta, f"{meta['base']}.xlsx")
        saida_png = os.path.join(pasta, f"{meta['base']}.png")

        stdout_antigo = sys.stdout
        sys.stdout = _LogStream(self._fila_log)
        resultado, erro = None, None
        try:
            modulo = importlib.import_module(meta["modname"])
            kwargs = dict(entrada=caminho, saida=saida_xlsx,
                          saida_grafico=saida_png, plotar=True, mostrar=False)
            if meta.get("geojson"):
                kwargs["saida_geojson"] = os.path.join(
                    pasta, f"{meta['base']}_zonas.geojson")
            resultado = modulo.main(**kwargs)
        except Exception as e:  # noqa: BLE001 - reporta qualquer falha na interface
            erro = e
            import traceback
            self._fila_log.put("\n" + traceback.format_exc())
        finally:
            sys.stdout = stdout_antigo

        self.after(0, self._finaliza, meta, resultado, erro)

    def _finaliza(self, meta, resultado, erro):
        self.barra.stop()
        self._rodando = False
        self._trava_acoes(False)

        if erro is not None:
            messagebox.showerror(f"{meta['titulo']} - erro ao calcular", str(erro))
            self._mostra_msg_canvas("Falhou. Veja o erro no log acima.")
            self._status("Erro ao calcular. Confira o log.")
            return
        if not resultado:
            messagebox.showwarning(
                "Sem resultado",
                "Nao foi possivel gerar o resultado. Confira o arquivo de coordenadas.")
            self._mostra_msg_canvas("Sem resultado.")
            self._status("Sem resultado.")
            return

        xlsx = resultado.get("xlsx")
        png = resultado.get("png")
        geojson = resultado.get("geojson")
        veredito = resultado.get("veredito")

        if veredito:
            icone = "[OK]" if "COERENTE" in str(veredito).upper() else "[!]"
            cor = TEAL if "COERENTE" in str(veredito).upper() else "#B25A00"
            titulo = f"Concluido ({meta['titulo']})!   {icone} Veredito: {veredito}"
        else:
            cor, titulo = TEAL, f"Concluido ({meta['titulo']})!"
        ttk.Label(self.box_botoes, text=titulo, font=self.f_corpo_b,
                  foreground=cor, background=BRANCO).pack(side="left", padx=(2, 12))

        if xlsx and os.path.exists(xlsx):
            self._botao_abrir("Abrir planilha (Excel)", xlsx)
        if png and os.path.exists(png):
            self._botao_abrir("Abrir grafico", png)
        if geojson and os.path.exists(geojson):
            self._botao_abrir("Abrir GeoJSON", geojson)
        if xlsx:
            self._botao_abrir("Abrir pasta", os.path.dirname(xlsx))

        if png and os.path.exists(png):
            self._mostra_grafico(png)
            self._status("Concluido. Arquivos salvos na pasta do CSV.")
        else:
            self._mostra_msg_canvas("Calculo concluido (sem grafico).")
            self._status("Concluido (sem grafico).")

    def _botao_abrir(self, texto, alvo):
        b = self._botao(self.box_botoes, texto, lambda: self._abre(alvo),
                        dica=f"Abrir: {alvo}")
        b.pack(side="left", padx=4)

    def _mostra_grafico(self, png):
        self.canvas_img.delete("all")
        try:
            img = tk.PhotoImage(file=png)
        except Exception:
            self._mostra_msg_canvas("Nao foi possivel exibir a previa do grafico.")
            return
        self._preview_img = img
        self.canvas_img.create_image(0, 0, anchor="nw", image=img)
        self.canvas_img.configure(scrollregion=(0, 0, img.width(), img.height()))

    def _mostra_msg_canvas(self, texto):
        self.canvas_img.delete("all")
        self.canvas_img.configure(scrollregion=(0, 0, 0, 0))
        self.canvas_img.create_text(16, 16, anchor="nw", fill=TXT_SUAVE,
                                    text=texto, font=self.f_corpo)

    def _status(self, texto):
        if hasattr(self, "lbl_status"):
            self.lbl_status.configure(text=f"{texto}    |    v{VERSAO}")

    @staticmethod
    def _abre(caminho):
        try:
            os.startfile(caminho)  # Windows
        except AttributeError:
            import subprocess
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, caminho])
        except Exception as e:
            messagebox.showerror("Erro", f"Nao foi possivel abrir:\n{caminho}\n\n{e}")

    # ---------- atualizacao do log ----------
    def _drena_log(self):
        try:
            while True:
                texto = self._fila_log.get_nowait()
                self.txt_log.configure(state="normal")
                self.txt_log.insert("end", texto)
                self.txt_log.see("end")
                self.txt_log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drena_log)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
