r"""Modelo de opciones del cDAQ-9178 con NI 9213 y NI 9201, y su traducción a NI-DAQmx (9-nov-2026).

CANDIDATO para el panel (aún no conectado a cdaq.py). Cada opción del panel tiene aquí:
  - sus valores posibles (lo que el panel enseñará), con la clave del panel y el valor exacto de NI-DAQmx;
  - la propiedad o la función del driver que la aplica (la traducción 1:1);
  - de dónde sale: driver (sonda herramientas\cdaq_opciones.py) y documentación de NI.
Y las reglas que el DRIVER NO VIGILA y el panel tiene que vigilar, sobre todo la velocidad máxima del 9213: si se
pide más, DAQmx lo acepta y el módulo repite muestras sin avisar (manual del cDAQ, «slow sample rate modules»).

Validado contra el hardware con herramientas\cdaq_validar_modelo.py.
"""

# ------------------------------------------------------------------ opciones comunes a los dos módulos
MUESTREO = {   # cómo se toman las muestras
    "bajo_demanda": None,                 # sin reloj: task.read() cuando el panel quiere (lo que hace hoy el panel)
    "continuo": "CONTINUOUS",             # timing.cfg_samp_clk_timing(fs, sample_mode=CONTINUOUS)
    "finito": "FINITE",                   # timing.cfg_samp_clk_timing(fs, sample_mode=FINITE, samps_per_chan=N)
}
RELOJ = {"interno": None, "PFI0": "/{chasis}/PFI0", "PFI1": "/{chasis}/PFI1"}     # cfg_samp_clk_timing(source=...)
FLANCO = {"subida": "RISING", "bajada": "FALLING"}
NIVEL = {"alto": "HIGH", "bajo": "LOW"}
DISPARO = {    # solo DIGITAL: ni el 9213 ni el 9201 generan disparo analógico (DAQmx -200265; manual del cDAQ)
    "ninguno": None,
    "inicio": "start_trigger.cfg_dig_edge_start_trig(fuente, trigger_edge)",
    "referencia": "reference_trigger.cfg_dig_edge_ref_trig(fuente, pretrigger_samples, trigger_edge)",
    "pausa": "pause_trigger: trig_type=DIGITAL_LEVEL, dig_lvl_src, dig_lvl_when",
}
FUENTE_DISPARO = {"PFI0": "/{chasis}/PFI0", "PFI1": "/{chasis}/PFI1"}
REGISTRO_DRIVER = {"no": "OFF", "registrar": "LOG", "registrar_y_leer": "LOG_AND_READ"}   # in_stream.configure_logging (TDMS)
LECTURA = {"escalada": "task.read", "cruda": "AnalogUnscaledReader.read_int16/int32"}

# ------------------------------------------------------------------ NI 9213 (termopares)
TC_MODO_ADC = {"alta_resolucion": "HIGH_RESOLUTION", "alta_velocidad": "HIGH_SPEED", "automatico": "AUTOMATIC"}
TC_T_CONV_S = {"HIGH_RESOLUTION": 0.055, "HIGH_SPEED": 0.000740,     # especificaciones del 9213, tabla 3
               "AUTOMATIC": 0.055}    # «automático» eligió alta resolución (medido el 9-nov): el máximo prudente
TC_TOPE_S_S = 100.0                                                 # «o 100 S/s, lo que sea menor»
TC_AUTOZERO = {"cada_muestra": "EVERY_SAMPLE", "una_vez": "ONCE", "no": "NONE"}
TC_CJC = {"interna": "BUILT_IN", "constante": "CONSTANT_USER_VALUE"}   # se elige al CREAR el canal
TC_ESCALA = {"polinomio": "POLYNOMIAL", "tabla": "TABLE"}
TC_UNIDADES = {"C": "DEG_C", "F": "DEG_F"}       # K y °R los acepta pero leen mal (medido): no se ofrecen
TC_TIPOS = {   # rango que admite el driver con escala polinomio (sonda del 9-nov); con TABLA el B baja a 0 °C
    "J": (-210, 1200), "K": (-200, 1372), "N": (-200, 1300), "R": (-50, 1768), "S": (-50, 1768),
    "T": (-200, 400), "B": (250, 1820), "E": (-200, 1000), "A": (100, 2480), "C": (0, 2315)}
TC_TIPOS_TABLA = {"B": (0, 1820)}
TC_CANALES = 16

# ------------------------------------------------------------------ NI 9201 (tensión)
V_RANGO = (-10.0, 10.0)                           # fijo; ai_min/ai_max no lo cambian
V_TERMINAL = "RSE"                                # el único
V_MAX_1CH = 800_000.0                             # especificaciones del 9201: un canal
V_MAX_AGREGADO = 500_000.0                        # escaneando: repartido entre los canales
V_CANALES = 8
V_BITS = 12


def tasa_max_9213(n_canales, modo="alta_resolucion", autozero="cada_muestra"):
    """Muestras por segundo y canal: 1 / (t_conv × (n + canales internos)), con tope de 100 S/s.
    Canales internos: unión fría siempre; autocero solo con «cada_muestra» (lo que mida el validador lo confirma)."""
    t = TC_T_CONV_S[TC_MODO_ADC[modo] if modo in TC_MODO_ADC else modo]
    internos = 2 if autozero == "cada_muestra" else 1
    return min(TC_TOPE_S_S, 1.0 / (t * (n_canales + internos)))


def tasa_max_9201(n_canales):
    return V_MAX_1CH if n_canales == 1 else V_MAX_AGREGADO / n_canales


def periodo_min_s(modulo, n, **kw):
    return 1.0 / (tasa_max_9213(n, **kw) if modulo == "9213" else tasa_max_9201(n))


TASA_AUTO_MARGEN = 0.98       # tasa vacía en el 9213 = la máxima × 0,98 (en el límite exacto podría repetir alguna)
TASA_AUTO_9201 = 1000.0       # tasa vacía en el 9201 = 1 kS/s (la máxima, 800 kS/s, solo si se pide)


def tasa_auto(modulo, n, modo="alta_resolucion", autozero="cada_muestra"):
    """Tasa con la casilla vacía (9-nov, pedido del usuario: continuo por defecto)."""
    if modulo == "9213":
        return tasa_max_9213(n, modo, autozero) * TASA_AUTO_MARGEN
    return min(TASA_AUTO_9201, tasa_max_9201(n))


def comprobar(modulo, cfg):
    """Lo que el DRIVER no vigila y el panel sí: devuelve la lista de errores (vacía = se puede)."""
    e = []
    canales = [c for c in cfg.get("canales", []) if c.get("activo", True)]
    n = len(canales)
    if not n:
        e.append("ningún canal activo")
        return e
    fs = cfg.get("tasa_S_s")
    if cfg.get("muestreo", "bajo_demanda") != "bajo_demanda":
        mx = tasa_max_9213(n, cfg.get("modo_adc", "alta_resolucion"), cfg.get("autozero", "cada_muestra")) if modulo == "9213" else tasa_max_9201(n)
        if not fs or fs <= 0:
            e.append("falta la tasa de muestreo")
        elif fs > mx * 1.0001:
            e.append(f"{fs:g} S/s por encima del máximo con {n} canales ({mx:.4g} S/s)"
                     + (": el 9213 repetiría muestras sin avisar" if modulo == "9213" else ""))
    if modulo == "9213":
        for c in canales:
            lo, hi = (TC_TIPOS_TABLA if cfg.get("escala") == "tabla" and c["tipo"] in TC_TIPOS_TABLA else TC_TIPOS)[c["tipo"]]
            if not (lo <= c.get("min_C", 0) < c.get("max_C", 100) <= hi):
                e.append(f"{c['nombre']}: rango {c.get('min_C')}..{c.get('max_C')} °C fuera del tipo {c['tipo']} ({lo}..{hi})")
    if cfg.get("disparo", "ninguno") != "ninguno" and cfg.get("muestreo", "bajo_demanda") == "bajo_demanda":
        e.append("un disparo necesita muestreo con reloj (continuo o finito)")
    if cfg.get("disparo") == "referencia" and cfg.get("muestreo") != "finito":
        e.append("el disparo de referencia solo va con muestreo finito")
    return e


# ------------------------------------------------------------------ traducción a NI-DAQmx
def crear_tarea(nidaqmx, modulo, dispositivo, chasis, cfg):
    """cfg del panel -> tarea de NI-DAQmx SIN arrancar. Una tarea por módulo (un módulo no se reparte entre tareas)."""
    K = nidaqmx.constants
    t = nidaqmx.Task()
    try:
        for c in [c for c in cfg["canales"] if c.get("activo", True)]:
            fis = f"{dispositivo}/ai{c['canal']}"
            if modulo == "9213":
                cjc = TC_CJC[cfg.get("cjc", "interna")]
                # el rango se guarda en °C; con unidades °F el driver lo espera en °F
                a_u = (lambda x: x * 1.8 + 32.0) if cfg.get("unidades", "C") == "F" else (lambda x: x)
                ch = t.ai_channels.add_ai_thrmcpl_chan(
                    fis, name_to_assign_to_channel=c.get("nombre", ""), min_val=a_u(c.get("min_C", 0.0)),
                    max_val=a_u(c.get("max_C", 100.0)),
                    units=K.TemperatureUnits[TC_UNIDADES[cfg.get("unidades", "C")]],
                    thermocouple_type=K.ThermocoupleType[c.get("tipo", "K")],
                    cjc_source=K.CJCSource[cjc], cjc_val=a_u(cfg.get("cjc_valor_C", 25.0)))   # también en °F (9-nov)
                ch.ai_thrmcpl_scale_type = K.ScaleType[TC_ESCALA[cfg.get("escala", "polinomio")]]
                ch.ai_adc_timing_mode = K.ADCTimingMode[TC_MODO_ADC[cfg.get("modo_adc", "alta_resolucion")]]
                ch.ai_auto_zero_mode = K.AutoZeroType[TC_AUTOZERO[cfg.get("autozero", "cada_muestra")]]
                if c.get("desfase_cables_V"):
                    ch.ai_thrmcpl_lead_offset_voltage = float(c["desfase_cables_V"])
            else:
                esc = c.get("escala")          # {"pendiente": 10, "ordenada": 0, "unidad": "V"}: p. ej. una sonda 10:1
                if esc:
                    from nidaqmx.scale import Scale
                    nombre = f"panel_{dispositivo}_ai{c['canal']}"
                    Scale.create_lin_scale(nombre, slope=float(esc["pendiente"]), y_intercept=float(esc.get("ordenada", 0.0)),
                                           pre_scaled_units=K.UnitsPreScaled.VOLTS, scaled_units=esc.get("unidad", "V"))
                    a, b = (float(esc["pendiente"]) * x + float(esc.get("ordenada", 0.0)) for x in V_RANGO)
                    ch = t.ai_channels.add_ai_voltage_chan(fis, name_to_assign_to_channel=c.get("nombre", ""),
                                                           terminal_config=K.TerminalConfiguration.RSE, min_val=min(a, b),
                                                           max_val=max(a, b), units=K.VoltageUnits.FROM_CUSTOM_SCALE,
                                                           custom_scale_name=nombre)
                else:
                    ch = t.ai_channels.add_ai_voltage_chan(fis, name_to_assign_to_channel=c.get("nombre", ""),
                                                           terminal_config=K.TerminalConfiguration.RSE, min_val=V_RANGO[0], max_val=V_RANGO[1])
        m = MUESTREO[cfg.get("muestreo", "bajo_demanda")]
        if m:
            fuente = RELOJ[cfg.get("reloj", "interno")]
            kw = {"source": fuente.format(chasis=chasis)} if fuente else {}
            t.timing.cfg_samp_clk_timing(float(cfg["tasa_S_s"]), sample_mode=K.AcquisitionType[m],
                                         samps_per_chan=int(cfg.get("muestras", 1000)), **kw)
            if modulo == "9201" and cfg.get("conversion_Hz"):
                t.timing[dispositivo].ai_conv_rate = float(cfg["conversion_Hz"])
            if cfg.get("bufer_muestras"):
                t.in_stream.input_buf_size = int(cfg["bufer_muestras"])
            if cfg.get("sobrescribir"):
                t.in_stream.over_write = K.OverwriteMode.OVERWRITE_UNREAD_SAMPLES
        d = cfg.get("disparo", "ninguno")
        if d != "ninguno":
            src = FUENTE_DISPARO[cfg.get("disparo_fuente", "PFI0")].format(chasis=chasis)
            if d == "inicio":
                t.triggers.start_trigger.cfg_dig_edge_start_trig(src, trigger_edge=K.Edge[FLANCO[cfg.get("disparo_flanco", "subida")]])
            elif d == "referencia":
                t.triggers.reference_trigger.cfg_dig_edge_ref_trig(src, pretrigger_samples=int(cfg.get("pre_muestras", 100)),
                                                                   trigger_edge=K.Edge[FLANCO[cfg.get("disparo_flanco", "subida")]])
            elif d == "pausa":
                p = t.triggers.pause_trigger
                p.trig_type = K.TriggerType.DIGITAL_LEVEL
                p.dig_lvl_src = src
                p.dig_lvl_when = K.Level[NIVEL[cfg.get("pausa_cuando", "alto")]]
        r = REGISTRO_DRIVER[cfg.get("registro_driver", "no")]
        if r != "OFF":
            t.in_stream.configure_logging(cfg["registro_ruta"], logging_mode=K.LoggingMode[r],
                                          operation=K.LoggingOperation.CREATE_OR_REPLACE)
        return t
    except Exception:
        t.close()
        raise
