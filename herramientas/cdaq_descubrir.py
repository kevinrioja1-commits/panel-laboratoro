"""Descubrimiento del cDAQ-9178 y sus módulos (NI 9213, NI 9201) con NI-DAQmx. SOLO LECTURA.

    *** NO PROBADO CON HARDWARE *** (fase 2A: probado sin chasis y con sustitutos de nidaqmx)

Uso (con el panel cerrado o con el cDAQ suelto no hace falta: no crea tareas ni reserva nada):
    python herramientas\\cdaq_descubrir.py            -> docs\\cdaq_descubrimiento_AAAAMMDD_HHMM.json y .md
    python herramientas\\cdaq_descubrir.py --salida C:\\ruta

Qué hace: lista el driver, los dispositivos (chasis y módulos) y, de cada uno, TODAS las propiedades de dispositivo
que expone la API instalada (nidaqmx.system.Device) y las de cada canal físico (nidaqmx.system.PhysicalChannel):
rangos, configuraciones de terminal, tasas, modos de muestreo, terminales/rutas, contadores, PFI... Cada propiedad se
lee por separado: si el dispositivo no la admite se anota «no soportada» con el código de DAQmx y se sigue.
Qué NO hace: no crea tareas ni lee canales. Por eso NO puede listar lo que DAQmx solo valida al crear un canal
(tipos de termopar admitidos, modos ADC del 9213, fuentes de CJC, máximo real de muestreo con N canales): eso está
en docs\\PLAN_PRUEBAS_LABORATORIO.md.
Los ficheros de salida llevan números de serie: docs\\cdaq_descubrimiento_* está en .gitignore.
"""
import argparse
import datetime
import enum
import inspect
import json
import os
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(os.path.dirname(AQUI), "docs")
# grupos de canales físicos por dispositivo (propiedad de Device -> tipo)
GRUPOS = (("ai_physical_chans", "AI"), ("ao_physical_chans", "AO"), ("di_lines", "DI"), ("do_lines", "DO"),
          ("ci_physical_chans", "CI"), ("co_physical_chans", "CO"))


def _importar():
    import nidaqmx
    from nidaqmx.system import Device, PhysicalChannel, System
    return nidaqmx, System, Device, PhysicalChannel


def _propiedades(clase):
    return sorted(n for n, v in inspect.getmembers(clase) if isinstance(v, property) and not n.startswith("_"))


def _valor(x, prof=0):
    """Convierte lo que devuelve nidaqmx a JSON (enums por nombre, colecciones por nombres de canal)."""
    if prof > 3:
        return repr(x)
    if x is None or isinstance(x, (bool, int, float, str)):
        return x
    if isinstance(x, enum.Enum):
        return x.name
    if hasattr(x, "major_version"):                 # DriverVersion (es una tupla con nombre)
        return f"{x.major_version}.{x.minor_version}.{x.update_version}"
    if isinstance(x, (bytes, bytearray)):
        return x.hex()
    if hasattr(x, "channel_names"):
        return list(x.channel_names)
    if hasattr(x, "device_names"):
        return list(x.device_names)
    if isinstance(x, dict):
        return {str(k): _valor(v, prof + 1) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [_valor(v, prof + 1) for v in x]
    if hasattr(x, "name") and isinstance(getattr(x, "name", None), str):
        return getattr(x, "name")
    try:
        return [_valor(v, prof + 1) for v in x]
    except TypeError:
        return repr(x)


def _leer(obj, nombre):
    """{'valor': ...} o {'no_soportada': código, 'detalle': ...}; nunca lanza."""
    try:
        return {"valor": _valor(getattr(obj, nombre))}
    except Exception as e:
        codigo = getattr(e, "error_code", None)
        texto = str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__
        return {"no_soportada": codigo, "detalle": texto} if codigo is not None else \
            {"error": f"{type(e).__name__}: {texto}"}


def descubrir(importar=_importar):
    """Devuelve el inventario como dict. Sin dispositivos: {'dispositivos': []} (no es un error)."""
    nidaqmx, System, Device, PhysicalChannel = importar()
    sistema = System.local()
    inv = {"fecha": datetime.datetime.now().isoformat(timespec="seconds"),
           "probado_con_hardware": False,
           "nidaqmx_python": getattr(nidaqmx, "__version__", None) or _version_paquete(),
           "driver": _leer(sistema, "driver_version"),
           "tareas_guardadas_en_MAX": _leer(sistema, "tasks"),
           "canales_globales_en_MAX": _leer(sistema, "global_channels"),
           "escalas_en_MAX": _leer(sistema, "scales"),
           "dispositivos": []}
    props_dev, props_can = _propiedades(Device), _propiedades(PhysicalChannel)
    for d in sistema.devices:
        disp = {"nombre": d.name, "propiedades": {}, "canales": {}}
        for p in props_dev:
            disp["propiedades"][p] = _leer(d, p)
        for prop, tipo in GRUPOS:
            try:
                coleccion = getattr(d, prop)
                nombres = list(coleccion.channel_names)
            except Exception:
                continue
            disp["canales"][tipo] = {}
            for nombre in nombres:
                pc = PhysicalChannel(nombre)
                disp["canales"][tipo][nombre] = {p: _leer(pc, p) for p in props_can if _aplica(p, tipo)}
        inv["dispositivos"].append(disp)
    return inv


def _aplica(prop, tipo):
    """Solo las propiedades del tipo de canal (las demás darían «no soportada» por definición)."""
    pref = prop.split("_")[0]
    if pref in ("ai", "ao", "ci", "co", "di", "do"):
        return pref.upper() == tipo
    if pref == "dig":
        return tipo in ("DI", "DO")
    return True                                    # name, teds_*


def _version_paquete():
    try:
        from importlib.metadata import version
        return version("nidaqmx")
    except Exception:
        return None


def _v(r):
    if "valor" in r:
        return json.dumps(r["valor"], ensure_ascii=False)
    if "no_soportada" in r:
        return f"no soportada (DAQmx {r['no_soportada']})"
    return r.get("error", "?")


def a_markdown(inv):
    L = [f"# Descubrimiento del cDAQ ({inv['fecha']})", "",
         "Generado por `herramientas/cdaq_descubrir.py` (solo lectura, sin tareas). **Contiene números de serie: "
         "no se sube a git.**", "",
         f"- NI-DAQmx (driver): {_v(inv['driver'])}", f"- nidaqmx (Python): {inv['nidaqmx_python']}",
         f"- Tareas guardadas en NI MAX: {_v(inv['tareas_guardadas_en_MAX'])}",
         f"- Canales globales en NI MAX: {_v(inv['canales_globales_en_MAX'])}", ""]
    if not inv["dispositivos"]:
        L.append("**NI-DAQmx no ve ningún dispositivo.**")
    L.append("| Dispositivo | Producto | Serie | Simulado | Chasis | Ranura | Bus |")
    L.append("|---|---|---|---|---|---|---|")
    for d in inv["dispositivos"]:
        p = d["propiedades"]
        L.append(f"| {d['nombre']} | {_v(p.get('product_type', {}))} | {_v(p.get('serial_num', {}))} | "
                 f"{_v(p.get('is_simulated', {}))} | {_v(p.get('compact_daq_chassis_device', {}))} | "
                 f"{_v(p.get('compact_daq_slot_num', {}))} | {_v(p.get('bus_type', {}))} |")
    for d in inv["dispositivos"]:
        L += ["", f"## {d['nombre']}", "", "### Propiedades de dispositivo", "", "| Propiedad | Valor |", "|---|---|"]
        for k, r in d["propiedades"].items():
            L.append(f"| {k} | {_v(r).replace('|', '/')} |")
        for tipo, canales in d["canales"].items():
            if not canales:
                continue
            L += ["", f"### Canales {tipo} ({len(canales)})", ""]
            claves = sorted({k for c in canales.values() for k in c})
            L.append("| Canal | " + " | ".join(claves) + " |")
            L.append("|---" * (len(claves) + 1) + "|")
            for n, c in canales.items():
                L.append(f"| {n} | " + " | ".join(_v(c[k]).replace("|", "/") if k in c else "" for k in claves) + " |")
    L += ["", "## Lo que esta herramienta NO puede saber sin crear tareas", "",
          "Tipos de termopar admitidos (¿C y A en el 9213?), modos ADC del 9213, fuentes de CJC, valores por defecto "
          "de autozero y la tasa máxima real con N canales: ver docs/PLAN_PRUEBAS_LABORATORIO.md.", ""]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--salida", default=DOCS, help="carpeta de salida (por defecto docs del repo)")
    a = ap.parse_args(argv)
    try:
        inv = descubrir()
    except ImportError as e:
        print(f"No se puede importar nidaqmx: {e}. Instálalo según requirements.txt.")
        return 2
    except Exception as e:
        print(f"Error al consultar NI-DAQmx: {type(e).__name__}: {e}")
        return 3
    print(f"NI-DAQmx {_v(inv['driver'])}; nidaqmx {inv['nidaqmx_python']}")
    if not inv["dispositivos"]:
        print("NI-DAQmx no ve ningún dispositivo (¿cDAQ conectado por USB y alimentado?). No se escribe nada.")
        return 0
    os.makedirs(a.salida, exist_ok=True)
    base = os.path.join(a.salida, "cdaq_descubrimiento_" + datetime.datetime.now().strftime("%Y%m%d_%H%M"))
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(inv, f, ensure_ascii=False, indent=1)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(a_markdown(inv))
    for d in inv["dispositivos"]:
        p = d["propiedades"]
        print(f"  {d['nombre']}: {_v(p.get('product_type', {}))}  "
              + "  ".join(f"{t}={len(c)}" for t, c in d["canales"].items() if c))
    print(f"Escrito {base}.json y .md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
