"""Escribir un .picolog (PicoLog 6) con las temperaturas que ha registrado el panel (2026-11-03).

Formato (descifrado de los .picolog de PicoLog 6.2.13; sin documentar): un tar con
  metadata.json      la captura (devices.items[].thermocoupleN: channelIdentifier, description, input, enabled,
                     captureConfig.interval en ms...), startDate, savedDate...
  data-map.json.gz   [[ "<equipo>|<serie>.<canal>/<nivel>/<inicio del bloque, ms Unix>", [fichero, offset, bytes] ], ...]
  1.0.gz             nivel 1: valores en bruto, float32 big-endian, 1000 por bloque (NaN donde no hay dato)
  10.0.gz            nivel 10: pares (minimo, maximo) de cada 10 muestras, 1000 pares por bloque
  ov.gz              niveles 100, 1000 y 10000: igual, de cada 100, 1000 y 10000 muestras
  thumb.png          miniatura (opcional)
Los bloques del nivel L empiezan en multiplos de intervalo x 1000 x L (ms Unix). Comprobado reconstruyendo un
.picolog real con este codigo: mismos bloques, byte a byte.
"""
import gzip
import io
import json
import math
import struct
import tarfile
import time

NIVELES = (1, 10, 100, 1000, 10000)
POR_BLOQUE = 1000
NAN = float("nan")


def bloques(muestras, intervalo_ms):
    """{nivel: {inicio_ms: [floats]}} a partir de {t_ms (multiplo del intervalo): valor}."""
    out = {}
    for nivel in NIVELES:
        paso = intervalo_ms * nivel                       # ms que resume cada posicion
        largo = paso * POR_BLOQUE                         # ms que cubre un bloque
        tramos = {}
        for t, v in muestras.items():
            q = t // paso * paso
            mn, mx = tramos.get(q, (v, v))
            tramos[q] = (min(mn, v), max(mx, v))
        nb = {}
        for q, (mn, mx) in tramos.items():
            ini = q // largo * largo
            i = (q - ini) // paso
            b = nb.setdefault(ini, [NAN] * (POR_BLOQUE if nivel == 1 else 2 * POR_BLOQUE))
            if nivel == 1:
                b[i] = mn
            else:
                b[2 * i], b[2 * i + 1] = mn, mx
        out[nivel] = nb
    return out


def escribir(ruta, canales, intervalo_ms, metadata, miniatura=None):
    """canales: [{"clave": "tc-08.A0067|685.ch1", "muestras": {t_ms: valor}}]; metadata: dict de metadata.json."""
    ficheros = {"1.0.gz": io.BytesIO(), "10.0.gz": io.BytesIO(), "ov.gz": io.BytesIO()}
    mapa = []
    for c in canales:
        for nivel, nb in bloques(c["muestras"], intervalo_ms).items():
            fi = "1.0.gz" if nivel == 1 else "10.0.gz" if nivel == 10 else "ov.gz"
            for ini in sorted(nb):
                datos = struct.pack(f">{len(nb[ini])}f", *nb[ini])
                mapa.append([f"{c['clave']}/{nivel}/{ini}", [fi, ficheros[fi].tell(), len(datos)]])
                ficheros[fi].write(datos)
    ahora = time.time()

    def poner(tar, nombre, contenido):
        ti = tarfile.TarInfo(nombre)
        ti.size, ti.mode, ti.mtime = len(contenido), 0o777, int(ahora)
        tar.addfile(ti, io.BytesIO(contenido))
    with tarfile.open(ruta, "w", format=tarfile.PAX_FORMAT) as tar:
        poner(tar, "metadata.json", json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode())
        if miniatura:
            poner(tar, "thumb.png", miniatura)
        poner(tar, "data-map.json.gz", gzip.compress(json.dumps(mapa, separators=(",", ":")).encode()))
        for nombre in ("ov.gz", "10.0.gz", "1.0.gz"):
            poner(tar, nombre, gzip.compress(ficheros[nombre].getvalue()))
    return mapa


def leer(ruta):
    """(metadata, {clave_canal: {t_ms: valor}}, intervalo_ms por canal) de un .picolog (nivel 1)."""
    with tarfile.open(ruta) as t:
        meta = json.loads(t.extractfile("metadata.json").read())
        mapa = json.loads(gzip.decompress(t.extractfile("data-map.json.gz").read()))
        datos = {m.name: gzip.decompress(t.extractfile(m).read()) for m in t.getmembers()
                 if m.name.endswith(".gz") and m.name != "data-map.json.gz"}
    intervalo = {}
    for d in meta["devices"]["items"]:
        for ch in d.values() if isinstance(d, dict) else ():
            if isinstance(ch, dict) and ch.get("channelIdentifier"):
                clave = f"{ch.get('deviceSerial', d.get('serialInternal', '')).replace('/', '|')}.{ch['channelIdentifier']}"
                intervalo[clave] = (ch.get("captureConfig") or {}).get("interval", 1000)
    canales = {}
    for k, (fi, off, ln) in mapa:
        cab, nivel, ini = k.rsplit("/", 2)
        if nivel != "1":
            continue
        dt = next((v for c, v in intervalo.items() if cab.endswith(c)), 1000)
        x = struct.unpack(f">{ln // 4}f", datos[fi][off:off + ln])
        m = canales.setdefault(cab, {})
        for i, v in enumerate(x):
            if not math.isnan(v):
                m[int(ini) + i * dt] = v
    return meta, canales, intervalo
