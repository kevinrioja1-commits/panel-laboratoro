r"""Capacidades y límites reales del cDAQ-9178 con el NI 9213 y el NI 9201 (fase 2B, 9-nov-2026).

Script de UNA SOLA VEZ (pasos 3.5, 4, 5.1, 6 y 7.3 de docs/PLAN_PRUEBAS_LABORATORIO.md). Crea tareas TEMPORALES de
NI-DAQmx (nada guardado en NI MAX) y las cierra al acabar. El panel tiene que haber SOLTADO el cDAQ antes.
Solo mide entradas: no saca nada por ningún terminal.

    python herramientas\cdaq_capacidades.py [--rapido]

Escribe docs\cdaq_capacidades_<fecha>.json y .md (fuera de git: llevan números de serie).
"""
import argparse
import datetime as dt
import json
import math
import os
import statistics
import sys
import time

import nidaqmx
import nidaqmx.system
from nidaqmx.constants import (AcquisitionType, ADCTimingMode, AutoZeroType, CJCSource, TaskMode, TemperatureUnits,
                               TerminalConfiguration, ThermocoupleType)

AQUI = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(os.path.dirname(AQUI), "docs")
R = {"fecha": dt.datetime.now().isoformat(timespec="seconds")}


def err(e):
    c = getattr(e, "error_code", None)
    t = str(e).split("\n")
    return f"[{c}] " + " ".join(l.strip() for l in t[:3] if l.strip())[:300]


def prop(obj, nombre):
    try:
        v = getattr(obj, nombre)
        return v.name if hasattr(v, "name") else v
    except Exception as e:
        return "ERROR " + err(e)


def modulos():
    s = nidaqmx.system.System.local()
    tc = tv = chasis = None
    for d in s.devices:
        t = d.product_type
        if "9213" in t:
            tc = d.name
        elif "9201" in t:
            tv = d.name
        elif "9178" in t:
            chasis = d.name
    return chasis, tc, tv


def canal_tc(task, mod, i, tipo=ThermocoupleType.K, cjc=CJCSource.BUILT_IN, mn=0.0, mx=100.0, valor_cjc=25.0):
    return task.ai_channels.add_ai_thrmcpl_chan(f"{mod}/ai{i}", min_val=mn, max_val=mx, units=TemperatureUnits.DEG_C,
                                                thermocouple_type=tipo, cjc_source=cjc, cjc_val=valor_cjc)


def seccion(n):
    print("\n=== " + n, flush=True)


# ------------------------------------------------------------------ 9213
def m9213(mod, rapido):
    out = {}
    seccion("9213 · valores por defecto (3.5)")
    with nidaqmx.Task() as t:
        ch = canal_tc(t, mod, 0)
        out["por_defecto"] = {k: prop(ch, k) for k in (
            "ai_adc_timing_mode", "ai_auto_zero_mode", "ai_rng_high", "ai_rng_low", "ai_min", "ai_max",
            "ai_thrmcpl_cjc_src", "ai_open_thrmcpl_detect_enable", "ai_lowpass_enable", "ai_resolution")}
        out["por_defecto"]["lectura_ai0_C"] = t.read()
    print(out["por_defecto"], flush=True)

    seccion("9213 · tipos de termopar (5.1) y su rango")
    tipos = {}
    for tp in ThermocoupleType:
        r = {}
        # 1) rango que admite el driver para este tipo: minimo y maximo buscados por biseccion (TASK_VERIFY)
        def vale(mn, mx):
            try:
                with nidaqmx.Task() as t:
                    canal_tc(t, mod, 0, tp, mn=mn, mx=mx)
                    t.control(TaskMode.TASK_VERIFY)
                return True
            except Exception:
                return False
        base = next(((c - 1.0, c + 1.0) for c in (25.0, 300.0, 600.0, 1000.0, 1500.0) if vale(c - 1.0, c + 1.0)), None)
        if base:
            lo_ok, lo_mal = base[0], -400.0
            for _ in range(14):
                m = (lo_ok + lo_mal) / 2
                lo_ok, lo_mal = (m, lo_mal) if vale(m, base[1]) else (lo_ok, m)
            hi_ok, hi_mal = base[1], 3500.0
            for _ in range(14):
                m = (hi_ok + hi_mal) / 2
                hi_ok, hi_mal = (m, hi_mal) if vale(base[0], m) else (hi_ok, m)
            r["rango_admitido_C"] = [round(lo_ok, 1), round(hi_ok, 1)]
            r["rango_completo_a_la_vez"] = vale(lo_ok, hi_ok)
        else:
            r["rango_admitido_C"] = "ningún rango de prueba aceptado"
        # 2) un rango dentro del admitido, arrancar y leer ai0 (el termopar conectado es K: con otro tipo no vale)
        mn0, mx0 = (0.0, 100.0) if not base else (max(base[0], 0.0), max(base[1], 100.0))
        try:
            with nidaqmx.Task() as t:
                canal_tc(t, mod, 0, tp, mn=mn0, mx=mx0)
                r["lectura_ai0"] = t.read()
                r["crear_y_leer"] = "OK"
        except Exception as e:
            r["crear_y_leer"] = err(e)
        tipos[tp.name] = r
        print(tp.name, r, flush=True)
    out["tipos"] = tipos

    seccion("9213 · fuente de la unión fría (CJC)")
    cjc = {}
    for nombre, src in (("interna", CJCSource.BUILT_IN), ("constante_25C", CJCSource.CONSTANT_USER_VALUE)):
        try:
            with nidaqmx.Task() as t:
                canal_tc(t, mod, 0, cjc=src)
                cjc[nombre] = {"lectura_ai0_C": t.read()}
        except Exception as e:
            cjc[nombre] = err(e)
    out["cjc"] = cjc
    print(cjc, flush=True)

    seccion("9213 · detección de termopar abierto (6)")
    try:
        with nidaqmx.Task() as t:
            for i in range(4):
                canal_tc(t, mod, i)
            t.start()
            v = t.read()
            out["abiertos"] = {"lecturas_ai0_ai3": v, "existen": prop(t.in_stream, "open_thrmcpl_chans_exist"),
                               "canales": prop(t.in_stream, "open_thrmcpl_chans")}
    except Exception as e:
        out["abiertos"] = err(e)
    print(out["abiertos"], flush=True)

    seccion("9213 · velocidad real (D3, paso 4)")
    dev = nidaqmx.system.Device(mod)
    filas = []
    for n in (16, 8, 1):
        for modo in (ADCTimingMode.HIGH_RESOLUTION, ADCTimingMode.HIGH_SPEED):
            f = {"canales": n, "modo": modo.name, "max_multi_entre_n": dev.ai_max_multi_chan_rate / n}
            try:
                with nidaqmx.Task() as t:
                    for i in range(n):
                        ch = canal_tc(t, mod, i)
                        ch.ai_adc_timing_mode = modo
                    t.timing.cfg_samp_clk_timing(1.0, sample_mode=AcquisitionType.CONTINUOUS)
                    t.control(TaskMode.TASK_COMMIT)     # sin reservar devuelve la del chasis (4 MS/s)
                    f["samp_clk_max_rate"] = t.timing.samp_clk_max_rate
            except Exception as e:
                f["samp_clk_max_rate"] = err(e)
            # (b) bajo demanda
            try:
                with nidaqmx.Task() as t:
                    for i in range(n):
                        ch = canal_tc(t, mod, i)
                        ch.ai_adc_timing_mode = modo
                    t.start()
                    t.read()
                    k = 5 if (rapido or modo == ADCTimingMode.HIGH_RESOLUTION and n == 16) else 15
                    t0 = time.perf_counter()
                    for _ in range(k):
                        t.read()
                    f["bajo_demanda_lecturas_s"] = k / (time.perf_counter() - t0)
            except Exception as e:
                f["bajo_demanda_lecturas_s"] = err(e)
            # (c) reloj de hardware. En el 9213 (módulo lento) DAQmx admite cualquier tasa (samp_clk_max_rate da la del
            # chasis) y si va más rápido de lo que convierte, el módulo REPITE muestras sin avisar (manual 372838E).
            # Se prueba al 90 % y al 150 % de lo medido bajo demanda y se cuentan las repetidas en ai0 (termopar puesto).
            od = f.get("bajo_demanda_lecturas_s")
            if isinstance(od, float):
                f["hw"] = {}
                for fac in (0.9, 1.5):
                    fs = od * fac
                    dur = 4 if rapido else 8
                    try:
                        with nidaqmx.Task() as t:
                            for i in range(n):
                                ch = canal_tc(t, mod, i)
                                ch.ai_adc_timing_mode = modo
                            t.timing.cfg_samp_clk_timing(fs, sample_mode=AcquisitionType.CONTINUOUS,
                                                         samps_per_chan=max(100, int(fs * 30)))
                            t.start()
                            datos = []
                            t0 = time.perf_counter()
                            while time.perf_counter() - t0 < dur:
                                time.sleep(0.5)
                                d = t.read(number_of_samples_per_channel=nidaqmx.constants.READ_ALL_AVAILABLE)
                                datos += (d[0] if n > 1 else d)
                            tt = time.perf_counter() - t0
                        rep = sum(1 for a, b in zip(datos, datos[1:]) if a == b)
                        f["hw"][f"{fac:g}x"] = {"tasa": round(fs, 2), "muestras_ai0": len(datos), "esperadas": round(fs * tt),
                                               "repetidas": rep, "frac_repetidas": round(rep / max(1, len(datos) - 1), 3),
                                               "ruido_std_C": round(statistics.pstdev(datos), 4) if len(datos) > 2 else None}
                    except Exception as e:
                        f["hw"][f"{fac:g}x"] = err(e)
            print(f, flush=True)
            filas.append(f)
    out["velocidad"] = filas

    seccion("9213 · autocero (cada muestra / una vez / nunca) a 16 canales alta velocidad")
    az = {}
    for a in (AutoZeroType.EVERY_SAMPLE, AutoZeroType.ONCE, AutoZeroType.NONE):
        try:
            with nidaqmx.Task() as t:
                for i in range(16):
                    ch = canal_tc(t, mod, i)
                    ch.ai_adc_timing_mode = ADCTimingMode.HIGH_SPEED
                    ch.ai_auto_zero_mode = a
                t.timing.cfg_samp_clk_timing(1.0, sample_mode=AcquisitionType.CONTINUOUS)
                t.control(TaskMode.TASK_COMMIT)
                az[a.name] = t.timing.samp_clk_max_rate
        except Exception as e:
            az[a.name] = err(e)
    out["autocero_max_rate_16ch"] = az
    print(az, flush=True)
    return out


# ------------------------------------------------------------------ 9201
def m9201(mod, rapido):
    out = {}
    dev = nidaqmx.system.Device(mod)
    out["dispositivo"] = {"ai_max_single_chan_rate": dev.ai_max_single_chan_rate, "ai_max_multi_chan_rate": dev.ai_max_multi_chan_rate,
                          "ai_min_rate": dev.ai_min_rate, "ai_voltage_rngs": dev.ai_voltage_rngs}
    seccion("9201 · configuración de terminal y rangos")
    term = {}
    for tc in TerminalConfiguration:
        try:
            with nidaqmx.Task() as t:
                t.ai_channels.add_ai_voltage_chan(f"{mod}/ai0", terminal_config=tc, min_val=-10, max_val=10)
                t.read()
                term[tc.name] = "OK"
        except Exception as e:
            term[tc.name] = err(e)
    out["terminal"] = term
    rng = {}
    for mn, mx in ((-10, 10), (-1, 1), (0, 5), (-10.5, 10.5), (-20, 20)):
        try:
            with nidaqmx.Task() as t:
                ch = t.ai_channels.add_ai_voltage_chan(f"{mod}/ai0", min_val=mn, max_val=mx)
                t.control(nidaqmx.constants.TaskMode.TASK_VERIFY)
                rng[f"{mn}..{mx}"] = {"rng_real": [prop(ch, "ai_rng_low"), prop(ch, "ai_rng_high")]}
        except Exception as e:
            rng[f"{mn}..{mx}"] = err(e)
    out["rangos"] = rng
    with nidaqmx.Task() as t:
        ch = t.ai_channels.add_ai_voltage_chan(f"{mod}/ai0")
        out["por_defecto"] = {k: prop(ch, k) for k in ("ai_term_cfg", "ai_rng_low", "ai_rng_high", "ai_resolution",
                                                        "ai_coupling", "ai_lowpass_enable")}
    print(term, rng, out["por_defecto"], flush=True)

    seccion("9201 · velocidad")
    vel = []
    for n in (1, 2, 4, 8):
        f = {"canales": n}
        try:
            with nidaqmx.Task() as t:
                t.ai_channels.add_ai_voltage_chan(f"{mod}/ai0:{n - 1}")
                t.timing.cfg_samp_clk_timing(1000.0, sample_mode=AcquisitionType.FINITE, samps_per_chan=100)
                t.control(TaskMode.TASK_COMMIT)
                f["samp_clk_max_rate"] = fs = t.timing.samp_clk_max_rate
                try:
                    tm = t.timing[mod]
                except Exception:
                    tm = t.timing
                f["conv_rate_por_defecto"] = prop(tm, "ai_conv_rate")
                f["conv_max_rate"] = prop(tm, "ai_conv_max_rate")
            with nidaqmx.Task() as t:      # adquisicion finita de 0,5 s a la tasa maxima
                t.ai_channels.add_ai_voltage_chan(f"{mod}/ai0:{n - 1}")
                ns = int(fs * 0.5)
                t.timing.cfg_samp_clk_timing(fs, sample_mode=AcquisitionType.FINITE, samps_per_chan=ns)
                t0 = time.perf_counter()
                d = t.read(number_of_samples_per_channel=ns, timeout=10)
                f["finita_0_5s"] = {"muestras_por_canal": len(d[0]) if n > 1 else len(d), "pedidas": ns,
                                    "tiempo_s": round(time.perf_counter() - t0, 3)}
            with nidaqmx.Task() as t:      # bajo demanda
                t.ai_channels.add_ai_voltage_chan(f"{mod}/ai0:{n - 1}")
                t.start()
                t.read()
                t0 = time.perf_counter()
                for _ in range(200):
                    t.read()
                f["bajo_demanda_lecturas_s"] = 200 / (time.perf_counter() - t0)
            # por encima del maximo: ¿error o se ajusta?
            try:
                with nidaqmx.Task() as t:
                    t.ai_channels.add_ai_voltage_chan(f"{mod}/ai0:{n - 1}")
                    t.timing.cfg_samp_clk_timing(fs * 1.5, sample_mode=AcquisitionType.FINITE, samps_per_chan=100)
                    t.control(nidaqmx.constants.TaskMode.TASK_VERIFY)
                    f["pedir_1_5x_max"] = f"aceptado, tasa real {t.timing.samp_clk_rate}"
            except Exception as e:
                f["pedir_1_5x_max"] = err(e)
        except Exception as e:
            f["error"] = err(e)
        print(f, flush=True)
        vel.append(f)
    out["velocidad"] = vel

    seccion("9201 · entradas SIN NADA conectado: ¿se puede saber que flotan?")
    flot = {}
    try:
        with nidaqmx.Task() as t:
            t.ai_channels.add_ai_voltage_chan(f"{mod}/ai0:7")
            t.timing.cfg_samp_clk_timing(10000.0, sample_mode=AcquisitionType.FINITE, samps_per_chan=10000)
            d = t.read(number_of_samples_per_channel=10000, timeout=10)
        for i, s in enumerate(d):
            flot[f"ai{i}"] = {"media_V": round(statistics.fmean(s), 5), "std_V": round(statistics.pstdev(s), 5),
                              "min_V": round(min(s), 5), "max_V": round(max(s), 5)}
        # cada canal solo (sin los demas escaneando): si el valor cambia mucho, es una entrada flotante que arrastra
        # la carga del canal anterior (efecto «ghosting» del multiplexor)
        solo = {}
        for i in (0, 7):
            with nidaqmx.Task() as t:
                t.ai_channels.add_ai_voltage_chan(f"{mod}/ai{i}")
                t.timing.cfg_samp_clk_timing(10000.0, sample_mode=AcquisitionType.FINITE, samps_per_chan=5000)
                s = t.read(number_of_samples_per_channel=5000, timeout=10)
            solo[f"ai{i}"] = {"media_V": round(statistics.fmean(s), 5), "std_V": round(statistics.pstdev(s), 5)}
        flot["cada_canal_solo"] = solo
    except Exception as e:
        flot["error"] = err(e)
    out["entradas_al_aire"] = flot
    print(flot, flush=True)
    return out


# ------------------------------------------------------------------ chasis
def chasis(nombre, tc, tv):
    out = {}
    seccion("Chasis · dos tareas a la vez y una tarea con los dos módulos")
    try:
        with nidaqmx.Task() as a, nidaqmx.Task() as b:
            canal_tc(a, tc, 0)
            a.timing.cfg_samp_clk_timing(10.0, sample_mode=AcquisitionType.CONTINUOUS)
            b.ai_channels.add_ai_voltage_chan(f"{tv}/ai0")
            b.timing.cfg_samp_clk_timing(1000.0, sample_mode=AcquisitionType.CONTINUOUS)
            a.start(); b.start()
            na = nb = 0
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 3:
                time.sleep(0.5)
                na += len(a.read(number_of_samples_per_channel=nidaqmx.constants.READ_ALL_AVAILABLE))
                nb += len(b.read(number_of_samples_per_channel=nidaqmx.constants.READ_ALL_AVAILABLE))
            out["dos_tareas_simultaneas"] = {"segundos": 3, "9213_a_10_S_s": na, "9201_a_1000_S_s": nb}
    except Exception as e:
        out["dos_tareas_simultaneas"] = err(e)
    try:
        with nidaqmx.Task() as a, nidaqmx.Task() as b:     # dos tareas sobre el MISMO modulo
            a.ai_channels.add_ai_voltage_chan(f"{tv}/ai0")
            b.ai_channels.add_ai_voltage_chan(f"{tv}/ai1")
            a.start(); b.start()
            out["dos_tareas_mismo_modulo"] = "aceptado"
    except Exception as e:
        out["dos_tareas_mismo_modulo"] = err(e)
    try:
        with nidaqmx.Task() as t:
            canal_tc(t, tc, 0)
            t.ai_channels.add_ai_voltage_chan(f"{tv}/ai0")
            t.timing.cfg_samp_clk_timing(1.0, sample_mode=AcquisitionType.CONTINUOUS)
            t.control(TaskMode.TASK_COMMIT)
            out["una_tarea_dos_modulos"] = {"samp_clk_max_rate": t.timing.samp_clk_max_rate}
        with nidaqmx.Task() as t:
            canal_tc(t, tc, 0)
            t.ai_channels.add_ai_voltage_chan(f"{tv}/ai0")
            out["una_tarea_dos_modulos"]["lectura_bajo_demanda"] = t.read()
    except Exception as e:
        out["una_tarea_dos_modulos"] = err(e)
    try:   # disparo digital por PFI0: solo se configura y se verifica, no se arranca
        with nidaqmx.Task() as t:
            t.ai_channels.add_ai_voltage_chan(f"{tv}/ai0")
            t.timing.cfg_samp_clk_timing(1000.0, sample_mode=AcquisitionType.FINITE, samps_per_chan=100)
            t.triggers.start_trigger.cfg_dig_edge_start_trig(f"/{nombre}/PFI0")
            t.control(nidaqmx.constants.TaskMode.TASK_VERIFY)
            out["disparo_PFI0"] = "se puede configurar"
    except Exception as e:
        out["disparo_PFI0"] = err(e)
    try:
        d = nidaqmx.system.Device(nombre)
        out["chasis"] = {k: prop(d, k) for k in ("product_type", "terminals", "ci_physical_chans", "co_physical_chans",
                                                 "di_lines", "do_lines")}
    except Exception as e:
        out["chasis"] = err(e)
    print({k: v for k, v in out.items() if k != "chasis"}, flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rapido", action="store_true")
    a = ap.parse_args()
    nombre, tc, tv = modulos()
    print("chasis", nombre, "· 9213", tc, "· 9201", tv, flush=True)
    if not (tc and tv):
        sys.exit("No veo los dos módulos")
    R["equipos"] = {"chasis": nombre, "9213": tc, "9201": tv, "driver": nidaqmx.system.System.local().driver_version.__repr__()}
    for clave, fn in (("9213", lambda: m9213(tc, a.rapido)), ("9201", lambda: m9201(tv, a.rapido)), ("chasis", lambda: chasis(nombre, tc, tv))):
        try:
            R[clave] = fn()
        except nidaqmx.errors.DaqError as e:
            R[clave] = {"error_general": err(e)}
            print("ERROR", clave, err(e), flush=True)
    os.makedirs(DOCS, exist_ok=True)
    base = os.path.join(DOCS, "cdaq_capacidades_" + time.strftime("%Y%m%d_%H%M"))
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(R, f, ensure_ascii=False, indent=1, default=str)
    print("\nEscrito", base + ".json", flush=True)


if __name__ == "__main__":
    main()
