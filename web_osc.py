"""Frontal virtual del MXO por su web (la misma API que usa «Web control»), sin pasar el osciloscopio a REMOTO.

Estudiado el 2026-11-02 en el codigo de la web del MXO (device-web, lxi-element):
  PATCH /api/lxi-web/v1/vnccontrol/key    {"id": "RUN_STOP", "action": 1}          pulsar un boton
  PATCH /api/lxi-web/v1/vnccontrol/wheel  {"id": "VERTICAL_POSITION", "increment": n}  girar una ruleta
  GET   /api/lxi-web/v1/vnccontrol/states                                          LEDs
Autenticacion: Basic con el usuario y la «Web password» del MXO. La contrasena NO esta en ningun fichero: la
guarda el usuario en el Administrador de credenciales de Windows (credencial generica «mxo34-web») y se lee aqui
solo al hacer cada peticion. Nunca se escribe en el registro ni en pantalla.
La web NO permite leer la configuracion (escalas, offset...): eso sigue por SCPI.
"""
import base64
import ctypes
import ctypes.wintypes as wt
import json
import urllib.error
import urllib.request

CREDENCIAL = "mxo34-web"
API = "/api/lxi-web/v1/vnccontrol"


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
                ("LastWritten", wt.FILETIME), ("CredentialBlobSize", wt.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wt.DWORD),
                ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wt.LPWSTR),
                ("UserName", wt.LPWSTR)]


def _credencial():
    """(usuario, contrasena) de la credencial generica de Windows, o None si no existe."""
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    p = ctypes.POINTER(_CREDENTIAL)()
    if not adv.CredReadW(CREDENCIAL, 1, 0, ctypes.byref(p)):      # 1 = CRED_TYPE_GENERIC
        return None
    try:
        c = p.contents
        blob = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
        clave = blob.decode("utf-16-le") if c.CredentialBlobSize % 2 == 0 else blob.decode("utf-8")
        return c.UserName or "", clave
    finally:
        adv.CredFree(p)


def hay_credencial():
    return _credencial() is not None


_CLAVE_WEB = {}


def _clave_de_la_web(host):
    """Contrasena con la que la propia pagina «Web control» del MXO entra en su frontal virtual. La sirve el
    osciloscopio a cualquier navegador dentro de su codigo (lxi-element); se lee de ahi, solo en memoria, para
    hacer lo mismo que la web. Decidido con el usuario el 2026-11-02 (equipo propio). Nunca se guarda ni se muestra."""
    if host not in _CLAVE_WEB:
        import re
        url = f"http://{host}/device-web/assets/elements/lxi-element/lxi-element-es2015.js"
        with urllib.request.urlopen(url, timeout=10) as r:
            js = r.read().decode("utf-8", "replace")
        m = re.search(r"hardcodedPasswordFrontPanel\s*=\s*([\"'`])(.*?)\1", js)
        _CLAVE_WEB[host] = m.group(2) if m else None
    return _CLAVE_WEB[host]


class FrontalWeb:
    def __init__(self, host, timeout=3.0):
        self.host, self.timeout = host, timeout

    def _peticion(self, metodo, ruta, cuerpo=None):
        # la clave del frontal virtual es la de la propia web del MXO; si no se encuentra, la de Windows («mxo34-web»)
        clave = _clave_de_la_web(self.host)
        if clave is None:
            cred = _credencial()
            if cred is None:
                raise RuntimeError("No se encuentra la clave del frontal virtual en la web del osciloscopio")
            clave = cred[1]
        # la web del MXO entra siempre con el usuario fijo «User» (visto en su codigo)
        token = base64.b64encode(f"User:{clave}".encode()).decode()
        datos = None if cuerpo is None else json.dumps(cuerpo).encode()
        req = urllib.request.Request(f"http://{self.host}{API}{ruta}", data=datos, method=metodo, headers={
            "Authorization": "Basic " + token, "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                txt = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError("El osciloscopio rechaza la credencial «mxo34-web» (401): revisa usuario y "
                                   "contraseña en el Administrador de credenciales") from None
            raise RuntimeError(f"Web del osciloscopio: HTTP {e.code}") from None
        return json.loads(txt) if txt.strip().startswith(("{", "[")) else txt

    def estados(self):
        return self._peticion("GET", "/states")

    def tecla(self, ident):
        return self._peticion("PATCH", "/key", {"id": ident, "action": 1})

    def rueda(self, ident, incremento):
        return self._peticion("PATCH", "/wheel", {"id": ident, "increment": int(incremento)})
