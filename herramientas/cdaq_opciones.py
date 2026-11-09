r"""Qué configuraciones ADMITE de verdad el driver para el NI 9213 y el NI 9201 en este cDAQ-9178 (9-nov-2026).

Para cada opción y cada valor posible: crea una tarea TEMPORAL, aplica el valor y la reserva en el hardware
(TASK_COMMIT; en las de canal además arranca y lee una muestra). Apunta «admitido» o el código de error de DAQmx.
No guarda nada en NI MAX ni saca señales. El panel tiene que haber SOLTADO el cDAQ.

    python herramientas\cdaq_opciones.py

Escribe docs\cdaq_opciones_<fecha>.json y .md
"""
import datetime as dt
import json
import os
import tempfile
import time

import nidaqmx
import nidaqmx.system
from nidaqmx import constants as C
from nidaqmx.scale import Scale

TC, TV, CH = "cDAQ1Mod5", "cDAQ1Mod6", "cDAQ1"
AQUI = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(os.path.dirname(AQUI), "docs")
FILAS = []          # (modulo, grupo, opcion, valor, resultado, como_se_programa)


def err(e):
    c = getattr(e, "error_code", "")
    return f"rechazado [{c}] " + str(e).splitlines()[0][:150]


def nuevo(modulo, n=1):
    t = nidaqmx.Task()
    if modulo == "9213":
        chs = [t.ai_channels.add_ai_thrmcpl_chan(f"{TC}/ai{i}", min_val=0, max_val=100, thermocouple_type=C.ThermocoupleType.K,
                                                 cjc_source=C.CJCSource.BUILT_IN) for i in range(n)]
    else:
        chs = [t.ai_channels.add_ai_voltage_chan(f"{TV}/ai{i}") for i in range(n)]
    return t, chs


def probar(modulo, grupo, opcion, valor, como, aplicar, leer=True, tras=None):
    """aplicar(task, canales) configura; luego COMMIT (y si leer, start + read de una muestra)."""
    try:
        t, chs = nuevo(modulo)
        with t:
            extra = aplicar(t, chs)
            t.control(C.TaskMode.TASK_COMMIT)
            res = "admitido"
            if tras:
                extra = tras(t, chs)
                leer = False
            if leer:
                if t.timing.samp_timing_type == C.SampleTimingType.ON_DEMAND:
                    v = t.read()
                else:
                    t.start()
                    v = t.read(number_of_samples_per_channel=1, timeout=10)
                    v = v[0] if isinstance(v, list) and v else v
                res += f" · lee {v:.4g}" if isinstance(v, float) else ""
            if extra:
                res += " · " + str(extra)
    except Exception as e:
        res = err(e)
    FILAS.append((modulo, grupo, opcion, str(valor), res, como))
    print(f"{modulo:5} {grupo:12} {opcion:28} {str(valor):30} {res}", flush=True)


def enum(modulo, grupo, prop, clase, como=None):
    for v in clase:
        probar(modulo, grupo, prop, v.name, como or f"canal.{prop} = {clase.__name__}.{v.name}",
               lambda t, chs, v=v: [setattr(c, prop, v) for c in chs] and None)


def canal(modulo):
    g = "canal"
    enum(modulo, g, "ai_adc_timing_mode", C.ADCTimingMode)
    enum(modulo, g, "ai_auto_zero_mode", C.AutoZeroType)
    enum(modulo, g, "ai_coupling", C.Coupling)
    enum(modulo, g, "ai_term_cfg", C.TerminalConfiguration)
    for b in (True, False):
        probar(modulo, g, "ai_lowpass_enable", b, f"canal.ai_lowpass_enable = {b}", lambda t, chs, b=b: [setattr(c, "ai_lowpass_enable", b) for c in chs] and None)
        probar(modulo, g, "ai_dither_enable", b, f"canal.ai_dither_enable = {b}", lambda t, chs, b=b: [setattr(c, "ai_dither_enable", b) for c in chs] and None)
    enum(modulo, g, "ai_data_xfer_mech", C.DataTransferActiveTransferMode)
    enum(modulo, g, "ai_raw_data_compression_type", C.RawDataCompressionType)
    # escala propia (p. ej. sonda 10:1 o termopar con corrección): escala lineal y = 10 x
    nombre = f"esc_prueba_{modulo}_{int(time.time())}"

    def con_escala(t, chs):
        Scale.create_lin_scale(nombre, slope=10.0, y_intercept=0.0,
                               pre_scaled_units=C.UnitsPreScaled.VOLTS if modulo == "9201" else C.UnitsPreScaled.DEG_C,
                               scaled_units="x10")
        for c in chs:
            c.ai_custom_scale = Scale(nombre)
            if modulo == "9201":
                c.ai_voltage_units = C.VoltageUnits.FROM_CUSTOM_SCALE
                c.ai_min, c.ai_max = -100.0, 100.0
            else:
                c.ai_temp_units = C.TemperatureUnits.FROM_CUSTOM_SCALE
                c.ai_min, c.ai_max = 0.0, 1000.0
    probar(modulo, g, "escala propia (lineal ×10)", "FROM_CUSTOM_SCALE",
           "Scale.create_lin_scale(...) + canal.ai_custom_scale + unidades FROM_CUSTOM_SCALE (escala solo en memoria)", con_escala)


def solo_9213():
    g = "termopar"
    enum("9213", g, "ai_temp_units", C.TemperatureUnits)
    for v in (C.CJCSource.BUILT_IN, C.CJCSource.CONSTANT_USER_VALUE, C.CJCSource.SCANNABLE_CHANNEL):
        def cjc(t, chs, v=v):     # la fuente de CJC se elige al CREAR el canal (la propiedad no se puede cambiar luego)
            c = t.ai_channels.add_ai_thrmcpl_chan(f"{TC}/ai1", min_val=0, max_val=100, thermocouple_type=C.ThermocoupleType.K,
                                                  cjc_source=v, cjc_val=25.0,
                                                  cjc_channel=f"{TC}/_cjcTemp0" if v == C.CJCSource.SCANNABLE_CHANNEL else "")
            return f"fuente {c.ai_thrmcpl_cjc_src.name}"
        probar("9213", g, "fuente de la unión fría (CJC)", v.name, f"add_ai_thrmcpl_chan(cjc_source=CJCSource.{v.name}, cjc_val=...)", cjc)
    for v in (C.ScaleType.POLYNOMIAL, C.ScaleType.TABLE):
        probar("9213", g, "ai_thrmcpl_scale_type", v.name, f"canal.ai_thrmcpl_scale_type = ScaleType.{v.name}",
               lambda t, chs, v=v: [setattr(c, "ai_thrmcpl_scale_type", v) for c in chs] and None)
    # con escala TABLA, ¿se amplía el rango del tipo B (polinomio: 250..1820 °C)?
    def b_tabla(t, chs):
        for c in chs:
            c.ai_thrmcpl_type = C.ThermocoupleType.B
            c.ai_thrmcpl_scale_type = C.ScaleType.TABLE
            c.ai_min, c.ai_max = 0.0, 1820.0
    probar("9213", g, "tipo B con escala TABLE", "0..1820 °C", "ai_thrmcpl_type = B; ai_thrmcpl_scale_type = TABLE", b_tabla, leer=False)
    probar("9213", g, "ai_thrmcpl_lead_offset_voltage", "0,000010 V", "canal.ai_thrmcpl_lead_offset_voltage = 10e-6",
           lambda t, chs: [setattr(c, "ai_thrmcpl_lead_offset_voltage", 10e-6) for c in chs] and None)


def solo_9201():
    g = "tensión"
    probar("9201", g, "ai_min/ai_max", "-10..10", "canal.ai_min/ai_max (el módulo solo tiene ±10 V)",
           lambda t, chs: [setattr(c, "ai_min", -10.0) or setattr(c, "ai_max", 10.0) for c in chs] and f"rango real {chs[0].ai_rng_low}..{chs[0].ai_rng_high}")


def temporizacion(modulo):
    g = "temporización"
    rapido = 10.0 if modulo == "9213" else 1000.0
    probar(modulo, g, "bajo demanda (sin reloj)", "ON_DEMAND", "sin timing.cfg_samp_clk_timing; task.read() cuando se quiera",
           lambda t, chs: None)
    for m in C.AcquisitionType:
        probar(modulo, g, "reloj de muestreo", m.name, f"timing.cfg_samp_clk_timing(fs, sample_mode=AcquisitionType.{m.name})",
               lambda t, chs, m=m: t.timing.cfg_samp_clk_timing(rapido, sample_mode=m, samps_per_chan=100) or None)
    for fuente in ("/cDAQ1/PFI0", "/cDAQ1/PFI1"):
        probar(modulo, g, "reloj EXTERNO", fuente, f"cfg_samp_clk_timing(fs, source='{fuente}')",
               lambda t, chs, f=fuente: t.timing.cfg_samp_clk_timing(rapido, source=f, sample_mode=C.AcquisitionType.FINITE, samps_per_chan=100) or None,
               leer=False)
    probar(modulo, g, "tasa mínima del módulo", "", "Device.ai_min_rate", lambda t, chs: f"{nidaqmx.system.Device(TC if modulo == '9213' else TV).ai_min_rate:.3g} S/s", leer=False)
    if modulo == "9201":
        def conv(t, chs):
            t.timing.cfg_samp_clk_timing(1000.0, sample_mode=C.AcquisitionType.FINITE, samps_per_chan=100)
            tm = t.timing[TV]
            tm.ai_conv_rate = 500000.0
            return f"conversión fijada a {tm.ai_conv_rate:.0f} Hz (máx {tm.ai_conv_max_rate:.0f})"
        probar(modulo, g, "velocidad de conversión", "500 kHz", "task.timing['cDAQ1Mod6'].ai_conv_rate = ...", conv, leer=False)


def disparos(modulo):
    g = "disparo"
    fs = 10.0 if modulo == "9213" else 1000.0

    def base(t):
        t.timing.cfg_samp_clk_timing(fs, sample_mode=C.AcquisitionType.FINITE, samps_per_chan=100)
    for e in (C.Edge.RISING, C.Edge.FALLING):
        probar(modulo, g, "inicio · digital por PFI0", e.name, "triggers.start_trigger.cfg_dig_edge_start_trig('/cDAQ1/PFI0', trigger_edge)",
               lambda t, chs, e=e: base(t) or t.triggers.start_trigger.cfg_dig_edge_start_trig("/cDAQ1/PFI0", trigger_edge=e) or None, leer=False)
    fuente_a = f"{TV}/ai0" if modulo == "9201" else f"{TC}/ai0"
    probar(modulo, g, "inicio · analógico (flanco)", f"{fuente_a} sube 1,0", "start_trigger.cfg_anlg_edge_start_trig(fuente, Slope.RISING, nivel)",
           lambda t, chs: base(t) or t.triggers.start_trigger.cfg_anlg_edge_start_trig(fuente_a, trigger_slope=C.Slope.RISING, trigger_level=1.0) or None, leer=False)
    probar(modulo, g, "inicio · analógico (ventana)", "entrar en 0..2", "start_trigger.cfg_anlg_window_start_trig(fuente, top, bottom)",
           lambda t, chs: base(t) or t.triggers.start_trigger.cfg_anlg_window_start_trig(2.0, 0.0, trigger_source=fuente_a) or None, leer=False)
    probar(modulo, g, "referencia (pre-disparo) digital", "PFI0, 20 muestras antes", "reference_trigger.cfg_dig_edge_ref_trig('/cDAQ1/PFI0', pretrigger_samples)",
           lambda t, chs: base(t) or t.triggers.reference_trigger.cfg_dig_edge_ref_trig("/cDAQ1/PFI0", pretrigger_samples=20) or None, leer=False)
    probar(modulo, g, "referencia (pre-disparo) analógica", f"{fuente_a} sube 1,0", "reference_trigger.cfg_anlg_edge_ref_trig(fuente, pretrigger_samples, ...)",
           lambda t, chs: base(t) or t.triggers.reference_trigger.cfg_anlg_edge_ref_trig(fuente_a, pretrigger_samples=20, trigger_level=1.0) or None, leer=False)

    def pausa(t, chs):
        t.timing.cfg_samp_clk_timing(fs, sample_mode=C.AcquisitionType.CONTINUOUS)
        pt = t.triggers.pause_trigger
        pt.trig_type = C.TriggerType.DIGITAL_LEVEL
        pt.dig_lvl_src = "/cDAQ1/PFI0"
        pt.dig_lvl_when = C.Level.HIGH
    probar(modulo, g, "pausa · digital (nivel)", "PFI0 alto", "pause_trigger.trig_type = DIGITAL_LEVEL; dig_lvl_src; dig_lvl_when", pausa, leer=False)


def lectura(modulo):
    g = "lectura/registro"
    fs = 10.0 if modulo == "9213" else 1000.0
    probar(modulo, g, "búfer de entrada", "10000 muestras", "in_stream.input_buf_size = 10000",
           lambda t, chs: t.timing.cfg_samp_clk_timing(fs, sample_mode=C.AcquisitionType.CONTINUOUS) or setattr(t.in_stream, "input_buf_size", 10000) or None)
    probar(modulo, g, "sobrescribir lo no leído", "OVERWRITE_UNREAD_SAMPLES", "in_stream.over_write = OverwriteMode.OVERWRITE_UNREAD_SAMPLES",
           lambda t, chs: t.timing.cfg_samp_clk_timing(fs, sample_mode=C.AcquisitionType.CONTINUOUS) or setattr(t.in_stream, "over_write", C.OverwriteMode.OVERWRITE_UNREAD_SAMPLES) or None)
    def reciente(t, chs):
        t.start()
        time.sleep(0.6)
        v = t.read(number_of_samples_per_channel=1)
        return f"última muestra {v[0]:.4g}"
    probar(modulo, g, "leer lo más reciente", "MOST_RECENT_SAMPLE", "in_stream.relative_to = ReadRelativeTo.MOST_RECENT_SAMPLE; in_stream.offset = 0 (con -1 da -200277)",
           lambda t, chs: t.timing.cfg_samp_clk_timing(fs, sample_mode=C.AcquisitionType.CONTINUOUS) or setattr(t.in_stream, "relative_to", C.ReadRelativeTo.MOST_RECENT_SAMPLE) or setattr(t.in_stream, "offset", 0) or None,
           tras=reciente)

    def crudo_tras(t, chs):
        import numpy as np
        from nidaqmx.stream_readers import AnalogUnscaledReader
        bits = chs[0].ai_raw_samp_size
        buf = np.zeros((1, 10), dtype=np.int16 if bits == 16 else np.int32)
        t.start()
        r = AnalogUnscaledReader(t.in_stream)
        (r.read_int16 if bits == 16 else r.read_int32)(buf, number_of_samples_per_channel=10, timeout=15)
        coef = [round(x, 6) for x in chs[0].ai_dev_scaling_coeff] if modulo == "9201" else "(9213: lineariza el driver)"
        return f"enteros de {bits} bits, p. ej. {buf[0][:3].tolist()}; coef. de escala {coef}"
    probar(modulo, g, "lectura CRUDA (sin escalar)", "AnalogUnscaledReader", "stream_readers.AnalogUnscaledReader(task.in_stream).read_int16/int32 (enteros del ADC; a voltios con ai_dev_scaling_coeff)",
           lambda t, chs: t.timing.cfg_samp_clk_timing(fs, sample_mode=C.AcquisitionType.FINITE, samps_per_chan=10) or None, tras=crudo_tras)
    tdms = os.path.join(tempfile.gettempdir(), f"cdaq_prueba_{modulo}.tdms")
    for m in (C.LoggingMode.LOG, C.LoggingMode.LOG_AND_READ):
        def reg(t, chs, m=m):
            t.timing.cfg_samp_clk_timing(fs, sample_mode=C.AcquisitionType.FINITE, samps_per_chan=20)
            t.in_stream.configure_logging(tdms, logging_mode=m, operation=C.LoggingOperation.CREATE_OR_REPLACE)

        def reg_tras(t, chs, m=m):
            t.start()
            if m == C.LoggingMode.LOG_AND_READ:
                t.read(number_of_samples_per_channel=20, timeout=15)
            else:
                t.wait_until_done(timeout=15)
            return f"TDMS escrito ({os.path.getsize(tdms) if os.path.exists(tdms) else 0} bytes)"
        probar(modulo, g, "registro a disco del DRIVER (TDMS)", m.name, f"in_stream.configure_logging(ruta, LoggingMode.{m.name})", reg, tras=reg_tras)
        try:
            os.remove(tdms)
        except OSError:
            pass


def chasis():
    g = "chasis"
    d = nidaqmx.system.Device(CH)
    for nombre in ("ci_physical_chans", "co_physical_chans", "di_lines", "do_lines", "terminals"):
        try:
            v = getattr(d, nombre)
            v = [x.name if hasattr(x, "name") else x for x in v]
        except Exception as e:
            v = err(e)
        FILAS.append(("chasis", g, nombre, "", str(v)[:300], f"Device('{CH}').{nombre}"))
    # contadores internos del chasis (ocultos: _ctr0.._ctr3), p. ej. contar flancos en PFI0
    for ctr in ("_ctr0", "_ctr3"):
        try:
            with nidaqmx.Task() as t:
                c = t.ci_channels.add_ci_count_edges_chan(f"{CH}/{ctr}")
                c.ci_count_edges_term = f"/{CH}/PFI0"
                t.control(C.TaskMode.TASK_COMMIT)
                res = "admitido"
        except Exception as e:
            res = err(e)
        FILAS.append(("chasis", g, f"contador interno {ctr}", "contar flancos en PFI0", res, f"ci_channels.add_ci_count_edges_chan('{CH}/{ctr}')"))
    for fila in FILAS[-7:]:
        print(fila, flush=True)


def escribir():
    os.makedirs(DOCS, exist_ok=True)
    base = os.path.join(DOCS, "cdaq_opciones_" + time.strftime("%Y%m%d_%H%M"))
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump({"fecha": dt.datetime.now().isoformat(timespec="seconds"), "filas": FILAS}, f, ensure_ascii=False, indent=1)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(f"# Opciones que admite el driver (NI-DAQmx {nidaqmx.system.System.local().driver_version}) · {dt.date.today()}\n\n")
        f.write("Cada fila: tarea temporal con esa opción, reservada en el hardware (COMMIT) y, si aplica, arrancada y leída.\n\n")
        f.write("| Módulo | Grupo | Opción | Valor | Resultado | Cómo se programa (nidaqmx) |\n|---|---|---|---|---|---|\n")
        for m, g, o, v, r, c in FILAS:
            f.write(f"| {m} | {g} | {o} | {v} | {r} | `{c}` |\n")
    print("\nEscrito", base + ".md")


if __name__ == "__main__":
    for mod, extra in (("9213", solo_9213), ("9201", solo_9201)):
        canal(mod)
        extra()
        temporizacion(mod)
        disparos(mod)
        lectura(mod)
    chasis()
    escribir()
