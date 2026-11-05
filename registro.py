"""Registro de cambios del panel: quien cambio que, en que equipo y cuando.

Cada evento se guarda en registro/cambios_AAAAMMDD.jsonl (una linea JSON por
evento) y se mantiene en memoria para mostrarlo en el panel.
"""
import datetime as dt
import json
import os
import threading
from collections import deque

CARPETA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "registro")


class Registro:
    def __init__(self, max_memoria=500):
        self.eventos = deque(maxlen=max_memoria)
        self.lock = threading.Lock()
        os.makedirs(CARPETA, exist_ok=True)

    def anotar(self, autor, equipo, accion, detalle=None):
        ev = {"hora": dt.datetime.now().isoformat(timespec="milliseconds"),
              "autor": autor, "equipo": equipo, "accion": accion, "detalle": detalle}
        with self.lock:
            self.eventos.append(ev)
            ruta = os.path.join(CARPETA, dt.date.today().strftime("cambios_%Y%m%d.jsonl"))
            with open(ruta, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev

    def ultimos(self, n=100):
        with self.lock:
            return list(self.eventos)[-n:]
