"""Control de la fuente EA-PS 9500-20 1U por USB (puerto COM virtual) para el panel.

Portado de control_fuente.m (PR00737-ECOCOOK): mismos comandos SCPI, mismo
simulador. Con la fuente real (2026-10-29, firmware V2.06/V3.09) se confirmaron
SYST:LOCK ON/OFF, SYST:NOM:*, protecciones, limites de ajuste, los bits de
alarma de STAT:QUES:COND? y los de modo de STAT:OPER:COND? (CV/CC/CP).

La zona de trabajo de la fuente no es una potencia constante: es V <= Vnom,
I <= Inom y V*I <= Pnom a la vez (500 V, 20 A, 3000 W: los 20 A solo hasta 150 V).
Las consignas V, I y P funcionan igual, como techos: regula en CV, CC o CP segun
cual se alcance primero.

El puerto se detecta solo: se prueban los puertos serie (primero los de
Elektro-Automatik) con *IDN? hasta encontrar una PS 9000.
"""
import collections
import os
import re
import threading
import time

COMANDOS = {
    "idn": "*IDN?",
    "bloquear": "SYST:LOCK ON",
    "liberar": "SYST:LOCK OFF",          # confirmado: SYST:LOCK:OWN? pasa de REMOTE a NONE
    "dueno": "SYST:LOCK:OWN?",
    "set_v": "SOUR:VOLT {:.3f}",
    "set_i": "SOUR:CURR {:.4f}",
    "set_p": "SOUR:POW {:.1f}",
    "get_v": "SOUR:VOLT?",
    "get_i": "SOUR:CURR?",
    "get_p": "SOUR:POW?",
    "on": "OUTP ON",
    "off": "OUTP OFF",
    "salida": "OUTP?",
    "med_v": "MEAS:VOLT?",
    "med_i": "MEAS:CURR?",
    "med_p": "MEAS:POW?",
    "errores": "SYST:ERR?",
    # --- confirmados con la fuente real el 2026-10-29 (no estan en control_fuente.m)
    "v_nom": "SYST:NOM:VOLT?",           # valores absolutos del modelo: 500 V, 20 A, 3000 W
    "i_nom": "SYST:NOM:CURR?",
    "p_nom": "SYST:NOM:POW?",
    "ovp": "SOUR:VOLT:PROT?",            # umbral de sobretension
    "ocp": "SOUR:CURR:PROT?",            # umbral de sobrecorriente
    "opp": "SOUR:POW:PROT?",             # umbral de sobrepotencia
    "v_lim_alto": "SOUR:VOLT:LIM:HIGH?",  # limites de ajuste puestos en el menu de la fuente
    "v_lim_bajo": "SOUR:VOLT:LIM:LOW?",
    "i_lim_alto": "SOUR:CURR:LIM:HIGH?",
    "i_lim_bajo": "SOUR:CURR:LIM:LOW?",
    "p_lim_alto": "SOUR:POW:LIM:HIGH?",
    # los topes del panel SON estos limites de ajuste de la fuente (decidido por el usuario el 2026-11-02):
    # el panel los adopta al conectar y, si se cambian en el panel, se escriben en la fuente
    "set_v_lim": "SOUR:VOLT:LIM:HIGH {:.3f}",
    "set_i_lim": "SOUR:CURR:LIM:HIGH {:.4f}",
    "set_p_lim": "SOUR:POW:LIM:HIGH {:.1f}",
    "alarmas": "STAT:QUES:COND?",        # bits de OV/OC/OP (ver BITS_ALARMA); bit 10 = remoto, bit 11 = salida ON
    "modo": "STAT:OPER:COND?",           # bits de CV/CC/CP (ver BITS_MODO); 0 con la salida apagada
    # vigilancia de la interfaz («interface monitoring», guia de programacion 10k/20k de EA; firmware KE 3.09):
    # si no llega ningun mensaje en N s, la fuente sale de remoto y APAGA la salida (probado el 2026-11-03 con
    # 1 ohm: 0,53 A -> OFF y bit 7 de STAT:OPER:COND). Solo se puede cambiar estando en remoto (si no, -221).
    "vig_tiempo": "SYST:COMM:MON:TIM {:d}",
    "vig_activar": "SYST:COMM:MON:ACT {}",
    "vig_estado": "SYST:COMM:MON:ACT?",
}
# Bits confirmados el 2026-10-29 haciendo saltar cada proteccion con 1 ohm (ver FUENTE.md).
# Se pueden corregir en config.json ("bits_alarma").
BITS_ALARMA = {"OV": 0, "OC": 1, "OP": 2}
BITS_MODO = {"CV": 8, "CC": 9, "CP": 10}
# bit de STAT:OPER:COND que pone la fuente cuando salta la vigilancia de la interfaz (medido el 2026-11-03)
BIT_VIGILANTE = 7
# segundos sin mensajes del panel tras los que la fuente apaga sola (config.json: fuente.vigilante_s). El panel le
# pregunta varias veces por segundo; si el panel o el PC se cuelgan, o se suelta el USB, la salida se apaga igual
VIGILANTE_S = 5
# cada cuanto se lee la fuente (s): por debajo de 0,1 s las cinco consultas por ciclo van justas por USB
PERIODO_MIN, PERIODO_MAX = 0.1, 10.0
# la fuente se lee siempre: las ultimas 2 h quedan en memoria (pre-registro y grafica en vivo)
HISTORIA_S = 2 * 3600
# con «apagar al terminar»: segundos que se sigue grabando tras apagar, para que el apagado quede en fuente.csv
# (por defecto; el usuario lo elige en cada registro, p. ej. minutos de enfriamiento en un ensayo termico)
COLA_APAGADO_S = 3.0
# cada cuanto se releen las consignas y quien tiene el control: el usuario puede salir del remoto en el frontal
# («si insistes, sale del remoto») y cambiar las consignas a mano; el panel tiene que enterarse (2026-11-02)
FRONTAL_S = 1.0
# fuente.csv: mismo estilo que control_fuente.m (cabecera con '#', comas, punto decimal)
COLUMNAS = ("fecha_hora", "t_unix_s", "t_rel_s", "V_V", "I_A", "P_W", "V_consigna_V", "I_consigna_A",
            "P_consigna_W", "modo", "salida", "OV", "OC", "OP", "evento")
VID_EA = 0x232E        # Elektro-Automatik (USB vendor id; se prueban primero)
BAUDIOS = 115200


def numero(txt):
    m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", txt or "")
    return float(m.group()) if m else None


class EnlaceSerie:
    def __init__(self, puerto):
        import serial
        self.puerto = puerto
        self.s = serial.Serial(puerto, BAUDIOS, timeout=1.0, write_timeout=1.0)

    def escribir(self, cmd):
        self.s.write((cmd + "\n").encode())

    def consultar(self, cmd):
        self.s.reset_input_buffer()
        self.escribir(cmd)
        return self.s.readline().decode(errors="replace").strip()

    def cerrar(self):
        self.s.close()


class EnlaceSimulado:
    """Fuente falsa (cadena de LED), igual que enlaceSim() de control_fuente.m."""
    puerto = "simulada"

    def __init__(self):
        self.n, self.vset, self.iset, self.pset = 57, 300.0, 0.0, 1000.0
        self.out, self.ton = False, time.time()
        # protecciones y limites de ajuste como los de la fuente real (leidos el 2026-10-29)
        self.ovp, self.ocp, self.opp = 550.0, 22.0, 3300.0
        self.vlim, self.ilim, self.plim = 330.0, 8.0, 1898.0
        self.mon = "OFF"
        self.dueno = "NONE"                 # como la real: REMOTE solo tras SYST:LOCK ON (2026-11-04)

    def _vi(self):
        if not self.out:
            return 0.0, 0.0
        t = time.time() - self.ton
        deriva = 0.0008 * self.n * (1 - pow(2.718281828, -t / 20)) * (self.iset / 0.5)
        v = self.n * (2.45 + 0.5 * self.iset) - deriva
        i = self.iset
        if v > self.vset:
            i, v = i * self.vset / v, self.vset
        if v * i > self.pset:
            i = self.pset / v
        return v, i

    def escribir(self, cmd):
        c = cmd.upper().strip()
        val = numero(c.split(" ")[-1]) if " " in c else None
        if c.startswith("SOUR:VOLT:LIM:HIGH "):
            self.vlim = val
        elif c.startswith("SOUR:CURR:LIM:HIGH "):
            self.ilim = val
        elif c.startswith("SOUR:POW:LIM:HIGH "):
            self.plim = val
        elif c.startswith("SOUR:VOLT "):
            self.vset = val
        elif c.startswith("SOUR:CURR "):
            self.iset = val
        elif c.startswith("SOUR:POW "):
            self.pset = val
        elif c == "OUTP ON":
            self.out, self.ton = True, time.time()
        elif c == "OUTP OFF":
            self.out = False
        elif c.startswith("SYST:COMM:MON:ACT "):
            self.mon = c.split(" ")[-1]
        elif c == "SYST:LOCK ON":
            self.dueno = "REMOTE"
        elif c == "SYST:LOCK OFF":
            self.dueno = "NONE"

    def consultar(self, cmd):
        c = cmd.upper().strip()
        v, i = self._vi()
        alarmas = (v > self.ovp) << BITS_ALARMA["OV"] | (i > self.ocp) << BITS_ALARMA["OC"] | \
                  (v * i > self.opp) << BITS_ALARMA["OP"]
        if alarmas and self.out:            # como la real: una proteccion apaga la salida
            self.out = False
        return {"*IDN?": "SIMULADO,EA-PS 9500-20 1U,0,0", "SOUR:VOLT?": f"{self.vset:.2f} V",
                "SYST:NOM:VOLT?": "500.0 V", "SYST:NOM:CURR?": "20.000 A", "SYST:NOM:POW?": "3000 W",
                "STAT:OPER:COND?": str(1 << BITS_MODO["CV"]) if self.out else "0",
                "SOUR:VOLT:PROT?": f"{self.ovp:.2f} V", "SOUR:CURR:PROT?": f"{self.ocp:.3f} A",
                "SOUR:POW:PROT?": f"{self.opp:.1f} W", "SOUR:VOLT:LIM:HIGH?": f"{self.vlim:.2f} V",
                "SOUR:VOLT:LIM:LOW?": "0.00 V", "SOUR:CURR:LIM:HIGH?": f"{self.ilim:.3f} A", "SOUR:CURR:LIM:LOW?": "0.000 A",
                "SOUR:POW:LIM:HIGH?": f"{self.plim:.1f} W", "STAT:QUES:COND?": str(alarmas),
                "SOUR:CURR?": f"{self.iset:.3f} A", "SOUR:POW?": f"{self.pset:.1f} W",
                "OUTP?": "ON" if self.out else "OFF", "MEAS:VOLT?": f"{v:.2f} V", "MEAS:CURR?": f"{i:.3f} A",
                "MEAS:POW?": f"{v * i:.1f} W", "SYST:ERR?": '0,"No error"', "SYST:LOCK:OWN?": self.dueno,
                "SYST:COMM:MON:ACT?": self.mon}.get(c, "")

    def cerrar(self):
        pass


def puertos():
    from serial.tools import list_ports
    lista = [{"puerto": p.device, "descripcion": p.description, "vid": p.vid, "pid": p.pid,
              "ea": p.vid == VID_EA} for p in list_ports.comports()]
    return sorted(lista, key=lambda p: not p["ea"])


def detectar():
    """Devuelve (puerto, idn) de la primera PS 9000 que responda, o (None, motivo)."""
    probados = []
    for p in puertos():
        try:
            e = EnlaceSerie(p["puerto"])
            try:
                idn = e.consultar("*IDN?")
            finally:
                e.cerrar()
        except Exception as ex:
            probados.append(f"{p['puerto']}: {ex}")
            continue
        if re.search(r"PS\s*9\d{3}|ELEKTRO|EA-PS", idn, re.I):
            return p["puerto"], idn
        probados.append(f"{p['puerto']}: responde '{idn}'")
    return None, ("no hay puertos serie (¿está enchufada por USB?)" if not probados else "ninguno es una EA PS 9000: " + "; ".join(probados))


class EncendidoBloqueado(RuntimeError):
    """El plan (u otro automatismo) intenta encender tras un APAGAR, una protección, el vigilante o un corte."""


class ControlPerdido(RuntimeError):
    """La fuente ha salido del remoto (frontal, «local» o vigilante) y el plan no lo retoma por su cuenta."""


class Fuente:
    def __init__(self, registro, limites, periodo=0.5, bits_alarma=None):
        self.reg = registro
        self.lim = dict(limites)            # imax_A, vmax_V, pmax_W (limites del panel)
        self.bits = dict(bits_alarma or BITS_ALARMA)
        self.equipo = {}                    # protecciones y limites de ajuste leidos de la fuente
        self.alarmas = {}                   # {"OV": bool|None, ...}
        self._sin_alarmas = False
        self.periodo = periodo
        self.lock = threading.RLock()
        self.enlace = None
        self.idn = ""
        self.estado = "desconectada"        # desconectada | conectada | sin_comunicacion
        self.mensaje = ""
        self.medida = {}
        self.consignas = {}
        self.remoto = False
        self.apagar_si_corte = False        # opcion que decide el usuario
        self.fallos = 0
        self._hilo = None
        self._parar = threading.Event()
        self._origen = None                 # "auto" | "COMx" | "sim"
        self._t_frontal = 0.0
        self._t_orden = 0.0                 # ultima orden del panel (para distinguir un ON/OFF dado en el frontal)
        self._salida_ant = None
        self.historia = collections.deque()  # muestras de las ultimas HISTORIA_S
        self.grab = None                    # registro a fichero en marcha (fuente.csv)
        self.vigilante_s = VIGILANTE_S
        self.vigilante = None               # segundos con los que esta activa la vigilancia (None = no activada)
        self._vigilante_saltado = False
        # Encendido bloqueado (2026-11-04, revisión externa): tras un APAGAR, una protección, el vigilante, un OFF en
        # el frontal o un corte, NINGÚN automatismo (plan) puede volver a encender; solo una persona, confirmando.
        self.bloqueo = None                 # motivo (texto) o None
        self._salida_pedida = False         # la ultima orden de salida del panel fue ON

    # ---------------------------------------------------------------- conexion
    def conectar(self, origen, autor):
        self.desconectar(autor, anotar=False)
        if origen == "sim":
            enlace = EnlaceSimulado()
        else:
            puerto = origen
            if origen == "auto":
                puerto, info = detectar()
                if puerto is None:
                    raise RuntimeError(f"No se encuentra la fuente: {info}")
            enlace = EnlaceSerie(puerto)
        with self.lock:
            self.enlace = enlace
            self.idn = enlace.consultar("*IDN?")
            if not self.idn:
                enlace.cerrar()
                self.enlace = None
                raise RuntimeError(f"{enlace.puerto} no responde a *IDN?")
            self._origen = origen
            self._sin_alarmas = False
            self.estado, self.mensaje, self.fallos = "conectada", "", 0
            self.consignas = self._leer_consignas()
            self.equipo = self._leer_equipo()
            cambio = self._adoptar_limites()
            # activa desde una sesion que no cerro bien (panel muerto): se anota para desactivarla al desconectar
            if enlace.consultar(COMANDOS["vig_estado"]).upper() == "ON":
                self.vigilante = numero(enlace.consultar("SYST:COMM:MON:TIM?"))
                self.reg.anotar("panel (automático)", "fuente", "vigilante activo de una sesión anterior",
                                f"{self.vigilante} s; se desactiva al desconectar")
            # las consultas "por confirmar" pueden haber dejado errores en la cola: se limpia
            self.enlace.escribir("*CLS")
        self.reg.anotar(autor, "fuente", "conectar", {"puerto": enlace.puerto, "idn": self.idn,
                                                       "consignas": self.consignas, "topes": self.lim})
        if cambio:
            self.reg.anotar("panel (automático)", "fuente", "topes del panel = límites de la fuente", cambio)
        self._parar.clear()
        self._hilo = threading.Thread(target=self._bucle, daemon=True)
        self._hilo.start()

    def _salida_real(self):
        """Salida leida AHORA de la fuente (con el lock); si no contesta, la ultima conocida."""
        try:
            r = self.enlace.consultar(COMANDOS["salida"]).upper()
            if r:
                return r in ("ON", "1")
        except Exception:
            pass
        return bool(self.consignas.get("salida", self.medida.get("salida")))

    def desconectar(self, autor, anotar=True, si_encendida="error"):
        """si_encendida: "error" (no se desconecta con la salida ON: desde la pagina o al reconectar) o "apagar"
        (cierre del panel). Antes (revisión del 2026-11-04) se desconectaba con la salida ON y además se quitaba el
        vigilante: la salida quedaba encendida sin nadie leyéndola. Si no se puede apagar, el vigilante se deja
        activo para que la fuente apague sola."""
        with self.lock:
            encendida = self.enlace is not None and self.estado == "conectada" and self._salida_real()
        if encendida and si_encendida == "error":
            raise RuntimeError("La salida está encendida: apágala antes de desconectar la fuente "
                               "(o «local» para manejarla desde su frontal)")
        if self.grab:
            self.parar_registro(autor, "fuente desconectada", inmediato=True)
        self._parar.set()
        # esperar a que termine el hilo de lectura anterior (si no, al reconectar podian quedar dos)
        if self._hilo is not None and self._hilo.is_alive() and self._hilo is not threading.current_thread():
            self._hilo.join(timeout=3)
        with self.lock:
            if self.enlace is not None:
                apagada = True
                if self.estado == "conectada" and (encendida or (self.apagar_si_corte and self._salida_real())):
                    self._apagar_seguro("cierre del panel con la salida encendida")
                    apagada = not self._salida_real()
                try:
                    if self.vigilante is not None and self.estado == "conectada" and not apagada:
                        self.reg.anotar("panel (automático)", "fuente", "VIGILANTE SE DEJA ACTIVO",
                                        f"no se pudo apagar la salida: la fuente la apagará sola en {self.vigilante} s")
                    elif self.vigilante is not None and self.estado == "conectada":
                        self._desactivar_vigilante()
                    if self.remoto:
                        self.enlace.escribir(COMANDOS["liberar"])
                    self.enlace.cerrar()
                except Exception:
                    pass
            self.enlace, self.remoto, self.estado, self.vigilante = None, False, "desconectada", None
            # lo leido de esa fuente deja de valer (si no, tras probar con el simulador se seguian dibujando sus valores)
            self.equipo, self.consignas = {}, {}
        if anotar:
            self.reg.anotar(autor, "fuente", "desconectar")

    def _tiempo_vigilante(self):
        # con la lectura muy lenta (hasta 10 s) saltaria sin motivo: al menos 3 periodos de lectura + 2 s
        return int(max(self.vigilante_s, 3 * self.periodo + 2) + 0.999)

    def _activar_vigilante(self):
        """Con el lock tomado y la fuente en remoto. La vigilancia queda guardada en la propia fuente."""
        t = self._tiempo_vigilante()
        self.enlace.escribir("*CLS")
        self.enlace.escribir(COMANDOS["vig_tiempo"].format(t))
        self.enlace.escribir(COMANDOS["vig_activar"].format("ON"))
        err = self.enlace.consultar(COMANDOS["errores"])
        if err.startswith("0") and self.enlace.consultar(COMANDOS["vig_estado"]).upper() == "ON":
            if self.vigilante != t:
                self.reg.anotar("panel (automático)", "fuente", "vigilante activo",
                                f"si pasan {t} s sin mensajes del panel, la fuente apaga la salida sola")
            self.vigilante = t
        else:
            self.vigilante = None
            self.reg.anotar("panel (automático)", "fuente", "NO SE PUDO ACTIVAR EL VIGILANTE", err)

    def _desactivar_vigilante(self):
        """Al desconectar de forma ordenada: si no, un script que tome la fuente despues (p. ej. control_fuente.m)
        perderia el remoto y la salida al estar quieto N s. Hay que estar en remoto para cambiarlo."""
        try:
            if not self.remoto:
                self.enlace.escribir(COMANDOS["bloquear"])
                fin = time.time() + 2
                while "REMOTE" not in self.enlace.consultar(COMANDOS["dueno"]).upper() and time.time() < fin:
                    time.sleep(0.05)
                self.remoto = True
            self.enlace.escribir("*CLS")
            self.enlace.escribir(COMANDOS["vig_activar"].format("OFF"))
            err = self.enlace.consultar(COMANDOS["errores"])
            self.reg.anotar("panel (automático)", "fuente", "vigilante desactivado" if err.startswith("0")
                            else "NO SE PUDO DESACTIVAR EL VIGILANTE", None if err.startswith("0") else err)
        except Exception as e:
            self.reg.anotar("panel (automático)", "fuente", "NO SE PUDO DESACTIVAR EL VIGILANTE", str(e))
        self.vigilante = None

    def _apagar_seguro(self, motivo):
        try:
            self._t_orden = time.time()
            if not self.remoto:                  # sin remoto la fuente no acepta OUTP OFF
                self.enlace.escribir(COMANDOS["bloquear"])
                fin = time.time() + 2
                while "REMOTE" not in self.enlace.consultar(COMANDOS["dueno"]).upper() and time.time() < fin:
                    time.sleep(0.05)
                self.remoto = True
            self._salida_pedida = False      # OFF del propio panel: no es «sin orden del panel»
            self.enlace.escribir(COMANDOS["off"])
            self._evento(f"salida OFF: {motivo}")
            self.reg.anotar("panel (automático)", "fuente", "salida OFF", motivo)
        except Exception:
            self.reg.anotar("panel (automático)", "fuente", "NO SE PUDO APAGAR", f"{motivo}: apagar a mano")

    def _bloquear(self, motivo):
        """Con el lock tomado. Desde aqui ningun automatismo puede encender (ver salida)."""
        if not self.bloqueo:
            self.bloqueo = motivo
            self.reg.anotar("panel (automático)", "fuente", "encendido bloqueado",
                            f"{motivo}: solo se vuelve a encender a mano (Encender, confirmando)")

    def desbloquear(self, autor):
        """Una persona confirma que se puede volver a encender (Encender o Continuar del plan, confirmando)."""
        with self.lock:
            if self.bloqueo:
                self.reg.anotar(autor, "fuente", "encendido desbloqueado", f"estaba bloqueado por: {self.bloqueo}")
                self.bloqueo = None

    # ---------------------------------------------------------------- lectura periodica
    def _leer_consignas(self):
        q = self.enlace.consultar
        return {"V_lim_V": numero(q(COMANDOS["get_v"])), "I_A": numero(q(COMANDOS["get_i"])),
                "P_W": numero(q(COMANDOS["get_p"])), "salida": q(COMANDOS["salida"]).upper() in ("ON", "1")}

    def _leer_equipo(self):
        """Valores nominales, protecciones (OVP/OCP/OPP) y limites de ajuste. None si la fuente no responde."""
        claves = ("v_nom", "i_nom", "p_nom", "ovp", "ocp", "opp",
                  "v_lim_alto", "v_lim_bajo", "i_lim_alto", "i_lim_bajo", "p_lim_alto")
        out = {}
        for k in claves:
            try:
                out[k] = numero(self.enlace.consultar(COMANDOS[k]))
            except Exception:
                out[k] = None
        return out

    def _adoptar_limites(self):
        """Los topes del panel pasan a ser los limites de ajuste leidos de la fuente. Devuelve lo que cambia."""
        q = self.equipo
        nuevos = {"vmax_V": q.get("v_lim_alto"), "imax_A": q.get("i_lim_alto"), "pmax_W": q.get("p_lim_alto")}
        cambio = {k: [self.lim.get(k), v] for k, v in nuevos.items() if v is not None and v != self.lim.get(k)}
        self.lim.update({k: v for k, (_, v) in cambio.items()})
        return cambio

    def _releer_limites(self, motivo):
        """Pueden haberse cambiado en el menu de la fuente (en local): se releen y se adoptan."""
        self.equipo = self._leer_equipo()
        cambio = self._adoptar_limites()
        if cambio:
            self.reg.anotar("panel (automático)", "fuente", f"topes = límites de la fuente ({motivo})", cambio)

    def _leer_alarmas(self):
        if self._sin_alarmas:
            return {k: None for k in self.bits}
        n = numero(self.enlace.consultar(COMANDOS["alarmas"]))
        if n is None:
            # no responde: se deja de preguntar para no frenar la lectura de V/I
            self._sin_alarmas = True
            self.reg.anotar("panel (automático)", "fuente", "la fuente no responde a STAT:QUES:COND?",
                            "estado OV/OC/OP no disponible; revisar el comando")
            return {k: None for k in self.bits}
        return {k: bool(int(n) >> b & 1) for k, b in self.bits.items()}

    def _bucle(self):
        while not self._parar.is_set():
            t0 = time.time()
            with self.lock:
                if self.enlace is None:
                    return
                try:
                    q = self.enlace.consultar
                    v, i = numero(q(COMANDOS["med_v"])), numero(q(COMANDOS["med_i"]))
                    sal = q(COMANDOS["salida"]).upper()
                    if v is None or i is None:
                        raise IOError("respuesta vacia")
                    n = numero(q(COMANDOS["modo"]))
                    modo = next((k for k, b in BITS_MODO.items() if n is not None and int(n) >> b & 1), None)
                    saltado = n is not None and bool(int(n) >> BIT_VIGILANTE & 1)
                    if saltado and not self._vigilante_saltado:
                        self.remoto = False      # la fuente ha salido de remoto: la proxima orden lo retoma
                        self._t_orden = time.time()   # el OFF no es «en el frontal» (ver _nueva_muestra)
                        self.reg.anotar("fuente", "fuente", "VIGILANTE: la fuente apagó la salida y salió de remoto",
                                        f"más de {self.vigilante} s sin mensajes del panel")
                        self._evento("VIGILANTE: salida OFF por falta de comunicación con el panel")
                        self._bloquear("el vigilante de la fuente apagó la salida")
                    self._vigilante_saltado = saltado
                    self.medida = {"V": v, "I": i, "P": v * i, "salida": sal in ("ON", "1"), "modo": modo,
                                   "hora": time.time()}
                    previas = self.alarmas
                    self.alarmas = self._leer_alarmas()
                    for k, activa in self.alarmas.items():
                        if activa and not previas.get(k):
                            self.reg.anotar("fuente", "fuente", f"PROTECCION {k} ACTIVADA",
                                            {"V": v, "I": i, "P": v * i})
                            self._evento(f"PROTECCION {k} ACTIVADA")
                            self._bloquear(f"protección {k} activada")
                    self._nueva_muestra()
                    if time.time() - self._t_frontal >= FRONTAL_S:
                        self._t_frontal = time.time()
                        self._vigilar_frontal()
                    if self.estado == "sin_comunicacion":
                        self.reg.anotar("panel (automático)", "fuente", "comunicación recuperada")
                        if self.apagar_si_corte:
                            self._apagar_seguro("comunicación recuperada tras un corte")
                    self.estado, self.fallos = "conectada", 0
                except Exception as e:
                    self.fallos += 1
                    if self.fallos >= 5 and self.estado != "sin_comunicacion":
                        self.estado = "sin_comunicacion"
                        self.mensaje = f"Sin respuesta de la fuente ({e})"
                        self.reg.anotar("panel (automático)", "fuente", "comunicación perdida", str(e))
                        self._bloquear("comunicación perdida con la fuente")
                    if self.estado == "sin_comunicacion" and self._origen != "sim":
                        self._reabrir()
            # espera a trozos para que un cambio de periodo se aplique al momento
            while not self._parar.is_set() and time.time() - t0 < self.periodo:
                self._parar.wait(min(0.05, max(0.0, self.periodo - (time.time() - t0))))

    def _vigilar_frontal(self):
        """Consignas y control cambiados desde el frontal de la fuente (con el lock tomado)."""
        q = self.enlace.consultar
        if self.remoto and "REMOTE" not in q(COMANDOS["dueno"]).upper():
            self.remoto = False                 # la proxima orden del panel vuelve a tomar el remoto (ver _orden)
            self.reg.anotar("fuente (frontal)", "fuente", "control devuelto al frontal desde la propia fuente")
            self._evento("control: frontal (salida del remoto en la fuente)")
        k = self._leer_consignas()
        if None in (k.get("V_lim_V"), k.get("I_A"), k.get("P_W")):
            return
        cambios = {n: [self.consignas.get(n), k[n]] for n in ("V_lim_V", "I_A", "P_W")
                   if self.consignas.get(n) is None or abs(k[n] - self.consignas[n]) > 1e-6}
        self.consignas = k
        if cambios and any(a is not None for a, _ in cambios.values()):
            nombre = {"V_lim_V": "V", "I_A": "I", "P_W": "P"}
            txt = ", ".join(f"{nombre[n]} {a:g} → {b:g}" for n, (a, b) in cambios.items() if a is not None)
            self.reg.anotar("fuente (frontal)", "fuente", "consignas cambiadas en el frontal", txt)
            self._evento("consigna cambiada en el frontal: " + txt)

    def _reabrir(self):
        try:
            puerto = self.enlace.puerto
            try:
                self.enlace.cerrar()
            except Exception:
                pass
            if self._origen == "auto":
                puerto, _ = detectar()
            if puerto:
                self.enlace = EnlaceSerie(puerto)
                self.remoto = False
        except Exception:
            time.sleep(1.0)

    # ---------------------------------------------------------------- ordenes
    def _tomar_remoto(self):
        """Con el lock tomado: remoto, limites releidos y vigilante activado."""
        self.enlace.escribir(COMANDOS["bloquear"])
        self._releer_limites("al tomar el control")
        # la fuente tarda en pasar a remoto: una orden enviada antes da -221 «Settings conflict»
        # (visto el 2026-11-02 con la primera consigna tras conectar o tras «local»)
        fin = time.time() + 2
        while "REMOTE" not in self.enlace.consultar(COMANDOS["dueno"]).upper() and time.time() < fin:
            time.sleep(0.05)
        self.remoto = True
        self._activar_vigilante()

    def _orden(self, clave, autor, accion, *args, tomar_control=True):
        """tomar_control=False (el plan en marcha): si la fuente ha salido del remoto (frontal, «local», vigilante)
        no se retoma: ControlPerdido, y el plan se pausa. Antes cada paso de una rampa lo retomaba solo."""
        with self.lock:
            if self.enlace is None or self.estado != "conectada":
                raise RuntimeError("La fuente no está conectada")
            if not tomar_control and self.remoto and "REMOTE" not in self.enlace.consultar(COMANDOS["dueno"]).upper():
                self.remoto = False             # se acaba de devolver al frontal (la lectura lo ve cada 1 s)
                self._evento("control: frontal (salida del remoto en la fuente)")
            if not self.remoto:
                if not tomar_control:
                    raise ControlPerdido("la fuente ha salido del control remoto (frontal, «local» o vigilante)")
                self._tomar_remoto()
            self.enlace.escribir("*CLS")    # que SYST:ERR? hable solo de esta orden, no de una anterior
            t_orden = self._t_orden = time.time()   # hora del evento: la del envio, no tras releer (eran 20-40 ms despues)
            if clave in ("on", "off"):
                self._salida_pedida = clave == "on"
            self.enlace.escribir(COMANDOS[clave].format(*args))
            err = self.enlace.consultar(COMANDOS["errores"])
            self.consignas = self._leer_consignas()
            self._evento(accion + ": " + COMANDOS[clave].format(*args)
                         + (f" (ERROR {err})" if err and not err.startswith("0") else ""), t_orden)
        detalle = {"enviado": COMANDOS[clave].format(*args), "fuente_confirma": self.consignas}
        if err and not err.startswith("0"):
            detalle["error_fuente"] = err
        self.reg.anotar(autor, "fuente", accion, detalle)
        if err and not err.startswith("0"):
            raise RuntimeError(f"La fuente respondió con error: {err}")
        return self.consignas

    def fijar(self, autor, V_lim_V=None, I_A=None, P_W=None, tomar_control=True):
        if I_A is not None and not 0 <= I_A <= self.lim["imax_A"]:
            raise ValueError(f"I fuera de 0..{self.lim['imax_A']} A (tope de seguridad)")
        if V_lim_V is not None and not 0 <= V_lim_V <= self.lim["vmax_V"]:
            raise ValueError(f"V fuera de 0..{self.lim['vmax_V']} V (tope de seguridad)")
        if P_W is not None and not 0 <= P_W <= self.lim["pmax_W"]:
            raise ValueError(f"P fuera de 0..{self.lim['pmax_W']} W (tope de seguridad)")
        t = dict(tomar_control=tomar_control)
        if V_lim_V is not None:
            self._orden("set_v", autor, "fijar V", V_lim_V, **t)
        if P_W is not None:
            self._orden("set_p", autor, "fijar P", P_W, **t)
        if I_A is not None:
            self._orden("set_i", autor, "fijar I", I_A, **t)
        return self.consignas

    def salida(self, encender, confirmado, autor, tomar_control=True):
        """autor "plan" = automatismo: no enciende con el encendido bloqueado. Cualquier otro autor es una persona
        (o Claude por la API) y siempre confirma: su ON quita el bloqueo; su OFF (APAGAR) lo pone."""
        if not encender:
            r = self._orden("off", autor, "salida OFF", tomar_control=tomar_control)
            if autor != "plan":
                with self.lock:
                    self._bloquear(f"salida apagada ({autor})")
            return r
        if not confirmado:
            raise ValueError("Encender la salida necesita confirmación explícita")
        # se releen las consignas de la fuente: pueden haber cambiado en su frontal (tras «local»)
        # o se pueden haber bajado los límites del panel despues de fijarlas
        with self.lock:
            if self.enlace is None or self.estado != "conectada":
                raise RuntimeError("La fuente no está conectada")
            if autor == "plan" and self.bloqueo:
                raise EncendidoBloqueado(f"encendido bloqueado ({self.bloqueo}): solo se vuelve a encender a mano")
            k = self._leer_consignas()
            if k.get("salida"):
                # ya encendida: no se repite la orden ni queda otro «ON» en el registro (2026-11-02)
                return {**self.consignas, "aviso": "La salida ya estaba encendida"}
            if not self.remoto:
                if not tomar_control:
                    raise ControlPerdido("la fuente ha salido del control remoto (frontal, «local» o vigilante)")
                self._tomar_remoto()
            else:
                self._releer_limites("antes de encender")
            if self.vigilante is None:
                self._activar_vigilante()
            if self.vigilante is None:
                # sin vigilante, si el panel se cuelga la salida quedaria encendida sin nadie leyendola
                raise RuntimeError("No se enciende: no se pudo activar el vigilante de la fuente (ver historial)")
        fuera = [f"{n} {v:g} > {lim:g}" for n, v, lim in (("I", k["I_A"], self.lim["imax_A"]),
                 ("V", k["V_lim_V"], self.lim["vmax_V"]), ("P", k["P_W"], self.lim["pmax_W"]))
                 if v is not None and v > lim]
        if fuera:
            raise ValueError("No se enciende: las consignas de la fuente superan los topes del panel ("
                             + ", ".join(fuera) + "). Bájalas antes de encender")
        r = self._orden("on", autor, "salida ON", tomar_control=tomar_control)
        if autor != "plan":
            self.desbloquear(autor)
        return r

    def local(self, autor):
        with self.lock:
            if self.enlace is not None:
                self.enlace.escribir(COMANDOS["liberar"])
                self.remoto = False
        self.reg.anotar(autor, "fuente", "devolver el control al frontal de la fuente (local)")

    def opciones(self, autor, apagar_si_corte=None, limites=None, confirmado=False, periodo=None):
        if periodo is not None:
            periodo = float(periodo)
            if not PERIODO_MIN <= periodo <= PERIODO_MAX:
                raise ValueError(f"La lectura de la fuente va de {PERIODO_MIN:g} a {PERIODO_MAX:g} s".replace(".", ","))
            if periodo != self.periodo:
                self.reg.anotar(autor, "fuente", f"lectura cada {self.periodo:g} → {periodo:g} s".replace(".", ","))
                self.periodo = periodo
                with self.lock:              # el tiempo del vigilante depende del periodo de lectura
                    if self.vigilante is not None and self.remoto and self.enlace is not None:
                        self._activar_vigilante()
            if apagar_si_corte is None and not limites:
                return
        if apagar_si_corte is not None:
            self.apagar_si_corte = bool(apagar_si_corte)
        if limites:
            self._escribir_limites(autor, limites, confirmado)
        self.reg.anotar(autor, "fuente", "opciones", {"apagar_si_corte": self.apagar_si_corte, "limites": self.lim})

    def _escribir_limites(self, autor, limites, confirmado):
        """Los topes son los limites de ajuste de la fuente: se escriben en ella y se releen."""
        nuevos = {k: float(v) for k, v in limites.items() if k in self.lim and v not in (None, "")}
        if any(v < 0 for v in nuevos.values()):
            raise ValueError("Los topes no pueden ser negativos")
        with self.lock:
            if self.enlace is None or self.estado != "conectada":
                raise RuntimeError("Conecta la fuente: los topes son los límites de la fuente y se guardan en ella")
            self._releer_limites("antes de cambiarlos")
            q, k = self.equipo, self._leer_consignas()
            nom = {"vmax_V": q.get("v_nom"), "imax_A": q.get("i_nom"), "pmax_W": q.get("p_nom")}
            unidad = {"vmax_V": "V", "imax_A": "A", "pmax_W": "W"}
            nombre = {"vmax_V": "V", "imax_A": "I", "pmax_W": "P"}
            fuera = [f"{nombre[c]} {v:g} > {nom[c]:g} {unidad[c]} del equipo" for c, v in nuevos.items()
                     if nom[c] is not None and v > nom[c]]
            if fuera:
                raise ValueError("Por encima del equipo: " + ", ".join(fuera))
            # la fuente no deja un limite por debajo de la consigna actual: se avisa en vez de dejar -221
            cons = {"vmax_V": k.get("V_lim_V"), "imax_A": k.get("I_A"), "pmax_W": k.get("P_W")}
            bajo = [f"{nombre[c]} {cons[c]:g} > {v:g}" for c, v in nuevos.items() if cons[c] is not None and cons[c] > v]
            if bajo:
                raise ValueError("Baja antes la consigna: " + ", ".join(bajo) + " (la fuente no admite un tope por debajo de la consigna)")
            suben = [nombre[c] for c, v in nuevos.items() if v > self.lim[c]]
            # subir un limite de seguridad es decision del usuario: hace falta confirmarlo
            if suben and not confirmado:
                raise ValueError("Subir los topes (" + ", ".join(suben) + ") necesita confirmación explícita del usuario")
            orden = {"vmax_V": ("set_v_lim", "tope V"), "imax_A": ("set_i_lim", "tope I"), "pmax_W": ("set_p_lim", "tope P")}
            for c, v in nuevos.items():
                if v != self.lim[c]:
                    self._orden(orden[c][0], autor, f"fijar {orden[c][1]} en la fuente", v)
            self._releer_limites("tras cambiarlos")

    # ---------------------------------------------------------------- registro (fuente.csv)
    def _muestra(self, hora=None):
        return dict(self.medida, hora=hora or self.medida.get("hora") or time.time(),
                    cons=dict(self.consignas), alarmas=dict(self.alarmas))

    def _nueva_muestra(self):
        """En el bucle de lectura (con el lock): a la memoria y, si se esta registrando, al fichero."""
        m = self._muestra()
        sal = bool(m.get("salida"))
        if self._salida_ant is not None and sal != self._salida_ant and m["hora"] - self._t_orden > 1.5:
            texto = f"salida {'ON' if sal else 'OFF'} en el frontal (OUTP {'ON' if sal else 'OFF'})"
            if not sal and any(self.alarmas.values()):
                texto = "salida OFF por protección (OUTP OFF)"
            self.reg.anotar("fuente (frontal)", "fuente", texto)
            self._evento(texto, m["hora"])
        if self._salida_pedida and not sal and m["hora"] - self._t_orden > 0.5:
            # apagada sin que el panel lo pidiera (protección, vigilante, frontal), aunque la lectura no llegara a
            # verla encendida o sea justo tras una orden: antes, con menos de 1,5 s desde la última orden, no se
            # tomaba como ajena (prueba del 2026-11-04)
            alarmas = [k for k, on in self.alarmas.items() if on]
            self._bloquear(f"salida apagada sin orden del panel (protección {', '.join(alarmas)})" if alarmas
                           else "salida apagada sin orden del panel (frontal, protección o vigilante)")
            self._salida_pedida = False
        self._salida_ant = sal
        self.historia.append(m)
        while self.historia and self.historia[0]["hora"] < m["hora"] - HISTORIA_S:
            self.historia.popleft()
        g = self.grab
        if not g:
            return
        self._fila(m)
        if g["tras_apagar_s"] is not None and g["salida_previa"] and not m.get("salida") and g["fin"] is None:
            g["fin"] = m["hora"] + g["tras_apagar_s"]
            self._fila(m, f"salida apagada: el registro sigue {g['tras_apagar_s']:g} s")
        g["salida_previa"] = bool(m.get("salida"))
        if g["fin"] is not None and m["hora"] >= g["fin"]:
            self.parar_registro("panel (automático)", "fin previsto")

    def marca(self, texto):
        """Evento suelto en fuente.csv (si se esta registrando), p. ej. las etapas del ensayo programado."""
        with self.lock:
            self._evento(texto)

    def _evento(self, texto, hora=None):
        if self.grab:
            self._fila(self._muestra(hora or time.time()), texto)

    def exportar_csv(self, fichero, t0, t1, cabecera=None):
        """✂ Tramo (2026-11-04): lo que hay en memoria entre t0 y t1 (hora Unix) a 'fichero', con el formato de
        fuente.csv (t_rel_s desde t0). Devuelve el numero de filas."""
        with self.lock:
            filas = [m for m in self.historia if t0 <= m["hora"] <= t1]
            periodo, idn, lim = self.periodo, self.idn, dict(self.lim)
        with open(fichero, "w", encoding="utf-8", newline="") as f:
            f.write("# Registro de la fuente (panel de laboratorio). MATLAB: readtable(ruta, 'CommentStyle', '#') u osc(ruta)\n")
            for c, v in [("equipo", idn), ("inicio", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0))),
                         ("t_rel_s", "segundos desde el inicio del tramo"), ("lectura_cada_s", f"{periodo:g}"),
                         ("topes", f"{lim['vmax_V']:g} V, {lim['imax_A']:g} A, {lim['pmax_W']:g} W"),
                         ("hora", "reloj del PC (hora local); t_unix_s en segundos Unix")] + list((cabecera or {}).items()):
                f.write(f"# {c}: {v}\n")
            f.write(",".join(COLUMNAS) + "\n")
            g = {"f": f, "t0": t0, "filas": 0}
            for m in filas:
                self._fila(m, g=g)
        return len(filas)

    def _fila(self, m, evento="", g=None):
        g = g or self.grab
        k, a, t = m.get("cons") or {}, m.get("alarmas") or {}, m["hora"]
        num = lambda x, f: "" if x is None else format(x, f)
        bit = lambda x: "" if x is None else str(int(bool(x)))
        campos = [time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}",
                  f"{t:.3f}", f"{t - g['t0']:.3f}", num(m.get("V"), ".3f"), num(m.get("I"), ".4f"),
                  num(m.get("P"), ".3f"), num(k.get("V_lim_V"), ".3f"), num(k.get("I_A"), ".4f"),
                  num(k.get("P_W"), ".1f"), m.get("modo") or "", bit(m.get("salida")),
                  bit(a.get("OV")), bit(a.get("OC")), bit(a.get("OP")),
                  '"' + evento.replace('"', "'") + '"' if evento else ""]
        g["f"].write(",".join(campos) + "\n")
        g["f"].flush()
        g["filas"] += 1

    def iniciar_registro(self, fichero, autor, pre_s=30.0, duracion_s=None, tras_apagar_s=None, cabecera=None,
                         apagar_al_terminar=False, cola_s=COLA_APAGADO_S):
        """Escribe la fuente en 'fichero' (fuente.csv) desde ahora, con los ultimos pre_s segundos que ya
        estaban en memoria (t_rel_s < 0). Termina con parar_registro, al cumplir duracion_s o
        tras_apagar_s segundos despues de apagar la salida. Con apagar_al_terminar, al terminar (por lo que sea)
        se apaga la salida y se graban COLA_APAGADO_S mas para que el apagado quede en el fichero."""
        pre_s = max(0.0, float(pre_s or 0))
        with self.lock:
            if self.grab:
                raise RuntimeError(f"Ya se está registrando la fuente en {self.grab['fichero']}")
            if self.enlace is None or self.estado != "conectada":
                raise RuntimeError("La fuente no está conectada")
            t0 = time.time()
            f = open(fichero, "w", encoding="utf-8", newline="")
            f.write("# Registro de la fuente (panel de laboratorio). MATLAB: readtable(ruta, 'CommentStyle', '#') u osc(ruta)\n")
            for c, v in [("equipo", self.idn), ("inicio", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0))),
                         ("t_rel_s", "segundos desde el inicio; negativo = pre-registro (lo que ya estaba en memoria)"),
                         ("lectura_cada_s", f"{self.periodo:g}"), ("pre_registro_s", f"{pre_s:g}"),
                         ("topes", f"{self.lim['vmax_V']:g} V, {self.lim['imax_A']:g} A, {self.lim['pmax_W']:g} W"),
                         ("hora", "reloj del PC (hora local); t_unix_s en segundos Unix")] + list((cabecera or {}).items()):
                f.write(f"# {c}: {v}\n")
            f.write(",".join(COLUMNAS) + "\n")
            self.grab = {"fichero": fichero, "f": f, "t0": t0, "filas": 0, "autor": autor, "pre_s": pre_s,
                         "fin": t0 + float(duracion_s) if duracion_s else None,
                         "tras_apagar_s": None if tras_apagar_s in (None, "") else float(tras_apagar_s),
                         "salida_previa": bool(self.medida.get("salida")),
                         "apagar": bool(apagar_al_terminar), "apagado": False,
                         "cola_s": max(0.0, float(COLA_APAGADO_S if cola_s in (None, "") else cola_s))}
            for m in list(self.historia):
                if m["hora"] >= t0 - pre_s:
                    self._fila(m)
            self._fila(self._muestra(t0), "inicio del registro")
        self.reg.anotar(autor, "fuente", "registro de la fuente iniciado",
                        {"fichero": fichero, "pre_registro_s": pre_s, "duracion_s": duracion_s,
                         "tras_apagar_s": tras_apagar_s, "apagar_al_terminar": bool(apagar_al_terminar)})

    def parar_registro(self, autor, motivo="parado", inmediato=False):
        with self.lock:
            g = self.grab
            if not g:
                return None
            if g["apagar"] and not g["apagado"]:
                # «apagar al terminar»: primero la salida OFF y unos segundos mas de registro
                g["apagado"] = True
                try:
                    if self.medida.get("salida"):
                        self._orden("off", "panel (automático)", "salida OFF (fin del registro)")
                except Exception as e:
                    self.reg.anotar("panel (automático)", "fuente", "NO SE PUDO APAGAR al terminar el registro",
                                    f"{e}: apagar a mano")
                if not inmediato:
                    g["fin"] = time.time() + g["cola_s"]
                    g["motivo_final"] = motivo
                    self._fila(self._muestra(time.time()), f"{motivo}: salida apagada, se graban {g['cola_s']:g} s más")
                    return {"fichero": g["fichero"], "apagando": True}
            motivo = g.get("motivo_final") or motivo
            self._fila(self._muestra(time.time()), f"fin del registro: {motivo}")
            g["f"].close()
            self.grab = None
        info = {"fichero": g["fichero"], "filas": g["filas"], "duracion_s": round(time.time() - g["t0"], 1),
                "motivo": motivo}
        self.reg.anotar(autor, "fuente", "registro de la fuente terminado", info)
        return info

    def historia_desde(self, desde=0.0):
        """Muestras en memoria desde 'desde' (hora Unix), compactas para la grafica en vivo:
        [hora, V, I, P, salida, modo, V_consigna, I_consigna, P_consigna]."""
        with self.lock:
            return [[round(m["hora"], 3), m.get("V"), m.get("I"), m.get("P"), m.get("salida"), m.get("modo"),
                     (m.get("cons") or {}).get("V_lim_V"), (m.get("cons") or {}).get("I_A"), (m.get("cons") or {}).get("P_W")]
                    for m in self.historia if m["hora"] > desde]

    def resumen(self):
        return {"estado": self.estado, "mensaje": self.mensaje, "idn": self.idn,
                "puerto": getattr(self.enlace, "puerto", None), "simulada": isinstance(self.enlace, EnlaceSimulado),
                "medida": self.medida, "consignas": self.consignas, "remoto": self.remoto,
                "equipo": self.equipo, "alarmas": self.alarmas, "bits_alarma": self.bits,
                "comandos_por_confirmar": [],
                "apagar_si_corte": self.apagar_si_corte, "bloqueo": self.bloqueo, "limites": self.lim, "periodo_s": self.periodo,
                "vigilante_s": self.vigilante,
                "registro": None if not self.grab else
                {"fichero": self.grab["fichero"], "desde": self.grab["t0"], "filas": self.grab["filas"],
                 "fin": self.grab["fin"], "tras_apagar_s": self.grab["tras_apagar_s"]},
                "memoria_s": round(self.historia[-1]["hora"] - self.historia[0]["hora"], 1) if self.historia else 0}
