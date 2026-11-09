r"""Valida cdaq_modelo.py (opciones del panel -> NI-DAQmx) contra el cDAQ real (9-nov-2026).

Comprueba que cada opción del panel hace en el driver lo mismo que haría el driver a mano, que lo que el modelo da
por imposible el driver también lo rechaza (o, si el driver lo acepta en silencio, que el modelo lo frena) y que la
regla de velocidad del 9213 (modo ADC × número de canales × autocero) es la real. Tareas TEMPORALES; no saca señales.
El panel tiene que haber SOLTADO el cDAQ.

    python herramientas\cdaq_validar_modelo.py [--rapido]
"""
import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time

import nidaqmx
from nidaqmx import constants as K

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # cdaq_modelo.py está en la raíz del panel
import cdaq_modelo as M          # noqa: E402

CH, TC, TV = "cDAQ1", "cDAQ1Mod5", "cDAQ1Mod6"
RES = []
DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")


def chk(g, nombre, cond, detalle=""):
    est = "OMITIDA" if cond is None else "OK" if cond else "FALLO"
    RES.append((est, g, nombre, str(detalle)))
    print(f"{est:7} [{g}] {nombre}" + (f"  [{detalle}]" if detalle != "" else ""), flush=True)


def cod(e):
    return getattr(e, "error_code", None)


def tc_cfg(n=1, **kw):
    c = {"canales": [{"canal": i, "nombre": f"T{i}", "tipo": "K", "min_C": 0.0, "max_C": 100.0} for i in range(n)]}
    c.update(kw)
    return c


def v_cfg(n=1, **kw):
    c = {"canales": [{"canal": i, "nombre": f"V{i}"} for i in range(n)]}
    c.update(kw)
    return c


def tarea(mod, cfg):
    return M.crear_tarea(nidaqmx, mod, TC if mod == "9213" else TV, CH, cfg)


# ------------------------------------------------------------------ 1 · cada opción: el driver queda como dice el panel
def opciones():
    g = "1 opción → driver"
    for clave, valor in M.TC_MODO_ADC.items():
        with tarea("9213", tc_cfg(modo_adc=clave)) as t:
            t.control(K.TaskMode.TASK_COMMIT)
            real = t.ai_channels[0].ai_adc_timing_mode.name
            v = t.read()
        chk(g, f"9213 modo_adc={clave}", real == valor or (clave == "automatico" and real in ("AUTOMATIC", "HIGH_RESOLUTION", "HIGH_SPEED")),
            f"driver {real}, lee {v:.2f} °C")
    for clave, valor in M.TC_AUTOZERO.items():
        with tarea("9213", tc_cfg(autozero=clave)) as t:
            t.control(K.TaskMode.TASK_COMMIT)
            chk(g, f"9213 autozero={clave}", t.ai_channels[0].ai_auto_zero_mode.name == valor)
    for clave, valor in M.TC_CJC.items():
        with tarea("9213", tc_cfg(cjc=clave, cjc_valor_C=25.0)) as t:
            ch = t.ai_channels[0]
            v = t.read()
            chk(g, f"9213 cjc={clave}", ch.ai_thrmcpl_cjc_src.name == valor, f"lee {v:.2f} °C")
    for clave, valor in M.TC_ESCALA.items():
        with tarea("9213", tc_cfg(escala=clave)) as t:
            chk(g, f"9213 escala={clave}", t.ai_channels[0].ai_thrmcpl_scale_type.name == valor, f"lee {t.read():.2f} °C")
    lecturas = {}
    for clave, valor in M.TC_UNIDADES.items():
        cfg = tc_cfg(unidades=clave)
        if clave == "F":
            cfg["canales"][0].update(min_C=32.0, max_C=212.0)
        with tarea("9213", cfg) as t:
            lecturas[clave] = t.read()
            chk(g, f"9213 unidades={clave}", t.ai_channels[0].ai_temp_units.name == valor, f"lee {lecturas[clave]:.2f}")
    if len(lecturas) == 2:
        chk(g, "°F = °C × 1,8 + 32 (misma temperatura)", abs(lecturas["F"] - (lecturas["C"] * 1.8 + 32)) < 0.5,
            f"{lecturas['C']:.2f} °C → {lecturas['F']:.2f} °F")
    for tp, (lo, hi) in M.TC_TIPOS.items():
        cfg = tc_cfg()
        cfg["canales"][0].update(tipo=tp, min_C=float(lo), max_C=float(hi))
        try:
            with tarea("9213", cfg) as t:
                t.control(K.TaskMode.TASK_COMMIT)
                ok = t.ai_channels[0].ai_thrmcpl_type.name == tp
            chk(g, f"9213 tipo {tp} con su rango completo {lo}..{hi} °C", ok)
        except Exception as e:
            chk(g, f"9213 tipo {tp} con su rango completo {lo}..{hi} °C", False, cod(e))
    cfg = tc_cfg(escala="tabla")
    cfg["canales"][0].update(tipo="B", min_C=0.0, max_C=1820.0)
    try:
        with tarea("9213", cfg) as t:
            t.control(K.TaskMode.TASK_COMMIT)
        chk(g, "9213 tipo B con escala tabla: 0..1820 °C", True)
    except Exception as e:
        chk(g, "9213 tipo B con escala tabla: 0..1820 °C", False, cod(e))
    cfg = tc_cfg()
    cfg["canales"][0]["desfase_cables_V"] = 10e-6
    with tarea("9213", cfg) as t:
        chk(g, "9213 desfase de los cables (10 µV)", abs(t.ai_channels[0].ai_thrmcpl_lead_offset_voltage - 10e-6) < 1e-9)
    # 9201: escala propia ×10 frente al mismo canal sin escala
    with tarea("9201", v_cfg(muestreo="finito", tasa_S_s=10000.0, muestras=2000)) as t:
        sin = statistics.fmean(t.read(number_of_samples_per_channel=2000))
    cfg = v_cfg(muestreo="finito", tasa_S_s=10000.0, muestras=2000)
    cfg["canales"][0]["escala"] = {"pendiente": 10.0, "ordenada": 0.0, "unidad": "V"}
    with tarea("9201", cfg) as t:
        con = statistics.fmean(t.read(number_of_samples_per_channel=2000))
        rng = (t.ai_channels[0].ai_min, t.ai_channels[0].ai_max)
    chk(g, "9201 escala propia ×10 (sonda 10:1): lee 10 veces lo mismo", abs(con - 10 * sin) < 0.01, f"sin {sin * 1e3:.2f} mV · con {con * 1e3:.1f} mV · rango {rng}")
    with tarea("9201", v_cfg()) as t:
        ch = t.ai_channels[0]
        chk(g, "9201 terminal RSE y rango ±10 V", ch.ai_term_cfg.name == "RSE" and (ch.ai_rng_low, ch.ai_rng_high) == M.V_RANGO)
    with tarea("9201", v_cfg(2, muestreo="continuo", tasa_S_s=1000.0, conversion_Hz=200000.0)) as t:
        t.control(K.TaskMode.TASK_COMMIT)
        chk(g, "9201 velocidad de conversión 200 kHz", abs(t.timing[TV].ai_conv_rate - 200000.0) < 1, t.timing[TV].ai_conv_rate)
    # muestreo, reloj, búfer
    for clave in M.MUESTREO:
        for mod in ("9213", "9201"):
            cfg = (tc_cfg if mod == "9213" else v_cfg)(muestreo=clave, tasa_S_s=5.0 if mod == "9213" else 1000.0, muestras=5)
            with tarea(mod, cfg) as t:
                t.control(K.TaskMode.TASK_COMMIT)
                tipo = t.timing.samp_timing_type.name
                if clave != "bajo_demanda":
                    modo = t.timing.samp_quant_samp_mode.name
                v = t.read() if clave == "bajo_demanda" else t.read(number_of_samples_per_channel=5, timeout=15)
            esperado = "ON_DEMAND" if clave == "bajo_demanda" else "SAMPLE_CLOCK"
            chk(g, f"{mod} muestreo={clave}", tipo == esperado and (clave == "bajo_demanda" or modo == M.MUESTREO[clave]),
                f"{tipo}, {len(v) if isinstance(v, list) else 1} muestra(s)")
    for clave in ("PFI0", "PFI1"):
        with tarea("9201", v_cfg(muestreo="finito", tasa_S_s=1000.0, muestras=10, reloj=clave)) as t:
            t.control(K.TaskMode.TASK_COMMIT)
            chk(g, f"9201 reloj externo {clave} (configurado; sin señal no avanza)", t.timing.samp_clk_src.endswith(clave), t.timing.samp_clk_src)
    with tarea("9201", v_cfg(muestreo="continuo", tasa_S_s=1000.0, bufer_muestras=50000, sobrescribir=True)) as t:
        t.control(K.TaskMode.TASK_COMMIT)
        chk(g, "9201 búfer 50000 y sobrescribir", t.in_stream.input_buf_size == 50000 and t.in_stream.over_write.name == "OVERWRITE_UNREAD_SAMPLES")
    # disparos (solo configuración: con PFI0 sin cablear no se puede disparar de verdad)
    for d, extra in (("inicio", {"disparo_flanco": "bajada"}), ("referencia", {"pre_muestras": 50}), ("pausa", {"pausa_cuando": "bajo"})):
        for mod in ("9213", "9201"):
            cfg = (tc_cfg if mod == "9213" else v_cfg)(muestreo="finito" if d != "pausa" else "continuo",
                                                       tasa_S_s=5.0 if mod == "9213" else 1000.0, muestras=200, disparo=d, **extra)
            try:
                with tarea(mod, cfg) as t:
                    t.control(K.TaskMode.TASK_COMMIT)
                    tr = t.triggers
                    if d == "inicio":
                        ok = tr.start_trigger.trig_type.name == "DIGITAL_EDGE" and tr.start_trigger.dig_edge_edge.name == "FALLING"
                    elif d == "referencia":
                        ok = tr.reference_trigger.trig_type.name == "DIGITAL_EDGE" and tr.reference_trigger.pretrig_samples == 50
                    else:
                        ok = tr.pause_trigger.trig_type.name == "DIGITAL_LEVEL" and tr.pause_trigger.dig_lvl_when.name == "LOW"
                chk(g, f"{mod} disparo {d} por PFI0 ({extra})", ok)
            except Exception as e:
                chk(g, f"{mod} disparo {d} por PFI0", False, cod(e))
    # registro TDMS del driver
    for clave in ("registrar", "registrar_y_leer"):
        ruta = os.path.join(tempfile.gettempdir(), f"cdaq_val_{clave}.tdms")
        with tarea("9201", v_cfg(muestreo="finito", tasa_S_s=1000.0, muestras=500, registro_driver=clave, registro_ruta=ruta)) as t:
            t.start()
            if clave == "registrar_y_leer":
                t.read(number_of_samples_per_channel=500, timeout=10)
            else:
                t.wait_until_done(timeout=10)
        tam = os.path.getsize(ruta) if os.path.exists(ruta) else 0
        try:
            from nptdms import TdmsFile
            n = sum(len(c) for gr in TdmsFile.read(ruta).groups() for c in gr.channels())
            det = f"{tam} bytes, {n} muestras al releerlo"
            ok = n == 500
        except ImportError:
            det, ok = f"{tam} bytes (nptdms no instalado: no se relee)", tam > 0
        chk(g, f"9201 registro del driver «{clave}» (TDMS)", ok, det)
        try:
            os.remove(ruta)
            os.remove(ruta + "_index")
        except OSError:
            pass


# ------------------------------------------------------------------ 2 · lo que el driver NO vigila
def guardas():
    g = "2 guardas del panel"
    cfg = tc_cfg(16, modo_adc="alta_resolucion", muestreo="continuo", tasa_S_s=50.0)
    try:
        with tarea("9213", cfg) as t:
            t.control(K.TaskMode.TASK_COMMIT)
            acepta = True
    except Exception:
        acepta = False
    chk(g, "el driver ACEPTA 16 termopares en alta resolución a 50 S/s (imposible: ~1 S/s)", acepta)
    chk(g, "… y el modelo lo frena", bool(M.comprobar("9213", cfg)), M.comprobar("9213", cfg))
    cfg = tc_cfg()
    cfg["canales"][0].update(tipo="T", min_C=0.0, max_C=500.0)
    try:
        with tarea("9213", cfg) as t:
            t.control(K.TaskMode.TASK_COMMIT)
        drv = "acepta"
    except Exception as e:
        drv = f"rechaza {cod(e)}"
    chk(g, "tipo T hasta 500 °C (máx 400): el modelo lo frena igual que el driver", bool(M.comprobar("9213", cfg)) and drv.startswith("rechaza"), drv)
    cfg = v_cfg(2, muestreo="continuo", tasa_S_s=300000.0)
    try:
        with tarea("9201", cfg) as t:
            t.control(K.TaskMode.TASK_COMMIT)
        drv = "acepta"
    except Exception as e:
        drv = f"rechaza {cod(e)}"
    chk(g, "9201 2 canales a 300 kS/s (máx 250k): modelo y driver lo rechazan", bool(M.comprobar("9201", cfg)) and drv.startswith("rechaza"), drv)
    cfg = v_cfg(muestreo="continuo", tasa_S_s=1000.0, disparo="referencia")
    try:
        with tarea("9201", cfg) as t:
            t.control(K.TaskMode.TASK_COMMIT)
        drv = "acepta"
    except Exception as e:
        drv = f"rechaza {cod(e)}"
    chk(g, "disparo de referencia con muestreo continuo: el modelo lo frena", bool(M.comprobar("9201", cfg)), f"driver: {drv}")
    try:
        with tarea("9213", tc_cfg(2)) as a, tarea("9213", tc_cfg(1)) as b:
            a.start()
            b.start()
        drv = "acepta"
    except Exception as e:
        drv = cod(e)
    chk(g, "dos tareas en el mismo módulo: el driver las rechaza (-50103 = «ocupado» en el panel)", drv == -50103, drv)
    # mezcla de modos en el mismo módulo
    cfg = tc_cfg(2)
    try:
        with tarea("9213", cfg) as t:
            t.ai_channels[1].ai_adc_timing_mode = K.ADCTimingMode.HIGH_SPEED
            t.control(K.TaskMode.TASK_COMMIT)
        drv = "acepta"
    except Exception as e:
        drv = f"rechaza {cod(e)}"
    chk(g, "9213 con un canal en alta resolución y otro en alta velocidad", True, f"driver {drv} → el panel usa UN modo por módulo")


# ------------------------------------------------------------------ 3 · regla de velocidad del 9213
def medir(cfg, fs, minimo=9, tmin=2.0):
    cfg = dict(cfg, muestreo="continuo", tasa_S_s=fs)
    with tarea("9213", cfg) as t:
        t.in_stream.input_buf_size = max(200, int(fs * 60))
        t.start()
        d = []
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < max(tmin, minimo / fs + 0.5):
            time.sleep(0.3)
            r = t.read(number_of_samples_per_channel=K.READ_ALL_AVAILABLE)
            d += r[0] if len(cfg["canales"]) > 1 else r
    rep = sum(1 for a, b in zip(d, d[1:]) if a == b)
    return len(d), rep


def regla(rapido):
    g = "3 velocidad 9213"
    ns = (1, 2, 4, 8, 16) if rapido else (1, 2, 3, 4, 6, 8, 12, 16)
    filas = []
    for modo in ("alta_resolucion", "alta_velocidad"):
        for az in ("cada_muestra", "no"):
            for n in ns:
                if az == "no" and n not in (1, 2, 16):
                    continue
                pred = M.tasa_max_9213(n, modo, az)
                cfg = tc_cfg(n, modo_adc=modo, autozero=az)
                n1, r1 = medir(cfg, pred * 0.97)
                n2, r2 = medir(cfg, pred * 1.12)
                ok = r1 == 0 and r2 > 0
                filas.append({"modo": modo, "autocero": az, "canales": n, "prevista_S_s": round(pred, 3),
                              "periodo_ms": round(1000 / pred, 2), "al_97": f"{n1} muestras, {r1} repetidas",
                              "al_112": f"{n2} muestras, {r2} repetidas"})
                chk(g, f"{modo} · autocero {az} · {n} canales: máx {pred:.3g} S/s (cada {1000 / pred:.4g} ms)", ok,
                    f"al 97 %: {r1} repetidas de {n1} · al 112 %: {r2} repetidas de {n2}")
    return filas


def regla_9201():
    g = "3 velocidad 9201"
    for n in range(1, 9):
        pred = M.tasa_max_9201(n)
        with tarea("9201", v_cfg(n, muestreo="finito", tasa_S_s=1000.0, muestras=100)) as t:
            t.control(K.TaskMode.TASK_COMMIT)
            drv = t.timing.samp_clk_max_rate
        try:
            with tarea("9201", v_cfg(n, muestreo="finito", tasa_S_s=pred * 1.02, muestras=100)) as t:
                t.control(K.TaskMode.TASK_COMMIT)
            encima = "acepta"
        except Exception as e:
            encima = cod(e)
        with tarea("9201", v_cfg(n, muestreo="finito", tasa_S_s=pred, muestras=int(pred * 0.2))) as t:
            d = t.read(number_of_samples_per_channel=int(pred * 0.2), timeout=10)
            m = len(d[0]) if n > 1 else len(d)
        chk(g, f"{n} canal(es): máx {pred / 1000:g} kS/s (cada {1e6 / pred:.3g} µs)",
            abs(drv - pred) < 1 and encima == -200332 and m == int(pred * 0.2),
            f"driver {drv / 1000:g} kS/s · al 102 %: {encima} · 0,2 s a la máxima: {m} muestras")


# ------------------------------------------------------------------ 4 · los dos módulos a la vez, cada uno a su máximo
def simultaneo():
    g = "4 a la vez"
    res = {}

    def v():
        with tarea("9201", v_cfg(1, muestreo="continuo", tasa_S_s=M.tasa_max_9201(1))) as t:
            t.in_stream.input_buf_size = 4_000_000
            t.start()
            n, t0 = 0, time.perf_counter()
            while time.perf_counter() - t0 < 6:
                time.sleep(0.05)
                n += len(t.read(number_of_samples_per_channel=K.READ_ALL_AVAILABLE))
            res["v"] = (n, time.perf_counter() - t0)
    h = threading.Thread(target=v)
    h.start()
    fs = M.tasa_max_9213(2, "alta_velocidad")
    n, rep = medir(tc_cfg(2, modo_adc="alta_velocidad"), fs, tmin=6)
    h.join()
    nv, tv = res["v"]
    chk(g, f"9213 2 termopares a {fs:g} S/s + 9201 1 canal a 800 kS/s, en dos tareas", rep == 0 and nv > 0.95 * 800000 * tv,
        f"9213: {n} muestras, {rep} repetidas · 9201: {nv} muestras en {tv:.1f} s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rapido", action="store_true")
    ap.add_argument("--solo", choices=("opciones", "guardas", "velocidad", "a_la_vez"))
    a = ap.parse_args()
    filas = None
    if a.solo in (None, "opciones"):
        opciones()
    if a.solo in (None, "guardas"):
        guardas()
    if a.solo in (None, "velocidad"):
        regla_9201()
        filas = regla(a.rapido)
    if a.solo in (None, "a_la_vez"):
        simultaneo()
    n = {e: sum(1 for r in RES if r[0] == e) for e in ("OK", "FALLO", "OMITIDA")}
    print(f"\n{n['OK']} OK · {n['FALLO']} FALLO · {n['OMITIDA']} OMITIDA")
    base = os.path.join(DOCS, "cdaq_validacion_modelo_" + time.strftime("%Y%m%d_%H%M"))
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump({"resultados": RES, "velocidad_9213": filas}, f, ensure_ascii=False, indent=1)
    print("Informe:", base + ".json")


if __name__ == "__main__":
    main()
