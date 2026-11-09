"""Panel de control de laboratorio: osciloscopio R&S MXO34 + fuente EA-PS 9500-20.

Arranque:  python servidor.py      (o arrancar_panel.bat)  ->  http://127.0.0.1:8765
Solo escucha en local (127.0.0.1): no es accesible desde otros PCs.

Cada peticion que cambia algo indica su autor con la cabecera X-Autor
("usuario" desde el navegador, "claude" cuando lo hace Claude). Todo queda
en el registro de cambios (registro/cambios_AAAAMMDD.jsonl).
"""
import json
import os
import threading
import time
import webbrowser

import uvicorn
from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import almacen
from ensayo import Ensayos
from fuente import COLA_APAGADO_S, Fuente, puertos
from osciloscopio import Osciloscopio
from picolog import PicoLog
from tc08 import TC08, config_por_defecto
from cdaq import CDAQ, config_por_defecto as cdaq_config_por_defecto
from siglent import Siglent, config_por_defecto as spd_config_por_defecto
from registro import Registro
from detalle_osc import DetalleOsc
from tramo import Tramos

AQUI = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(AQUI, "config.json")
cfg = json.load(open(CONFIG, encoding="utf-8"))

reg = Registro()
detalle = DetalleOsc()             # puntos reales del ultimo registro del osciloscopio (En vivo)
osc = Osciloscopio(cfg["osciloscopio"]["host"], reg)
# IP por DHCP: si el nombre .local no resuelve, se busca por mDNS, por la MAC (ARP) y por la ultima IP buena
osc.mac = cfg["osciloscopio"].get("mac")
osc._ip_conocida = osc._ip_conocida or cfg["osciloscopio"].get("ip_respaldo")
osc.iniciar_sincronizacion(cfg["osciloscopio"].get("sincronizar_s", 1.0))
# Por defecto NO se lee cada segundo: la pagina pide una lectura al volver al panel y tras cada
# ajuste. La lectura continua queda como opcion (interruptor de la pagina o config.json).
osc.sincronizar = bool(cfg["osciloscopio"].get("sincronizar_continuo", False))
osc.ajuste_pantalla = bool(cfg["osciloscopio"].get("ajuste_pantalla", False))
fte = Fuente(reg, cfg["fuente"]["limites"], cfg["fuente"].get("periodo_s", 0.5), cfg["fuente"].get("bits_alarma"))
fte.vigilante_s = cfg["fuente"].get("vigilante_s", fte.vigilante_s)
fte.apagar_si_corte = cfg["fuente"].get("apagar_si_corte", False)
ensayos = Ensayos(osc, fte, reg)
osc.limpiar_vistas()               # temporales de vistas en vivo de sesiones anteriores
pl = PicoLog(reg)                  # temperaturas: lee lo que va guardando PicoLog (que sigue mandando)
cfg.setdefault("temperaturas", config_por_defecto())


def guardar_cfg():
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


tc = TC08(reg, cfg["temperaturas"], guardar_cfg)   # TC-08 manejado por el panel (si lo toma)
# cDAQ-9178 (NI 9213 + NI 9201), validado con el hardware el 9-nov-2026. Con «habilitado»: false no carga nidaqmx
daq = CDAQ(reg, cfg.setdefault("cdaq", cdaq_config_por_defecto()), guardar_cfg)
# Siglent SPD3303X (9-nov-2026): segunda fuente, equipo aparte (siglent.py; modelo validado en siglent_modelo.py)
spd = Siglent(reg, cfg.setdefault("siglent", spd_config_por_defecto()), guardar_cfg)


class _Temperaturas:
    """La fuente de temperaturas en uso: el TC-08 si lo tiene el panel; si no, lo que vaya capturando PicoLog.
    El cDAQ NO entra aquí (9-nov, pedido del usuario): puede medir a la vez que PicoLog y es un equipo aparte, con
    su historia (/api/cdaq/historia), su cdaq.csv en Registro, Ensayo y Tramo, y su sitio propio en En vivo."""
    def activa(self):
        return tc if (tc.estado == "midiendo" or tc.grab) else pl

    def nombre(self):
        return "tc08" if self.activa() is tc else "picolog"

    def __getattr__(self, nombre):
        return getattr(self.activa(), nombre)


temps = _Temperaturas()
osc.no_borrar = set()              # temporales de la vista con un ✂ Tramo pendiente de recortar
tramos = Tramos(osc, fte, temps, reg)
tramos.daq = daq                   # 9-nov: el ✂ Tramo guarda también el cdaq.csv si el cDAQ mide
tramos.spd = spd                   # 9-nov: y el siglent.csv si la Siglent está conectada
ensayos.pl = temps
ensayos.tc = tc
ensayos.daq = daq                  # 9-nov: el cDAQ, equipo aparte (su cdaq.csv en el ensayo)
ensayos.spd = spd                  # 9-nov: la Siglent, segunda fuente (su siglent.csv en el ensayo)

app = FastAPI(title="Panel de laboratorio")
app.mount("/static", StaticFiles(directory=os.path.join(AQUI, "static")), name="static")


@app.middleware("http")
async def exigir_autor(request, call_next):
    # Toda orden (POST) debe llevar la cabecera X-Autor. Un navegador no deja que OTRA pagina web
    # mande esa cabecera a 127.0.0.1 sin permiso (CORS), asi una web ajena no puede manejar los equipos.
    if request.method == "POST" and not request.headers.get("x-autor"):
        return JSONResponse(status_code=403, content={"detail": "Falta la cabecera X-Autor"})
    respuesta = await call_next(request)
    # que el navegador no guarde la pagina: si no, tras una mejora se seguia viendo la version vieja
    if request.url.path == "/" or request.url.path.startswith("/static"):
        respuesta.headers["Cache-Control"] = "no-store"
    return respuesta


def autor_de(x_autor):
    return (x_autor or "usuario").strip()[:40]


def ejecutar(fn):
    try:
        return fn()
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.exception_handler(Exception)
async def _error(_, exc):
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


@app.get("/")
def inicio():
    return FileResponse(os.path.join(AQUI, "static", "index.html"))


# ------------------------------------------------------------------- general
@app.get("/api/estado")
def estado():
    """Todo el estado conocido, sin tocar los equipos (no bloquea el osciloscopio)."""
    return {"osciloscopio": osc.resumen(), "fuente": fte.resumen(), "ensayo": ensayos.resumen(),
            "preparado": ensayos.preparado, "picolog": pl.resumen(), "tc08": tc.resumen(),
            "temperaturas": {"fuente": temps.nombre(), "grabando": REG_TEMP["ruta"]},
            "cdaq": dict(daq.resumen(), grabando=REG_CDAQ["ruta"]),
            "siglent": dict(spd.resumen(), grabando=REG_SPD["ruta"]),
            "registro": reg.ultimos(60)}


@app.get("/api/registro")
def registro(n: int = 200):
    return reg.ultimos(n)


CARPETA_CONTEXTO = cfg.get("carpeta_contexto", r"C:\Proyectos\Osciloscopio\Contexto Claude")


@app.get("/api/contexto")
def contexto():
    """Ficha para Claude: como usar el panel y las normas de cada equipo (en la carpeta Contexto Claude)."""
    return FileResponse(os.path.join(CARPETA_CONTEXTO, "http", "CONTEXTO_CLAUDE.md"), media_type="text/markdown")


# ------------------------------------------------------------------- osciloscopio
@app.post("/api/osc/leer")
def osc_leer(x_autor: str = Header(None)):
    return ejecutar(lambda: osc.leer(autor_de(x_autor)))


@app.post("/api/osc/aplicar")
def osc_aplicar(p: dict = Body(...), x_autor: str = Header(None)):
    """Varios cambios a la vez. Recomendado: {"cambios": {...}, "base": <estado de tu ultima lectura>};
    si el equipo difiere de 'base' no se aplica nada. Tambien acepta los cambios sueltos (sin 'base')."""
    if "cambios" in p:
        return ejecutar(lambda: osc.aplicar(p["cambios"], autor_de(x_autor), p.get("base")))
    return ejecutar(lambda: osc.aplicar(p, autor_de(x_autor)))


@app.post("/api/osc/sincronizacion")
def osc_sincronizacion(p: dict = Body(...), x_autor: str = Header(None)):
    """{"activa": false} pausa la lectura periodica (p.ej. durante una captura); true la reanuda."""
    osc.sincronizar = bool(p["activa"])
    reg.anotar(autor_de(x_autor), "osciloscopio", "sincronización " + ("activada" if osc.sincronizar else "pausada"))
    return {"sincronizar": osc.sincronizar}


@app.post("/api/osc/ajuste_pantalla")
def osc_ajuste_pantalla(p: dict = Body(...), x_autor: str = Header(None)):
    """{"activo": true|false}: sample rate y record length acoplados para llenar justo la pantalla."""
    r = ejecutar(lambda: osc.poner_ajuste_pantalla(p["activo"], autor_de(x_autor)))
    cfg["osciloscopio"]["ajuste_pantalla"] = osc.ajuste_pantalla
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return r


@app.post("/api/osc/registro")
def osc_registro():
    """Mide (bajo demanda) donde empieza y acaba el registro respecto al disparo. Hasta ~0,5 s en Roll."""
    return ejecutar(osc.medir_registro)


@app.post("/api/osc/pausa_vista")
def osc_pausa_vista():
    """Latido de la pagina mientras el usuario usa la vista en directo: pausa la lectura 3 s."""
    osc.pausar_por_vista()
    return {"ok": True}


@app.post("/api/osc/campo")
def osc_campo(p: dict = Body(...), x_autor: str = Header(None)):
    """Ajuste inmediato de un campo: {"clave": "canales.1.escala_V_div", "esperado": 0.5, "valor": 1}."""
    def hacer():
        # flechas (web: true): por el frontal virtual de la web, sin pasar a remoto; si no se puede, por SCPI
        if p.get("web"):
            r = osc.ajustar_web(p["clave"], p.get("esperado"), p["valor"], autor_de(x_autor))
            if r is not None:
                return r
        return osc.aplicar_campo(p["clave"], p.get("esperado"), p["valor"], autor_de(x_autor),
                                 bool(p.get("confirmar_50ohm")))
    return ejecutar(hacer)


@app.post("/api/osc/accion/{accion}")
def osc_accion(accion: str, x_autor: str = Header(None)):
    if accion not in ("run", "stop", "single"):
        raise HTTPException(404, "accion desconocida")
    return ejecutar(lambda: osc.accion(accion, autor_de(x_autor)))


@app.post("/api/osc/manual")
def osc_manual(x_autor: str = Header(None)):
    """Modo manual: cierra la conexion y el panel (y Claude) dejan de enviar nada al osciloscopio."""
    return ejecutar(lambda: osc.forzar_manual(autor_de(x_autor)))


@app.post("/api/osc/retomar")
def osc_retomar(x_autor: str = Header(None)):
    return ejecutar(lambda: osc.retomar(autor_de(x_autor)))


# Registro con osciloscopio y «apagar al terminar»: a la hora prevista (o con Parar) se apaga primero la salida y
# osciloscopio y fuente siguen grabando COLA_APAGADO_S, para que los dos recojan el apagado (2026-11-02: antes el
# datalogger acababa y luego se apagaba, y el osciloscopio no lo veia)
_apagado_dl = {}


def _apagar_registro(motivo):
    if _apagado_dl.get("off"):
        return False
    _apagado_dl["off"] = True
    if fte.estado == "conectada" and fte.medida.get("salida"):
        try:
            fte.salida(False, True, "panel (automático)")
            reg.anotar("panel (automático)", "fuente", f"salida OFF al terminar el registro ({motivo})",
                       f"se sigue grabando {_apagado_dl.get('cola', COLA_APAGADO_S):g} s")
        except Exception as e:
            reg.anotar("panel (automático)", "fuente", "NO SE PUDO APAGAR al terminar el registro", f"{e}: apagar a mano")
    return True


@app.post("/api/osc/datalogger/iniciar")
def dl_iniciar(p: dict = Body(...), x_autor: str = Header(None)):
    """Con 'nombre' (y 'carpeta', 'notas') guarda en <proyecto>\\registros\\...\\AAAAMMDD_HHMM_nombre\\osc.h5.
    Con "fuente": true registra tambien la fuente (fuente.csv en la misma carpeta, con pre-registro 'pre_s')
    hasta que termine el datalogger: un solo Iniciar para los dos, ambos con el reloj del PC."""
    autor = autor_de(x_autor)
    con_fuente = bool(p.get("fuente"))
    apagar = bool(p.get("apagar_al_terminar"))      # al terminar el registro (por lo que sea): salida OFF
    cola = max(0.0, float(p.get("cola_s") if p.get("cola_s") not in (None, "") else COLA_APAGADO_S))
    if con_fuente and fte.estado != "conectada":
        raise HTTPException(409, "«Registrar también la fuente» necesita la fuente conectada")
    dur = float(p.get("duracion", 0))
    # con «apagar»: el datalogger dura la cola mas; el apagado va a la hora pedida
    dur_dl = dur + cola if apagar and dur > 0 else dur
    r = ejecutar(lambda: osc.iniciar_datalogger(p["canales"], float(p["sr"]), dur_dl,
                                                p.get("proyecto", ""), autor, p.get("carpeta", ""),
                                                p.get("nombre") or None, p.get("notas", "")))
    h5 = osc.dl["fichero"]
    if con_fuente:
        csv = os.path.join(os.path.dirname(h5), "fuente.csv") if p.get("nombre") else h5[:-3] + "_fuente.csv"
        # el apagado lo lleva el panel (abajo): la fuente se graba hasta que acabe el datalogger
        ejecutar(lambda: fte.iniciar_registro(csv, autor, p.get("pre_s", 30), cabecera={"datalogger": h5}))
    if apagar:
        _apagado_dl.clear()
        _apagado_dl.update(h5=h5, off=False, cola=cola)
    vivo = lambda: osc.dl.get("fichero") == h5 and osc.dl.get("fin") is None

    if apagar and dur > 0:
        def a_su_hora():
            # la duracion del datalogger cuenta desde que empieza a registrar (tras configurar y llenar la ventana)
            while vivo() and not any(l.startswith("Registrando") for l in osc.dl.get("salida", [])):
                _cerrado.wait(0.2)
            t0 = time.time()
            while vivo() and time.time() - t0 < dur:
                _cerrado.wait(0.1)
            if vivo():
                _apagar_registro("fin previsto")
        threading.Thread(target=a_su_hora, daemon=True).start()

    if con_fuente or apagar:
        def al_terminar():
            # termine como termine el datalogger (duracion, Parar, error, cierre a la fuerza)
            while vivo():
                _cerrado.wait(0.2)
            if apagar and _apagar_registro("el datalogger terminó antes") and con_fuente:
                _cerrado.wait(cola)                  # que el apagado quede al menos en fuente.csv
            if con_fuente and fte.grab and fte.grab["fichero"] == csv:
                fte.parar_registro("panel (automático)", "fin del datalogger")
        threading.Thread(target=al_terminar, daemon=True).start()
    return r


@app.post("/api/osc/capturar")
def osc_capturar(p: dict = Body(...), x_autor: str = Header(None)):
    """Captura suelta: {proyecto, carpeta, nombre, notas, formas, ambito: DISP|ALL, png, ficha, fuente, single}."""
    foto_fuente = None
    if p.get("fuente", True):
        r = fte.resumen()
        if r["estado"] == "conectada":
            foto_fuente = {k: r[k] for k in ("idn", "medida", "consignas", "alarmas", "equipo", "limites")}
    proyecto, carpeta, tipo = p["proyecto"], p.get("carpeta", ""), "capturas"
    if p.get("en_ensayo"):                      # durante un ensayo: a su carpeta capturas\
        proyecto, carpeta, tipo = ensayos.destino_capturas()
    return ejecutar(lambda: osc.capturar(proyecto, carpeta, p["nombre"], p.get("notas", ""),
                                         p.get("ambito", "DISP"), bool(p.get("png", True)), bool(p.get("ficha", True)),
                                         bool(p.get("single", True)), foto_fuente, autor_de(x_autor),
                                         formas=bool(p.get("formas", True)), tipo=tipo))


# ------------------------------------------------------------------- diagnostico de la vista
# Cada pestana del panel manda cada pocos segundos como se esta viendo (tamano, zoom, ajustes guardados, encuadre
# de la imagen de R&S). Sirve para comparar pestanas/navegadores cuando el encuadre se ve distinto.
DIAG = {}


@app.post("/api/diag")
def diag_guardar(p: dict = Body(...)):
    import time as _t
    DIAG[str(p.get("id", "?"))[:40]] = {**p, "recibido": _t.strftime("%H:%M:%S")}
    for k in sorted(DIAG, key=lambda k: DIAG[k]["recibido"])[:-8]:
        DIAG.pop(k, None)
    return {"ok": True}


@app.get("/api/diag")
def diag_ver():
    return DIAG


# ------------------------------------------------------------------- donde se guarda (capturas, registros, ensayos)
@app.get("/api/almacen/proyectos")
def alm_proyectos():
    return almacen.proyectos()


@app.get("/api/almacen/carpetas")
def alm_carpetas(proyecto: str, tipo: str):
    return ejecutar(lambda: almacen.carpetas(proyecto, tipo))


@app.get("/api/almacen/recientes")
def alm_recientes(proyecto: str, tipo: str):
    return ejecutar(lambda: almacen.recientes(proyecto, tipo))


@app.post("/api/almacen/carpeta")
def alm_carpeta(p: dict = Body(...), x_autor: str = Header(None)):
    r = ejecutar(lambda: almacen.crear_carpeta(p["proyecto"], p["tipo"], p["sub"]))
    reg.anotar(autor_de(x_autor), "panel", "carpeta creada", {"proyecto": p["proyecto"], "tipo": p["tipo"], "carpeta": r})
    return {"carpeta": r}


@app.post("/api/almacen/proyecto")
def alm_proyecto(p: dict = Body(...), x_autor: str = Header(None)):
    r = ejecutar(lambda: almacen.crear_proyecto(p["nombre"]))
    reg.anotar(autor_de(x_autor), "panel", "carpeta de proyecto creada", r)
    return {"proyecto": r}


@app.post("/api/osc/ajustar_senal")
def osc_ajustar_senal(p: dict = Body(...), x_autor: str = Header(None)):
    """{"canal": 1}: escala y offset para que la senal que hay ahora quepa con margen (antes de un registro)."""
    return ejecutar(lambda: osc.ajustar_a_senal(p["canal"], autor_de(x_autor)))


@app.get("/api/osc/vivo")
def osc_vivo(desde: float = 0.0):
    """Grafica en vivo: resumen (min, max, media cada 0,1 s, en V) de lo que lee el datalogger. Fuera de un
    datalogger el panel no lee formas de onda: queda lo del ultimo registro (hasta 2 h)."""
    return osc.vivo_desde(desde)


@app.post("/api/tramo/guardar")
def tramo_guardar(p: dict = Body(...), x_autor: str = Header(None)):
    """✂ Tramo de En vivo: {t0, t1 (hora Unix), proyecto, carpeta, nombre, notas, que: {fuente, osc, temp}}."""
    return ejecutar(lambda: tramos.guardar(p, autor_de(x_autor)))


@app.post("/api/osc/vista/iniciar")
def osc_vista_iniciar(p: dict = Body(...), x_autor: str = Header(None)):
    """{canales: [1, 2], sr}: el osciloscopio en En vivo sin ensayo ni registro (fichero temporal, <= 1 MSa/s)."""
    return ejecutar(lambda: osc.iniciar_vista(p.get("canales") or [], float(p.get("sr") or 0), autor_de(x_autor)))


@app.post("/api/osc/vista/parar")
def osc_vista_parar(x_autor: str = Header(None)):
    return ejecutar(lambda: osc.parar_vista(autor_de(x_autor)))


@app.get("/api/osc/vivo/detalle")
def osc_vivo_detalle(t0: float, t1: float, puntos: int = 2000):
    """Puntos reales del ultimo registro del osciloscopio entre t0 y t1 (hora Unix), para En vivo (2026-11-04).
    Solo con el registro YA CERRADO (no se abre un osc.h5 que el datalogger esta escribiendo). Respuesta:
    {"estado": "sin registro" | "registrando" | "preparando" | "listo" | "error", "avance", "canales": {...}}"""
    fich = osc.dl.get("fichero")
    if not fich or not os.path.exists(fich):
        return {"estado": "sin registro"}
    if osc._datalogger_vivo() or osc.dl.get("fin") is None:
        return {"estado": "registrando", "fichero": fich}
    e = detalle.preparar(fich)
    if e["error"]:
        return {"estado": "error", "mensaje": e["error"], "fichero": fich}
    if not e["listo"]:
        return {"estado": "preparando", "avance": e["avance"], "fichero": fich}
    try:
        return {"estado": "listo", "fichero": fich, "canales": detalle.tramo(t0, t1, puntos)}
    except Exception as ex:
        return {"estado": "error", "mensaje": str(ex), "fichero": fich}


@app.post("/api/osc/datalogger/parar")
def dl_parar(x_autor: str = Header(None)):
    """Con «apagar al terminar»: primero la salida OFF y el datalogger para la cola (s) elegida despues."""
    autor = autor_de(x_autor)
    if _apagado_dl.get("h5") == osc.dl.get("fichero") and not _apagado_dl.get("off") and osc._datalogger_vivo():
        _apagar_registro("Parar")

        def despues():
            _cerrado.wait(_apagado_dl.get("cola", COLA_APAGADO_S))
            try:
                osc.parar_datalogger(autor)
            except RuntimeError:
                pass                                 # ya habia terminado
        threading.Thread(target=despues, daemon=True).start()
        return {"apagando": True, "para_en_s": _apagado_dl.get("cola", COLA_APAGADO_S)}
    return ejecutar(lambda: osc.parar_datalogger(autor))


# ------------------------------------------------------------------- fuente
@app.get("/api/fuente/puertos")
def fte_puertos():
    return puertos()


@app.post("/api/fuente/conectar")
def fte_conectar(p: dict = Body({}), x_autor: str = Header(None)):
    """origen: "auto" (detectar), "COMx" o "sim" (fuente simulada)."""
    return ejecutar(lambda: fte.conectar(p.get("origen", "auto"), autor_de(x_autor)))


@app.post("/api/fuente/desconectar")
def fte_desconectar(x_autor: str = Header(None)):
    return ejecutar(lambda: fte.desconectar(autor_de(x_autor)))


@app.post("/api/fuente/fijar")
def fte_fijar(p: dict = Body(...), x_autor: str = Header(None)):
    f = lambda k: None if p.get(k) in (None, "") else float(p[k])
    return ejecutar(lambda: fte.fijar(autor_de(x_autor), V_lim_V=f("V_lim_V"), I_A=f("I_A"), P_W=f("P_W")))


@app.post("/api/fuente/salida")
def fte_salida(p: dict = Body(...), x_autor: str = Header(None)):
    return ejecutar(lambda: fte.salida(bool(p["encender"]), bool(p.get("confirmado")), autor_de(x_autor)))


@app.post("/api/fuente/local")
def fte_local(x_autor: str = Header(None)):
    return ejecutar(lambda: fte.local(autor_de(x_autor)))


@app.post("/api/fuente/opciones")
def fte_opciones(p: dict = Body(...), x_autor: str = Header(None)):
    r = ejecutar(lambda: fte.opciones(autor_de(x_autor), p.get("apagar_si_corte"), p.get("limites"),
                                      bool(p.get("confirmado")), p.get("periodo_s")))
    cfg["fuente"]["apagar_si_corte"] = fte.apagar_si_corte
    cfg["fuente"]["limites"] = fte.lim
    cfg["fuente"]["periodo_s"] = fte.periodo
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return r


@app.post("/api/fuente/registro/iniciar")
def fte_registro_iniciar(p: dict = Body(...), x_autor: str = Header(None)):
    r"""Registro de la fuente sola: <proyecto>\registros\<carpeta>\AAAAMMDD_HHMM_nombre\fuente.csv + registro.json.
    {proyecto, carpeta, nombre, notas, pre_s (30), duracion_s, tras_apagar_s}"""
    def hacer():
        almacen._limpio(p["nombre"])
        ruta, _, rel = almacen.destino(p.get("proyecto", ""), "registros", p.get("carpeta", ""), p["nombre"],
                                       carpeta_propia=True)
        num = lambda k: None if p.get(k) in (None, "") else float(p[k])
        with open(os.path.join(ruta, "registro.json"), "w", encoding="utf-8") as f:
            json.dump({"inicio": time.strftime("%Y-%m-%dT%H:%M:%S"), "nombre": p["nombre"], "notas": p.get("notas", ""),
                       "proyecto": p.get("proyecto", ""), "carpeta": p.get("carpeta", ""), "ruta": rel,
                       "tipo": "fuente", "equipo": fte.idn, "pre_registro_s": num("pre_s") or 0,
                       "duracion_s": num("duracion_s"), "tras_apagar_s": num("tras_apagar_s"),
                       "topes": fte.lim}, f, ensure_ascii=False, indent=1)
        fte.iniciar_registro(os.path.join(ruta, "fuente.csv"), autor_de(x_autor), num("pre_s") or 0,
                             num("duracion_s"), num("tras_apagar_s"), {"notas": p.get("notas", "")},
                             apagar_al_terminar=bool(p.get("apagar_al_terminar")), cola_s=p.get("cola_s"))
        return {"ruta": rel}
    return ejecutar(hacer)


@app.post("/api/fuente/capturar")
def fte_capturar(p: dict = Body(...), x_autor: str = Header(None)):
    r"""Foto del instante de la fuente sola (Capturar · Fuente):
    <proyecto>\capturas\<carpeta>\AAAAMMDD_HHMM_nombre_fuente.json. {proyecto, carpeta, nombre, notas}"""
    def hacer():
        r = fte.resumen()
        if r["estado"] != "conectada":
            raise RuntimeError("La fuente no está conectada")
        almacen._limpio(p["nombre"])
        proyecto, carpeta, tipo = p.get("proyecto", ""), p.get("carpeta", ""), "capturas"
        if p.get("en_ensayo"):
            proyecto, carpeta, tipo = ensayos.destino_capturas()
        ruta, _, rel = almacen.destino(proyecto, tipo, carpeta, p["nombre"])
        fichero, k = ruta + "_fuente.json", 2
        while os.path.exists(fichero):          # destino() no ve los «_fuente.json»: que no se pise uno del mismo minuto
            fichero, k = f"{ruta}_{k}_fuente.json", k + 1
        with open(fichero, "w", encoding="utf-8") as f:
            json.dump({"hora": time.strftime("%Y-%m-%dT%H:%M:%S"), "nombre": p["nombre"], "notas": p.get("notas", ""),
                       "proyecto": p.get("proyecto", ""), "carpeta": p.get("carpeta", ""), "tipo": "fuente",
                       "fuente": {k: r[k] for k in ("idn", "medida", "consignas", "alarmas", "equipo", "limites")}},
                      f, ensure_ascii=False, indent=1)
        info = {"ruta": rel, "ficheros": [os.path.basename(fichero)]}
        reg.anotar(autor_de(x_autor), "fuente", "captura guardada", info)
        return info
    return ejecutar(hacer)


@app.post("/api/fuente/registro/parar")
def fte_registro_parar(x_autor: str = Header(None)):
    return ejecutar(lambda: fte.parar_registro(autor_de(x_autor), "Parar"))


@app.post("/api/tc08/tomar")
def tc08_tomar(x_autor: str = Header(None)):
    """El panel toma el TC-08 y mide de forma continua (PicoLog tiene que estar cerrado)."""
    return ejecutar(lambda: tc.tomar(autor_de(x_autor)))


@app.post("/api/tc08/soltar")
def tc08_soltar(x_autor: str = Header(None)):
    """Deja el TC-08 libre para PicoLog."""
    return ejecutar(lambda: tc.soltar(autor_de(x_autor)))


@app.post("/api/tc08/configurar")
def tc08_configurar(p: dict = Body(...), x_autor: str = Header(None)):
    """{intervalo_ms, red_hz, canales: {"1": {activo, nombre, tipo, limite_C}, ...}} (se guarda en config.json)."""
    return ejecutar(lambda: tc.configurar(p, autor_de(x_autor)))


# grabacion de temperaturas solas (boton «Grabar» de la ventana Temperaturas): como el registro de la fuente
REG_TEMP = {"ruta": None}


@app.post("/api/temperaturas/registro/iniciar")
def temperaturas_registro_iniciar(p: dict = Body(...), x_autor: str = Header(None)):
    r"""<proyecto>\registros\<carpeta>\AAAAMMDD_HHMM_nombre\temperaturas.csv (+ .picolog al parar) y registro.json.
    {proyecto, carpeta, nombre, notas, pre_s}"""
    def hacer():
        if temps.grab:
            raise RuntimeError("Ya se están grabando las temperaturas (¿ensayo en marcha?)")
        if not temps.canales:
            raise RuntimeError("No hay temperaturas: usa el TC-08 desde el panel o captura con PicoLog")
        almacen._limpio(p["nombre"])
        ruta, _, rel = almacen.destino(p.get("proyecto", ""), "registros", p.get("carpeta", ""), p["nombre"],
                                       carpeta_propia=True)
        pre = float(p.get("pre_s") or 0)
        with open(os.path.join(ruta, "registro.json"), "w", encoding="utf-8") as f:
            json.dump({"inicio": time.strftime("%Y-%m-%dT%H:%M:%S"), "nombre": p["nombre"], "notas": p.get("notas", ""),
                       "proyecto": p.get("proyecto", ""), "carpeta": p.get("carpeta", ""), "ruta": rel,
                       "tipo": "temperaturas", "pre_registro_s": pre, "temperaturas": temps.ficha()},
                      f, ensure_ascii=False, indent=1)
        temps.iniciar_registro(os.path.join(ruta, "temperaturas.csv"), autor_de(x_autor), pre,
                               {"notas": p.get("notas", ""), "registro": rel})
        REG_TEMP["ruta"] = rel
        return {"ruta": rel}
    return ejecutar(hacer)


@app.post("/api/temperaturas/registro/parar")
def temperaturas_registro_parar(x_autor: str = Header(None)):
    def hacer():
        if not REG_TEMP["ruta"]:
            raise RuntimeError("No hay ninguna grabación de temperaturas suelta (las de un ensayo se paran con Terminar)")
        rel, REG_TEMP["ruta"] = REG_TEMP["ruta"], None
        temps.parar_registro(autor_de(x_autor), "parado")
        return {"ruta": rel}
    return ejecutar(hacer)


@app.get("/api/temperaturas/historia")
def temperaturas_historia(desde: float = 0.0):
    return temps.historia_desde(desde)


@app.get("/api/picolog/historia")
def picolog_historia(desde: float = 0.0):
    """Temperaturas de PicoLog desde 'desde' (s Unix): {canales: [{id, nombre, unidad, color}], datos: {id: [[t, v]...]}}."""
    return temps.historia_desde(desde)


@app.get("/api/fuente/historia")
def fte_historia(desde: float = 0.0):
    """Lo leido de la fuente en las ultimas 2 h (desde 'desde', hora Unix): [hora, V, I, P, salida, modo]."""
    return fte.historia_desde(desde)


# ------------------------------------------------------------------- Siglent SPD3303X (9-nov-2026)
REG_SPD = {"ruta": None}


@app.post("/api/siglent/conectar")
def spd_conectar(x_autor: str = Header(None)):
    return ejecutar(lambda: spd.conectar(autor_de(x_autor)))


@app.post("/api/siglent/desconectar")
def spd_desconectar(x_autor: str = Header(None)):
    """Apaga sus salidas y cierra (la Siglent no tiene vigilante propio)."""
    return ejecutar(lambda: spd.desconectar(autor_de(x_autor)))


@app.post("/api/siglent/fijar")
def spd_fijar(p: dict = Body(...), x_autor: str = Header(None)):
    """{canal: CH1|CH2, V, I}: comprobado con el modelo (negativos, máximos, topes) y leído de vuelta."""
    f = lambda k: None if p.get(k) in (None, "") else float(p[k])
    return ejecutar(lambda: spd.fijar(autor_de(x_autor), p.get("canal", "CH1"), V=f("V"), I=f("I")))


@app.post("/api/siglent/salida")
def spd_salida(p: dict = Body(...), x_autor: str = Header(None)):
    """{canal: CH1|CH2|CH3, encender, confirmado}: encender pide confirmación; apagar nunca."""
    return ejecutar(lambda: spd.salida(autor_de(x_autor), p.get("canal", "CH1"), bool(p["encender"]), bool(p.get("confirmado"))))


@app.post("/api/siglent/apagar")
def spd_apagar(x_autor: str = Header(None)):
    return ejecutar(lambda: spd.apagar_todo(autor_de(x_autor)))


@app.post("/api/siglent/modo")
def spd_modo(p: dict = Body(...), x_autor: str = Header(None)):
    """{modo: independiente | serie | paralelo} (solo con las salidas apagadas)."""
    return ejecutar(lambda: spd.poner_modo(autor_de(x_autor), p.get("modo", "independiente")))


@app.post("/api/siglent/opciones")
def spd_opciones(p: dict = Body(...), x_autor: str = Header(None)):
    """{topes: {V, I}, nombres: {CH1, CH2, CH3}, periodo_s}"""
    return ejecutar(lambda: spd.opciones(autor_de(x_autor), p.get("topes"), p.get("nombres"), p.get("periodo_s")))


@app.get("/api/siglent/historia")
def spd_historia(desde: float = 0.0):
    return spd.historia_desde(desde)


@app.post("/api/siglent/registro/iniciar")
def spd_registro_iniciar(p: dict = Body(...), x_autor: str = Header(None)):
    r"""<proyecto>\registros\<carpeta>\AAAAMMDD_HHMM_nombre\siglent.csv y registro.json. {proyecto, carpeta, nombre, notas, pre_s}"""
    def hacer():
        almacen._limpio(p["nombre"])
        ruta, _, rel = almacen.destino(p.get("proyecto", ""), "registros", p.get("carpeta", ""), p["nombre"], carpeta_propia=True)
        pre = float(p.get("pre_s") or 0)
        with open(os.path.join(ruta, "registro.json"), "w", encoding="utf-8") as f:
            json.dump({"inicio": time.strftime("%Y-%m-%dT%H:%M:%S"), "nombre": p["nombre"], "notas": p.get("notas", ""),
                       "proyecto": p.get("proyecto", ""), "carpeta": p.get("carpeta", ""), "ruta": rel,
                       "tipo": "siglent", "pre_registro_s": pre, "siglent": spd.ficha()}, f, ensure_ascii=False, indent=1)
        spd.iniciar_registro(os.path.join(ruta, "siglent.csv"), autor_de(x_autor), pre, {"notas": p.get("notas", ""), "registro": rel})
        REG_SPD["ruta"] = rel
        return {"ruta": rel}
    return ejecutar(hacer)


@app.post("/api/siglent/registro/parar")
def spd_registro_parar(x_autor: str = Header(None)):
    def hacer():
        f = spd.parar_registro(autor_de(x_autor))
        REG_SPD["ruta"] = None
        return {"fichero": f}
    return ejecutar(hacer)


@app.post("/api/apagar_todo")
def apagar_todo(x_autor: str = Header(None)):
    """APAGAR TODO (9-nov): todas las salidas de todas las fuentes conectadas. Sigue aunque una falle y dice cuál."""
    autor, res = autor_de(x_autor), {}
    if fte.estado == "conectada":
        try:
            fte.salida(False, True, autor)
            res["fuente_ea"] = "apagada"
        except Exception as e:
            res["fuente_ea"] = f"ERROR: {e}"
    if spd.estado == "conectada":
        try:
            spd.apagar_todo(autor, "APAGAR TODO")
            res["siglent"] = "apagada"
        except Exception as e:
            res["siglent"] = f"ERROR: {e}"
    reg.anotar(autor, "panel", "APAGAR TODO", res)
    if any(str(v).startswith("ERROR") for v in res.values()):
        raise HTTPException(status_code=500, detail=f"APAGAR TODO incompleto: {res}")
    return res


# ------------------------------------------------------------------- cDAQ-9178 (validado con hardware el 9-nov-2026)
REG_CDAQ = {"ruta": None}


@app.post("/api/cdaq/tomar")
def cdaq_tomar(x_autor: str = Header(None)):
    """El panel toma el cDAQ (crea y arranca las tareas en su hilo). Devuelve el estado tras el primer intento."""
    return ejecutar(lambda: daq.tomar(autor_de(x_autor)))


@app.post("/api/cdaq/soltar")
def cdaq_soltar(x_autor: str = Header(None)):
    """Cierra las tareas DAQmx: los módulos quedan libres para otro programa."""
    return ejecutar(lambda: daq.soltar(autor_de(x_autor)))


@app.post("/api/cdaq/configurar")
def cdaq_configurar(p: dict = Body(...), x_autor: str = Header(None)):
    """Cambia la sección «cdaq» de config.json (ver cdaq.config_por_defecto)."""
    return ejecutar(lambda: daq.configurar(p, autor_de(x_autor)))


@app.get("/api/cdaq/historia")
def cdaq_historia(desde: float = 0.0):
    """{simulado, aviso, canales: [{id, nombre, unidad, color, limite, modulo}], datos: {id: [[t, v]...]}}; v null = termopar abierto."""
    return daq.historia_desde(desde)


@app.post("/api/cdaq/registro/iniciar")
def cdaq_registro_iniciar(p: dict = Body(...), x_autor: str = Header(None)):
    r"""<proyecto>\registros\<carpeta>\AAAAMMDD_HHMM_nombre\cdaq.csv (cdaq_SIMULADO.csv si es simulado) y registro.json.
    {proyecto, carpeta, nombre, notas, pre_s}"""
    def hacer():
        if daq.grab:
            raise RuntimeError("Ya se está grabando el cDAQ")
        almacen._limpio(p["nombre"])
        ruta, _, rel = almacen.destino(p.get("proyecto", ""), "registros", p.get("carpeta", ""), p["nombre"],
                                       carpeta_propia=True)
        pre = float(p.get("pre_s") or 0)
        with open(os.path.join(ruta, "registro.json"), "w", encoding="utf-8") as f:
            json.dump({"inicio": time.strftime("%Y-%m-%dT%H:%M:%S"), "nombre": p["nombre"], "notas": p.get("notas", ""),
                       "proyecto": p.get("proyecto", ""), "carpeta": p.get("carpeta", ""), "ruta": rel,
                       "tipo": "cdaq", "pre_registro_s": pre, "cdaq": daq.ficha()}, f, ensure_ascii=False, indent=1)
        daq.iniciar_registro(os.path.join(ruta, "cdaq_SIMULADO.csv" if daq.simulado else "cdaq.csv"),
                             autor_de(x_autor), pre, {"notas": p.get("notas", ""), "registro": rel})
        REG_CDAQ["ruta"] = rel
        return {"ruta": rel, "simulado": daq.simulado}
    return ejecutar(hacer)


@app.post("/api/cdaq/registro/parar")
def cdaq_registro_parar(x_autor: str = Header(None)):
    def hacer():
        if not REG_CDAQ["ruta"]:
            raise RuntimeError("No hay ninguna grabación del cDAQ en marcha")
        rel, REG_CDAQ["ruta"] = REG_CDAQ["ruta"], None
        daq.parar_registro(autor_de(x_autor), "parado")
        return {"ruta": rel}
    return ejecutar(hacer)


_cerrado = threading.Event()


@app.on_event("shutdown")
def al_cerrar():
    """Cierre ordenado: fuente (con la opcion 'apagar si corte') y osciloscopio devuelto a LOCAL.
    Se llama al parar el servidor, desde el boton «Cerrar panel» y al cerrar la ventana de consola."""
    if _cerrado.is_set():
        return
    _cerrado.set()
    try:
        ensayos.cerrar_panel()                   # un ensayo en marcha se cierra ordenadamente
    except Exception as e:
        reg.anotar("panel (cierre)", "ensayo", "error al cerrar", str(e))
    try:
        tc.cerrar()                              # el TC-08 queda libre (si no, el proceso no terminaba)
    except Exception:
        pass
    try:
        daq.cerrar()                             # cierra las tareas DAQmx: los módulos del cDAQ quedan libres
    except Exception:
        pass
    try:
        spd.cerrar()                             # la Siglent no tiene vigilante propio: se apaga al cerrar el panel
    except Exception:
        pass
    try:
        # con la salida encendida se APAGA (antes quedaba encendida y sin vigilante: revisión del 2026-11-04)
        fte.desconectar("panel (cierre)", si_encendida="apagar")
    except Exception as e:
        reg.anotar("panel (cierre)", "fuente", "error al cerrar", str(e))
    # la vista en vivo del osciloscopio NO sigue sola (no es un registro): se para y se espera a que restaure el MXO
    if osc.dl.get("vista") and osc._datalogger_vivo():
        try:
            osc.parar_vista("panel (cierre)")
            fin = time.time() + 20
            while osc._datalogger_vivo() and time.time() < fin:
                time.sleep(0.2)
        except Exception as e:
            reg.anotar("panel (cierre)", "osciloscopio", "no se pudo parar la vista en vivo", str(e))
    try:
        osc.forzar_manual("panel (cierre)")
    except RuntimeError:
        pass     # datalogger en marcha: sigue solo y restaura el osciloscopio al acabar


# ------------------------------------------------------------------- ensayo (fase 4)
@app.post("/api/ensayo/iniciar")
def ensayo_iniciar(p: dict = Body(...), x_autor: str = Header(None)):
    """{proyecto, carpeta, nombre, condiciones, notas, osc: {canales, sr} | null, pre_s, apagar_al_terminar, cola_s}"""
    return ejecutar(lambda: ensayos.iniciar(p, autor_de(x_autor)))


# ------------------------------------------------------------------- ensayo programado (fase 5)
@app.get("/api/planes")
def planes_listar(proyecto: str):
    import plan as planes
    return ejecutar(lambda: {"planes": planes.listar(proyecto)})


@app.get("/api/plan")
def plan_leer(proyecto: str, nombre: str):
    import plan as planes
    return ejecutar(lambda: planes.leer(proyecto, nombre))


@app.post("/api/plan/guardar")
def plan_guardar(p: dict = Body(...), x_autor: str = Header(None)):
    """{proyecto, nombre, plan}: en <proyecto>\\ensayos\\_planes\\<nombre>.json (se guarda aunque no pase la comprobacion)."""
    import plan as planes

    def hacer():
        n = planes.guardar(p["proyecto"], p["nombre"], p["plan"])
        reg.anotar(autor_de(x_autor), "ensayo", "plan guardado", {"proyecto": p["proyecto"], "plan": n})
        return {"nombre": n}
    return ejecutar(hacer)


@app.get("/api/plan/medidas")
def plan_medidas():
    """Fase 4 (9-nov): medidas que se pueden usar en «esperar» y en las guardas, con su valor ahora."""
    import plan as planes
    return ejecutar(lambda: {"medidas": planes.medidas_disponibles(ensayos)})


@app.post("/api/plan/comprobar")
def plan_comprobar(p: dict = Body(...)):
    """{plan, osc: bool}: el plan normalizado y su resumen, o el error con todos los problemas."""
    def hacer():
        plan, resumen = ensayos.comprobar_plan(p["plan"], bool(p.get("osc")))
        return {"plan": plan, "resumen": resumen}
    return ejecutar(hacer)


@app.post("/api/ensayo/preparar")
def ensayo_preparar(p: dict = Body(...), x_autor: str = Header(None)):
    """{proyecto, plan}: la pagina abierta abre «Ensayo… → Programado» con ese plan guardado y sus opciones, para
    revisarlo e iniciarlo (no inicia nada). Asi Claude deja preparado un ensayo descrito con palabras."""
    import plan as planes

    def hacer():
        planes.leer(p["proyecto"], p["plan"])          # que exista
        ensayos.preparado = {"proyecto": p["proyecto"], "plan": p["plan"], "hora": time.time()}
        reg.anotar(autor_de(x_autor), "ensayo", "ensayo preparado para revisar", ensayos.preparado)
        return ensayos.preparado
    return ejecutar(hacer)


@app.post("/api/ensayo/plan/{accion}")
def ensayo_plan(accion: str, p: dict = Body({}), x_autor: str = Header(None)):
    """continuar admite {"confirmado": true}: hace falta si volveria a encender tras un APAGAR o una protección."""
    if accion not in ("pausar", "continuar", "siguiente", "parar"):
        raise HTTPException(404, "acción desconocida")
    return ejecutar(lambda: ensayos.plan_mando(accion, autor_de(x_autor), bool((p or {}).get("confirmado"))))


@app.post("/api/ensayo/terminar")
def ensayo_terminar(x_autor: str = Header(None)):
    """Apaga (si se eligio), sigue grabando lo elegido y cierra. No pregunta nada."""
    return ejecutar(lambda: ensayos.terminar(autor_de(x_autor)))


@app.post("/api/abrir_carpeta")
def abrir_carpeta(p: dict = Body(...)):
    r"""Abre en el Explorador una carpeta de C:\Proyectos\Osciloscopio (p. ej. fotos\ del ensayo en curso)."""
    ruta = os.path.realpath(os.path.join(almacen.BASE, p.get("rel", "")))
    base = os.path.realpath(almacen.BASE)
    # con separador: «...\OsciloscopioX» ya no pasa por estar dentro (revisión del 2026-11-04, hallazgo 17)
    if not (ruta == base or ruta.startswith(base + os.sep)) or not os.path.isdir(ruta):
        raise HTTPException(404, "Esa carpeta no existe")
    os.startfile(ruta)
    return {"ok": True}


@app.post("/api/cerrar")
def cerrar_panel(x_autor: str = Header(None)):
    """Cierra el panel de forma ordenada (lo mismo que Ctrl+C en la consola)."""
    reg.anotar(autor_de(x_autor), "panel", "cerrar panel")
    al_cerrar()
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"ok": True}


def _al_cerrar_consola():
    """Cerrar la ventana de consola mata el proceso sin pasar por el 'shutdown' de uvicorn.
    Windows avisa antes (CTRL_CLOSE_EVENT) y da unos segundos: se aprovechan para cerrar bien."""
    try:
        import ctypes
        from ctypes import wintypes
        manejador = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _evento(tipo):
            if tipo in (0, 2, 5, 6):        # Ctrl+C, cerrar ventana, cerrar sesion, apagar
                al_cerrar()
            return False                    # que Windows siga con el cierre normal
        _al_cerrar_consola.ref = manejador(_evento)   # referencia viva: si no, se libera
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_al_cerrar_consola.ref, True)
    except Exception:
        pass


if __name__ == "__main__":
    _al_cerrar_consola()
    puerto = cfg.get("puerto_panel", 8765)
    # 9-nov: al reiniciar, el panel anterior puede tardar en soltar el puerto (p. ej. esperando a una Siglent
    # colgada en NI-VISA): se espera hasta 4 min en vez de cerrarse con «puerto ocupado»
    import socket
    for _i in range(240):
        with socket.socket() as _s:
            if _s.connect_ex(("127.0.0.1", puerto)) != 0:
                break
        if _i == 0:
            print(f"El puerto {puerto} sigue ocupado (el panel anterior se está cerrando): espero...", flush=True)
        time.sleep(1)
    if cfg.get("abrir_navegador", True):
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{puerto}")).start()
    uvicorn.run(app, host="127.0.0.1", port=puerto, log_level="warning")
