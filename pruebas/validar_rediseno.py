r"""Validación automática del rediseño v4 del panel (fases A-E), colores, aspecto B y lectura del ratón (2026-11-04).

Abre la página del panel en un Edge/Chrome APARTE (perfil temporal propio: no toca tu navegador ni tus preferencias) y
la recorre por el protocolo de depuración (CDP), como lo haría una persona: casillas de equipos en uso, zona principal,
sacar/traer la gráfica, fichas del osciloscopio, Encender/APAGAR de la cabecera, barra de estado, Guardar con sus
cuatro modos, ensayo en tres pasos, espacios de trabajo, Solo mirar, temas y colores, lectura del ratón, y que nada se
desborde a varios tamaños de ventana.

NO enciende la fuente, NO inicia registros, capturas ni ensayos (abre sus paneles y los cierra) y NO cambia nada del
osciloscopio: solo la página. Al abrirse, la página lee el osciloscopio como siempre (lectura bajo demanda).
Lo que depende de un equipo se comprueba si está conectado; si no, sale como OMITIDA con el motivo.

    python validar_rediseno.py                          contra el panel (http://127.0.0.1:8765)
    python validar_rediseno.py --url http://127.0.0.1:8766   contra el banco sin equipos (banco_sin_equipos.py)
    opciones: --oculto (sin ver el navegador; la lectura del ratón puede fallar porque el navegador oculto frena el
              dibujo)   --informe <carpeta>
    Por defecto se ve una ventana de Edge aparte mientras se recorre la página (unos 3 min): no hace falta tocarla.

Resultado: líneas OK / FALLO / AVISO / OMITIDA, informe .txt y capturas .png en
pruebas\informes\rediseno_AAAAMMDD_HHMM\ (o la carpeta de --informe). Código de salida 1 si hay algún FALLO.
"""
import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

AQUI = os.path.dirname(os.path.abspath(__file__))
NAVEGADORES = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
               r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
               r"C:\Program Files\Google\Chrome\Application\chrome.exe"]
PUERTO_CDP = 9341
RES = []


def chk(grupo, nombre, cond, detalle="", nivel="FALLO"):
    """cond True = OK; False = nivel (FALLO o AVISO); None = OMITIDA."""
    est = "OMITIDA" if cond is None else "OK" if cond else nivel
    RES.append((est, grupo, nombre, str(detalle)))
    print(f"{est:8}[{grupo}] {nombre}" + (f"  [{detalle}]" if detalle != "" else ""), flush=True)


# ------------------------------------------------------------------ CDP mínimo (sin librerías externas)
class Pagina:
    def __init__(self, navegador, ancho, alto, visible):
        self.perfil = tempfile.mkdtemp(prefix="cdp_rediseno_")
        self.proc = subprocess.Popen(
            [navegador] + ([] if visible else ["--headless=new"]) +
            ["--no-first-run", "--no-default-browser-check", "--disable-gpu", "--disable-popup-blocking",
             # que el navegador oculto no frene el dibujo (requestAnimationFrame) de la página ni de la gráfica de dentro
             "--disable-renderer-backgrounding", "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
             f"--user-data-dir={self.perfil}", f"--remote-debugging-port={PUERTO_CDP}", f"--window-size={ancho},{alto}",
             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tabs = None
        for _ in range(150):
            try:
                tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PUERTO_CDP}/json", timeout=2).read())
                if any(t["type"] == "page" for t in tabs):
                    break
            except Exception:
                pass
            time.sleep(0.2)
        ws = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
        host, ruta = ws[5:].split("/", 1)
        h, p = host.split(":")
        self.s = socket.create_connection((h, int(p)))
        self.s.send((f"GET /{ruta} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.s.recv(4096)
        self.buf = self.buf.split(b"\r\n\r\n", 1)[1]
        self.nid = 0
        self.errores = []
        self.cmd("Runtime.enable")
        self.cmd("Page.enable")

    def _enviar(self, txt):
        d = txt.encode()
        m = os.urandom(4)
        n = len(d)
        cab = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536
                               else bytes([0x80 | 127]) + struct.pack(">Q", n))
        self.s.send(cab + m + bytes(b ^ m[i % 4] for i, b in enumerate(d)))

    def _exacto(self, n):
        while len(self.buf) < n:
            self.buf += self.s.recv(1 << 20)
        r, self.buf = self.buf[:n], self.buf[n:]
        return r

    def _recibir(self):
        partes = b""
        while True:
            h = self._exacto(2)
            fin, n = h[0] & 0x80, h[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exacto(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exacto(8))[0]
            partes += self._exacto(n)
            if fin:
                return json.loads(partes)

    def cmd(self, metodo, **params):
        self.nid += 1
        self._enviar(json.dumps({"id": self.nid, "method": metodo, "params": params}))
        while True:
            r = self._recibir()
            if r.get("id") == self.nid:
                return r.get("result", r.get("error"))
            if r.get("method") == "Runtime.exceptionThrown":
                ex = r["params"]["exceptionDetails"]
                self.errores.append("EXCEPCIÓN " + ex.get("exception", {}).get("description", ex.get("text", "")).split("\n")[0])
            elif r.get("method") == "Runtime.consoleAPICalled" and r["params"]["type"] == "error":
                self.errores.append("CONSOLA " + " ".join(str(a.get("value", a.get("description", ""))) for a in r["params"]["args"]))

    def js(self, expr):
        r = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError("JS: " + r["exceptionDetails"].get("exception", {}).get("description", str(r))[:300])
        return r.get("result", {}).get("value")

    def esperar(self, expr, t=6.0):
        fin = time.time() + t
        while time.time() < fin:
            try:
                if self.js(expr):
                    return True
            except RuntimeError:
                pass
            time.sleep(0.2)
        return bool(self.js(expr))

    def ir(self, url):
        self.cmd("Page.navigate", url=url)
        time.sleep(0.5)
        self.esperar("document.readyState === 'complete'", 15)
        self.js(AYUDAS)

    def foto(self, ruta):
        open(ruta, "wb").write(base64.b64decode(self.cmd("Page.captureScreenshot", format="png")["data"]))

    def tamano(self, ancho, alto):
        self.cmd("Emulation.setDeviceMetricsOverride", width=ancho, height=alto, deviceScaleFactor=1, mobile=False)
        time.sleep(1.0)

    def raton(self, x, y):
        self.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)

    def tecla_esc(self):
        for t in ("keyDown", "keyUp"):
            self.cmd("Input.dispatchKeyEvent", type=t, key="Escape", code="Escape", windowsVirtualKeyCode=27)

    def cerrar(self):
        try:
            self.proc.kill()
            self.proc.wait(5)
        except Exception:
            pass
        shutil.rmtree(self.perfil, ignore_errors=True)


AYUDAS = r"""
window.T = {
  q: s => document.querySelector(s),
  vis: el => { if (typeof el === "string") el = document.querySelector(el); if (!el) return false;
    const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== "hidden"; },
  clic: s => { const el = document.querySelector(s); if (!el) throw new Error("no existe " + s); el.click(); return true; },
  desborda: () => document.documentElement.scrollWidth > innerWidth + 1,
  unaLinea: s => { const els = [...document.querySelector(s).children].filter(e => T.vis(e));
    const t = els.map(e => e.getBoundingClientRect().top); return Math.max(...t) - Math.min(...t) < 14; },
  ifr: () => document.querySelector("#vivoIfr"),
  idoc: () => { try { return document.querySelector("#vivoIfr").contentDocument; } catch (e) { return null; } },
  ieval: x => document.querySelector("#vivoIfr").contentWindow.eval(x),
  var: (n, el) => getComputedStyle(el || document.documentElement).getPropertyValue(n).trim().toLowerCase(),
  bg: s => getComputedStyle(typeof s === "string" ? document.querySelector(s) : s).backgroundColor,
  marcar: (id, si) => { const i = document.querySelector(id); if (i.checked !== si) i.click(); return i.checked; },
};
true
"""


# ------------------------------------------------------------------ pruebas
def estado_servidor(url):
    with urllib.request.urlopen(url + "/api/estado", timeout=10) as r:
        return json.loads(r.read())


def equipos(E):
    o, f = E.get("osciloscopio") or {}, E.get("fuente") or {}
    osc = o.get("modo") not in (None, "error", "desconectado") and bool(o.get("estado"))
    if "BANCO" in str(o.get("idn", "")):    # banco: configuración real guardada de un ensayo, sin MXO
        osc = bool(o.get("estado"))
    fte = f.get("estado") == "conectada"
    t = E.get("temperaturas") or {}
    temp = t.get("fuente") == "tc08" or (E.get("picolog") or {}).get("estado") == "capturando"
    return {"osc": osc, "fte": fte, "temp": temp}


def huella_servidor(E):
    """Lo que estas pruebas NO deben cambiar en los equipos."""
    f, o = E.get("fuente") or {}, E.get("osciloscopio") or {}
    return {"ensayo": bool(E.get("ensayo")), "salida": bool((f.get("medida") or {}).get("salida")),
            "registro_fuente": bool(f.get("registro")), "datalogger": bool((o.get("datalogger") or {}).get("en_marcha")),
            "modo_osc": o.get("modo")}


# los textos más largos que puede tener la cabecera (se reescriben cada 50 ms porque el panel los refresca solo)
PEOR_CABECERA = """clearInterval(window.__peor); window.__peor = setInterval(() => {
  T.q('#pOsc').innerHTML = '<span class="nl">Osciloscopio</span><span class="nc">Osc.</span> · sin comunicación';
  T.q('#pFte').textContent = 'Fuente · sin comunicación'; T.q('#pPl').textContent = 'TC-08 · 8 canales · ⚠ LÍMITE';
  if (uso.fte) { T.q('#cabFte').hidden = false; T.q('#cabSal').textContent = 'ON · 12,34 V · 1,234 A'; }
  T.q('#bManual').classList.add('oculto'); T.q('#bRetomar').classList.remove('oculto'); }, 50); true"""
ESTABLE = r"C:\Users\kgaviria\Documents\Copias\version_20261104_estable\Panel_laboratorio"
PANEL = os.path.dirname(AQUI)


def como_antes_estatico():
    """Contra la versión estable de antes del rediseño (4-nov 10:45): no ha desaparecido ningún elemento de las páginas,
    ninguna llamada a la API ni ninguna ruta del servidor. Lo nuevo puede añadir, no quitar."""
    import re
    G = "Como antes (código)"
    if not os.path.isdir(ESTABLE):
        chk(G, "comparación con la versión estable", None, f"no está {ESTABLE}")
        return
    leer = lambda p: open(p, encoding="utf-8", errors="replace").read()
    for pag in ("index.html", "vivo.html"):
        a, b = leer(os.path.join(ESTABLE, "static", pag)), leer(os.path.join(PANEL, "static", pag))
        ids = lambda s: set(re.findall(r'id="([^"$]+)"', s))
        api = lambda s: set(re.findall(r'["`](/api/[A-Za-z0-9_/\-]+)', s))
        quitados = sorted(ids(a) - ids(b))
        chk(G, f"{pag}: siguen todos los elementos de antes ({len(ids(a))})", not quitados, " ".join(quitados))
        quitadas = sorted(api(a) - api(b))
        chk(G, f"{pag}: sigue usando todas las llamadas a la API de antes ({len(api(a))})", not quitadas, " ".join(quitadas))
    rutas = lambda s: set(re.findall(r'@app\.(?:get|post|put|delete)\("([^"]+)"', s))
    a, b = rutas(leer(os.path.join(ESTABLE, "servidor.py"))), rutas(leer(os.path.join(PANEL, "servidor.py")))
    chk(G, f"servidor.py: siguen todas las rutas de antes ({len(a)})", not (a - b), " ".join(sorted(a - b)))


def sin_desborde(P, grupo, donde):
    d = P.js("T.desborda()")
    chk(grupo, f"sin desborde horizontal ({donde})", not d,
        f"la página mide {P.js('document.documentElement.scrollWidth')} px de ancho y la ventana {P.js('innerWidth')}" if d else "")


def pruebas(P, url, EQ, carpeta):
    foto = lambda n: P.foto(os.path.join(carpeta, n + ".png"))
    combo = lambda: P.js("comboUso()")

    # ---------------------------------------------------------- 0 · carga
    P.ir(url + "/")
    ok = P.esperar("typeof E !== 'undefined' && !!E && typeof aplicarUso === 'function'", 20)
    chk("0", "la página carga y lee el estado del servidor", ok)
    if not ok:
        return
    time.sleep(1.5)
    foto("00_inicio")
    sin_desborde(P, "0", "al abrir, 1536×864")
    chk("0", "cabecera en una sola línea (1536 px)", P.js("T.unaLinea('header')"))

    # ---------------------------------------------------------- A1 · equipos en uso
    G = "A1 equipos en uso"
    chk(G, "tres casillas (osciloscopio, fuente, temperaturas) marcadas al empezar",
        P.js("['#usaOsc','#usaFte','#usaTemp'].every(s => T.q(s) && T.q(s).checked)"))
    P.js("T.marcar('#usaOsc', false)")
    time.sleep(0.4)
    chk(G, "sin osciloscopio: se ocultan su pantalla, sus ajustes y el selector Pantalla/Gráfica",
        P.js("document.body.classList.contains('sin-osc') && !T.vis('#cajaVista') && !T.vis('#tOsc') && !T.vis('#selPrincipal')"))
    chk(G, "sin osciloscopio: la gráfica ocupa la zona grande", P.js("T.vis('#cajaVivo')"))
    P.js("T.marcar('#usaFte', false)")
    time.sleep(0.3)
    chk(G, "sin fuente: su tarjeta y Encender/APAGAR de la cabecera no se ven", P.js("!T.vis('#cajaFte') && !T.vis('#cabFte')"))
    P.js("T.marcar('#usaTemp', false)")
    time.sleep(0.3)
    chk(G, "no deja quitar el último equipo en uso", P.js("T.q('#usaTemp').checked"))
    for s in ("#usaOsc", "#usaFte", "#usaTemp"):
        P.js(f"T.marcar('{s}', true)")
    time.sleep(0.5)
    chk(G, "se recuerda en el navegador (enUso)", P.js("JSON.parse(localStorage.getItem('enUso')).osc === true"))

    # ---------------------------------------------------------- A2 · zona principal por combinación
    G = "A2 zona principal"
    esperado = {"osc+fte+temp": "grafica", "osc": "pantalla", "osc+fte": "pantalla", "osc+temp": "pantalla",
                "fte+temp": "grafica", "fte": "grafica"}
    P.js("localStorage.removeItem('principal:osc+fte+temp')")
    for c, p in esperado.items():
        partes = c.split("+")
        for k, s in (("osc", "#usaOsc"), ("fte", "#usaFte"), ("temp", "#usaTemp")):
            if k in partes:
                P.js(f"T.marcar('{s}', true)")
        for k, s in (("osc", "#usaOsc"), ("fte", "#usaFte"), ("temp", "#usaTemp")):
            if k not in partes:
                P.js(f"T.marcar('{s}', false)")
        P.js(f"localStorage.removeItem('principal:{c}'); mostrarPrincipal(); marcarGuardar && marcarGuardar()")
        time.sleep(0.4)
        real = P.js("document.body.classList.contains('principal-grafica') ? 'grafica' : 'pantalla'")
        habitual = P.js("modoGuardarHabitual()")
        hab_esp = "ensayo" if "fte" in partes and "temp" in partes else "instante" if partes == ["osc"] else "registro"
        chk(G, f"{c}: por defecto {p}", real == p, f"sale {real}")
        chk("D Guardar", f"{c}: resalta «{hab_esp}»", habitual == hab_esp and
            P.js(f"T.q('#guardarModos button[data-g={hab_esp}]').classList.contains('recomendado')"), f"sale {habitual}")
    for s in ("#usaOsc", "#usaFte", "#usaTemp"):
        P.js(f"T.marcar('{s}', true)")
    time.sleep(0.4)
    # con los tres: gráfica dentro (vivo.html incrustado) y la fuente en la columna derecha
    ok = P.esperar("T.vis('#vivoIfr') && T.idoc() && T.idoc().readyState === 'complete' && "
                   "T.idoc().body.classList.contains('incrustado')", 10)
    chk(G, "con los tres: la gráfica En vivo dentro del panel (incrustada)", ok)
    if EQ["fte"]:
        chk(G, "con la gráfica en el centro, la fuente sube a la columna derecha", P.js("!!T.q('#cajaFte').closest('#lado')"))
    foto("A2_grafica_dentro")
    P.js("T.clic('#selPrincipal button[data-p=pantalla]')")
    time.sleep(0.6)
    chk(G, "«Pantalla osc.»: la pantalla del osciloscopio en la zona grande", P.js("T.vis('#cajaVista') && !T.vis('#cajaVivo')"))
    chk(G, "lo elegido se guarda por combinación", P.js("localStorage.getItem('principal:osc+fte+temp') === 'pantalla'"))
    P.ir(url + "/")
    P.esperar("typeof E !== 'undefined' && !!E", 15)
    time.sleep(1)
    chk(G, "y se recuerda al recargar la página", P.js("!document.body.classList.contains('principal-grafica')"))
    foto("A2_pantalla")
    sin_desborde(P, G, "pantalla en el centro")
    P.js("T.clic('#selPrincipal button[data-p=grafica]')")
    P.esperar("T.vis('#vivoIfr')", 5)

    # ---------------------------------------------------------- A3 · sacar / traer la gráfica
    G = "A3 sacar/traer"
    P.js("T.clic('#bSacarVivo')")
    ok = P.esperar("vivoFuera && vivoVentana && !vivoVentana.closed", 6)
    chk(G, "«⧉ Sacar la gráfica»: se abre la ventana emergente", ok)
    if ok:
        chk(G, "con la gráfica fuera, el panel pone la pantalla del osciloscopio en el centro",
            P.js("T.vis('#cajaVista') && !T.vis('#cajaVivo')"))
        foto("A3_grafica_fuera")
        P.esperar("vivoVentana.document.readyState === 'complete' && !!vivoVentana.document.querySelector('#bAcoplar')", 8)
        chk(G, "la emergente es vivo.html?emergente=1", P.js("vivoVentana.location.search.includes('emergente=1')"))
        chk(G, "botón «⇲ Traer la gráfica» en la barra", P.js("T.q('#bSacarVivo').textContent.includes('Traer')"))
        chk(G, "la emergente tiene «⇲ Volver al panel»",
            P.js("(() => { const b = vivoVentana.document.querySelector('#bAcoplar'); return !b.hidden && b.textContent.includes('Volver'); })()"))
        P.js("vivoVentana.document.querySelector('#bAcoplar').click()")
        ok = P.esperar("!vivoFuera && T.vis('#vivoIfr') && (!vivoVentana || vivoVentana.closed)", 6)
        chk(G, "«⇲ Volver al panel» la devuelve y cierra la emergente", ok)
    P.esperar("T.idoc() && T.idoc().querySelector('#bAcoplar') && !T.idoc().querySelector('#bAcoplar').hidden", 8)
    P.js("T.idoc().querySelector('#bAcoplar').click()")
    ok = P.esperar("vivoFuera && vivoVentana && !vivoVentana.closed", 6)
    chk(G, "«⧉ Sacar» desde la propia gráfica", ok)
    if ok:
        P.js("T.clic('#bSacarVivo')")
        chk(G, "«⇲ Traer la gráfica» de la barra la devuelve", P.esperar("!vivoFuera && T.vis('#vivoIfr')", 6))
        # sin osciloscopio la zona grande es la gráfica: estando fuera, aviso con «⇲ Traer la gráfica al panel»
        P.js("T.marcar('#usaOsc', false)")
        time.sleep(0.4)
        P.js("T.clic('#bSacarVivo')")
        P.esperar("vivoFuera && vivoVentana && !vivoVentana.closed", 6)
        chk(G, "sin osciloscopio y la gráfica fuera: aviso en la zona grande", P.esperar("T.vis('#vivoFuera') && T.vis('#bTraerVivo')", 4))
        P.js("T.clic('#bTraerVivo')")
        chk(G, "«⇲ Traer la gráfica al panel» la devuelve", P.esperar("!vivoFuera && T.vis('#vivoIfr')", 6))
        P.js("T.marcar('#usaOsc', true)")
        P.esperar("T.vis('#vivoIfr')", 5)
        P.js("T.clic('#bSacarVivo')")
        P.esperar("vivoFuera && vivoVentana && !vivoVentana.closed", 6)
        P.js("vivoVentana.close()")
        chk(G, "cerrar la emergente la devuelve sola", P.esperar("!vivoFuera && T.vis('#vivoIfr')", 6))

    # ---------------------------------------------------------- A4 · termopares
    G = "A4 termopares"
    chk(G, "tarjeta de termopares en la columna derecha", P.js("T.vis('#cajaTemp') && !!T.q('#cajaTemp').closest('#lado')"))
    if EQ["temp"]:
        n = P.js("T.q('#tablaTemp').children.length / 3")
        chk(G, "con temperaturas: una fila por termopar con su valor", n and n >= 1 and P.js("T.vis('#tablaTemp')"), f"{n} filas")
    else:
        chk(G, "sin temperaturas: «Sin medir» con «Usar el TC-08»", P.js("T.vis('#tempNada') && T.vis('#bTempTomar')"))
        chk(G, "con temperaturas reales", None, "temperaturas no conectadas (TC-08 / PicoLog)")

    # ---------------------------------------------------------- A5 · fichas del osciloscopio
    G = "A5 fichas osc."
    if EQ["osc"]:
        P.js("T.clic('#selPrincipal button[data-p=pantalla]')")
        time.sleep(0.5)
        n = P.esperar("T.q('#fichasOsc').querySelectorAll('.ficha').length === 6", 8)
        chk(G, "6 fichas (C1-C4, horizontal, disparo) con valores leídos", n,
            P.js("[...T.q('#fichasOsc').querySelectorAll('.ficha')].map(b => b.textContent.trim()).join(' | ')"))
        chk(G, "Disparo, Canales y Horizontal cerrados por defecto", P.js("!['#secDis','#secCan','#secHor'].some(s => T.q(s).open)"))
        P.js("T.q('#fichasOsc .ficha[data-c=\"1\"]').click()")
        time.sleep(0.4)
        chk(G, "la ficha C1 abre Canales solo con C1",
            P.js("T.q('#secCan').open && [...document.querySelectorAll('#secCan .canal')].filter(c => !c.hidden).map(c => c.id).join() === 'canal1'"))
        chk(G, "la ficha abierta queda marcada", P.js("T.q('#fichasOsc .ficha[data-c=\"1\"]').classList.contains('abierta')"))
        foto("A5_ficha_C1")
        P.js("T.q('#fichasOsc .ficha[data-sec=secHor]').click()")
        time.sleep(0.3)
        chk(G, "otra ficha cierra la anterior", P.js("T.q('#secHor').open && !T.q('#secCan').open"))
        P.js("T.q('#fichasOsc .ficha[data-sec=secHor]').click()")
        time.sleep(0.3)
        chk(G, "un segundo toque la cierra", P.js("!T.q('#secHor').open"))
        P.js("T.clic('#selPrincipal button[data-p=grafica]')")
    else:
        chk(G, "fichas con el osciloscopio", None, "osciloscopio no conectado")

    # ---------------------------------------------------------- B · cabecera y barra de estado
    G = "B cabecera/estado"
    if EQ["fte"]:
        chk(G, "Encender/APAGAR fijos en la cabecera con la fuente conectada", P.js("T.vis('#cabFte') && T.vis('#cabOn') && T.vis('#cabOff')"))
        sal = P.js("!!(E.fuente.medida || {}).salida")
        txt = P.js("T.q('#cabSal').textContent")
        chk(G, "la cabecera dice el estado real de la salida", txt.startswith("ON") == sal, txt)
        chk(G, "Encender/APAGAR de la cabecera con el mismo estado que los de la tarjeta",
            P.js("T.q('#cabOn').disabled === T.q('#bOn').disabled && T.q('#cabOff').disabled === T.q('#bOff').disabled"))
        r = P.js("(() => { const r = T.q('#cabOff').getBoundingClientRect(); return [r.top, r.bottom, innerHeight]; })()")
        chk(G, "APAGAR a la vista sin desplazar", r[0] >= 0 and r[1] <= r[2], r)
    else:
        chk(G, "sin fuente conectada: la cabecera no muestra Encender/APAGAR", not P.js("T.vis('#cabFte')"))
        chk(G, "Encender/APAGAR de la cabecera con la fuente", None, "fuente no conectada")
    # las píldoras dicen el estado real de cada equipo (el que da el servidor), entero
    eo = {"conectado": "listo", "desconectado": "listo", "manual": "modo manual", "datalogger": "registrando",
          "error": "sin comunicación"}.get(P.js("E.osciloscopio.modo"), P.js("E.osciloscopio.modo"))
    tos = P.js("T.q('#pOsc').innerText")
    chk(G, "la píldora del osciloscopio dice su estado real", tos.endswith(eo), f"«{tos}», el servidor dice {P.js('E.osciloscopio.modo')}")
    ef = P.js("E.fuente.estado")
    eft = {"conectada": "SIMULADA" if P.js("!!E.fuente.simulada") else "conectada", "desconectada": "desconectada",
           "sin_comunicacion": "sin comunicación"}.get(ef, ef)
    tfs = P.js("T.q('#pFte').innerText")
    chk(G, "la píldora de la fuente dice su estado real", tfs.endswith(eft), f"«{tfs}», el servidor dice {ef}")
    corta = P.js("[...document.querySelectorAll('header .pill')].filter(p => T.vis(p) && p.scrollWidth > p.clientWidth + 1).map(p => p.id).join(' ')")
    chk(G, "las píldoras se leen enteras (sin cortar)", not corta, corta)
    barra = P.js("T.q('#estadoBarra').textContent")
    chk(G, "barra de estado con el osciloscopio y la fuente", "Osciloscopio" in barra and "Fuente" in barra, barra)
    if EQ["osc"] and not EQ.get("banco"):   # en el banco el MXO es de mentira y la lectura falla
        chk(G, "la barra dice que el osciloscopio responde", "sin comunicación" not in barra.split("Fuente")[0], barra)

    # ---------------------------------------------------------- D · Guardar y ensayo en tres pasos
    G = "D Guardar"
    chk(G, "«Guardar: Instante · ✂ Tramo · ● Registro · ▶ Ensayo»",
        P.js("[...document.querySelectorAll('#guardarModos button')].map(b => b.dataset.g).join() === 'instante,tramo,registro,ensayo'"))
    chk(G, "los botones antiguos quedan ocultos (Capturar…, Registrar…, En vivo ↗, Ensayo…)",
        P.js("!['#bCapturar','#bIrDl','#bVivo','#bEnsayo'].some(s => T.vis(s))"))
    for g, panel, titulo in (("instante", "#latCaptura", "Capturar"), ("registro", "#latDl", "Registrar")):
        P.js(f"T.clic('#guardarModos button[data-g={g}]')")
        ok = P.esperar(f"T.vis('#lateral') && T.vis('{panel}')", 5)
        chk(G, f"«{g}» abre el panel de {titulo}", ok, P.js("T.q('#latTitulo').textContent"))
        if ok and EQ["fte"] and P.js("T.vis('#cajaFte')"):
            chk(G, f"el panel de {titulo} no tapa la tarjeta de la fuente", P.js(
                "(() => { const a = T.q('#lateral').getBoundingClientRect(), b = T.q('#cajaFte').getBoundingClientRect();"
                " return a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top; })()"))
        P.js("T.clic('#bLatCerrar')")
        time.sleep(0.3)
    P.js("T.clic('#guardarModos button[data-g=ensayo]')")
    ok = P.esperar("T.vis('#latEnsayo')", 5)
    chk(G, "«▶ Ensayo» abre el ensayo", ok)
    if ok:
        vis = "[1,2,3].map(n => T.vis(document.querySelector('#latEnsayo .paso[data-paso=\"' + n + '\"]')))"
        chk(G, "ensayo: empieza en el paso 1 (Qué grabar)", P.js(vis) == [True, False, False] and P.js("T.q('#bEnsAnt').disabled"))
        P.js("T.clic('#bEnsSig')")
        time.sleep(0.3)
        chk(G, "Siguiente → paso 2 (La fuente)", P.js(vis) == [False, True, False])
        P.js("T.clic('#bEnsSig')")
        time.sleep(0.4)
        chk(G, "Siguiente → paso 3 (Dónde y cómo se llama), sin «Siguiente»",
            P.js(vis) == [False, False, True] and not P.js("T.vis('#bEnsSig')"))
        res = P.js("T.q('#ensResumen').textContent")
        chk(G, "paso 3: resumen con lo que se graba, la duración y la fuente", "Graba" in res and "Fuente" in res, res[:160])
        chk(G, "paso 3: «▶ Iniciar ensayo» a la vista (no se pulsa)", P.js("T.vis('#bEnsIni')"))
        foto("D_ensayo_paso3")
        P.js("T.clic('#bEnsAnt')")
        time.sleep(0.2)
        chk(G, "Anterior → paso 2", P.js(vis) == [False, True, False])
        P.js("T.clic('#ensPasos button[data-paso=\"1\"]')")
        time.sleep(0.2)
        chk(G, "los pasos se pueden tocar (→ paso 1)", P.js(vis) == [True, False, False])
        P.js("T.clic('#bLatCerrar')")
        time.sleep(0.3)
    # ✂ Tramo desde Guardar
    P.js("T.clic('#selPrincipal button[data-p=pantalla]')")
    time.sleep(0.4)
    P.js("T.clic('#guardarModos button[data-g=tramo]')")
    ok = P.esperar("document.body.classList.contains('principal-grafica') && T.idoc() && "
                   "T.idoc().querySelector('#bTramo') && T.idoc().querySelector('#bTramo').classList.contains('activo')", 10)
    chk(G, "«✂ Tramo» pone la gráfica y activa marcar", ok)
    if ok:
        foto("D_tramo_marcando")
        P.js("T.idoc().querySelector('#bTramo').click()")
        chk(G, "y se puede desactivar", P.esperar("!T.idoc().querySelector('#bTramo').classList.contains('activo')", 4))

    # ---------------------------------------------------------- E · espacios de trabajo y Solo mirar
    G = "E espacios/solo mirar"
    P.js("T.clic('#bMas')")
    time.sleep(0.3)
    chk(G, "«Espacios de trabajo» en el menú ⋯", P.js("T.vis('#espacios')"))
    P.js("T.q('#espNombre').value = 'prueba automática'; T.clic('#espGuardar')")
    time.sleep(0.3)
    chk(G, "guardar el espacio actual con nombre", P.js("'prueba automática' in JSON.parse(localStorage.getItem('espacios') || '{}')"))
    P.js("T.marcar('#usaTemp', false); T.clic('#selPrincipal button[data-p=pantalla]')")
    time.sleep(0.4)
    P.js("T.q('#espSel').value = 'prueba automática'; T.clic('#espUsar')")
    time.sleep(0.5)
    chk(G, "usar el espacio recupera los equipos y la zona grande",
        P.js("T.q('#usaTemp').checked && document.body.classList.contains('principal-grafica')"), P.js("comboUso()"))
    P.js("T.clic('#bMas'); T.q('#espSel').value = 'prueba automática'; T.clic('#espBorrar')")
    time.sleep(0.3)
    chk(G, "borrar el espacio", P.js("!('prueba automática' in JSON.parse(localStorage.getItem('espacios') || '{}'))"))
    P.js("T.q('#menuMas').classList.add('oculto')")
    for zona in ("grafica", "pantalla") if EQ["osc"] else ("grafica",):
        P.js(f"T.clic('#selPrincipal button[data-p={zona}]')")
        time.sleep(0.5)
        P.js("T.clic('#bSoloMirar')")
        time.sleep(0.8)
        caja = "#cajaVivo" if zona == "grafica" else "#cajaVista"
        r = P.js(f"(() => {{ const a = T.q('{caja}').getBoundingClientRect(); return [Math.round(a.width), innerWidth]; }})()")
        chk(G, f"«⛶ Solo mirar» ({zona}): la zona grande a todo el ancho, sin la columna derecha",
            P.js("document.body.classList.contains('solo-mirar') && !T.vis('#lado')") and r[0] >= 0.85 * r[1], f"{r[0]} de {r[1]} px")
        chk(G, f"«Solo mirar» ({zona}): cabecera y barra de estado a la vista",
            P.js("T.vis('header') && T.vis('#estadoBarra')") and (not EQ["fte"] or P.js("T.vis('#cabOff')")))
        foto(f"E_solo_mirar_{zona}")
        sin_desborde(P, G, f"Solo mirar, {zona}")
        P.js("document.activeElement && document.activeElement.blur(); window.focus()")
        P.tecla_esc()
        time.sleep(0.5)
        chk(G, f"Esc sale de «Solo mirar» ({zona})", P.js("!document.body.classList.contains('solo-mirar') && T.vis('#lado')"))
        if zona == "grafica":                # después de tocar la gráfica (el foco queda dentro de su marco)
            P.js("document.body.classList.contains('solo-mirar') || T.clic('#bSoloMirar')")
            time.sleep(0.6)
            xy = P.js("(() => { const f = T.ifr().getBoundingClientRect(); return [f.left + f.width / 2, f.top + f.height / 2]; })()")
            for t in ("mousePressed", "mouseReleased"):
                P.cmd("Input.dispatchMouseEvent", type=t, x=xy[0], y=xy[1], button="left", clickCount=1)
            time.sleep(0.3)
            P.tecla_esc()
            time.sleep(0.5)
            sale = P.js("!document.body.classList.contains('solo-mirar')")
            chk(G, "Esc sale de «Solo mirar» también después de tocar la gráfica", sale,
                "" if sale else "Esc se queda dentro del marco de la gráfica (vivo.html no se lo pasa al panel)")
            if P.js("document.body.classList.contains('solo-mirar')"):
                P.js("T.clic('#bSoloMirar')")
                time.sleep(0.4)
    P.js("T.clic('#selPrincipal button[data-p=grafica]')")
    P.esperar("T.vis('#vivoIfr') && T.idoc() && T.idoc().readyState === 'complete'", 8)

    # ---------------------------------------------------------- colores y aspecto B
    G = "Colores/aspecto"
    esperados = {"light": {"--acento": "#2b3440", "--c1": "#b38600", "--c4": "#2f69d9", "--fuente": "#6250d6", "marco": "rgb(35, 42, 51)"},
                 "dark": {"--acento": "#e3e8ef", "--c1": "#b38b00", "--c4": "#5a86ec", "--fuente": "#9085e9", "marco": "rgb(13, 16, 21)"}}
    for tema, ex in esperados.items():
        for _ in range(4):
            if P.js("document.documentElement.dataset.theme || 'auto'") == tema:
                break
            P.js("T.clic('#bTema')")
            time.sleep(0.3)
        chk(G, f"«⋯ → Tema» llega a {tema}", P.js("document.documentElement.dataset.theme") == tema)
        time.sleep(0.8)
        mal = {k: P.js(f"T.var('{k}')") for k in ("--acento", "--c1", "--c4", "--fuente") if P.js(f"T.var('{k}')") != ex[k]}
        chk(G, f"{tema}: grafito, canales y fuente con los colores elegidos", not mal, mal or "")
        chk(G, f"{tema}: cabecera en grafito oscuro (aspecto B)", P.js("T.bg('header')") == ex["marco"], P.js("T.bg('header')"))
        if P.js("!!T.idoc() && !!T.idoc().querySelector('#zona')"):
            z = P.js("getComputedStyle(T.idoc().querySelector('#zona')).backgroundColor")
            chk(G, f"{tema}: la gráfica sobre negro", z == "rgb(11, 15, 20)", z)
            it = P.js("T.var('--acento', T.idoc().documentElement)")
            chk(G, f"{tema}: la gráfica de dentro cambia de tema al momento", it == ex["--acento"], it, nivel="AVISO")
        foto(f"C_tema_{tema}")
    for _ in range(4):                       # dejar el tema en automático
        if not P.js("document.documentElement.dataset.theme"):
            break
        P.js("T.clic('#bTema')")
        time.sleep(0.2)

    # ---------------------------------------------------------- lectura del ratón en En vivo
    G = "Lectura ratón"
    hay = P.js("(() => { try { return T.ieval(\"(D.t.length > 0) || Object.values(O.datos).some(s => s.t.length > 0)\"); } catch (e) { return false; } })()")
    if hay:
        P.js("T.idoc().querySelector('#bMostrar').click()")
        time.sleep(0.4)
        chk(G, "«Mostrar ▾» tiene «En la lectura del ratón»", P.js("T.idoc().querySelector('#mostrar').textContent.includes('En la lectura del ratón')"))
        P.js("T.idoc().querySelector('#bMostrar').click()")
        xy = P.js("(() => { const f = T.ifr().getBoundingClientRect(), c = T.idoc().querySelector('#cv').getBoundingClientRect();"
                  " return [f.left + c.left + c.width * 0.6, f.top + c.top + c.height * 0.4, c.width]; })()")
        ok = False
        for fx, fy in ((0.6, 0), (0.5, 40), (0.75, -40), (0.35, 80), (0.9, 0)):   # varios puntos (fuera de las leyendas)
            P.raton(xy[0] + (fx - 0.6) * xy[2] - 5, xy[1] + fy)
            P.raton(xy[0] + (fx - 0.6) * xy[2], xy[1] + fy)
            ok = P.esperar("getComputedStyle(T.idoc().querySelector('#lectura')).display === 'block'", 2)
            if ok:
                break
        chk(G, "al pasar el ratón sale la lectura con un cuadradito del color de cada curva",
            ok and P.js("T.idoc().querySelectorAll('#lectura i.mc').length > 0"),
            P.js("T.idoc().querySelector('#lectura').textContent.slice(0, 160)"))
        if EQ["temp"]:
            chk(G, "la lectura incluye las temperaturas", "°C" in P.js("T.idoc().querySelector('#lectura').textContent"))
        foto("L_lectura_raton")
    else:
        chk(G, "lectura del ratón", None, "la gráfica no tiene datos todavía")

    # ---------------------------------------------------------- como antes: lo de antes del rediseño sigue igual
    G = "Como antes"
    P.js("T.clic('#selPrincipal button[data-p=pantalla]')")
    time.sleep(0.5)
    if EQ["osc"]:
        for img in ("botones", "web", "pantalla"):
            b = P.js(f"(() => {{ const b = T.q('[data-img={img}]'); if (!b) return null; b.click(); return true; }})()")
            time.sleep(0.6)
            chk(G, f"vista del osciloscopio «{img}» sigue funcionando",
                b and P.js(f"T.q('[data-img={img}]').classList.contains('activo') && T.vis('#cajaVista')"))
    chk(G, "«Leer equipo» y «leído hace…» siguen", P.js("T.vis('#bLeer') && !!T.q('#oscHora')") if EQ["osc"] else None,
        "" if EQ["osc"] else "osciloscopio no conectado")
    if EQ["osc"]:                            # cada sección del osciloscopio, abierta, con sus controles de siempre
        for sec, que in (("#secDis", "disparo"), ("#secCan", "canales"), ("#secHor", "horizontal")):
            P.js(f"T.q('{sec}').dataset.canal = ''; T.q('{sec}').open = true; filtrarCanales()")
            time.sleep(0.3)
            n = P.js(f"[...T.q('{sec}').querySelectorAll('input, select, button')].filter(e => T.vis(e)).length")
            chk(G, f"«{que}» abierta: sus controles se ven", n and n >= 3, f"{n} controles")
            if sec == "#secCan":
                chk(G, "«canales» abierta sin ficha: salen los 4 canales",
                    P.js("[...document.querySelectorAll('#secCan .canal')].filter(c => T.vis(c)).length") == 4)
            P.js(f"T.q('{sec}').open = false")
    if EQ["fte"]:
        chk(G, "tarjeta de la fuente: V, I, P, Fijar, Encender, APAGAR y sus desplegables",
            P.js("['#bFijar', '#bOn', '#bOff'].every(s => T.vis(s)) && T.q('#cajaFte').querySelectorAll('input').length >= 3 && "
                 "T.q('#cajaFte').querySelectorAll('details').length >= 1"))
    P.js("T.clic('#bHist')")
    time.sleep(0.4)
    chk(G, "el historial se abre (cajón abajo)", P.js("T.vis('#histCuerpo')"))
    P.js("T.clic('#bHist')")
    time.sleep(0.3)
    P.js("T.clic('#bMas')")
    time.sleep(0.3)
    chk(G, "menú ⋯: Solo panel, Tema y Cerrar panel siguen (no se pulsan)", P.js("['#bSoloPanel', '#bTema', '#bCerrar'].every(s => T.vis(s))"))
    P.js("T.q('#menuMas').classList.add('oculto')")
    if EQ["osc"]:
        chk(G, "Banco de trabajo · Control R&S completo y Modo manual siguen", P.js(
            "T.vis('#modosVista [data-modo=dividido]') && T.vis('#modosVista [data-modo=completa]') && T.vis('#bManual')"))
        P.js("T.clic('#modosVista [data-modo=completa]')")
        ok = P.esperar("document.body.classList.contains('rs-completo') && T.vis('#cajaVista')", 8)
        chk(G, "Control R&S completo: la web de R&S a todo el ancho (el panel pasa a modo manual)", ok)
        foto("X_control_rs_completo")
        P.js("T.clic('#modosVista [data-modo=dividido]')")
        ok = P.esperar("!document.body.classList.contains('rs-completo')", 8)
        time.sleep(1.5)
        if not EQ.get("banco"):
            ok = ok and P.esperar("E.osciloscopio.modo !== 'manual'", 10)
        chk(G, "volver a Banco de trabajo (el panel retoma el control)", ok, P.js("E.osciloscopio.modo"))
    # En vivo y Temperaturas en su ventana, como antes
    P.js("window.__vv = window.open('/static/vivo.html', 'vv_prueba')")
    ok = P.esperar("__vv.document.readyState === 'complete' && !!__vv.document.querySelector('#zona')", 10)
    time.sleep(1)
    chk(G, "En vivo suelta (/static/vivo.html): carga con su gráfica", ok)
    if ok:
        falta = P.js("['#bEncender', '#bApagar', '#bMostrar', '#bTramo', '#bSeguir'].filter(s => { const e = __vv.document.querySelector(s);"
                     " return !(e && e.getClientRects().length > 0); }).join(' ')")
        chk(G, "En vivo suelta: Encender/APAGAR, Seguir, Mostrar y ✂ Tramo", not falta, f"no se ven: {falta}" if falta else "")
        chk(G, "En vivo suelta: sin «⧉ Sacar» (eso es solo dentro del panel)", P.js("__vv.document.querySelector('#bAcoplar').hidden"))
    P.js("__vv.close(); window.__vt = window.open('/static/vivo.html?solo=temp', 'vt_prueba')")
    ok = P.esperar("__vt.document.readyState === 'complete' && !!__vt.document.querySelector('#zona')", 10)
    chk(G, "ventana de Temperaturas (Configurar ↗) carga", ok)
    P.js("__vt.close()")
    P.js("T.clic('#selPrincipal button[data-p=grafica]')")
    time.sleep(0.5)

    # ---------------------------------------------------------- tamaños de ventana
    G = "Tamaños"
    for w, h in ((1920, 1080), (1536, 864), (1366, 768), (1280, 720), (1100, 700)):
        P.tamano(w, h)
        chk(G, f"{w}×{h}: cabecera en una línea con los estados de los equipos", P.js("T.unaLinea('header')"))
        # el peor caso: los dos equipos sin comunicación, temperaturas con alarma, salida ON con valores y modo manual
        P.js(PEOR_CABECERA)
        time.sleep(0.4)
        una = P.js("T.unaLinea('header')")
        corta = P.js("[...document.querySelectorAll('header .pill')].filter(p => T.vis(p) && p.scrollWidth > p.clientWidth + 1).length")
        chk(G, f"{w}×{h}: cabecera en una línea también en el peor caso (sin comunicación, alarma, salida ON, modo manual)",
            una and not corta, "" if una else "se parte en 2 líneas (no se pierde nada)", nivel="FALLO" if w >= 1280 else "AVISO")
        if w == 1536:
            foto("T_cabecera_peor_1536")
        P.js("clearInterval(window.__peor); refrescar()")
        time.sleep(0.6)
        for zona in ("grafica", "pantalla") if EQ["osc"] else ("grafica",):
            P.js(f"T.clic('#selPrincipal button[data-p={zona}]')")
            time.sleep(0.6)
            sin_desborde(P, G, f"{w}×{h}, {zona}")
            if EQ["fte"]:
                r = P.js("(() => { const r = T.q('#cabOff').getBoundingClientRect(); return r.width > 0 && r.top >= 0 && r.bottom <= innerHeight; })()")
                chk(G, f"{w}×{h}, {zona}: APAGAR a la vista", r)
        foto(f"T_{w}x{h}")
    P.cmd("Emulation.clearDeviceMetricsOverride")


def main():
    ap = argparse.ArgumentParser(description="Validación automática del rediseño v4 del panel")
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    ap.add_argument("--oculto", action="store_true", help="navegador oculto (más rápido, pero frena el dibujo de la gráfica: la lectura del ratón puede fallar)")
    ap.add_argument("--informe")
    ap.add_argument("--navegador", default=next((n for n in NAVEGADORES if os.path.exists(n)), None))
    a = ap.parse_args()
    url = a.url.rstrip("/")
    try:
        E0 = estado_servidor(url)
    except Exception as e:
        sys.exit(f"El panel no responde en {url} ({e}). Arráncalo con arrancar_panel.bat")
    EQ = equipos(E0)
    EQ["banco"] = "BANCO" in str((E0.get("osciloscopio") or {}).get("idn", ""))
    print("Equipos que ve el panel:", {"osciloscopio": EQ["osc"], "fuente": EQ["fte"], "temperaturas": EQ["temp"]}, flush=True)
    carpeta = a.informe or os.path.join(AQUI, "informes", time.strftime("rediseno_%Y%m%d_%H%M"))
    os.makedirs(carpeta, exist_ok=True)
    como_antes_estatico()
    P = Pagina(a.navegador, 1536, 864, not a.oculto)
    try:
        P.tamano(1536, 864)
        pruebas(P, url, EQ, carpeta)
    except Exception as e:
        chk("!", "la prueba se interrumpió", False, f"{type(e).__name__}: {e}")
        try:
            P.foto(os.path.join(carpeta, "ZZ_error.png"))
        except Exception:
            pass
    finally:
        errores = list(dict.fromkeys(P.errores))
        P.cerrar()
    chk("0", "sin errores de JavaScript en la página", not errores, " | ".join(errores)[:600])
    E1 = estado_servidor(url)
    antes, despues = huella_servidor(E0), huella_servidor(E1)
    if "BANCO" in str((E0.get("osciloscopio") or {}).get("idn", "")):   # banco: el MXO es de mentira, la lectura da error
        antes.pop("modo_osc"), despues.pop("modo_osc")
    chk("0", "los equipos siguen igual (salida, registros, ensayo, modo del osciloscopio)", antes == despues,
        "" if antes == despues else f"antes {antes} · después {despues}")
    cuenta = {k: sum(1 for r in RES if r[0] == k) for k in ("OK", "FALLO", "AVISO", "OMITIDA")}
    cab = (f"Validación del rediseño {time.strftime('%Y-%m-%d %H:%M')} · {url} · equipos {EQ}\n"
           f"{cuenta['OK']} OK · {cuenta['FALLO']} FALLO · {cuenta['AVISO']} AVISO · {cuenta['OMITIDA']} OMITIDA")
    txt = cab + "\n" + "\n".join(f"{e:8}[{g}] {n}" + (f"  [{d}]" if d else "") for e, g, n, d in RES)
    with open(os.path.join(carpeta, "informe.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print("\n" + cab + f"\nInforme y capturas: {carpeta}", flush=True)
    sys.exit(1 if cuenta["FALLO"] else 0)


if __name__ == "__main__":
    main()
