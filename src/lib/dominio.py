"""Reglas de negocio puras del microservicio de Incidentes (sin dependencias de AWS)."""
import re
from datetime import datetime, timedelta, timezone

from .errores import HttpError

CATEGORIAS = ("red", "hardware", "software", "acceso", "otro")
URGENCIAS = ("baja", "media", "alta")
IMPACTOS = ("bajo", "medio", "alto")
PRIORIDADES = ("P1", "P2", "P3", "P4")
ESTADOS = ("nuevo", "asignado", "en_progreso", "resuelto", "cerrado")
ESTADOS_ABIERTOS = ("nuevo", "asignado", "en_progreso")

# Flujo: nuevo -> asignado -> en_progreso -> resuelto -> cerrado
TRANSICIONES = {
    "nuevo": ["asignado", "en_progreso"],
    "asignado": ["nuevo", "en_progreso"],
    "en_progreso": ["asignado", "resuelto"],
    "resuelto": ["en_progreso", "cerrado"],
    "cerrado": [],
}

SLA_HORAS = {"P1": 4, "P2": 8, "P3": 24, "P4": 72}
_NIVEL_URGENCIA = {"baja": 1, "media": 2, "alta": 3}
_NIVEL_IMPACTO = {"bajo": 1, "medio": 2, "alto": 3}


# ---------- Fechas (ISO 8601 UTC con milisegundos, ordenables como texto) ----------
def a_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def ahora_iso():
    return a_iso(datetime.now(timezone.utc))


def parse_iso(valor):
    return datetime.fromisoformat(valor.replace("Z", "+00:00"))


# ---------- Prioridad, SLA e identificadores ----------
def calcular_prioridad(urgencia, impacto):
    """Matriz urgencia x impacto -> P1..P4."""
    if urgencia not in _NIVEL_URGENCIA or impacto not in _NIVEL_IMPACTO:
        raise ValueError("urgencia/impacto invalidos")
    puntos = _NIVEL_URGENCIA[urgencia] + _NIVEL_IMPACTO[impacto]
    if puntos >= 6:
        return "P1"
    if puntos == 5:
        return "P2"
    if puntos == 4:
        return "P3"
    return "P4"


def calcular_sla(creado_en, prioridad):
    return a_iso(parse_iso(creado_en) + timedelta(hours=SLA_HORAS[prioridad]))


def formatear_id(numero):
    return f"INC#{int(numero):06d}"


def normalizar_id(raw):
    """Acepta 'INC#000123', 'INC-000123' o '123'. Devuelve None si no es valido."""
    m = re.fullmatch(r"(?:INC[#\-_ ]?)?([0-9]{1,6})", str(raw if raw is not None else "").strip(), re.IGNORECASE)
    return formatear_id(int(m.group(1))) if m else None


def normalizar_id_problema(raw):
    m = re.fullmatch(r"(?:PRB[#\-_ ]?)?([0-9]{1,6})", str(raw if raw is not None else "").strip(), re.IGNORECASE)
    return f"PRB#{int(m.group(1)):06d}" if m else None


def id_para_s3(incidente_id):
    """Clave S3: {tenant_id}/incidentes/INC-000123/archivo"""
    return incidente_id.replace("#", "-")


def sla_vencido(item, ahora=None):
    """El SLA vencido se calcula al leer (no hay tareas programadas)."""
    ahora = ahora or datetime.now(timezone.utc)
    return item.get("estado") in ESTADOS_ABIERTOS and parse_iso(item["sla_vence_en"]) < ahora


def serializar(item):
    """Oculta atributos internos (indice GSI1) y agrega sla_vencido."""
    salida = {k: v for k, v in item.items() if k != "estado_fecha"}
    salida["sla_vencido"] = sla_vencido(item)
    return salida


# ---------- Validaciones ----------
def _texto(valor, campo, minimo=0, maximo=500, requerido=False):
    if valor is None:
        if requerido:
            raise HttpError(400, f"{campo} es obligatorio")
        return None
    if not isinstance(valor, str):
        raise HttpError(400, f"{campo} debe ser texto")
    v = valor.strip()
    if requerido and len(v) < max(minimo, 1):
        raise HttpError(400, f"{campo} es obligatorio (minimo {max(minimo, 1)} caracteres)")
    if len(v) < minimo:
        raise HttpError(400, f"{campo} requiere minimo {minimo} caracteres")
    if len(v) > maximo:
        raise HttpError(400, f"{campo} admite maximo {maximo} caracteres")
    return v


def _enumerado(valor, campo, opciones, requerido=False, defecto=None):
    if valor is None or valor == "":
        if requerido:
            raise HttpError(400, f"{campo} es obligatorio", {"permitidos": list(opciones)})
        return defecto
    v = str(valor).strip().lower()
    if v not in opciones:
        raise HttpError(400, f"{campo} invalido", {"permitidos": list(opciones)})
    return v


def _referencia_problema(valor):
    if valor is None or valor == "":
        return None
    problema_id = normalizar_id_problema(valor)
    if not problema_id:
        raise HttpError(400, "problema_id invalido (formato PRB#000123)")
    return problema_id


def validar_crear(body):
    """tenant_id, estado, prioridad y SLA nunca se leen del body."""
    return {
        "titulo": _texto(body.get("titulo"), "titulo", minimo=3, maximo=200, requerido=True),
        "descripcion": _texto(body.get("descripcion"), "descripcion", maximo=4000) or "",
        "categoria": _enumerado(body.get("categoria"), "categoria", CATEGORIAS, defecto="otro"),
        "urgencia": _enumerado(body.get("urgencia"), "urgencia", URGENCIAS, requerido=True),
        "impacto": _enumerado(body.get("impacto"), "impacto", IMPACTOS, requerido=True),
        "servicio_afectado": _texto(body.get("servicio_afectado"), "servicio_afectado", maximo=200) or "",
        "problema_id": _referencia_problema(body.get("problema_id")),
    }


CAMPOS_ACTUALIZABLES = (
    "titulo", "descripcion", "categoria", "urgencia", "impacto", "servicio_afectado",
    "solucion", "estado", "asignado_a", "problema_id",
)


def validar_actualizar(body):
    """Devuelve solo los campos presentes en el body."""
    c = {}
    if "titulo" in body:
        c["titulo"] = _texto(body["titulo"], "titulo", minimo=3, maximo=200, requerido=True)
    if "descripcion" in body:
        c["descripcion"] = _texto(body["descripcion"], "descripcion", maximo=4000) or ""
    if "categoria" in body:
        c["categoria"] = _enumerado(body["categoria"], "categoria", CATEGORIAS, requerido=True)
    if "urgencia" in body:
        c["urgencia"] = _enumerado(body["urgencia"], "urgencia", URGENCIAS, requerido=True)
    if "impacto" in body:
        c["impacto"] = _enumerado(body["impacto"], "impacto", IMPACTOS, requerido=True)
    if "servicio_afectado" in body:
        c["servicio_afectado"] = _texto(body["servicio_afectado"], "servicio_afectado", maximo=200) or ""
    if "solucion" in body:
        c["solucion"] = _texto(body["solucion"], "solucion", maximo=4000) or ""
    if "estado" in body:
        c["estado"] = _enumerado(body["estado"], "estado", ESTADOS, requerido=True)
    if "asignado_a" in body:
        c["asignado_a"] = _texto(body["asignado_a"], "asignado_a", maximo=128) or None
    if "problema_id" in body:
        c["problema_id"] = _referencia_problema(body["problema_id"])
    if not c:
        raise HttpError(400, "No se envio ningun campo modificable", {"campos_permitidos": list(CAMPOS_ACTUALIZABLES)})
    return c
