"""Donde se guardan capturas, registros (datalogger) y ensayos: C:\\Proyectos\\Osciloscopio\\<proyecto>\\<tipo>\\...

Organizacion acordada con el usuario (propuesta v3, 2026-10-29):
    <proyecto>\\capturas\\[subcarpetas]\\AAAAMMDD_HHMM_nombre.h5 · .png · .json · .set
    <proyecto>\\registros\\[subcarpetas]\\AAAAMMDD_HHMM_nombre\\osc.h5 · registro.json
    <proyecto>\\ensayos\\AAAAMMDD_HHMM_nombre\\...   (fase 4)
La carpeta compartida esta montada en el osciloscopio como /home/storage/userData/NetworkStorage/.
"""
import os
import re
import time

BASE = r"C:\Proyectos\Osciloscopio"
BASE_OSC = "/home/storage/userData/NetworkStorage"
TIPOS = ("capturas", "registros", "ensayos", "tramos")     # tramos: ✂ Tramo de En vivo (2026-11-04)
NO_PROYECTOS = {"Contexto Claude", "_pruebas_panel"}


def _limpio(nombre, que="nombre"):
    """Nombre de carpeta o fichero seguro: letras, numeros, espacio, - _ . ( ); sin '..' ni separadores."""
    n = re.sub(r"\s+", " ", str(nombre or "")).strip()
    if not n or len(n) > 80 or not re.fullmatch(r"[\w\-. ()áéíóúÁÉÍÓÚñÑüÜ]+", n) or n.startswith(".") or ".." in n:
        etq = "Nombre" if que == "nombre" else f"Nombre de {que}"
        raise ValueError(f"{etq} no válido: usa letras, números, espacios y - _ . ( ), hasta 80 caracteres")
    return n


def _sub(sub):
    """Subcarpeta relativa (p. ej. 'puente_completo\\carga2'): cada parte limpia; '' = raiz del tipo."""
    partes = [p for p in re.split(r"[\\/]+", sub or "") if p]
    return [_limpio(p, "carpeta") for p in partes]


def proyectos():
    out = []
    for d in sorted(os.listdir(BASE)):
        if os.path.isdir(os.path.join(BASE, d)) and d not in NO_PROYECTOS and not d.startswith((".", "_")):
            out.append(d)
    return out


def _dir_tipo(proyecto, tipo):
    if tipo not in TIPOS:
        raise ValueError(f"Tipo no válido: {tipo}")
    p = _limpio(proyecto, "proyecto")
    if p not in proyectos():
        raise ValueError(f"No existe el proyecto {p}")
    return os.path.join(BASE, p, tipo)


def carpetas(proyecto, tipo):
    """Subcarpetas (hasta 3 niveles) de <proyecto>\\<tipo>. Las de registros/ensayos con fecha (son un registro o
    un ensayo, no una carpeta para ordenar) no se listan."""
    raiz = _dir_tipo(proyecto, tipo)
    out = [""]
    if not os.path.isdir(raiz):
        return out
    for dirpath, dirnames, _ in os.walk(raiz):
        rel = os.path.relpath(dirpath, raiz)
        nivel = 0 if rel == "." else rel.count(os.sep) + 1
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "_")) and not re.match(r"\d{8}_\d{4}_", d))
        if rel != ".":
            out.append(rel)
        if nivel >= 3:
            dirnames[:] = []
    return out


def crear_carpeta(proyecto, tipo, sub):
    partes = _sub(sub)
    if not partes:
        raise ValueError("Escribe el nombre de la carpeta nueva")
    ruta = os.path.join(_dir_tipo(proyecto, tipo), *partes)
    os.makedirs(ruta, exist_ok=True)
    return os.path.relpath(ruta, _dir_tipo(proyecto, tipo))


def crear_proyecto(nombre):
    n = _limpio(nombre, "proyecto")
    os.makedirs(os.path.join(BASE, n), exist_ok=True)
    return n


def destino(proyecto, tipo, sub, nombre, carpeta_propia=False):
    """Ruta base nueva (sin extension) AAAAMMDD_HHMM_nombre que no pisa nada; crea las carpetas.
    carpeta_propia=True (registros): la ruta es una carpeta nueva con ese nombre.
    Devuelve (ruta_pc, ruta_osc, relativa)."""
    dir_ = os.path.join(_dir_tipo(proyecto, tipo), *_sub(sub))
    os.makedirs(dir_, exist_ok=True)
    base = time.strftime("%Y%m%d_%H%M_") + _limpio(nombre).replace(" ", "_")
    cand, k = base, 2
    while any(f == cand or f.startswith(cand + ".") or f.startswith(cand + "_C") for f in os.listdir(dir_)):
        cand, k = f"{base}_{k}", k + 1
    ruta = os.path.join(dir_, cand)
    if carpeta_propia:
        os.makedirs(ruta)
    rel = os.path.relpath(ruta, BASE)
    return ruta, BASE_OSC + "/" + rel.replace(os.sep, "/"), rel


def recientes(proyecto, tipo, n=6):
    """Ultimos elementos guardados de ese tipo en el proyecto: [{nombre, carpeta, ficheros, hora}]."""
    raiz = _dir_tipo(proyecto, tipo)
    grupos = {}
    if tipo != "capturas":          # registros y ensayos: cada uno es una carpeta con fecha
        for dirpath, dirnames, _ in os.walk(raiz):
            for d in [d for d in dirnames if re.match(r"\d{8}_\d{4}_", d)]:
                ruta = os.path.join(dirpath, d)
                fich = sorted(os.listdir(ruta))
                grupos[ruta] = {"nombre": d, "carpeta": os.path.relpath(dirpath, BASE), "ficheros": fich,
                                "hora": max([os.path.getmtime(os.path.join(ruta, f)) for f in fich] or [os.path.getmtime(ruta)])}
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and not re.match(r"\d{8}_\d{4}_", d)]
        return sorted(grupos.values(), key=lambda g: -g["hora"])[:n]
    for dirpath, dirnames, files in os.walk(raiz):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in files:
            m = re.match(r"(\d{8}_\d{4}_.+?)\.(h5|png|json|set)$", f)
            if m:
                clave = os.path.join(dirpath, m.group(1))
                g = grupos.setdefault(clave, {"nombre": m.group(1), "carpeta": os.path.relpath(dirpath, BASE),
                                              "ficheros": [], "hora": 0})
                g["ficheros"].append(m.group(2))
                g["hora"] = max(g["hora"], os.path.getmtime(os.path.join(dirpath, f)))
    return sorted(grupos.values(), key=lambda g: -g["hora"])[:n]
