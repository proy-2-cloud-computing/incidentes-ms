import uuid

from .lib import repo
from .lib.auth import ROLES_ATENCION, get_context, requerir_rol
from .lib.dominio import TRANSICIONES, ahora_iso, calcular_prioridad, calcular_sla, serializar, validar_actualizar
from .lib.errores import HttpError
from .lib.eventos import crear_evento, publicar
from .lib.http import envolver, id_de_path, parse_body, respuesta

CAMPOS_SIMPLES = (
    "titulo", "descripcion", "categoria", "servicio_afectado", "solucion",
    "problema_id", "asignado_a", "urgencia", "impacto",
)


@envolver
def handler(event):
    """PUT /incidentes/{id} - actualizar / asignar / cambiar estado / resolver (roles agente o admin)."""
    ctx = get_context(event)
    requerir_rol(ctx, ROLES_ATENCION)
    incidente_id = id_de_path(event)
    cambios = validar_actualizar(parse_body(event))

    actual = repo.get_incidente(ctx.tenant_id, incidente_id, consistente=True)
    if not actual:
        raise HttpError(404, "Incidente no encontrado")
    if actual["estado"] == "cerrado":
        raise HttpError(409, "Un incidente cerrado no puede modificarse")

    ahora = ahora_iso()
    nuevo = {c: cambios[c] for c in CAMPOS_SIMPLES if c in cambios}

    # Prioridad y SLA siempre se recalculan en el backend
    prioridad = actual["prioridad"]
    if "urgencia" in cambios or "impacto" in cambios:
        prioridad = calcular_prioridad(cambios.get("urgencia", actual["urgencia"]), cambios.get("impacto", actual["impacto"]))
        nuevo["prioridad"] = prioridad
        nuevo["sla_vence_en"] = calcular_sla(actual["creado_en"], prioridad)

    # Asignar a alguien un incidente "nuevo" lo pasa a "asignado"
    estado_nuevo = cambios.get("estado")
    if estado_nuevo is None and actual["estado"] == "nuevo" and nuevo.get("asignado_a"):
        estado_nuevo = "asignado"

    cambia_estado = estado_nuevo is not None and estado_nuevo != actual["estado"]
    if cambia_estado:
        if estado_nuevo not in TRANSICIONES[actual["estado"]]:
            raise HttpError(409, f"Transicion no permitida: {actual['estado']} -> {estado_nuevo}",
                            {"permitidas": TRANSICIONES[actual["estado"]]})
        nuevo["estado"] = estado_nuevo
        nuevo["estado_fecha"] = f"{estado_nuevo}#{actual['creado_en']}"

    estado_final = nuevo.get("estado", actual["estado"])
    asignado_final = nuevo["asignado_a"] if "asignado_a" in nuevo else actual.get("asignado_a")
    solucion_final = nuevo["solucion"] if "solucion" in nuevo else actual.get("solucion")
    if estado_final == "asignado" and not asignado_final:
        raise HttpError(400, "asignado_a es obligatorio para el estado asignado")
    if estado_final == "resuelto" and not solucion_final:
        raise HttpError(400, "solucion es obligatoria para resolver el incidente")

    campos_modificados = [k for k in nuevo if k != "estado_fecha"]
    nuevo["actualizado_en"] = ahora
    hist = {
        "tenant_id": ctx.tenant_id,
        "incidente_id": f"HIST#{incidente_id}#{ahora}#{uuid.uuid4().hex[:8]}",
        "incidente_ref": incidente_id,
        "evento": "cambio-estado" if cambia_estado else "actualizacion",
        "estado_anterior": actual["estado"],
        "estado_nuevo": estado_final,
        "campos": campos_modificados,
        "actor_id": ctx.user_id,
        "fecha": ahora,
    }
    repo.guardar_cambios(ctx.tenant_id, incidente_id, nuevo, actual["estado"], hist)

    resultado = {**actual, **nuevo}
    eventos = []
    if cambia_estado and estado_final == "resuelto":
        eventos.append(crear_evento("incidente-resuelto", ctx.tenant_id, incidente_id, estado_final, ctx.user_id, {
            "solucion": resultado.get("solucion"), "problema_id": resultado.get("problema_id"), "prioridad": prioridad,
        }))
    if prioridad == "P1" and actual["prioridad"] != "P1":  # escalamiento
        eventos.append(crear_evento("incidente-critico", ctx.tenant_id, incidente_id, estado_final, ctx.user_id, {
            "titulo": resultado["titulo"], "categoria": resultado["categoria"], "prioridad": prioridad,
            "servicio_afectado": resultado.get("servicio_afectado"), "sla_vence_en": resultado["sla_vence_en"],
        }))
    if eventos:
        publicar(eventos)

    return respuesta(200, serializar(resultado))
