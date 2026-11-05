"""Ensayo (fase 4 del rediseno, acordado con el usuario el 2026-11-02).

Una carpeta <proyecto>\\ensayos\\[<carpeta>\\]AAAAMMDD_HHMM_nombre\\ donde el panel recoge, sin interpretar nada,
todo lo que pasa mientras el usuario maneja la fuente y el osciloscopio:

  ensayo.json   condiciones (montaje, termopares...), notas, opciones, equipos y su configuracion al empezar,
                inicio y fin
  fuente.csv    continuo, con el pre-registro: medidas, consignas, modo, ON/OFF y cambios en el frontal
  osc.h5        si se elige el osciloscopio (datalogger continuo, un solo fichero)
  capturas\\     las capturas que se hagan durante el ensayo
  fotos\\ picolog\\   las pone el usuario

Sin duracion: hasta Terminar. Al terminar no se pregunta nada (el analisis es posterior, en MATLAB). Con
«apagar al terminar», se apaga la salida y se sigue grabando lo elegido (p. ej. el enfriamiento).

Ensayo programado (fase 5): con "plan" el panel sigue sus etapas (plan.py) y el ensayo se termina al acabar el plan.
"""
import json
import os
import threading
import time

import almacen
import plan as planes


class Ensayos:
    def __init__(self, osc, fte, reg):
        self.osc, self.fte, self.reg = osc, fte, reg
        self.pl = None                         # temperaturas en uso (TC-08 del panel o PicoLog); lo pone el servidor
        self.tc = None                         # el TC-08 (tc08.py), para aplicar la configuracion de una plantilla
        self.actual = None
        self.preparado = None                  # {proyecto, plan, hora}: plan dejado listo para revisar (/api/ensayo/preparar)
        self.lock = threading.Lock()
        self._fin = threading.Event()          # para no esperar la cola si se cierra el panel

    # ---------------------------------------------------------------- estado
    def resumen(self):
        a = self.actual
        if not a:
            return None
        cap = os.path.join(a["ruta"], "capturas")
        n = len([f for f in os.listdir(cap) if f.endswith((".h5", ".png", ".json"))]) if os.path.isdir(cap) else 0
        return {"nombre": a["nombre"], "rel": a["rel"], "inicio": a["inicio"], "osc": bool(a["osc"]),
                "plan": a["plan"].resumen() if a.get("plan") else None,
                "fin_previsto": a.get("fin_previsto"), "terminar_al_cumplir": a.get("terminar_al_cumplir"),
                "temperaturas_registrando": bool(self.pl and self.pl.grab and self.pl.grab["fichero"] == a.get("tcsv")),
                "estado": a["estado"], "cierra_en": a.get("cierra_en"), "capturas": n, "encender": a.get("encender"),
                "fuente_registrando": bool(self.fte.grab and self.fte.grab["fichero"] == a["csv"]),
                "osc_registrando": bool(a["osc"] and self.osc.dl.get("fichero") == a["h5"]
                                        and self.osc.dl.get("fin") is None)}

    def destino_capturas(self):
        """(proyecto, subcarpeta, tipo) para que una captura vaya a <ensayo>\\capturas\\."""
        a = self.actual
        if not a:
            raise RuntimeError("No hay ningún ensayo en marcha")
        sub = "/".join(p for p in (a["carpeta"], a["dir"], "capturas") if p)
        return a["proyecto"], sub, "ensayos"

    # ---------------------------------------------------------------- iniciar
    def iniciar(self, p, autor):
        num = lambda k, d: d if p.get(k) in (None, "") else float(p[k])
        pre, cola, apagar = max(0.0, num("pre_s", 30.0)), max(0.0, num("cola_s", 3.0)), bool(p.get("apagar_al_terminar"))
        encender = bool(p.get("encender_al_iniciar"))
        if encender and not p.get("confirmado"):
            # antes solo lo preguntaba la pagina: por la API se encendia sin confirmar (revisión del 2026-11-04)
            raise ValueError("«Al iniciar, encender la salida» necesita confirmación explícita")
        oscp = p.get("osc") or None              # {"canales": [1], "sr": 100000} o nada
        plan, plan_resumen = None, None
        if p.get("plan"):
            # se comprueba ANTES de crear nada; el encendido lo decide el plan
            plan, plan_resumen = self.comprobar_plan(p["plan"], bool(oscp))
            if plan_resumen["enciende"] and not p.get("confirmado"):
                raise ValueError("El plan enciende la salida: hace falta confirmarlo")
            encender = False
        with self.lock:
            if self.actual:
                raise RuntimeError(f"Ya hay un ensayo en marcha: {self.actual['nombre']}")
            if self.fte.estado != "conectada":
                raise RuntimeError("Conecta la fuente: el ensayo registra siempre la fuente")
            if self.fte.grab:
                raise RuntimeError("Ya se está registrando la fuente (Registrar): páralo antes de empezar el ensayo")
            if oscp and (self.osc.modo == "datalogger" or self.osc._datalogger_vivo()):
                raise RuntimeError("Hay un registro del osciloscopio en marcha: páralo antes")
            nombre = p.get("nombre", "").strip()
            almacen._limpio(nombre)
            proyecto, carpeta = p.get("proyecto", ""), p.get("carpeta", "")
            ruta, _, rel = almacen.destino(proyecto, "ensayos", carpeta, nombre, carpeta_propia=True)
            for d in ("fotos", "picolog", "capturas"):
                os.makedirs(os.path.join(ruta, d), exist_ok=True)
            # configuracion de los equipos al empezar (el osciloscopio solo si se puede leer ahora)
            osc_estado = None
            if self.osc.modo not in ("manual", "datalogger") and not self.osc._datalogger_vivo():
                try:
                    osc_estado = self.osc.leer(autor)
                except Exception as e:
                    osc_estado = {"no_leido": str(e)}
            f = self.fte.resumen()
            ficha = {"tipo": "ensayo", "nombre": nombre, "proyecto": proyecto, "carpeta": carpeta, "ruta": rel,
                     "inicio": time.strftime("%Y-%m-%dT%H:%M:%S"), "fin": None,
                     "condiciones": p.get("condiciones", ""), "notas": p.get("notas", ""),
                     "opciones": {"pre_registro_s": pre, "apagar_al_terminar": apagar, "seguir_tras_apagar_s": cola,
                                  "encender_al_iniciar": encender, "osciloscopio": oscp,
                                  "duracion_s": num("duracion_s", None), "terminar_al_cumplir": bool(p.get("terminar_al_cumplir"))},
                     "plan": plan, "plan_resumen": plan_resumen["texto"] if plan_resumen else None,
                     "fuente": {k: f.get(k) for k in ("idn", "consignas", "limites", "equipo", "periodo_s")},
                     "osciloscopio": {"idn": self.osc.idn, "al_empezar": osc_estado}}
            # PicoLog: si esta capturando, su configuracion (equipo, canales con los nombres del usuario, tipo,
            # intervalo) y temperaturas.csv en la carpeta del ensayo, con el mismo reloj que fuente.csv
            # «Temperaturas» en Qué registrar: True = grabarlas (si el TC-08 no lo tiene nadie, lo toma el panel);
            # un dict = ademas configurar los termopares (plantillas); False = no grabarlas
            quiere_t = p.get("temperaturas", True)
            if quiere_t and self.tc is not None:
                if isinstance(quiere_t, dict):
                    self.tc.configurar(quiere_t, autor)
                usa_picolog = getattr(self.pl, "activa", lambda: None)() is not self.tc and self.pl.estado == "capturando"
                if self.tc.estado != "midiendo" and not usa_picolog:
                    self.tc.tomar(autor)       # error claro si PicoLog esta abierto o no hay TC-08
                    time.sleep(0.6)            # que lleguen las primeras medidas
            con_pl = bool(quiere_t and self.pl and self.pl.estado in ("capturando", "midiendo") and self.pl.canales)
            ficha["picolog"] = self.pl.ficha() if con_pl else {
                "no_registrado": (self.pl.mensaje or f"PicoLog: {self.pl.estado}") if self.pl else "sin PicoLog"}
            ruta_json = os.path.join(ruta, "ensayo.json")
            with open(ruta_json, "w", encoding="utf-8") as fh:
                json.dump(ficha, fh, ensure_ascii=False, indent=1)
            csv, h5 = os.path.join(ruta, "fuente.csv"), os.path.join(ruta, "osc.h5")
            tcsv = os.path.join(ruta, "temperaturas.csv")
            self.fte.iniciar_registro(csv, autor, pre, None, None, {"ensayo": rel})   # el apagado lo lleva el ensayo
            if con_pl:
                try:
                    self.pl.iniciar_registro(tcsv, autor, pre, {"ensayo": rel})
                except Exception as e:
                    self.reg.anotar("panel (automático)", "picolog", "temperaturas NO registradas", str(e))
            if oscp:
                try:
                    self.osc.iniciar_datalogger(oscp["canales"], float(oscp["sr"]), 0, proyecto, autor, ruta_h5=h5)
                except Exception:
                    self.fte.parar_registro(autor, "no arrancó el osciloscopio", inmediato=True)
                    raise
            self.actual = {"nombre": nombre, "proyecto": proyecto, "carpeta": carpeta, "dir": os.path.basename(ruta),
                           "ruta": ruta, "rel": rel, "json": ruta_json, "csv": csv, "h5": h5, "osc": oscp, "tcsv": tcsv,
                           "inicio": time.time(), "estado": "en marcha", "apagar": apagar, "cola": cola,
                           "encender": "pendiente" if encender else None, "plan": None,
                           # consignas que vio la persona al confirmar el encendido (o las de ahora)
                           "consignas_confirmadas": (p.get("consignas_confirmadas") or
                                                     {k: f.get("consignas", {}).get(k) for k in ("V_lim_V", "I_A", "P_W")})
                                                    if encender else None,
                           "fin_previsto": time.time() + num("duracion_s", 0) if num("duracion_s", 0) else None,
                           "terminar_al_cumplir": bool(p.get("terminar_al_cumplir")) and bool(num("duracion_s", 0))}
            if self.actual["fin_previsto"]:
                threading.Thread(target=self._vigilar_duracion, args=(self.actual,), daemon=True).start()
            if plan:
                self.actual["plan"] = planes.Ejecucion(self, self.actual, plan, autor)
        if plan and plan_resumen["enciende"]:
            # iniciar un plan que enciende ya lo ha confirmado la persona (con sus etapas): quita un bloqueo anterior
            # (si no, tras un APAGAR de antes el plan se pausaba nada más empezar; visto en Chrome el 2026-11-04)
            self.fte.desbloquear(autor)
        self.reg.anotar(autor, "ensayo", "ensayo iniciado", {"ruta": rel, "osciloscopio": oscp, "apagar_al_terminar": apagar,
                                                            "seguir_tras_apagar_s": cola,
                                                            "encender_al_iniciar": encender,
                                                            "plan": plan_resumen["texto"] if plan_resumen else None})
        if plan:
            self.actual["plan"].arrancar()
        if encender:
            threading.Thread(target=self._encender_al_empezar, args=(self.actual, autor), daemon=True).start()
        return self.resumen()

    def comprobar_plan(self, plan, con_osc):
        """(plan normalizado, resumen) o ValueError con todos los problemas (ver plan.comprobar)."""
        f = self.fte.resumen()
        if f["estado"] != "conectada":
            raise ValueError("Conecta la fuente para comprobar el plan (se compara con sus límites)")
        return planes.comprobar(plan, f["limites"], f.get("equipo") or {}, f.get("consignas") or {}, con_osc)

    def _esperar_registro(self, a):
        """Hasta que se graba todo (el osciloscopio tarda unos segundos en configurarse y llenar su ventana; sin el,
        enseguida), mas 1 s de «antes»: lo que haga el ensayo queda dentro del registro."""
        fin = time.time() + 90
        if a["osc"]:
            while self.actual is a and a["estado"] == "en marcha" and time.time() < fin and                     not any(l.startswith("Registrando") for l in self.osc.dl.get("salida", [])):
                if self.osc.dl.get("fin") is not None:
                    break
                time.sleep(0.2)
        time.sleep(1.0)                                   # un poco de «antes» tambien en el osciloscopio

    def _encender_al_empezar(self, a, autor):
        """«Al iniciar, encender la salida»: cuando ya se graba todo (ver _esperar_registro)."""
        self._esperar_registro(a)
        if self.actual is not a or a["estado"] != "en marcha":
            a["encender"] = "no (ensayo terminado antes)"
            return
        grabando = (self.osc.dl.get("fichero") == a["h5"] and self.osc.dl.get("fin") is None
                    and any(l.startswith("Registrando") for l in self.osc.dl.get("salida", [])))
        if a["osc"] and not grabando:
            a["encender"] = "NO: el osciloscopio no llegó a grabar"
            self.reg.anotar("panel (automático)", "ensayo", "salida NO encendida al iniciar",
                            "el osciloscopio no llegó a grabar: enciéndela a mano si quieres seguir")
            return
        # hasta 90 s de espera: si entretanto cambiaron las consignas, no se enciende con unas que nadie confirmo
        conf, ahora = a.get("consignas_confirmadas") or {}, self.fte.consignas
        distintas = [f"{n} {conf[c]:g} → {ahora.get(c)}" for n, c in (("V", "V_lim_V"), ("I", "I_A"), ("P", "P_W"))
                     if conf.get(c) is not None and (ahora.get(c) is None or
                                                    abs(float(conf[c]) - ahora[c]) > max(1e-6, 1e-4 * abs(float(conf[c]))))]
        if distintas:
            a["encender"] = "NO: las consignas cambiaron tras confirmar (" + ", ".join(distintas) + ")"
            self.reg.anotar("panel (automático)", "ensayo", "salida NO encendida al iniciar",
                            a["encender"] + ": enciéndela a mano si quieres seguir")
            return
        try:
            self.fte.salida(True, True, autor)
            a["encender"] = "hecho"
        except Exception as e:
            a["encender"] = f"NO: {e}"
            self.reg.anotar("panel (automático)", "ensayo", "salida NO encendida al iniciar", str(e))

    # ---------------------------------------------------------------- terminar
    def terminar(self, autor, forzar_apagar=False):
        with self.lock:
            a = self.actual
            if not a:
                raise RuntimeError("No hay ningún ensayo en marcha")
            if a["estado"] != "en marcha":
                return self.resumen()
            a["estado"] = "terminando"
        ej = a.get("plan")
        if ej and ej.vivo() and ej._hilo is not threading.current_thread():
            # el plan se para ANTES de apagar: si no, podria volver a encender justo despues
            ej.parar(autor, "Terminar ensayo")
            ej._hilo.join(timeout=5)
            forzar_apagar = forzar_apagar or ej.estado != "terminado"
        cola = 0.0
        if a["apagar"] or forzar_apagar:
            # la consigna de salida es la de la ultima orden; la medida puede ir 0,1 s por detras (y se apagaba dos veces)
            encendida = self.fte.consignas.get("salida", self.fte.medida.get("salida"))
            if self.fte.estado == "conectada" and encendida:
                try:
                    self.fte.salida(False, True, "panel (automático)")
                    cola = a["cola"]
                except Exception as e:
                    self.reg.anotar("panel (automático)", "fuente", "NO SE PUDO APAGAR al terminar el ensayo",
                                    f"{e}: apagar a mano")
            else:
                cola = a["cola"]          # ya apagada: igualmente se graba lo pedido despues de Terminar
        a["cierra_en"] = time.time() + cola
        self.reg.anotar(autor, "ensayo", "terminar ensayo", f"se sigue grabando {cola:g} s" if cola else None)
        threading.Thread(target=self._cerrar_tras, args=(cola, autor), daemon=True).start()
        return self.resumen()

    def _vigilar_duracion(self, a):
        """Duracion del ensayo: al cumplirse, si se pidio, se termina (con «apagar al terminar» se apaga la salida y
        se sigue grabando lo elegido); si es orientativa solo se anota (la pagina avisa)."""
        while self.actual is a and a["estado"] == "en marcha" and time.time() < a["fin_previsto"]:
            self._fin.wait(min(0.5, max(0.05, a["fin_previsto"] - time.time())))
            if self._fin.is_set():
                return
        if self.actual is not a or a["estado"] != "en marcha":
            return
        self.reg.anotar("panel (automático)", "ensayo", "duración cumplida",
                        "se termina el ensayo" if a["terminar_al_cumplir"] else "orientativa: el ensayo sigue")
        self.fte.marca("duración cumplida" + ("" if a["terminar_al_cumplir"] else " (orientativa)"))
        if a["terminar_al_cumplir"]:
            try:
                self.terminar("panel (duración cumplida)")
            except Exception as e:
                self.reg.anotar("panel (automático)", "ensayo", "no se pudo terminar al cumplirse la duración", str(e))

    def plan_mando(self, accion, autor, confirmado=False):
        """Pausar / continuar / siguiente / parar del plan en curso (PARAR apaga y termina el ensayo)."""
        a = self.actual
        if not a or not a.get("plan"):
            raise RuntimeError("No hay ningún ensayo programado en marcha")
        ej = a["plan"]
        if accion == "parar":
            ej.parar(autor)
            return self.terminar(autor, forzar_apagar=True)
        if accion == "continuar":
            ej.continuar(autor, confirmado)
        else:
            {"pausar": ej.pausar, "siguiente": ej.siguiente}[accion](autor)
        return self.resumen()

    def _fin_del_plan(self, a, apagar):
        """Lo llama el plan al acabar (bien, por PARAR o por error): termina el ensayo si sigue en marcha."""
        if self.actual is a and a["estado"] == "en marcha":
            try:
                self.terminar("plan", forzar_apagar=apagar)
            except Exception as e:
                self.reg.anotar("panel (automático)", "ensayo", "no se pudo terminar el ensayo al acabar el plan", str(e))

    def _capturar(self, nombre, autor):
        """Etapa «captura» del plan: como Capturar con «Guardar en el ensayo en curso» (pantalla, formas y ficha)."""
        r = self.fte.resumen()
        foto = {k: r[k] for k in ("idn", "medida", "consignas", "alarmas", "equipo", "limites")} \
            if r["estado"] == "conectada" else None
        proyecto, carpeta, tipo = self.destino_capturas()
        return self.osc.capturar(proyecto, carpeta, nombre, "etapa del plan", "DISP", True, True, True, foto, autor,
                                 formas=True, tipo=tipo)

    def _cerrar_tras(self, cola, autor):
        self._fin.wait(cola)
        self._cerrar(autor, "Terminar")

    def _cerrar(self, autor, motivo):
        with self.lock:
            a = self.actual
            if not a:
                return
            otro = a.get("cerrando")
            if not otro:
                a["cerrando"] = threading.Event()
        if otro:
            # ya lo esta cerrando otro hilo: se espera a que acabe (al cerrar el panel, si no, se salia antes de
            # completar ensayo.json)
            otro.wait(90)
            return
        dl_fin = None
        if a["osc"] and self.osc.dl.get("fichero") == a["h5"]:
            if self.osc.dl.get("fin") is None:
                try:
                    self.osc.parar_datalogger(autor)
                except RuntimeError:
                    pass
                t = time.time()
                while self.osc.dl.get("fin") is None and time.time() - t < 60:
                    time.sleep(0.2)
            dl_fin = self.osc.dl.get("fin")
        if self.pl and self.pl.grab and self.pl.grab["fichero"] == a.get("tcsv"):
            self.pl.parar_registro(autor, f"fin del ensayo ({motivo})")
        if self.fte.grab and self.fte.grab["fichero"] == a["csv"]:
            self.fte.parar_registro(autor, f"fin del ensayo ({motivo})", inmediato=True)
        try:
            with open(a["json"], encoding="utf-8") as fh:
                ficha = json.load(fh)
            if a.get("plan"):
                ej = a["plan"]
                ficha.update(plan_estado_final=ej.estado, plan_mensaje=ej.mensaje, plan_historial=ej.historial)
            ficha.update(fin=time.strftime("%Y-%m-%dT%H:%M:%S"), motivo_fin=motivo,
                         duracion_s=round(time.time() - a["inicio"], 1),
                         osciloscopio_codigo_salida=dl_fin,
                         ficheros=sorted(os.listdir(a["ruta"])))
            with open(a["json"], "w", encoding="utf-8") as fh:
                json.dump(ficha, fh, ensure_ascii=False, indent=1)
        except Exception as e:
            self.reg.anotar("panel (automático)", "ensayo", "no se pudo completar ensayo.json", str(e))
        self.reg.anotar(autor, "ensayo", "ensayo terminado", {"ruta": a["rel"], "motivo": motivo})
        self.actual = None
        a["cerrando"].set()

    def cerrar_panel(self):
        """Al cerrar el panel con un ensayo en marcha: se cierra ordenadamente (sin esperar la cola)."""
        self._fin.set()
        if self.actual and self.actual.get("plan"):
            ej = self.actual["plan"]
            ej.parar("panel (cierre)", "cierre del panel")
            if ej._hilo.is_alive() and ej._hilo is not threading.current_thread():
                ej._hilo.join(timeout=5)
        # la salida la apaga después fte.desconectar(si_encendida="apagar") (servidor.al_cerrar)
        if self.actual:
            self._cerrar("panel (automático)", "cierre del panel")
