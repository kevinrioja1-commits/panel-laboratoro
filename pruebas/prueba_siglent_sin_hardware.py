r"""Regresión SIN equipos de siglent.py (9-nov-2026): vigilante del panel con un USB que se desenchufa (simulado).

Lo que se vio con la fuente real el 9-nov: al desenchufar, el vigilante avisaba «NO se ha podido mandar apagar» pero
al segundo el reintento de conexión lo cambiaba por «desconectada» y el aviso desaparecía. Ahora:
  - el aviso se mantiene («sin_comunicacion») mientras no vuelva la comunicación;
  - al volver, lo que siguiera encendido se apaga y se comprueba;
  - desconectar y cerrar el panel apagan.
    python pruebas\prueba_siglent_sin_hardware.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import siglent  # noqa: E402

siglent.CORTE_S, siglent.REINTENTO_S = 1.0, 0.4
RES = []


def chk(n, cond, det=""):
    est = "OK" if cond else "FALLO"
    RES.append(est)
    print(f"{est:6} {n}" + (f"  [{det}]" if det != "" else ""), flush=True)


class Registro:
    def __init__(self):
        self.notas = []

    def anotar(self, autor, equipo, accion, detalle=None):
        self.notas.append((accion, str(detalle)))


class Equipo:
    """La fuente simulada (su estado sobrevive a las reconexiones) y un USB que se puede desenchufar."""
    def __init__(self):
        self.sim = siglent.EnlaceSimulado()
        self.enchufado = True


class Enlace:
    def __init__(self, eq):
        if not eq.enchufado:
            raise ConnectionError("NI-VISA no ve la Siglent SPD3303X (simulado: desenchufada)")
        self.eq = eq

    def _ok(self):
        if not self.eq.enchufado:
            raise ConnectionError("VisaIOError simulado: USB desenchufado")

    def escribir(self, o):
        self._ok()
        self.eq.sim.escribir(o)

    def consultar(self, o):
        self._ok()
        r = self.eq.sim.consultar(o)
        return r.replace("SIMULADA", "PRUEBA") if o == "*IDN?" else r

    def numero(self, o, intentos=3):
        return float(self.consultar(o))

    def cerrar(self):
        pass


def esperar(cond, t=6.0):
    fin = time.time() + t
    while time.time() < fin:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def nueva(eq, reg):
    return siglent.Siglent(reg, {"periodo_s": 0.3}, lambda: None, fabrica=lambda cfg: Enlace(eq))


def main():
    eq, reg = Equipo(), Registro()
    s = nueva(eq, reg)
    s.conectar("prueba")
    chk("conecta", s.estado == "conectada", s.estado)
    s.fijar("prueba", "CH1", V=2.0, I=0.1)
    s.salida("prueba", "CH1", True, confirmado=True)
    chk("CH1 encendida", esperar(lambda: eq.sim.c["CH1"][2] and s.canales["CH1"]["salida"]))

    # 1. desenchufar: aviso del vigilante, y se mantiene durante los reintentos
    eq.enchufado = False
    chk("desenchufada: el vigilante avisa en ≈ CORTE_S", esperar(lambda: s.estado == "sin_comunicacion", 4), s.estado)
    chk("dice que NO ha podido apagar", "NO se ha podido mandar apagar" in s.mensaje, s.mensaje)
    time.sleep(2.5)            # varios reintentos fallidos
    chk("el aviso SIGUE tras los reintentos (antes pasaba a «desconectada» y se perdía)",
        s.estado == "sin_comunicacion" and "NO se ha podido mandar apagar" in s.mensaje, f"{s.estado}: {s.mensaje}")
    chk("la página sigue viendo la última salida conocida (CH1 ON)", s.resumen()["canales"]["CH1"]["salida"] is True)

    # 2. volver a enchufar con la salida aún encendida (nadie la apagó en el frontal): el panel la apaga
    eq.enchufado = True
    chk("vuelve la comunicación", esperar(lambda: s.estado == "conectada", 5), s.estado)
    chk("lo que seguía encendido se APAGA al volver", esperar(lambda: not eq.sim.c["CH1"][2], 3), eq.sim.c)
    chk("lo dice el mensaje y el historial", "APAGADO" in s.mensaje and any("recuperada" in a for a, _ in reg.notas),
        s.mensaje)
    chk("las medidas vuelven a 0 V", esperar(lambda: s.canales["CH1"]["salida"] is False and (s.canales["CH1"]["V"] or 0) < 0.05, 3),
        s.canales["CH1"])

    # 3. un corte con la salida ya apagada (en el frontal): al volver no hay nada que apagar
    eq.enchufado = False
    esperar(lambda: s.estado == "sin_comunicacion", 4)
    eq.enchufado = True
    esperar(lambda: s.estado == "conectada", 5)
    chk("corte con todo apagado: al volver lo dice sin tocar nada", "ya estaban apagadas" in s.mensaje, s.mensaje)

    # 4. desconectar y cerrar el panel apagan
    s.salida("prueba", "CH1", True, confirmado=True)
    esperar(lambda: eq.sim.c["CH1"][2])
    s.desconectar("prueba")
    chk("desconectar apaga la salida", not eq.sim.c["CH1"][2], eq.sim.c)
    s2 = nueva(eq, reg)
    s2.conectar("prueba")
    s2.salida("prueba", "CH2", True, confirmado=True)
    esperar(lambda: eq.sim.c["CH2"][2])
    s2.cerrar()
    chk("cerrar el panel apaga la salida", not eq.sim.c["CH2"][2], eq.sim.c)

    # 5. el enlace real en un proceso hijo: un driver que no devuelve la lectura no retiene al panel
    import siglent_modelo as M
    os.environ["SIGLENT_HIJO_PRUEBA"] = "cuelga"
    try:
        en = M.EnlaceProceso()
        chk("proceso hijo: abre", en.recurso == "PRUEBA", en.recurso)
        t = time.time()
        try:
            en.consultar("*IDN?")
            colgado = False
        except ConnectionError as x:
            colgado = str(x)
        dt = time.time() - t
        chk(f"driver colgado: la orden falla a los {M.LIMITE_ORDEN_S:g} s (no 100 s) con un motivo claro",
            bool(colgado) and dt < M.LIMITE_ORDEN_S + 2 and "no contesta" in colgado, f"{dt:.1f} s · {colgado}")
        chk("el hijo colgado se abandona y las órdenes siguientes fallan al momento", en.muerto, "")
        esperar(lambda: M.hijos_retenidos() == 0, 5)
        chk("el hijo abandonado termina (aquí nadie lo retiene)", M.hijos_retenidos() == 0, M.hijos_retenidos())
    finally:
        os.environ.pop("SIGLENT_HIJO_PRUEBA", None)

    n = {k: RES.count(k) for k in ("OK", "FALLO")}
    print(f"\n{n['OK']} OK, {n['FALLO']} FALLO")
    sys.exit(1 if n["FALLO"] else 0)


if __name__ == "__main__":
    main()
