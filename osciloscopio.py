"""Control del osciloscopio R&S MXO34 para el panel.

Todos los comandos SCPI de PARAMS se comprobaron en el equipo el 2026-10-28
(firmware 2.8.2.0). Lo que no aparece aqui no se toca desde el panel.
"""
import collections
import contextlib
import copy
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pyvisa
from pyvisa import constants

import almacen
import web_osc

# campo -> (consulta, plantilla de escritura o None si es solo lectura, tipo)
HORIZONTAL = {
    "escala_s_div": ("TIMebase:SCALe?", "TIMebase:SCALe {}", float),
    "rango_s": ("TIMebase:RANGe?", None, float),
    "posicion_s": ("TIMebase:HORizontal:POSition?", "TIMebase:HORizontal:POSition {}", float),
    "referencia_pct": ("TIMebase:REFerence?", "TIMebase:REFerence {}", float),
    "muestreo_Sa_s": ("ACQuire:SRATe?", "ACQuire:SRATe:MODE MAN;:ACQuire:SRATe {}", float),
    "muestreo_modo": ("ACQuire:SRATe:MODE?", "ACQuire:SRATe:MODE {}", str),
    "puntos": ("ACQuire:POINts?", "ACQuire:POINts:MODE MAN;:ACQuire:POINts {}", float),
    "puntos_modo": ("ACQuire:POINts:MODE?", "ACQuire:POINts:MODE {}", str),
    "adquisicion": ("ACQuire:TYPE?", "ACQuire:TYPE {}", str),
    "roll": ("TIMebase:ROLL:ENABle?", "TIMebase:ROLL:ENABle {}", str),
    "roll_activo": ("TIMebase:ROLL:STATe?", None, str),
    # que parte del registro se exporta / se transfiere por SCPI. El usuario usa DISPlay (lo visible)
    "ambito_exportacion": ("EXPort:WAVeform:SCOPe?", "EXPort:WAVeform:SCOPe {}", str),
}
DISPARO = {
    "modo": ("TRIGger:MODE?", "TRIGger:MODE {}", str),
    "fuente": ("TRIGger:EVENt1:SOURce?", "TRIGger:EVENt1:SOURce {}", str),
    "tipo": ("TRIGger:EVENt1:TYPE?", None, str),
    "nivel_V": ("TRIGger:EVENt1:LEVel1:VALue?", "TRIGger:EVENt1:LEVel1:VALue {}", float),
    "pendiente": ("TRIGger:EVENt1:EDGE:SLOPe?", "TRIGger:EVENt1:EDGE:SLOPe {}", str),
}
CANAL = {
    "activo": ("CHANnel{c}:STATe?", "CHANnel{c}:STATe {}", str),
    "escala_V_div": ("CHANnel{c}:SCALe?", "CHANnel{c}:SCALe {}", float),
    "offset_V": ("CHANnel{c}:OFFSet?", "CHANnel{c}:OFFSet {}", float),
    "posicion_div": ("CHANnel{c}:POSition?", "CHANnel{c}:POSition {}", float),
    "acoplamiento": ("CHANnel{c}:COUPling?", "CHANnel{c}:COUPling {}", str),
    "ancho_banda_Hz": ("CHANnel{c}:BANDwidth?", "CHANnel{c}:BANDwidth {}", float),
    "sonda": ("PROBe{c}:SETup:NAME?", None, str),
    "atenuacion": ("PROBe{c}:SETup:ATTenuation?", None, float),
    "unidad": ("PROBe{c}:SETup:ATTenuation:UNIT?", None, str),   # V o A (sonda de corriente)
    "etiqueta": ("DISPlay:SIGNal:LABel? C{c}", None, str),       # la que se pone en el osciloscopio (p. ej. Icab)
}
N_CANALES = 4
DATALOGGER = r"C:\Proyectos\_HERRAMIENTAS\Osciloscopio_datalogger\datalogger.py"
# Vista en vivo del osciloscopio sin ensayo ni registro (2026-11-04): un datalogger a un fichero TEMPORAL, para ver el
# osciloscopio en En vivo y, al pararla, ampliar sus puntos reales o guardar un tramo. Se borra al empezar otra vista o
# al arrancar el panel.
VISTA_DIR = os.path.join(tempfile.gettempdir(), "panel_vista_osc")
VISTA_SR_MAX = 1e6           # por canal: 2 canales a 1 MSa/s = 4 MB/s = 14 GB/h
VISTA_MAX_BYTES = 20e9       # se para sola al llegar aqui
# la crea datalogger.py mientras registra (lo lance el panel o no): el panel deja de leer
# Tiempo minimo en REMOTO antes de devolver a LOCAL. Con el GTL inmediato (pocos ms tras el primer comando)
# el MXO vuelve a local pero deja de dibujar las trazas hasta un cambio del horizontal o un reinicio; con
# >= 0,25 s se recupera siempre (medido el 2026-10-29 con refresco en remoto OFF, firmware 2.8.2.0).
MIN_REMOTO_S = 0.5
MARCA_DATALOGGER = os.path.join(os.environ.get("TEMP", r"C:\Windows\Temp"), "mxo_datalogger_activo.marca")
# Vigilante del datalogger (2026-11-02: se quedo 6 min colgado en la configuracion y no atendia a Parar).
# Informa al menos cada 10 s mientras registra: sin ninguna linea en DL_SIN_SENAL_S se da por atascado;
# tras pedir Parar se le dejan DL_PARAR_S para cerrar solo. En los dos casos el panel lo cierra a la fuerza
# y vuelve a cargar la configuracion que el datalogger guardo al empezar (si llego a guardarla).
DL_SIN_SENAL_S = 90
DL_PARAR_S = 20
DL_CONFIG_PREVIA = "/home/storage/userData/datalogger_config_previa.set"
# Sin la unidad de red montada (se pierde al reiniciar el MXO), la captura se guarda aqui, en el disco del MXO, se
# trae al PC con MMEMory:DATA? (1,4 MB en 0,04 s, medido el 2026-11-02) y se borra del MXO.
TMP_OSC = "/home/storage/userData/panel_tmp"
# Tras perder la comunicacion (p. ej. al reiniciar el MXO) no se abre sesion hasta que el puerto HiSLIP acepta
# conexiones: conectar antes de que muestre la pantalla interrumpe su arranque («Failed to update the Frontpanel
# Controller» y el frontal fisico sin respuesta, 2026-11-03). Medido: la web (5900) y el ping vuelven antes que la
# pantalla; el 4880 se abre ~3 s antes de verla, y comprobarlo (abrir y cerrar, sin enviar nada) no la estropea.
PUERTO_HISLIP = 4880
# IP por DHCP: ultima IPv4 buena, para cuando no se encuentre de ninguna otra forma (sobrevive al reinicio del panel)
FICHERO_ULTIMA_IP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "registro", "ultima_ip_osciloscopio.txt")


def _mdns_ipv4(nombre, timeout=1.0):
    """Pregunta por mDNS (224.0.0.251:5353) la IPv4 de un nombre .local, como hace Windows por debajo. Tras reiniciar
    el MXO, Resolve-DnsName la daba pero gethostbyname de Python fallaba durante minutos (2026-11-03)."""
    import struct
    pregunta = b"".join(bytes([len(e)]) + e.encode() for e in nombre.rstrip(".").split(".")) + b"\0"
    # id 0, sin flags, 1 pregunta; tipo A, clase IN con el bit «respuesta unicast» (QU)
    paquete = struct.pack(">6H", 0, 0, 1, 0, 0, 0) + pregunta + struct.pack(">2H", 1, 0x8001)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.sendto(paquete, ("224.0.0.251", 5353))
        fin = time.monotonic() + timeout
        while time.monotonic() < fin:
            datos, _ = s.recvfrom(4096)
            ip = _mdns_respuesta_a(datos, nombre)
            if ip:
                return ip
    except OSError:
        pass
    finally:
        s.close()
    return None


def _mdns_respuesta_a(datos, nombre):
    """Primera respuesta tipo A para 'nombre' en un paquete DNS (o None)."""
    import struct

    def leer_nombre(i):
        partes, saltado, fin = [], False, None
        while True:
            n = datos[i]
            if n == 0:
                i += 1
                break
            if n & 0xC0 == 0xC0:          # puntero de compresion
                if not saltado:
                    fin = i + 2
                i, saltado = ((n & 0x3F) << 8) | datos[i + 1], True
                continue
            partes.append(datos[i + 1:i + 1 + n].decode(errors="replace"))
            i += 1 + n
        return ".".join(partes).lower(), (fin if saltado else i)

    try:
        _, flags, nq, nr, _, _ = struct.unpack(">6H", datos[:12])
        if not flags & 0x8000:
            return None
        i = 12
        for _ in range(nq):
            _, i = leer_nombre(i)
            i += 4
        for _ in range(nr):
            nom, i = leer_nombre(i)
            tipo, _, _, largo = struct.unpack(">HHIH", datos[i:i + 10])
            i += 10
            if tipo == 1 and largo == 4 and nom == nombre.rstrip(".").lower():
                return socket.inet_ntoa(datos[i:i + 4])
            i += largo
    except (IndexError, struct.error):
        pass
    return None


def _arp_ipv4(mac):
    """IPv4 de una MAC en la tabla ARP de Windows (solo si el PC ha hablado hace poco con ese equipo)."""
    if not mac:
        return None
    mac = mac.lower().replace(":", "-")
    try:
        r = subprocess.run(["arp", "-a"], capture_output=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for linea in r.stdout.decode(errors="replace").splitlines():
            campos = linea.split()
            if len(campos) >= 2 and campos[1].lower() == mac:
                return campos[0]
    except Exception:
        pass
    return None


def _valor(txt, tipo):
    if txt is None:               # el osciloscopio no admite esa consulta ahora (ver NO_DISPONIBLES)
        return None
    txt = txt.strip().strip('"')
    return tipo(txt) if tipo is float else txt


def _indice_1245(v):
    """Posicion de v en la serie de las ruletas de escala del MXO (1-2-4-5 por decada, medida el 2026-11-02);
    None si v no es de la serie."""
    import math
    if not v or v <= 0:
        return None
    e = math.floor(math.log10(v) + 1e-9)
    m = v / 10 ** e
    for i, x in enumerate((1, 2, 4, 5)):
        if abs(m - x) < 1e-6 * x:
            return 4 * e + i
    return None


class Osciloscopio:
    def __init__(self, host, registro):
        self.host = host
        self.reg = registro
        self.lock = threading.RLock()
        self.inst = None
        self.modo = "desconectado"   # desconectado | conectado | manual | datalogger | error
        self.mensaje = ""
        self.estado = None           # ultimo estado leido
        self.hora_lectura = None
        self.idn = ""
        self.dl = {"proceso": None, "salida": [], "args": None, "fichero": None, "fin": None}
        # grafica en vivo: resumen de cada bloque que lee el datalogger (sin lecturas extra), ultimas 2 h
        self.vivo = {"info": None, "datos": {}}
        self._pendientes = {}        # cambios a mano aun sin anotar (se agrupan mientras se gira la ruleta)
        self._t_cambio = 0.0
        self.sincronizar = True      # el usuario la puede pausar (p.ej. durante una captura delicada)
        self.pausa_motivo = ""
        self._vista_hasta = 0.0
        self.registro = None         # ultimo "donde cae el registro" medido bajo demanda
        # "Registro ajustado a la pantalla": sample rate y record length acoplados (RL = SR x span). Al cambiar
        # uno desde el panel (o la escala de tiempo) el otro se recalcula para llenar justo la pantalla.
        self.ajuste_pantalla = False
        self._ip, self._t_ip = host, 0.0
        self._t_remoto = 0.0         # inicio de la operacion en curso (ver MIN_REMOTO_S)
        self._anidado = 0            # operaciones dentro de una misma pasada en remoto (ver _pasada)
        self._web = web_osc.FrontalWeb(host)   # frontal virtual por la web (flechas sin pasar a remoto)
        self._en_remoto = False
        self._perdido = False        # se perdio la comunicacion: antes de reconectar, ver _comprobar_arranque
        self.mac = None              # osciloscopio.mac de config.json: para encontrarlo en la tabla ARP
        try:                         # ultima IPv4 buena (si no, osciloscopio.ip_respaldo de config.json)
            with open(FICHERO_ULTIMA_IP, encoding="utf-8") as f:
                self._ip_conocida = f.read().strip() or None
        except OSError:
            self._ip_conocida = None

    # ---------------------------------------------------------------- conexion
    def ip(self):
        # se resuelve como mucho cada 60 s: /api/estado se pide cada segundo y sin red bloquearia
        if time.time() - self._t_ip > 60:
            self._t_ip = time.time()
            self._ip = self._ipv4()
        return self._ip

    def _ipv4(self):
        """IPv4 del osciloscopio (DHCP: puede cambiar). Por orden: el nombre, una pregunta mDNS propia, su MAC en la
        tabla ARP y la ultima conocida. Tras reiniciar el MXO, gethostbyname fallo minutos (2026-11-03)."""
        ip = None
        try:
            ip = socket.gethostbyname(self.host)
        except OSError:
            if self.host.lower().endswith(".local"):
                ip = _mdns_ipv4(self.host)
            ip = ip or _arp_ipv4(self.mac)
        if ip and ip != self._ip_conocida:
            if self._ip_conocida:
                self.reg.anotar("panel", "osciloscopio", "IP nueva", {"antes": self._ip_conocida, "ahora": ip})
            self._ip_conocida = ip
            try:
                with open(FICHERO_ULTIMA_IP, "w", encoding="utf-8") as f:
                    f.write(ip)
            except OSError:
                pass
        return self._ip_conocida or self.host

    def _responde_ping(self):
        """Ping sin SCPI: no toca el osciloscopio mientras arranca."""
        try:
            r = subprocess.run(["ping", "-4", "-n", "1", "-w", "1000", self._ipv4()], capture_output=True, timeout=5,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return r.returncode == 0 and b"TTL=" in r.stdout.upper()
        except Exception:
            return False

    def _sin_comunicacion(self, mensaje, perdido=True):
        self._cerrar(devolver_local=False)
        self.modo = "error"
        self.mensaje = f"Sin comunicación con el osciloscopio: {mensaje}"
        self._perdido = self._perdido or perdido
        return self.mensaje

    def _comprobar_arranque(self):
        """Tras una perdida, solo se deja conectar cuando el puerto HiSLIP acepta conexiones (se abre y se cierra
        sin enviar nada), como hace la web con su pantalla."""
        try:
            socket.create_connection((self._ipv4(), PUERTO_HISLIP), timeout=1).close()
        except OSError:
            raise RuntimeError(self._sin_comunicacion("no responde (apagado o arrancando); se reintenta cada 2 s"))

    def _conectar(self):
        if self.inst is None:
            if self._perdido:
                self._comprobar_arranque()
            NO_DISPONIBLES.clear()
            self.inst = pyvisa.ResourceManager().open_resource(f"TCPIP::{self._ipv4()}::hislip0")
            self.inst.timeout = 5000
            self.idn = self.inst.query("*IDN?").strip()
            if self.modo in ("desconectado", "error"):
                self.modo = "conectado"
            self.mensaje = ""
            if self._perdido:
                self._perdido = False
                self._t_ip = 0.0                 # tras reiniciar, el DHCP puede haberle dado otra IP
                self.reg.anotar("panel", "osciloscopio", "reconectado")

    def _datalogger_vivo(self):
        p = self.dl.get("proceso")
        return p is not None and p.poll() is None

    def _a_local(self):
        # ver MIN_REMOTO_S: se espera a que el osciloscopio haya terminado de pasar a remoto
        espera = self._t_remoto + MIN_REMOTO_S - time.monotonic()
        if espera > 0:
            time.sleep(espera)
        self.inst.control_ren(constants.RENLineOperation.address_gtl)
        self._en_remoto = False

    def _cerrar(self, devolver_local=True):
        if self.inst is not None:
            try:
                if devolver_local:
                    self._a_local()
            except Exception:
                pass
            try:
                self.inst.close()
            except Exception:
                pass
            self.inst = None

    def _bloqueado(self):
        if self.modo == "manual":
            return "Modo manual: el panel no toca el osciloscopio. Pulsa «Volver a modo panel» para usarlo desde aquí"
        if self.modo == "datalogger" or self._datalogger_vivo():
            return "Hay un datalogger en marcha: el osciloscopio no se puede tocar hasta pararlo"
        if marca_datalogger_activa():
            return "Hay un datalogger lanzado fuera del panel en marcha: el osciloscopio no se puede tocar"
        return None

    @contextlib.contextmanager
    def _pasada(self):
        """Varias operaciones (leer, escribir, releer...) en UNA sola entrada en remoto: el GTL se manda al salir
        de la pasada y no tras cada una. Antes cada ajuste ponia el osciloscopio en remoto dos o tres veces (leer
        antes, escribir, releer), cada una con su MIN_REMOTO_S: la pantalla «Remote mode» duraba el doble
        (propuesta aceptada por el usuario el 2026-11-02)."""
        with self.lock:
            self._anidado += 1
            try:
                yield
            finally:
                self._anidado -= 1
                if self._anidado == 0 and self._en_remoto and self.inst is not None:
                    try:
                        self._a_local()
                    except Exception:
                        pass

    def _con_equipo(self, fn):
        with self.lock:
            # la comprobacion va dentro del lock: asi nadie cambia de modo entre comprobar y actuar
            motivo = self._bloqueado()
            if motivo:
                raise RuntimeError(motivo)
            try:
                if not self._en_remoto:          # dentro de una pasada ya en remoto: cuenta desde la primera
                    self._t_remoto = time.monotonic()
                self._conectar()
                self._en_remoto = True
                return fn(self.inst)
            except pyvisa.errors.VisaIOError as e:
                # un timeout con el equipo respondiendo al ping (consulta lenta, p. ej. HEADer? en Roll) no es un
                # reinicio: se reconecta sin esperar
                perdido = e.error_code != constants.StatusCode.error_timeout or not self._responde_ping()
                raise RuntimeError(self._sin_comunicacion(e, perdido=perdido))
            finally:
                # Cualquier comando pone el osciloscopio en REMOTO (pantalla y ruletas bloqueadas).
                # Se devuelve a LOCAL nada mas terminar: el usuario lo puede tocar siempre, salvo
                # el instante en que el panel lee o escribe. La sesion sigue abierta.
                # Dentro de una pasada (_pasada) el GTL lo manda la pasada al terminar.
                if self.inst is not None and self._anidado == 0:
                    try:
                        self._a_local()
                    except Exception:
                        pass

    def forzar_manual(self, autor):
        with self.lock:
            if self.modo == "datalogger" or self._datalogger_vivo():
                raise RuntimeError("Hay un datalogger en marcha: páralo antes de pasar a modo manual")
            self._cerrar(devolver_local=True)
            self.modo = "manual"
        self.reg.anotar(autor, "osciloscopio", "modo manual",
                        "el panel (y Claude) no envían nada al osciloscopio hasta «Volver a modo panel»")

    def retomar(self, autor):
        with self.lock:
            if self.modo == "datalogger" or self._datalogger_vivo():
                raise RuntimeError("Hay un datalogger en marcha: el osciloscopio no se puede tocar hasta pararlo")
            self.modo = "desconectado"
            self.reg.anotar(autor, "osciloscopio", "volver a modo panel")
            estado = self.leer(autor)
            cambios = self._volcar_pendientes(forzar=True)   # lo hecho a mano durante el modo manual
            return {**estado, "cambios_a_mano": cambios}

    # ---------------------------------------------------------------- lectura
    def medir_registro(self):
        """Lectura puntual (bajo demanda) de donde cae el registro. Tarda hasta ~0,5 s en Roll.

        Se lee antes la configuracion y se guarda con la medida: si luego cambia la escala, el
        sample rate, etc., la pagina sabe que la medida ya no vale y no la explica."""
        with self._pasada():
            estado = self.leer("panel")
            reg = self._con_equipo(lambda o: _leer_registro(o, estado["canales"]))
            if reg:
                h = estado["horizontal"]
                reg["config"] = {k: h[k] for k in ("escala_s_div", "muestreo_Sa_s", "puntos", "referencia_pct",
                                                    "posicion_s", "roll_activo")}
            self.registro = reg
            return reg

    def leer(self, autor="panel"):
        # todo dentro del lock: una lectura periodica no puede intercalarse con un ajuste y
        # reponer un estado viejo (ni anotar como "a mano" un ajuste del panel)
        with self.lock:
            nuevo = self._con_equipo(_leer_todo)
            self._acumular(nuevo)
            self.estado = nuevo
            self.hora_lectura = time.time()
            self._volcar_pendientes()
            return nuevo

    def _acumular(self, nuevo):
        """Anota en pendientes lo que cambio en el equipo respecto al ultimo estado conocido."""
        if self.estado is None:
            return
        for k, (antes, despues) in _diferencias(self.estado, nuevo).items():
            # se agrupan: mientras se gira una ruleta solo cuenta el primer y el ultimo valor
            self._pendientes[k] = [self._pendientes.get(k, [antes])[0], despues]
            self._t_cambio = time.time()

    def _volcar_pendientes(self, forzar=False):
        """Pasa al registro los cambios a mano acumulados (cuando se deja de girar, o ya si forzar)."""
        if not self._pendientes or (not forzar and time.time() - self._t_cambio <= 1.5):
            return {}
        cambios = {k: v for k, v in self._pendientes.items() if v[0] != v[1]}
        self._pendientes = {}
        if cambios:
            self.reg.anotar("equipo (a mano o externo)", "osciloscopio", "cambios detectados al leer", cambios)
        return cambios

    def iniciar_sincronizacion(self, periodo):
        """Lee el osciloscopio cada 'periodo' s para que el panel muestre siempre lo que tiene el equipo.

        No lee (para no interferir con capturas) si: esta en modo manual, hay un datalogger
        del panel, existe la marca de un datalogger lanzado fuera del panel, o el usuario
        ha pausado la sincronizacion.
        """
        def bucle():
            while True:
                time.sleep(periodo if self.modo != "error" else max(periodo, 2))
                self.pausa_motivo = self._motivo_pausa()
                if self.pausa_motivo:
                    continue
                try:
                    self.leer("sincronizacion")
                except RuntimeError:
                    pass                      # ya deja modo/mensaje (sin comunicacion, bloqueado...)
                except Exception as e:        # algo inesperado: que se vea en el panel
                    self.mensaje = f"Error al sincronizar: {type(e).__name__}: {e}"

        def vigilar_ping():
            # Sin SCPI: con la lectura continua apagada nadie se enteraba de un reinicio del MXO hasta la
            # siguiente lectura, que entraba en mitad de su arranque (2026-11-03)
            fallos = 0
            while True:
                time.sleep(3)
                if self._perdido or self.modo in ("manual", "datalogger") or self._datalogger_vivo():
                    fallos = 0
                    continue
                fallos = 0 if self._responde_ping() else fallos + 1
                if fallos >= 2:
                    with self.lock:
                        if not self._perdido and self.modo not in ("manual", "datalogger"):
                            self._sin_comunicacion("no responde al ping (apagado o reiniciando)")
                            self.reg.anotar("panel", "osciloscopio", "sin respuesta al ping",
                                            "no se reconecta hasta que su puerto SCPI acepte conexiones")
        threading.Thread(target=bucle, daemon=True).start()
        threading.Thread(target=vigilar_ping, daemon=True).start()

    def pausar_por_vista(self, segundos=3.0):
        """Lo llama la pagina mientras el usuario usa la vista en directo: cada lectura pone el
        osciloscopio en remoto y le cerraria los menus de la pantalla."""
        self._vista_hasta = time.time() + segundos

    def _motivo_pausa(self):
        if self.modo == "manual":
            return "modo manual"
        if time.time() < self._vista_hasta:
            return "estás usando la vista en directo"
        if self.modo == "datalogger" or self._datalogger_vivo():
            return "datalogger del panel en marcha"
        if marca_datalogger_activa():
            return "datalogger lanzado fuera del panel en marcha"
        # sin comunicacion (p. ej. tras reiniciar el MXO) se reintenta aunque la lectura continua este
        # apagada: si no, nadie volvia a conectar hasta la siguiente lectura de la pagina (2026-11-03)
        if not self.sincronizar and self.modo != "error":
            return "lectura continua apagada"
        return ""

    # ---------------------------------------------------------------- escritura
    def aplicar(self, cambios, autor, base=None):
        """cambios = {"horizontal": {...}, "disparo": {...}, "canales": {"1": {...}}}

        'base' es el estado que vio quien pide el cambio (lo que devolvio su /api/osc/leer).
        Antes de escribir se vuelve a leer el equipo: si difiere de 'base' (cambio a mano o
        desde fuera), NO se aplica nada y se devuelve {"bloqueado": True, ...}.
        Sin 'base' se compara con la ultima lectura del panel, que la sincronizacion renueva
        cada segundo: protege menos (por eso Claude debe enviar 'base' o usar /api/osc/campo).
        """
        if self.estado is None and base is None:
            raise RuntimeError("Lee primero el estado del osciloscopio («Leer equipo»): "
                               "no se aplican cambios sin conocer la configuración actual")
        ordenes = []
        for grupo, tabla in (("horizontal", HORIZONTAL), ("disparo", DISPARO)):
            for k, v in (cambios.get(grupo) or {}).items():
                if k not in tabla or tabla[k][1] is None:
                    raise ValueError(f"{grupo}.{k} no se puede cambiar desde el panel")
                ordenes.append((f"{grupo}.{k}", tabla[k][1].format(_seguro(v))))
        for c, campos in (cambios.get("canales") or {}).items():
            if not 1 <= int(c) <= N_CANALES:
                raise ValueError(f"canal {c} no existe")
            for k, v in campos.items():
                if k == "confirmar_50ohm":
                    continue
                if k not in CANAL or CANAL[k][1] is None:
                    raise ValueError(f"canal {c}.{k} no se puede cambiar desde el panel")
                if k == "acoplamiento" and str(v).upper() == "DC" and not campos.get("confirmar_50ohm"):
                    raise ValueError(f"C{c}: acoplamiento DC = 50 Ω. Confírmalo: una sonda de tensión alta "
                                     "puede dañar la entrada")
                ordenes.append((f"C{c}.{k}", CANAL[k][1].format(_seguro(v), c=c)))
        if not ordenes:
            return {"errores": "", "estado": self.estado}

        with self._pasada():
            referencia = base if base is not None else self.estado

            def _aplicar(o):
                actual = _leer_todo(o)
                dif = _diferencias(referencia, actual)
                if dif:
                    return actual, dif, None
                o.write("*CLS")
                for _, cmd in ordenes:
                    o.write(cmd)
                o.query("*OPC?")
                return actual, None, o.query("SYSTem:ERRor:ALL?").strip()
            actual, dif, errores = self._con_equipo(_aplicar)
            if dif:
                self.reg.anotar("equipo (a mano o externo)", "osciloscopio",
                                "cambios detectados antes de aplicar: NO se aplica nada", dif)
                self._pendientes = {}
                self.estado, self.hora_lectura = actual, time.time()
                return {"bloqueado": True, "cambios_en_equipo": dif, "estado": actual,
                        "errores": "El osciloscopio ha cambiado desde la última lectura; revisa antes de aplicar"}
            self.reg.anotar(autor, "osciloscopio", "aplicar", {k: c for k, c in ordenes} |
                            ({"errores_equipo": errores} if not errores.startswith("0") else {}))
            self.estado = None  # el siguiente leer() no debe atribuir estos cambios al equipo
            return {"errores": "" if errores.startswith("0") else errores, "estado": self.leer(autor)}

    def aplicar_campo(self, clave, esperado, valor, autor, confirmar_50ohm=False):
        """Cambio inmediato de un solo campo (flechas tipo ruleta, rueda del raton, Enter).

        Antes de escribir se lee SOLO ese campo del equipo: si ya no vale lo que el
        usuario estaba viendo ('esperado'), alguien lo cambio a mano y no se toca nada.
        """
        partes = clave.split(".")
        if partes[0] == "canales" and len(partes) == 3:
            c, campo = int(partes[1]), partes[2]
            if not 1 <= c <= N_CANALES or campo not in CANAL:
                raise ValueError(f"campo desconocido: {clave}")
            consulta, plantilla, tipo = CANAL[campo]
            consulta, plantilla = consulta.format(c=c), plantilla and plantilla.replace("{c}", str(c))
            if campo == "acoplamiento" and str(valor).upper() == "DC" and not confirmar_50ohm:
                raise ValueError(f"C{c}: acoplamiento DC = 50 Ω. Confírmalo: una sonda de tensión alta "
                                 "puede dañar la entrada")
        elif len(partes) == 2 and partes[0] in ("horizontal", "disparo"):
            tabla = HORIZONTAL if partes[0] == "horizontal" else DISPARO
            if partes[1] not in tabla:
                raise ValueError(f"campo desconocido: {clave}")
            consulta, plantilla, tipo = tabla[partes[1]]
        else:
            raise ValueError(f"campo desconocido: {clave}")
        if plantilla is None:
            raise ValueError(f"{clave} no se puede cambiar desde el panel")
        if esperado is None:
            raise ValueError("Falta «esperado»: el valor que viste en la última lectura (si no, no se puede "
                             "saber si alguien lo cambió a mano)")
        cmd = plantilla.format(_seguro(valor))

        def _escribir(o):
            # lectura completa justo antes (unos 10 ms): asi los cambios a mano que hubiera se
            # anotan como tales y no se confunden con este ajuste
            previo = _leer_todo(o)
            actual = _valor(o.query(consulta), tipo)
            if not _iguales(actual, esperado):
                return previo, actual, None
            o.write("*CLS")
            o.write(cmd)
            o.query("*OPC?")
            return previo, actual, o.query("SYSTem:ERRor:ALL?").strip()
        with self._pasada():   # atomico frente a la sincronizacion y en UNA sola entrada en remoto
            previo, actual, errores = self._con_equipo(_escribir)
            self._acumular(previo)
            self.estado = previo            # lo de antes del ajuste ya esta anotado: no se repite
            self._volcar_pendientes(forzar=True)
            if errores is None:
                self.reg.anotar("equipo (a mano o externo)", "osciloscopio", "cambio a mano detectado: NO se aplica",
                                {clave: [esperado, actual]})
                return {"bloqueado": True, "cambios_en_equipo": {clave: [esperado, actual]}, "estado": self.leer(autor),
                        "errores": "Ese valor cambió en el equipo desde la última lectura"}
            self.reg.anotar(autor, "osciloscopio", "ajuste", {clave: cmd} |
                            ({"errores_equipo": errores} if not errores.startswith("0") else {}))
            aviso = ""
            if self.ajuste_pantalla and clave in ("horizontal.muestreo_Sa_s", "horizontal.puntos", "horizontal.escala_s_div"):
                aviso = self._acoplar(clave, autor).get("aviso", "")
            self.estado = None  # los cambios derivados (rango, muestreo auto...) no son "a mano"
            return {"errores": "" if errores.startswith("0") else errores, "aviso": aviso, "estado": self.leer(autor)}

    def ajustar_web(self, clave, esperado, valor, autor):
        """Flechas del panel por el frontal virtual de la web: como girar la ruleta con la mano, SIN pasar a remoto.
        Por SCPI la pantalla del MXO se queda en negro ~2,5 s en cada cambio (medido el 2026-11-02 muestreando la
        imagen de «Web control»); por la web, nada. Solo los campos con ruleta de paso fijo (medido el 2026-11-02):
          offset del canal  -> ruleta «Position» vertical, 0,01 div por paso, +1 = baja el offset (actua sobre el
                               canal SELECCIONADO en el MXO: si no lo esta, se pulsa su tecla CHx, que con el canal
                               encendido solo lo selecciona; con el canal apagado se va por SCPI)
          nivel de disparo  -> 0,01 div del canal fuente por paso, +1 = sube
          posicion horizontal -> escala/200 por paso, +1 = resta tiempo
          escala del canal  -> ruleta «Scale» vertical, serie 1-2-4-5 (... 0,2 0,4 0,5 1 2 4 5 ...), +1 = menos V/div;
                               no mueve offset, posicion ni nivel de disparo (medido en C1 y C2 el 2026-11-02)
          escala de tiempo  -> ruleta «Scale» horizontal, la misma serie, +1 = menos s/div; solo con sample rate y
                               record length en MAN, sin «registro ajustado a la pantalla» y sin Roll (si no, el MXO
                               recalcula el muestreo y no se puede saber sin leer: por SCPI)
        Sin ruleta (por SCPI): posicion del canal en div (pulsar la ruleta Position pone el OFFSET a 0), muestreo,
        puntos, punto de referencia, y las escalas en fino o fuera de la serie 1-2-4-5. El valor nuevo no se puede
        leer sin SCPI (eso volveria a apagar la pantalla): se calcula; el MXO lo redondea a su paso. Devuelve None si
        el campo no va por la web (el que llama lo hace por SCPI)."""
        est = self.estado
        if est is None or self._bloqueado():
            return None
        p = clave.split(".")
        canal = None
        serie = False
        try:
            if p[0] == "canales" and len(p) == 3 and p[2] in ("offset_V", "escala_V_div"):
                canal, ch = p[1], est["canales"][p[1]]
                if str(ch["activo"]) not in ("1", "ON"):
                    return None
                if p[2] == "offset_V":
                    rueda, paso, signo = "VERTICAL_POSITION", 0.01 * ch["escala_V_div"], -1
                else:
                    rueda, serie = "VERTICAL_SCALE", True
            elif clave == "horizontal.escala_s_div":
                h = est["horizontal"]
                if (self.ajuste_pantalla or str(h.get("roll_activo")).upper() in ("1", "ON")
                        or not str(h.get("muestreo_modo")).upper().startswith("MAN")
                        or not str(h.get("puntos_modo")).upper().startswith("MAN")):
                    return None
                rueda, serie = "HORIZONTAL_SCALE", True
            elif clave == "disparo.nivel_V":
                fuente = str(est["disparo"]["fuente"]).upper().replace("C", "")
                if fuente not in est["canales"]:
                    return None
                rueda, paso, signo = "TRIGGER_LEVEL", 0.01 * est["canales"][fuente]["escala_V_div"], 1
            elif clave == "horizontal.posicion_s":
                rueda, paso, signo = "HORIZONTAL_POSITION", est["horizontal"]["escala_s_div"] / 200, -1
            else:
                return None
            if serie:
                # escalas: pasos de la serie 1-2-4-5 de la ruleta; +1 = escala menor. Fuera de la serie (fino): SCPI
                ia, ib = _indice_1245(float(esperado)), _indice_1245(float(valor))
                if ia is None or ib is None:
                    return None
                k, n, v = ia - ib, ia - ib, float(valor)
                if abs(n) > 30:
                    return None
            else:
                n = round((float(valor) - float(esperado)) / paso)
                k, v = signo * n, round(float(esperado) + n * paso, 9)
        except (KeyError, TypeError, ValueError):
            return None
        if n == 0 or abs(n) > 400:            # nada que mover, o un salto grande: mejor un valor exacto por SCPI
            return None
        # La ruleta mueve PASOS, no pone un valor: si quien pide partio de un valor que no es el que tiene el panel
        # (clic con la pagina desfasada), se moveria de mas. Se va por SCPI, que compara con el equipo y avisa sin
        # aplicar (2026-11-03: dos «4 -> 2» seguidos dejaron el MXO en 1 y el panel en 2).
        try:
            actual = est["canales"][canal][p[2]] if canal else est[p[0]][p[1]]
            if not math.isclose(float(actual), float(esperado), rel_tol=1e-6, abs_tol=1e-12):
                return None
        except (KeyError, TypeError, ValueError):
            return None
        with self.lock:
            try:
                if canal:
                    leds = {l["id"]: l for l in self._web.estados().get("ledstates", [])}
                    if leds.get("LED_POSITION", {}).get("argb") != leds.get(f"LED_CH{canal}", {}).get("argb"):
                        self._web.tecla(f"CH{canal}")         # seleccionar el canal (no lo apaga: esta encendido)
                        time.sleep(0.3)
                # La ruleta ACELERA: los mensajes seguidos en el mismo sentido se multiplican (x2, x4, hasta x10), aunque
                # pasen segundos; el primero tras cambiar de sentido es exacto (medido el 2026-11-02 en el offset de C2:
                # el usuario bajo de 2,7 a «2,0» y quedo en -1,0). Por eso antes de cada movimiento se da un paso al
                # reves y luego n+1: 12 de 12 movimientos exactos (+-1 a +-100 pasos).
                s1 = 1 if k > 0 else -1
                self._web.rueda(rueda, -s1)
                time.sleep(0.15)
                self._web.rueda(rueda, k + s1)
            except Exception as e:
                self.reg.anotar("panel (automático)", "osciloscopio", "frontal web no disponible: se usa SCPI", str(e))
                return None
            nuevo = copy.deepcopy(est)
            if canal:
                nuevo["canales"][canal][p[2]] = v
            else:
                nuevo[p[0]][p[1]] = v
                if clave == "horizontal.escala_s_div" and "rango_s" in nuevo["horizontal"]:
                    nuevo["horizontal"]["rango_s"] = 10 * v
            self.estado, self.hora_lectura = nuevo, time.time()
        self.reg.anotar(autor, "osciloscopio", "ajuste (frontal web, sin remoto)",
                        {clave: [esperado, v], "ruleta": rueda, "pasos": k})
        return {"errores": "", "aviso": "", "estado": nuevo, "web": True}

    def ajustar_a_senal(self, c, autor, margen=0.2):
        """«Ajustar a la señal actual» (Registrar, antes de iniciar; nunca durante un registro): escala y offset de
        C<c> para que la señal que hay AHORA quepa en pantalla con un margen arriba y abajo (posicion a 0 div).
        Si sale recortada, abre la escala x4 centrando en lo que se ve y vuelve a mirar. El usuario pone antes la
        fuente en el caso extremo que quiere ver (cada montaje es distinto: no se adivina)."""
        c = int(c)
        if not 1 <= c <= N_CANALES:
            raise ValueError("Canal no válido")
        pasos = []

        def leer_y(o, n=5):
            # varias adquisiciones juntas: con ventanas cortas (50 ns/div = 500 ns) cada una ve un instante y el
            # ruido lento de la sonda movia la senal +-0,3 V de una a otra (7 intentos para 1 A; 2026-11-02)
            y = []
            for _ in range(n):
                y += leer_una(o)
            return y

        def leer_una(o):
            # adquisicion NUEVA con la escala recien puesta: con el osciloscopio en Stop se leia siempre la misma
            # (congelada) y el ajuste salia mal (2026-11-02). Single y esperar a que termine; Run/Stop se restaura
            rango = float(o.query("TIMebase:RANGe?"))
            t = o.timeout
            o.timeout = int(5000 + 3000 * rango)
            try:
                o.write("SINGle")
                o.query("*OPC?")
            except pyvisa.errors.VisaIOError:
                _limpiar(o)
                raise RuntimeError("No llega disparo (¿disparo en Normal sin señal?): ponlo en Auto y repite")
            finally:
                o.timeout = t
            return [float(v) for v in o.query_binary_values(f"CHANnel{c}:DATA:VALues?", datatype="f")]

        def _hacer(o):
            previo = _leer_todo(o)
            if o.query(f"CHANnel{c}:STATe?").strip() not in ("1", "ON"):
                raise ValueError(f"C{c} está apagado: enciéndelo antes de ajustarlo")
            fmt, ambito = o.query("FORMat:DATA?").strip(), o.query("EXPort:WAVeform:SCOPe?").strip()
            corriendo = bool(int(float(o.query("STATus:OPERation:CONDition?"))) & 16)   # bit 4: adquiriendo
            # limites de escala y offset (dependen de la sonda): un valor fuera lo rechaza entero y no se acerca
            # al limite (con 1 A pedia 0,02 V/div, minimo 0,05 con la RT-ZHD15, y se quedaba en 0,4; 2026-11-02)
            esc_min, esc_max = float(o.query(f"CHANnel{c}:SCALe? MIN")), float(o.query(f"CHANnel{c}:SCALe? MAX"))
            off_min, off_max = float(o.query(f"CHANnel{c}:OFFSet? MIN")), float(o.query(f"CHANnel{c}:OFFSet? MAX"))
            o.write("FORMat:DATA REAL,32;:EXPort:WAVeform:SCOPe DISP")
            try:
                for _ in range(8):
                    esc = float(o.query(f"CHANnel{c}:SCALe?"))
                    off = float(o.query(f"CHANnel{c}:OFFSet?"))
                    pos = float(o.query(f"CHANnel{c}:POSition?"))
                    abajo, arriba = off - pos * esc - 5 * esc, off - pos * esc + 5 * esc
                    y = [v for v in leer_y(o) if v == v]
                    if not y:
                        raise RuntimeError(f"C{c}: el osciloscopio no ha devuelto datos")
                    mn, mx = min(y), max(y)
                    borde = 0.003 * (arriba - abajo)
                    recortes = sum(1 for v in y if v >= arriba - borde or v <= abajo + borde) / len(y)
                    pasos.append({"escala": esc, "offset": off, "min": mn, "max": mx, "recortada": recortes > 0.005})
                    if recortes > 0.005:
                        nueva, centro = esc * 4, (mn + mx) / 2          # abrir y volver a mirar
                    else:
                        span = max(mx - mn, 0.02 * max(abs(mn), abs(mx)), 1e-3)
                        nueva = _paso125_arriba(span / (10 * (1 - 2 * margen)))
                        centro = (mn + mx) / 2
                        ocupa = (mx - mn) / (arriba - abajo)
                        dentro = mn >= abajo + 0.05 * (arriba - abajo) and mx <= arriba - 0.05 * (arriba - abajo)
                        # el equipo no da la escala pedida (p. ej. minimo de la sonda): se queda con la que da
                        tope = len(pasos) > 1 and pasos[-2].get("pedida") and abs(esc - pasos[-2]["pedida"]) > 0.01 * esc
                        if dentro and (0.3 <= ocupa <= 0.85 or tope):
                            return previo, pasos               # bien encajada (o el equipo no da mas): hecho
                    nueva = min(max(nueva, esc_min), esc_max)
                    centro = min(max(centro, off_min), off_max)
                    if not pasos[-1]["recortada"] and abs(nueva - esc) < 0.01 * esc and abs(centro - (off - pos * esc)) < 0.5 * esc:
                        return previo, pasos               # ya en el limite del equipo y centrada: no hay mas
                    if pasos[-1]["recortada"] and esc >= esc_max:
                        raise RuntimeError(f"C{c}: la señal no cabe ni con la escala máxima ({esc_max:g} V/div)")
                    pasos[-1]["pedida"] = nueva
                    o.write(f"CHANnel{c}:POSition 0;:CHANnel{c}:SCALe {nueva:.6g};:CHANnel{c}:OFFSet {centro:.6g}")
                    o.query("*OPC?")
                raise RuntimeError(f"C{c}: no se ha conseguido encajar la señal en 8 intentos (¿cambia mucho?)")
            finally:
                o.write(f"FORMat:DATA {fmt};:EXPort:WAVeform:SCOPe {ambito}")
                if corriendo:
                    o.write("RUN")
        with self._pasada():
            previo, pasos = self._con_equipo(_hacer)
            self._acumular(previo)
            self.estado = previo
            self._volcar_pendientes(forzar=True)
            final = pasos[-1]
            self.reg.anotar(autor, "osciloscopio", f"C{c}: ajustar a la señal actual",
                            {"escala_V_div": [pasos[0]["escala"], final["escala"]],
                             "offset_V": [pasos[0]["offset"], final["offset"]],
                             "señal_V": [round(final["min"], 4), round(final["max"], 4)], "intentos": len(pasos)})
            self.estado = None
            return {"pasos": pasos, "estado": self.leer(autor)}

    def _acoplar(self, origen, autor):
        """Con «Registro ajustado a la pantalla»: recalcula el companero para que RL = SR x span.
        - cambio de record length  -> sample rate = RL / span (el equipo lo redondea a uno valido);
        - cambio de sample rate o de escala de tiempo -> record length = SR x span.
        Siempre se termina fijando el record length con el SR real, para que la pantalla quede llena.
        El record length tiene limites (MXO34: 1000 a 500 M puntos): si con el SR pedido harian falta menos de
        1000 (p. ej. 18 us a 10 MSa/s = 180), se sube el SR lo justo para que 1000 puntos llenen la pantalla; si
        harian falta mas del maximo, se baja. Antes daba -222 y el registro se quedaba mas largo que la pantalla
        (2026-11-02). Lo que se corrige se devuelve en 'aviso'."""
        def _hacer(o):
            rango = float(o.query("TIMebase:RANGe?"))
            rl_min, rl_max = float(o.query("ACQuire:POINts? MIN")), float(o.query("ACQuire:POINts? MAX"))
            if origen == "horizontal.puntos":
                pts = float(o.query("ACQuire:POINts?"))
                o.write(f"ACQuire:SRATe:MODE MAN;:ACQuire:SRATe {pts / rango:.6g}")
                o.query("*OPC?")
            sr = pedido = float(o.query("ACQuire:SRATe?"))
            for _ in range(6):                      # el equipo redondea el SR: subir/bajar hasta que el RL quepa
                n = sr * rango
                if rl_min - 0.5 <= n <= rl_max + 0.5:
                    break
                objetivo = (rl_min if n < rl_min else rl_max) / rango
                # un muestreo redondo que divida 5 GSa/s (si no, el osciloscopio interpola): el primero que llega
                validos = _muestreos_validos()
                elegido = next((v for v in validos if v >= objetivo), validos[-1]) if n < rl_min else                     next((v for v in reversed(validos) if v <= objetivo), validos[0])
                o.write(f"ACQuire:SRATe:MODE MAN;:ACQuire:SRATe {elegido:.6g}")
                o.query("*OPC?")
                nuevo = float(o.query("ACQuire:SRATe?"))
                if nuevo == sr:                     # no se mueve: no hay mas que hacer
                    break
                sr = nuevo
            aviso = ""
            if sr != pedido:
                aviso = (f"Con {_texto_ing(rango, 's')} en pantalla, {_texto_ing(pedido, 'Sa/s')} serían "
                         f"{pedido * rango:.0f} puntos y el osciloscopio admite de {rl_min:.0f} a {rl_max:.0f}: "
                         f"muestreo cambiado a {_texto_ing(sr, 'Sa/s')}")
            puntos = min(max(round(sr * rango), rl_min), rl_max)
            o.write(f"ACQuire:SRATe:MODE MAN;:ACQuire:POINts:MODE MAN;:ACQuire:POINts {puntos:.0f}")
            o.query("*OPC?")
            return {"sample_rate": sr, "record_length": float(o.query("ACQuire:POINts?")), "span_s": rango,
                    "aviso": aviso, "errores": o.query("SYSTem:ERRor:ALL?").strip()}
        r = self._con_equipo(_hacer)
        self.reg.anotar(autor, "osciloscopio", "registro ajustado a la pantalla", {"por": origen} | r)
        return r

    def poner_ajuste_pantalla(self, activo, autor):
        with self._pasada():
            self.ajuste_pantalla = bool(activo)
            self.reg.anotar(autor, "osciloscopio", "registro ajustado a la pantalla " + ("encendido" if activo else "apagado"))
            aviso = ""
            if activo:   # al encenderlo, se ajusta ya el record length al sample rate actual
                aviso = self._acoplar("horizontal.muestreo_Sa_s", autor).get("aviso", "")
            self.estado = None
            return self.leer(autor) | {"aviso_ajuste": aviso}

    def accion(self, accion, autor):
        cmd = {"run": "RUN", "stop": "STOP", "single": "SINGle"}[accion]
        self._con_equipo(lambda o: o.write(cmd))
        self.reg.anotar(autor, "osciloscopio", accion)

    # ---------------------------------------------------------------- captura suelta
    def capturar(self, proyecto, carpeta, nombre, notas, ambito, png, ficha, single, fuente, autor, formas=True,
                 tipo="capturas"):
        """Guarda en <proyecto>\\capturas\\<carpeta>\\AAAAMMDD_HHMM_nombre: .h5 (canales activos, formato R&S),
        .png (pantalla), .set (configuracion) y .json (ficha: disparo, medidas, notas, fuente...).
        Cada parte es opcional (formas, png, ficha), pero al menos una.

        Comprobado el 2026-10-29: el MXO no exporta varios canales a la vez por SCPI (no hay MULTichannel): se
        exporta canal a canal y el PC los junta en un .h5. La captura de pantalla en remoto con «Display update»
        OFF sale como «Remote mode»: se enciende solo para la foto (3,5 s para que dibuje todo) y despues se
        fuerza el redibujado moviendo la posicion horizontal 1 us y devolviendola a su valor exacto (si no, la
        traza deja de dibujarse; mismo fallo que el GTL inmediato)."""
        if ambito not in ("DISP", "ALL"):
            raise ValueError("Ámbito de exportación no válido")
        if not (formas or png or ficha):
            raise ValueError("Marca al menos una cosa que guardar: formas de onda, pantalla o ficha")
        ruta, ruta_osc, rel = almacen.destino(proyecto, tipo, carpeta, nombre)

        def hacer(o):
            t_prev = o.timeout
            o.timeout = 60000
            try:
                montada = _unidad_montada(o)
                # sin unidad de red: al disco del MXO y luego por SCPI al PC (ver TMP_OSC)
                destino = ruta_osc if montada else f"{TMP_OSC}/{os.path.basename(ruta)}"
                if not montada:
                    try:
                        o.write(f"MMEMory:MDIRectory '{TMP_OSC}'")
                        o.query("*OPC?")
                    except pyvisa.errors.VisaIOError:
                        _limpiar(o)
                    o.write("*CLS")          # «ya existe» no es un error para la captura
                traer = []                  # (fichero en el MXO, fichero en el PC)
                est = _leer_todo(o)
                activos = [c for c in sorted(est["canales"]) if est["canales"][c]["activo"] == "1"]
                if formas and not activos:
                    raise ValueError("No hay ningún canal activo que guardar")
                corriendo = bool(int(float(o.query("STATus:OPERation:CONDition?"))) & 16)   # bit 4: adquiriendo
                if single:
                    try:
                        o.write("SINGle")
                        o.query("*OPC?")
                    except pyvisa.errors.VisaIOError:
                        _limpiar(o)
                        raise RuntimeError("Single no terminó en 60 s (¿disparo en Normal sin señal?). No se ha guardado nada")
                medidas = _leer_medidas(o)
                if formas:
                    previo = {k: o.query(q).strip() for k, q in (("ambito", "EXPort:WAVeform:SCOPe?"),
                              ("fuente", "EXPort:WAVeform:SOURce?"), ("nombre", "EXPort:WAVeform:NAME?"))}
                    try:
                        o.write(f"EXPort:WAVeform:SCOPe {ambito}")
                        for c in activos:
                            o.write(f"EXPort:WAVeform:SOURce C{c}")
                            o.write(f"EXPort:WAVeform:NAME '{destino}_C{c}.h5'")
                            o.write("EXPort:WAVeform:SAVE")
                            o.query("*OPC?")
                            traer.append((f"{destino}_C{c}.h5", f"{ruta}_C{c}.h5"))
                    finally:
                        o.write(f"EXPort:WAVeform:SCOPe {previo['ambito']};:EXPort:WAVeform:SOURce {previo['fuente']}")
                        if previo["nombre"].startswith('"'):
                            o.write(f"EXPort:WAVeform:NAME {previo['nombre']}")
                if ficha:
                    o.write(f"*SAV 97;:MMEMory:STORe:STATe 97,'{destino}.set'")
                    o.query("*OPC?")
                    traer.append((f"{destino}.set", f"{ruta}.set"))
                if not montada:
                    for en_osc, en_pc in traer:
                        datos = o.query_binary_values(f"MMEMory:DATA? '{en_osc}'", datatype="B", container=bytes)
                        with open(en_pc, "wb") as fh:
                            fh.write(datos)
                        o.write(f"MMEMory:DELete '{en_osc}'")
                        o.query("*OPC?")
                imagen = None
                if png:
                    upd = o.query("SYSTem:DISPlay:UPDate?").strip()
                    pos = o.query("TIMebase:HORizontal:POSition?").strip()
                    try:
                        o.write("SYSTem:DISPlay:UPDate ON")
                        o.query("*OPC?")
                        time.sleep(3.5)
                        imagen = o.query_binary_values("HCOPy:DATA?", datatype="B", container=bytes)
                    finally:
                        o.write(f"SYSTem:DISPlay:UPDate {'ON' if upd == '1' else 'OFF'}")
                        o.write(f"TIMebase:HORizontal:POSition {float(pos) + 1e-6!r}")
                        o.query("*OPC?")
                        o.write(f"TIMebase:HORizontal:POSition {pos}")
                        o.query("*OPC?")
                if single and corriendo:
                    o.write("RUN")
                    o.query("*OPC?")
                errores = o.query("SYSTem:ERRor:ALL?").strip()
                return {"estado": est, "activos": activos, "corriendo_antes": corriendo, "medidas": medidas,
                        "imagen": imagen, "errores": errores}
            finally:
                o.timeout = t_prev

        r = self._con_equipo(hacer)
        self.estado, self.hora_lectura = r["estado"], time.time()
        ficheros = []
        if formas:
            _juntar_h5([f"{ruta}_C{c}.h5" for c in r["activos"]], ruta + ".h5")
            ficheros.append(os.path.basename(ruta) + ".h5")
        if r["imagen"]:
            open(ruta + ".png", "wb").write(r["imagen"])
            ficheros.append(os.path.basename(ruta) + ".png")
        if ficha:
            ficheros += [os.path.basename(ruta) + ".set", os.path.basename(ruta) + ".json"]
        ficha_json = {"hora": time.strftime("%Y-%m-%dT%H:%M:%S"), "nombre": nombre, "notas": notas or "",
                      "proyecto": proyecto, "carpeta": carpeta or "", "ruta": rel, "equipo": self.idn,
                      "ambito_exportacion": ambito, "single": single, "estaba_en_run": r["corriendo_antes"],
                      "canales_guardados": [f"C{c}" for c in r["activos"]] if formas else [], "medidas": r["medidas"],
                      "osciloscopio": r["estado"], "fuente": fuente, "errores_osciloscopio": r["errores"],
                      "ficheros": ficheros}
        if ficha:
            with open(ruta + ".json", "w", encoding="utf-8") as f:
                json.dump(ficha_json, f, ensure_ascii=False, indent=1)
        self.reg.anotar(autor, "osciloscopio", "captura guardada", {"ruta": rel, "ficheros": ficheros,
                                                                    "canales": ficha_json["canales_guardados"]})
        return {"ruta": rel, "ficheros": ficheros, "errores": r["errores"]}

    # ---------------------------------------------------------------- datalogger
    def iniciar_datalogger(self, canales, sr, duracion, proyecto, autor, carpeta="", nombre=None, notas="",
                           ruta_h5=None):
        r"""Con nombre: <proyecto>\registros\<carpeta>\AAAAMMDD_HHMM_nombre\osc.h5 + registro.json (ajustes, notas,
        configuracion al empezar y, al terminar, el resultado). Sin nombre: como antes (datalogger_*.h5 en el proyecto)."""
        # las mismas reglas que datalogger.py, pero aqui: el error se ve en el panel y no en una consola
        canales = sorted({int(c) for c in canales})
        if not canales or not all(1 <= c <= N_CANALES for c in canales):
            raise ValueError("Elige al menos un canal entre C1 y C4")
        div = 5e9 / sr
        if abs(div - round(div)) > 1e-9:
            raise ValueError(f"{sr:g} Sa/s no divide exactamente 5 GSa/s: el osciloscopio interpolaría y no hay "
                             "continuidad garantizada")
        if sr < 5e3:     # 9-nov: mínimo medido en Roll (a 2 y 1 kSa/s el datalogger se quedaba colgado)
            raise ValueError(f"{sr:g} Sa/s está por debajo del mínimo que admite el osciloscopio en Roll "
                             "(5 kSa/s, medido el 9-nov)")
        if sr * len(canales) > 12.5e6:
            raise ValueError(f"{sr * len(canales) / 1e6:g} MSa/s en total supera el máximo medido (~12,5 MSa/s). "
                             f"Con {len(canales)} canales, como mucho {12.5 / len(canales):g} MSa/s por canal")
        if nombre:
            almacen._limpio(nombre)                # que un nombre no valido falle antes de tocar nada
        with self.lock:
            if self.modo == "datalogger" or self._datalogger_vivo():
                if self.dl.get("vista"):
                    raise RuntimeError("La vista en vivo del osciloscopio está en marcha: párala antes "
                                       "(En vivo → Mostrar → Osciloscopio → Parar)")
                raise RuntimeError("Ya hay un datalogger en marcha")
            # antes de nada, la configuracion actual (puede haberse cambiado a mano)
            actual = self._con_equipo(_leer_todo)
            self._acumular(actual)
            self._volcar_pendientes(forzar=True)
            self.estado, self.hora_lectura = actual, time.time()
            self.reg.anotar(autor, "osciloscopio", "configuración antes del datalogger",
                            {"horizontal": actual["horizontal"], "disparo": actual["disparo"],
                             "canales_activos": [c for c, v in actual["canales"].items() if v["activo"] == "1"]})
            ficha = None
            if ruta_h5:                         # ensayo: osc.h5 en su carpeta, la ficha es ensayo.json
                fichero = ruta_h5
            elif nombre:
                ruta, _, rel = almacen.destino(proyecto, "registros", carpeta, nombre, carpeta_propia=True)
                fichero, ficha = os.path.join(ruta, "osc.h5"), os.path.join(ruta, "registro.json")
                with open(ficha, "w", encoding="utf-8") as f:
                    json.dump({"inicio": time.strftime("%Y-%m-%dT%H:%M:%S"), "nombre": nombre, "notas": notas or "",
                               "proyecto": proyecto, "carpeta": carpeta or "", "ruta": rel, "equipo": self.idn,
                               "canales": [f"C{c}" for c in canales], "muestreo_Sa_s": sr, "duracion_s": duracion,
                               "osciloscopio_al_empezar": actual}, f, ensure_ascii=False, indent=1)
            else:
                carpeta_vieja = os.path.join(r"C:\Proyectos\Osciloscopio", proyecto or "")
                os.makedirs(carpeta_vieja, exist_ok=True)
                fichero = os.path.join(carpeta_vieja, time.strftime("datalogger_%Y%m%d_%H%M%S.h5"))
            self._cerrar(devolver_local=False)   # el datalogger abre su propia sesion
            args = [sys.executable, "-u", DATALOGGER, "--canales", ",".join(map(str, canales)),
                    "--sr", str(sr), "--duracion", str(duracion), "--host", self.host, "--salida", fichero, "--vivo"]
            p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace", cwd=os.path.dirname(DATALOGGER),
                                 creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            self.dl = {"proceso": p, "salida": [], "args": {"canales": canales, "sr": sr, "duracion": duracion,
                       "proyecto": proyecto, "carpeta": carpeta, "nombre": nombre}, "fichero": fichero,
                       "ficha": ficha, "fin": None, "t_salida": time.time(), "parar_pedido": None,
                       "t_inicio": time.time()}     # para «● GRABANDO hh:mm:ss» de la barra de estado (2026-11-04)
            self.modo = "datalogger"         # antes de soltar el lock: nadie puede colarse en medio
        self.reg.anotar(autor, "osciloscopio", "datalogger iniciado", self.dl["args"] | {"fichero": fichero})
        threading.Thread(target=self._seguir_datalogger, args=(p, autor), daemon=True).start()
        threading.Thread(target=self._vigilar_datalogger, args=(p,), daemon=True).start()

    def _seguir_datalogger(self, p, autor):
        for linea in p.stdout:
            self.dl["t_salida"] = time.time()
            if linea.startswith(("VIVO ", "VIVO_INFO ")):      # para la grafica en vivo, no para la consola
                self._linea_vivo(linea)
                continue
            self.dl["salida"] = (self.dl["salida"] + [linea.rstrip()])[-200:]
        p.wait()
        with self.lock:
            self.dl["fin"] = p.returncode
            if self.modo == "datalogger":
                self.modo = "desconectado"
            self.estado = None   # el datalogger restaura la configuracion: no es un cambio "a mano"
        ultima = next((l for l in reversed(self.dl["salida"]) if l), "")
        self.reg.anotar(autor, "osciloscopio", "datalogger terminado",
                        {"codigo": p.returncode, "fichero": self.dl["fichero"], "ultima_linea": ultima})
        if self.dl.get("ficha"):
            try:
                with open(self.dl["ficha"], encoding="utf-8") as f:
                    d = json.load(f)
                progreso = next((l for l in reversed(self.dl["salida"]) if "huecos" in l), "")
                d.update(fin=time.strftime("%Y-%m-%dT%H:%M:%S"), codigo_salida=p.returncode, ultima_linea=ultima,
                         resumen=progreso, salida_final=self.dl["salida"][-15:])
                with open(self.dl["ficha"], "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, indent=1)
            except Exception as e:
                self.reg.anotar("panel (automático)", "osciloscopio", "no se pudo completar registro.json", str(e))

    def _linea_vivo(self, linea):
        """VIVO_INFO: canales, etiquetas y conversion a V. VIVO: [hora, min, max, media] en cuentas por canal y
        tramo de 0,1 s. Se guarda en V con 'recortada' si toca el borde de la pantalla (cuentas -32768 / 32767)."""
        try:
            tipo, js = linea.split(" ", 1)
            d = json.loads(js)
        except ValueError:
            return
        if tipo == "VIVO_INFO":
            self.vivo = {"info": d, "datos": {str(c): collections.deque() for c in d["canales"]}}
            return
        info = self.vivo.get("info") or {}
        for c, filas in d.items():
            conv = (info.get("conv") or {}).get(c)
            cola = self.vivo["datos"].setdefault(c, collections.deque())
            for t, mn, mx, me in filas:
                rec = mn <= -32768 or mx >= 32767
                if conv:
                    g, o = conv
                    cola.append((t, mn * g + o, mx * g + o, me * g + o, rec))
            while cola and cola[0][0] < cola[-1][0] - 7200:
                cola.popleft()

    def vivo_desde(self, desde=0.0):
        """Lo que ha visto el osciloscopio durante el datalogger, desde 'desde' (hora Unix):
        {"info": {canales, etiquetas, srate}, "datos": {"1": [[hora, min, max, media, recortada], ...]}}"""
        info = self.vivo.get("info")
        if not info:
            return {"info": None, "datos": {}}
        return {"info": {k: info.get(k) for k in ("canales", "etiquetas", "srate")},
                "datos": {c: [list(f) for f in list(cola) if f[0] > desde] for c, cola in list(self.vivo["datos"].items())}}

    # ---------------------------------------------------------------- vista en vivo (sin registro)
    def iniciar_vista(self, canales, sr, autor):
        """El osciloscopio en En vivo sin ensayo ni registro: datalogger a un fichero temporal (VISTA_DIR)."""
        sr = float(sr)
        if sr > VISTA_SR_MAX:
            raise ValueError(f"La vista en vivo va como mucho a {VISTA_SR_MAX / 1e6:g} MSa/s por canal (el fichero "
                             "temporal crecería demasiado). Para más, usa Registrar")
        os.makedirs(VISTA_DIR, exist_ok=True)
        self.limpiar_vistas()
        fichero = os.path.join(VISTA_DIR, time.strftime("vista_%Y%m%d_%H%M%S.h5"))
        self.iniciar_datalogger(canales, sr, 0, "", autor, ruta_h5=fichero)
        self.dl["vista"] = True
        self.reg.anotar(autor, "osciloscopio", "vista en vivo iniciada (temporal, no es un registro)",
                        {"canales": canales, "sr": sr, "fichero": fichero})
        threading.Thread(target=self._vigilar_vista, args=(fichero,), daemon=True).start()

    def parar_vista(self, autor):
        if not (self.dl.get("vista") and self._datalogger_vivo()):
            raise RuntimeError("La vista en vivo del osciloscopio no está en marcha")
        self.parar_datalogger(autor)

    def _vigilar_vista(self, fichero):
        while self.dl.get("fichero") == fichero and self._datalogger_vivo():
            try:
                if os.path.getsize(fichero) > VISTA_MAX_BYTES:
                    self.parar_datalogger("panel (automático)")
                    self.reg.anotar("panel (automático)", "osciloscopio", "vista en vivo parada",
                                    f"el temporal pasó de {VISTA_MAX_BYTES / 1e9:g} GB")
                    return
            except OSError:
                pass
            time.sleep(5)

    def limpiar_vistas(self):
        """Borra los temporales de vistas anteriores (nunca el que se esta usando)."""
        actual = self.dl.get("fichero") if self.dl.get("vista") else None
        guardar = set(getattr(self, "no_borrar", set())) | ({actual} if actual else set())
        try:
            nombres = os.listdir(VISTA_DIR)
        except OSError:
            return
        for n in nombres:
            r = os.path.join(VISTA_DIR, n)
            if any(r.startswith(x) for x in guardar):     # el de ahora y los que tienen un ✂ Tramo pendiente
                continue
            try:
                os.remove(r)
            except OSError:
                pass

    def parar_datalogger(self, autor):
        """Pide parar con el fichero .parar (tambien durante la configuracion y el llenado de la ventana): el
        datalogger cierra el .h5 y restaura la configuracion del osciloscopio. Si no lo hace en DL_PARAR_S,
        el vigilante lo cierra a la fuerza."""
        if not self._datalogger_vivo():
            raise RuntimeError("No hay un datalogger en marcha")
        open(self.dl["fichero"] + ".parar", "w").close()
        self.dl["parar_pedido"] = self.dl.get("parar_pedido") or time.time()
        self.reg.anotar(autor, "osciloscopio", "datalogger: orden de parar")

    def _vigilar_datalogger(self, p):
        while p.poll() is None:
            time.sleep(1)
            ahora, pedido = time.time(), self.dl.get("parar_pedido")
            if pedido and ahora - pedido > DL_PARAR_S:
                motivo = f"no ha parado {DL_PARAR_S} s después de pedirlo"
            elif ahora - self.dl.get("t_salida", ahora) > DL_SIN_SENAL_S:
                motivo = f"{DL_SIN_SENAL_S} s sin dar señales de vida (atascado)"
            else:
                continue
            if p.poll() is not None:
                return
            self.dl["forzado"] = motivo
            self.dl["salida"] = self.dl["salida"] + [f"PANEL: datalogger cerrado a la fuerza: {motivo}"]
            p.kill()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            self.reg.anotar("panel (automático)", "osciloscopio", "datalogger cerrado a la fuerza", motivo)
            self._restaurar_tras_forzar()
            return

    def _restaurar_tras_forzar(self):
        """El datalogger cerrado a la fuerza no restaura nada: lo hace el panel con el .set que guardo al
        empezar. Solo si llego a guardarlo (si no, se podria cargar el de un registro anterior)."""
        fin = time.time() + 15
        while self.modo == "datalogger" and time.time() < fin:     # que _seguir_datalogger cierre su parte
            time.sleep(0.2)
        try:
            os.remove(MARCA_DATALOGGER)
        except OSError:
            pass
        guardada = any(l.startswith(("Configurando: modo Roll", "Llenando", "Registrando")) for l in self.dl["salida"])
        if not guardada:
            self.reg.anotar("panel (automático)", "osciloscopio", "configuración NO restaurada",
                            "el datalogger no llegó a guardarla: revisa el osciloscopio (Roll, canales, muestreo)")
            self.dl["salida"] = self.dl["salida"] + ["PANEL: configuración NO restaurada (no llegó a guardarse): revisa el osciloscopio"]
            return

        pantalla = next((l for l in self.dl["salida"] if l.startswith("Pantalla del osciloscopio visible")), None)

        def cargar(o):
            o.write(f"*CLS;:MMEMory:RCL '{DL_CONFIG_PREVIA}'")
            o.query("*OPC?")
            if pantalla:      # el datalogger encendio el refresco en remoto: devolverlo y «empujon» del horizontal
                o.write(f"SYSTem:DISPlay:UPDate {'ON' if 'antes ON' in pantalla else 'OFF'}")
                pos = o.query("TIMebase:HORizontal:POSition?").strip()
                o.write(f"TIMebase:HORizontal:POSition {float(pos) + 1e-6!r}")
                o.query("*OPC?")
                o.write(f"TIMebase:HORizontal:POSition {pos}")
                o.query("*OPC?")
        try:
            self._con_equipo(cargar)
            texto = "PANEL: configuración previa del osciloscopio restaurada"
            self.reg.anotar("panel (automático)", "osciloscopio", "configuración previa restaurada tras forzar el cierre")
        except Exception as e:
            texto = f"PANEL: no se pudo restaurar la configuración ({e}): revisa el osciloscopio"
            self.reg.anotar("panel (automático)", "osciloscopio", "configuración NO restaurada", str(e))
        self.dl["salida"] = self.dl["salida"] + [texto]
        self.estado = None

    def resumen(self):
        dl = {k: v for k, v in self.dl.items() if k != "proceso"}
        dl["en_marcha"] = self.modo == "datalogger" or self._datalogger_vivo()
        dl["externo"] = marca_datalogger_activa() and not dl["en_marcha"]
        try:                                              # tamaño del osc.h5 para la barra de estado
            dl["tamano"] = os.path.getsize(dl["fichero"]) if dl.get("fichero") else None
        except OSError:
            dl["tamano"] = None
        return {"host": self.host, "ip": self.ip(), "modo": self.modo, "mensaje": self.mensaje, "idn": self.idn,
                "sincronizar": self.sincronizar, "sincro_pausa": self._motivo_pausa(), "registro": self.registro,
                "ajuste_pantalla": self.ajuste_pantalla,
                "estado": self.estado, "hora_lectura": self.hora_lectura, "datalogger": dl}


def _seguro(v):
    """Valor que se mete en un comando SCPI: solo numeros, letras y signos de numero
    (sin ';' ni saltos de linea que permitirian colar otro comando)."""
    s = str(v).strip()
    if not re.fullmatch(r"[A-Za-z0-9.+\-]{1,32}", s):
        raise ValueError(f"Valor no válido para el osciloscopio: {s!r}")
    return s


def _proceso_vivo(pid):
    """Sin os.kill: en Windows os.kill(pid, 0) MATA el proceso. Se pregunta al sistema."""
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        codigo = ctypes.c_ulong()
        return bool(k.GetExitCodeProcess(h, ctypes.byref(codigo))) and codigo.value == 259   # STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def marca_datalogger_activa():
    """La marca la deja datalogger.py mientras registra. Si el proceso murio de golpe la marca se
    queda huerfana: se comprueba su PID y, si ya no existe, se borra."""
    if not os.path.exists(MARCA_DATALOGGER):
        return False
    try:
        with open(MARCA_DATALOGGER) as f:      # cerrarla enseguida: abierta, el datalogger no puede borrarla
            pid = int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return True       # marca a medio escribir: mejor prudencia
    if _proceso_vivo(pid):
        return True
    try:
        os.remove(MARCA_DATALOGGER)
    except OSError:
        pass
    return False


def _iguales(a, b):
    if isinstance(a, float):
        try:
            b = float(b)
        except (TypeError, ValueError):
            return False
        return abs(a - b) <= 1e-6 * max(abs(a), abs(b), 1e-12)
    return str(a).strip().upper() == str(b).strip().upper()


def _consultas():
    """Lista ordenada de (destino, consulta, tipo) para leer todo el estado."""
    out = [(("horizontal", k), q, t) for k, (q, _, t) in HORIZONTAL.items()]
    out += [(("disparo", k), q, t) for k, (q, _, t) in DISPARO.items()]
    out += [(("canales", str(c), k), q.format(c=c), t) for c in range(1, N_CANALES + 1) for k, (q, _, t) in CANAL.items()]
    return out + [(("adquisiciones",), "ACQuire:CURRent?", float)]


CONSULTAS = _consultas()
# Consultas que el osciloscopio no admite ahora. P. ej. con una sonda activa (RT-ZHD15) CHANnel1:COUPling?
# da -397 "Not available" y no responde: en la consulta combinada eso deja TODA la lectura en timeout y el
# osciloscopio en remoto (visto el 2026-10-29). Se descubren de una en una y se quitan de la combinada;
# la lista se vacia al reconectar (por si se cambia de sonda).
NO_DISPONIBLES = set()


def _muestreos_validos():
    """Muestreos «redondos» (1; 1,25; 2; 2,5; 3,125; 5; 6,25 x 10^n) que dividen exactamente 5 GSa/s."""
    return sorted(m * 10 ** e for e in range(0, 10) for m in (1, 1.25, 2, 2.5, 3.125, 5, 6.25)
                  if m * 10 ** e <= 5e9 and abs(5e9 / (m * 10 ** e) - round(5e9 / (m * 10 ** e))) < 1e-6)


def _texto_ing(x, u):
    """1.8e-05, 's' -> '18 us' (prefijos de ingenieria, coma decimal)."""
    for f, p in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs(x) >= f or f == 1e-9:
            return f"{x / f:.4g} {p}{u}".replace(".", ",")


def _paso125_arriba(v):
    """El valor 1-2-5 mas pequeno que es >= v."""
    import math
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if m * e >= v * (1 - 1e-9):
            return m * e


def _unidad_montada(o):
    """¿Esta montada la unidad de red del MXO? Si no lo esta, NetworkStorage es una carpeta de su disco interno:
    el MXO guardaria alli sin dar error y el PC esperaria en vano (2026-11-02). Se compara lo que ve el
    osciloscopio con las carpetas de BASE en el PC. Sin montar, la captura va por SCPI (ver TMP_OSC): ya no hace
    falta volver a montarla cada vez que se reinicia el osciloscopio."""
    pc = {n for n in os.listdir(almacen.BASE) if os.path.isdir(os.path.join(almacen.BASE, n))}
    try:
        cat = o.query(f"MMEMory:CATalog? '{almacen.BASE_OSC}/'")
    except pyvisa.errors.VisaIOError:
        _limpiar(o)
        return False                # sin respuesta: por SCPI, que funciona siempre
    osc = {e.split(",")[0] for e in re.findall(r'"([^"]*)"', cat)}
    return not (pc - osc)


def _limpiar(o):
    try:
        o.clear()
    except Exception:
        pass
    try:
        o.write("*CLS")
    except Exception:
        pass


def _una_a_una(o):
    out, t = {}, o.timeout
    o.timeout = 700
    try:
        for _, q, _ in CONSULTAS:
            try:
                out[q] = o.query(q)
            except pyvisa.errors.VisaIOError:
                _limpiar(o)
                # ¿es esa consulta o es el equipo? Tras reiniciar el osciloscopio no contestaba a NADA: todo acababa
                # en NO_DISPONIBLES, la lectura salia vacia (TypeError en coherencia) y el panel no reconectaba
                # hasta reiniciarlo (2026-11-02). Si no contesta ni a *IDN?, se da por perdida la comunicacion.
                try:
                    o.query("*IDN?")
                except pyvisa.errors.VisaIOError:
                    raise
                NO_DISPONIBLES.add(q)
    finally:
        o.timeout = t
    return out


def _leer_todo(o):
    # Una sola consulta combinada: 7-18 ms (medido el 2026-10-28) frente a 26-42 ms de una en una.
    activas = [q for _, q, _ in CONSULTAS if q not in NO_DISPONIBLES]
    respuestas = None
    try:
        r = o.query(";:".join(activas)).strip().split(";")
        if len(r) == len(activas):               # si no (p.ej. un ';' dentro de un texto), de una en una
            respuestas = dict(zip(activas, r))
    except pyvisa.errors.VisaIOError:
        _limpiar(o)
    if respuestas is None:
        respuestas = _una_a_una(o)
    est = {"horizontal": {}, "disparo": {}, "canales": {str(c): {} for c in range(1, N_CANALES + 1)}}
    est["no_disponibles"] = sorted(NO_DISPONIBLES)
    for destino, q, tipo in CONSULTAS:
        r = respuestas.get(q)
        if destino == ("adquisiciones",):
            est["adquisiciones"] = int(float(r)) if r is not None else None
        elif destino[0] == "canales":
            est["canales"][destino[1]][destino[2]] = _valor(r, tipo)
        else:
            est[destino[0]][destino[1]] = _valor(r, tipo)
    est["avisos"] = coherencia(est["horizontal"])
    return est


def _leer_medidas(o):
    """Medidas activas (las que el usuario tiene en pantalla): tipo, fuente y valor actual."""
    out, t = [], o.timeout
    o.timeout = 1500
    try:
        for m in range(1, 9):
            try:
                if o.query(f"MEASurement{m}:ENABle?").strip() not in ("1", "ON"):
                    continue
                out.append({"medida": m, "tipo": o.query(f"MEASurement{m}:MAIN?").strip(),
                            "fuente": o.query(f"MEASurement{m}:SOURce?").strip(),
                            "valor": _valor(o.query(f"MEASurement{m}:RESult:ACTual?"), float)})
                if abs(out[-1]["valor"]) > 9e37:      # R&S marca asi «sin resultado» (9.91e37)
                    out[-1]["valor"] = None
            except (pyvisa.errors.VisaIOError, ValueError):
                _limpiar(o)
    finally:
        o.timeout = t
    return out


def _juntar_h5(origenes, destino, espera_s=40):
    """Junta los .h5 de un canal que exporta el MXO en uno solo con la misma estructura de R&S
    (FileType, Frame y Waveforms/C1, C2...). La carpeta compartida tarda unos segundos en mostrarlos
    en el PC: se espera a que existan y su tamano no cambie. Los temporales de cada canal (los ha
    creado el panel en esta misma captura) se quitan al terminar."""
    import h5py
    fin = time.time() + espera_s
    tam = {}
    while True:
        listos = all(os.path.exists(f) for f in origenes)
        if listos:
            nuevos = {f: os.path.getsize(f) for f in origenes}
            if nuevos == tam and all(v > 0 for v in nuevos.values()):
                break
            tam = nuevos
        if time.time() > fin:
            faltan = [os.path.basename(f) for f in origenes if not os.path.exists(f)]
            raise RuntimeError(f"El osciloscopio no ha dejado los ficheros en la carpeta compartida: {faltan}")
        time.sleep(0.5)
    if len(origenes) == 1:
        os.replace(origenes[0], destino)
        return
    with h5py.File(destino, "w") as d:
        with h5py.File(origenes[0], "r") as s0:
            d.attrs.update(s0.attrs)
            for k in s0:
                if k != "Waveforms":
                    s0.copy(s0[k], d, name=k)
            d.create_group("Waveforms").attrs.update(s0["Waveforms"].attrs)
        for f in origenes:
            with h5py.File(f, "r") as s:
                for k in s["Waveforms"]:
                    s.copy(s["Waveforms"][k], d["Waveforms"], name=k)
    for f in origenes:
        os.remove(f)


def _leer_registro(o, canales):
    """Donde empieza y acaba de verdad el registro (respecto al disparo), del primer canal activo.

    NO va en la sincronizacion de cada segundo: CHANnel:DATA:HEADer? tarda 264-484 ms en Roll
    (medido el 2026-10-28) y deja el osciloscopio en remoto todo ese tiempo. Es el de la ULTIMA
    adquisicion: si se cambia la configuracion y no hay otra nueva, puede ir atrasado.
    """
    activo = next((c for c, v in canales.items() if v["activo"] == "1"), None)
    if not activo:
        return None
    # HEADer? (y DATA?) siguen al ambito de exportacion: con DISPlay solo devuelven lo que se ve en
    # pantalla (manual: "Data transfer in roll mode"). Para ver el registro COMPLETO se pone ALL un
    # instante y se deja como estaba (el usuario lo usa para sus exportaciones).
    previo = None
    try:
        previo = o.query("EXPort:WAVeform:SCOPe?").strip()
        if previo.upper() != "ALL":
            o.write("EXPort:WAVeform:SCOPe ALL")
        x0, x1, n = (float(v) for v in o.query(f"CHANnel{activo}:DATA:HEADer?").split(",")[:3])
        return {"inicio": x0, "fin": x1, "puntos": n, "canal": activo, "hora": time.time()}
    except Exception:
        o.write("*CLS")
        return None
    finally:
        if previo and previo.upper() != "ALL":
            o.write(f"EXPort:WAVeform:SCOPe {previo}")


def coherencia(h):
    """Avisos si los puntos no cubren todo el span de la pantalla (puntos < muestreo * rango)."""
    avisos = []
    if None in (h.get("muestreo_Sa_s"), h.get("rango_s"), h.get("puntos")):
        return avisos                 # lectura incompleta (p. ej. el osciloscopio arrancando): sin aviso
    necesarios = h["muestreo_Sa_s"] * h["rango_s"]
    if necesarios > 0 and h["puntos"] < necesarios * 0.999:
        avisos.append({
            "tipo": "cobertura",
            "texto": (f"El registro ({h['puntos']:.0f} puntos) cubre solo el {100 * h['puntos'] / necesarios:.0f} % "
                      f"de la pantalla: a {h['muestreo_Sa_s']:g} Sa/s, {h['rango_s']:g} s necesitan "
                      f"{necesarios:.0f} puntos."),
            "puntos_necesarios": round(necesarios),
            "muestreo_que_cubre": h["puntos"] / h["rango_s"]})
    return avisos


def _diferencias(a, b, ruta=""):
    out = {}
    for k in b:
        if k in ("adquisiciones", "rango_s", "roll_activo", "avisos"):
            continue
        va, vb = a.get(k) if isinstance(a, dict) else None, b[k]
        if isinstance(vb, dict):
            out.update(_diferencias(va or {}, vb, f"{ruta}{k}."))
        elif va != vb:
            out[f"{ruta}{k}"] = [va, vb]
    return out
