import uuid

from .lib import repo
from .lib.auth import get_context
from .lib.dominio import ahora_iso, calcular_prioridad, calcular_sla, formatear_id, serializar, validar_crear
from .lib.eventos import crear_evento, publicar
from .lib.http import envolver, parse_body, respuesta


@envolver
def handler(event):
    """POST /incidentes - cualquier usuario autenticado puede reportar.
    tenant_id, estado, prioridad y SLA los calcula el backend; nunca se leen del body."""
    ctx = get_context(event)
    datos = validar_crear(parse_body(event))

    incidente_id = formatear_id(repo.siguiente_numero(ctx.tenant_id))
    ahora = ahora_iso()
    prioridad = calcular_prioridad(datos["urgencia"], datos["impacto"])

    item = {
        "tenant_id": ctx.tenant_id,
        "incidente_id": incidente_id,
        **datos,
        "prioridad": prioridad,
        "estado": "nuevo",
        "reportado_por": ctx.user_id,
        "asignado_a": None,
        "sla_vence_en": calcular_sla(ahora, prioridad),
        "solucion": None,
        "adjuntos": [],
        "creado_en": ahora,
        "actualizado_en": ahora,
        "estado_fecha": f"nuevo#{ahora}",  # GSI1 (indice disperso: solo items de negocio)
    }
    hist = {
        "tenant_id": ctx.tenant_id,
        "incidente_id": f"HIST#{incidente_id}#{ahora}#{uuid.uuid4().hex[:8]}",
        "incidente_ref": incidente_id,
        "evento": "creacion",
        "estado_anterior": None,
        "estado_nuevo": "nuevo",
        "actor_id": ctx.user_id,
        "fecha": ahora,
    }
    repo.guardar_nuevo(item, hist)

    resumen_evento = {
        "titulo": item["titulo"], "categoria": item["categoria"], "prioridad": prioridad,
        "servicio_afectado": item["servicio_afectado"], "sla_vence_en": item["sla_vence_en"],
    }
    eventos = [crear_evento("incidente-creado", ctx.tenant_id, incidente_id, "nuevo", ctx.user_id, resumen_evento)]
    if prioridad == "P1":
        eventos.append(crear_evento("incidente-critico", ctx.tenant_id, incidente_id, "nuevo", ctx.user_id, resumen_evento))
    publicar(eventos)

    return respuesta(201, serializar(item))
