"""
Versao WEB do Projeto RPA  -  interface no navegador.

Sobe um pequeno servidor local (so com a biblioteca padrao do Python, sem
Flask nem instalacao extra) e abre uma pagina HTML com o MESMO fluxo do app de
desktop: um botao para escolher o .csv e um botao por acao do menu (URBANO,
RURAL, VALIDACAO, ESTRADAS, VEGETACAO, VALIDA MATA).

Uso:
    python web.py

A pagina abre sozinha em http://127.0.0.1:8000 . Os calculos rodam no seu
proprio computador (a pagina e apenas a "cara"); os arquivos gerados
(.xlsx / .png / .geojson) sao salvos em 'saidas_web/<data-hora>/' e tambem
aparecem na pagina para baixar.

Observacao: e uma ferramenta LOCAL e de UM usuario - o servidor so escuta em
127.0.0.1 (nao fica acessivel na rede).
"""

import os
import sys
import io
import json
import base64
import datetime
import importlib
import importlib.util
import threading
import webbrowser
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# matplotlib sem janela: so salva o PNG (a pagina exibe a imagem).
os.environ.setdefault("MPLBACKEND", "Agg")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PASTA_SAIDAS = os.path.join(BASE_DIR, "saidas_web")

# Local x nuvem: provedores (Render, Railway...) injetam a porta na variavel de
# ambiente PORT. Quando ela existe, estamos na nuvem: escutamos em 0.0.0.0 (para
# o provedor conseguir expor o servico) e NAO abrimos o navegador. Sem PORT,
# rodamos local em 127.0.0.1:8000 e abrimos a pagina sozinho.
NA_NUVEM = bool(os.environ.get("PORT"))
PORTA = int(os.environ.get("PORT", "8000"))
HOST = "0.0.0.0" if NA_NUVEM else "127.0.0.1"


# ---------- .env simples (CHAVE=valor) ----------
def carrega_env(caminho=None):
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


# ---------- registro das acoes (as mesmas opcoes do menu.py) ----------
MODOS = [
    {"chave": "urbano", "modname": "urbano", "base": "perfil_urbano",
     "titulo": "URBANO",
     "rotulo": "Caminho real a pe pelas vias (origem -> destino por linha)",
     "pacotes": ("requests", "pandas", "pyproj", "matplotlib", "openpyxl"),
     "env": ("ORS_API_KEY",)},
    {"chave": "rural", "modname": "rural", "base": "perfil_rural",
     "titulo": "RURAL",
     "rotulo": "Menor distancia (linha geodesica) ligando os pontos",
     "pacotes": ("requests", "pandas", "pyproj", "matplotlib", "openpyxl"),
     "env": ()},
    {"chave": "validacao", "modname": "validacao", "base": "validacao",
     "titulo": "VALIDACAO",
     "rotulo": "Coerencia da altitude entre fontes livres (qualidade do dado)",
     "pacotes": ("requests", "pandas", "pyproj", "matplotlib", "openpyxl"),
     "env": ()},
    {"chave": "estradas", "modname": "estradas", "base": "estradas",
     "titulo": "ESTRADAS",
     "rotulo": "Estradas/proto-estradas dentro de um quadrante (OpenStreetMap)",
     "pacotes": ("requests", "pandas", "pyproj", "matplotlib", "openpyxl"),
     "env": ()},
    {"chave": "vegetacao", "modname": "vegetacao", "base": "vegetacao",
     "titulo": "VEGETACAO", "geojson": True,
     "rotulo": "Zonas de mata (NDVI >= 0.85) no quadrante + vias dentro da mata",
     "pacotes": ("requests", "pandas", "numpy", "pyproj", "matplotlib",
                 "rasterio", "shapely", "openpyxl"),
     "env": ()},
    {"chave": "validacao_vegetacao", "modname": "validacao_vegetacao",
     "base": "validacao_vegetacao", "titulo": "VALIDA MATA",
     "rotulo": "Valida a mata do NDVI contra o ESA WorldCover (referencia 10 m)",
     "pacotes": ("requests", "pandas", "numpy", "pyproj", "matplotlib",
                 "rasterio", "shapely", "openpyxl"),
     "env": ()},
]
MODOS_POR_CHAVE = {m["chave"]: m for m in MODOS}


def pacotes_faltando(pacotes):
    faltando = []
    for pacote in pacotes:
        try:
            if importlib.util.find_spec(pacote) is None:
                faltando.append(pacote)
        except (ImportError, ValueError):
            faltando.append(pacote)
    return faltando


def pre_checa(meta):
    """Impedimentos ANTES de rodar (pacotes e chave de API). Retorna lista."""
    problemas = []
    faltando = pacotes_faltando(meta["pacotes"])
    if faltando:
        problemas.append("Pacotes Python ausentes: " + ", ".join(faltando)
                         + ". Instale com: pip install " + " ".join(faltando))
    for var in meta.get("env", ()):
        if not os.environ.get(var):
            problemas.append(f"Variavel {var} nao definida. Crie um arquivo .env "
                             f"na pasta com a linha {var}=sua_chave.")
    return problemas


def _b64_arquivo(caminho):
    with open(caminho, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def roda_acao(chave, nome_csv, conteudo_csv):
    """Grava o CSV recebido, roda o modulo e devolve o resultado p/ a pagina."""
    meta = MODOS_POR_CHAVE.get(chave)
    if meta is None:
        return {"ok": False, "erro": f"Acao desconhecida: {chave}"}

    problemas = pre_checa(meta)
    if problemas:
        return {"ok": False, "erro": "\n".join(problemas)}

    # pasta de trabalho unica por execucao
    carimbo = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    pasta = os.path.join(PASTA_SAIDAS, f"{carimbo}_{meta['chave']}")
    os.makedirs(pasta, exist_ok=True)

    nome_csv = os.path.basename(nome_csv or "entrada.csv") or "entrada.csv"
    if not nome_csv.lower().endswith(".csv"):
        nome_csv += ".csv"
    caminho_csv = os.path.join(pasta, nome_csv)
    with open(caminho_csv, "w", encoding="utf-8", newline="") as f:
        f.write(conteudo_csv)

    saida_xlsx = os.path.join(pasta, f"{meta['base']}.xlsx")
    saida_png = os.path.join(pasta, f"{meta['base']}.png")

    kwargs = dict(entrada=caminho_csv, saida=saida_xlsx,
                  saida_grafico=saida_png, plotar=True, mostrar=False)
    if meta.get("geojson"):
        kwargs["saida_geojson"] = os.path.join(pasta, f"{meta['base']}_zonas.geojson")

    log = io.StringIO()
    try:
        # captura os print() do modulo p/ mostrar como "progresso" na pagina.
        # (ferramenta local de um usuario; nao ha concorrencia real de stdout.)
        with contextlib.redirect_stdout(log):
            modulo = importlib.import_module(meta["modname"])
            resultado = modulo.main(**kwargs)
    except Exception as e:  # noqa: BLE001
        import traceback
        return {"ok": False, "titulo": meta["titulo"],
                "erro": f"{type(e).__name__}: {e}",
                "log": log.getvalue() + "\n" + traceback.format_exc(),
                "pasta": pasta}

    resposta = {"ok": True, "titulo": meta["titulo"], "pasta": pasta,
                "log": log.getvalue(), "veredito": (resultado or {}).get("veredito")}

    xlsx = (resultado or {}).get("xlsx")
    png = (resultado or {}).get("png")
    geojson = (resultado or {}).get("geojson")
    if xlsx and os.path.exists(xlsx):
        resposta["xlsx_nome"] = os.path.basename(xlsx)
        resposta["xlsx_b64"] = _b64_arquivo(xlsx)
    if png and os.path.exists(png):
        resposta["png_b64"] = _b64_arquivo(png)
    if geojson and os.path.exists(geojson):
        resposta["geojson_nome"] = os.path.basename(geojson)
        resposta["geojson_b64"] = _b64_arquivo(geojson)
    return resposta


# ---------- pagina HTML (servida em /) ----------
def _cartoes_acoes():
    partes = []
    for m in MODOS:
        partes.append(
            f'<button class="acao" data-chave="{m["chave"]}" disabled>'
            f'<span class="acao-t">{m["titulo"]}</span>'
            f'<span class="acao-s">{m["rotulo"]}</span></button>')
    return "\n".join(partes)


PAGINA = """<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Projeto RPA - Grupo Energisa</title>
<style>
  :root{ --ciano:#22BCD6; --laranja:#F39200; --creme:#F6F1E8; --teal:#0E7490;
         --acao:#C2410C; --acao2:#9A3412; --txt:#1A1A1A; --suave:#5A5A5A;
         --borda:#E4DCD2; --logbg:#15202B; }
  *{ box-sizing:border-box; }
  body{ margin:0; font-family:"Segoe UI",system-ui,Arial,sans-serif;
        color:var(--txt); background:#fff; }
  header{ height:84px; display:flex; align-items:center; padding:0 22px;
          color:#0A3A44; font-weight:700;
          background:linear-gradient(90deg,var(--ciano) 0%,var(--creme) 50%,var(--laranja) 100%); }
  header .wm{ font-size:26px; color:#fff; text-shadow:0 1px 1px #0A3A44; }
  header .wm small{ display:block; font-size:10px; letter-spacing:1px; }
  main{ max-width:960px; margin:0 auto; padding:16px; }
  h1{ color:var(--teal); font-size:20px; margin:14px 2px 2px; }
  .sub{ color:var(--suave); font-size:13px; margin:0 2px 12px; }
  .card{ border:1px solid var(--borda); border-radius:10px; padding:14px;
         margin:12px 0; }
  .card > .lbl{ color:var(--teal); font-weight:700; font-size:13px;
                margin-bottom:10px; }
  .arquivo{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
  .arquivo .nome{ color:var(--suave); font-size:13px; flex:1 1 200px; }
  .btn{ border:0; border-radius:8px; padding:10px 16px; font-weight:700;
        cursor:pointer; background:#FCEAD9; color:var(--acao2); }
  .btn:hover{ background:var(--acao); color:#fff; }
  .acoes{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  @media (max-width:620px){ .acoes{ grid-template-columns:1fr; } }
  .acao{ text-align:left; border:0; border-radius:10px; padding:12px 14px;
         background:var(--acao); color:#fff; cursor:pointer; display:flex;
         flex-direction:column; gap:3px; }
  .acao:hover:not(:disabled){ background:var(--acao2); }
  .acao:disabled{ background:#D9B7A6; cursor:not-allowed; }
  .acao-t{ font-size:15px; font-weight:700; }
  .acao-s{ font-size:11px; color:#FFE7D3; }
  #barra{ height:4px; border-radius:4px; background:#E0F4F8; overflow:hidden;
          margin:8px 0; display:none; }
  #barra.on{ display:block; }
  #barra i{ display:block; height:100%; width:40%; background:var(--ciano);
            animation:corre 1.1s infinite linear; }
  @keyframes corre{ 0%{margin-left:-40%} 100%{margin-left:100%} }
  #log{ background:var(--logbg); color:#E6E6E6; font-family:Consolas,monospace;
        font-size:12px; padding:10px; border-radius:8px; white-space:pre-wrap;
        min-height:56px; max-height:200px; overflow:auto; }
  #res .top{ font-weight:700; margin-bottom:8px; }
  #res img{ max-width:100%; border:1px solid var(--borda); border-radius:8px;
            margin-top:8px; }
  .baixar{ display:inline-block; margin:4px 8px 0 0; text-decoration:none; }
  .ok{ color:var(--teal); } .warn{ color:#B25A00; } .err{ color:#B00020; }
  .pasta{ color:var(--suave); font-size:12px; margin-top:8px; word-break:break-all; }
</style></head>
<body>
<header><div class="wm">energisa<small>LIGADA NA SUA ENERGIA</small></div></header>
<main>
  <h1>Projeto RPA</h1>
  <p class="sub">Perfil de elevacao e analise de quadrante - Grupo Energisa</p>

  <div class="card">
    <div class="lbl">1. Arquivo de coordenadas (.csv)</div>
    <div class="arquivo">
      <span class="nome" id="nome">Nenhum arquivo selecionado</span>
      <input type="file" id="file" accept=".csv" style="display:none">
      <button class="btn" id="sel">Selecionar arquivo...</button>
    </div>
  </div>

  <div class="card">
    <div class="lbl">2. Escolha a acao</div>
    <div class="acoes">__ACOES__</div>
  </div>

  <div id="barra"><i></i></div>

  <div class="card">
    <div class="lbl">Progresso</div>
    <div id="log">Selecione um .csv e clique numa acao.</div>
  </div>

  <div class="card" id="res" style="display:none">
    <div class="lbl">Resultado / Grafico</div>
    <div id="res-inner"></div>
  </div>
</main>
<script>
  let conteudo=null, nomeArq=null;
  const $=s=>document.querySelector(s);
  const acoes=[...document.querySelectorAll(".acao")];
  function habilita(on){ acoes.forEach(b=>b.disabled=!on); }

  $("#sel").onclick=()=>$("#file").click();
  $("#file").onchange=e=>{
    const f=e.target.files[0]; if(!f) return;
    const r=new FileReader();
    r.onload=()=>{ conteudo=r.result; nomeArq=f.name;
      $("#nome").textContent=f.name; habilita(true); };
    r.readAsText(f);
  };

  acoes.forEach(b=>b.onclick=()=>rodar(b.dataset.chave, b.querySelector(".acao-t").textContent));

  async function rodar(chave, titulo){
    if(conteudo===null){ alert("Selecione primeiro o arquivo .csv."); return; }
    habilita(false); $("#sel").disabled=true;
    $("#barra").classList.add("on");
    $("#log").textContent="Calculando ("+titulo+")... aguarde.";
    $("#res").style.display="none";
    try{
      const resp=await fetch("/rodar",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({acao:chave, nome:nomeArq, conteudo})});
      const d=await resp.json();
      mostra(d);
    }catch(err){
      $("#log").innerHTML='<span class="err">Falha de comunicacao: '+err+'</span>';
    }finally{
      habilita(true); $("#sel").disabled=false;
      $("#barra").classList.remove("on");
    }
  }

  function link(nome, b64, mime){
    const a=document.createElement("a");
    a.className="baixar btn"; a.download=nome; a.textContent="Baixar "+nome;
    a.href="data:"+mime+";base64,"+b64; return a;
  }

  function mostra(d){
    $("#log").textContent=d.log || (d.ok?"Concluido.":"");
    const res=$("#res"), inner=$("#res-inner"); inner.innerHTML=""; res.style.display="block";
    const top=document.createElement("div"); top.className="top";
    if(!d.ok){
      top.innerHTML='<span class="err">Erro ('+(d.titulo||"")+'): '+d.erro+'</span>';
      inner.appendChild(top);
      if(d.pasta){ const p=document.createElement("div"); p.className="pasta";
        p.textContent="Pasta: "+d.pasta; inner.appendChild(p); }
      return;
    }
    let txt="Concluido ("+d.titulo+")!";
    if(d.veredito){ const co=/COERENTE/i.test(d.veredito)?"ok":"warn";
      txt+=' <span class="'+co+'">Veredito: '+d.veredito+'</span>'; }
    top.innerHTML='<span class="ok">'+txt+'</span>'; inner.appendChild(top);

    const barra=document.createElement("div");
    if(d.xlsx_b64) barra.appendChild(link(d.xlsx_nome, d.xlsx_b64,
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"));
    if(d.geojson_b64) barra.appendChild(link(d.geojson_nome, d.geojson_b64,
      "application/geo+json"));
    inner.appendChild(barra);

    if(d.png_b64){ const img=document.createElement("img");
      img.src="data:image/png;base64,"+d.png_b64; inner.appendChild(img); }
    if(d.pasta){ const p=document.createElement("div"); p.className="pasta";
      p.textContent="Arquivos salvos em: "+d.pasta; inner.appendChild(p); }
  }
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _envia(self, codigo, corpo, tipo="application/json; charset=utf-8"):
        dado = corpo if isinstance(corpo, bytes) else corpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(dado)))
        self.end_headers()
        self.wfile.write(dado)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._envia(200, PAGINA.replace("__ACOES__", _cartoes_acoes()),
                        "text/html; charset=utf-8")
        else:
            self._envia(404, "nao encontrado", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path != "/rodar":
            self._envia(404, json.dumps({"ok": False, "erro": "rota invalida"}))
            return
        try:
            tam = int(self.headers.get("Content-Length", 0))
            corpo = json.loads(self.rfile.read(tam) or b"{}")
            resultado = roda_acao(corpo.get("acao"), corpo.get("nome"),
                                  corpo.get("conteudo") or "")
        except Exception as e:  # noqa: BLE001
            resultado = {"ok": False, "erro": f"{type(e).__name__}: {e}"}
        self._envia(200, json.dumps(resultado))

    def log_message(self, *args):
        pass  # silencia o log de requisicoes no terminal


def main():
    carrega_env()
    os.makedirs(PASTA_SAIDAS, exist_ok=True)
    servidor = ThreadingHTTPServer((HOST, PORTA), Handler)
    print("=" * 56)
    print("  PROJETO RPA - versao web")
    print("=" * 56)
    if NA_NUVEM:
        print(f"  Servidor no ar (nuvem), escutando em {HOST}:{PORTA}.")
    else:
        url = f"http://127.0.0.1:{PORTA}"
        print(f"  Abra no navegador:  {url}")
        print("  (para encerrar, feche esta janela ou pressione Ctrl+C)")
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print("-" * 56)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\n  Encerrado.")
        servidor.shutdown()


if __name__ == "__main__":
    main()
