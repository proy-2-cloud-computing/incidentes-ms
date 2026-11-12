#!/usr/bin/env python3
"""Carga datos ficticios en la tabla de Incidentes (10,000 por tenant => 30,000 en total).

Uso:
    python scripts/seed.py --stage dev [--region us-east-1] [--count 10000]
                           [--tenants empresa-alfa,empresa-beta,empresa-gamma] [--table nombre]

Deja el item CONTADOR#INC de cada tenant en el total cargado (10000) y verifica los conteos.
Requiere credenciales de AWS configuradas (pip install -r requirements.txt).
"""
import argparse
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import boto3  # noqa: E402

from src.lib.dominio import (  # noqa: E402
    CATEGORIAS, IMPACTOS, URGENCIAS, a_iso, calcular_prioridad, calcular_sla, formatear_id,
)

SERVICIOS = [
    "Correo corporativo", "VPN", "ERP", "CRM", "Portal web", "Punto de venta", "Active Directory", "Firewall perimetral",
    "Base de datos", "Telefonia IP", "Wi-Fi corporativo", "Intranet", "Facturacion electronica", "Almacenamiento compartido",
    "Core bancario", "Plataforma de pagos", "Red movil", "Sistema de monitoreo",
]
FALLAS = {
    "red": ["Perdida de conectividad", "Latencia elevada", "Caida de enlace WAN", "Intermitencia en la red"],
    "hardware": ["Falla de disco", "Servidor no enciende", "Equipo con sobrecalentamiento", "Memoria defectuosa"],
    "software": ["Caida del servicio", "Error 500 al iniciar sesion", "Lentitud en la aplicacion", "Fallo tras actualizacion"],
    "acceso": ["Usuario bloqueado", "No puede autenticarse", "Permisos insuficientes", "Token expirado"],
    "otro": ["Alerta de monitoreo", "Incidencia reportada por usuario", "Comportamiento anomalo", "Servicio degradado"],
}
SOLUCIONES = [
    "Se reinicio el servicio y se valido el funcionamiento", "Se restauro la configuracion anterior",
    "Se reemplazo el componente defectuoso", "Se restablecieron credenciales y permisos",
    "Se aplico el parche del proveedor", "Se amplio la capacidad del recurso afectado",
]
ESTADOS_PESO = [("nuevo", 15), ("asignado", 15), ("en_progreso", 25), ("resuelto", 20), ("cerrado", 25)]
TAM_LOTE = 250       # items por tarea (batch_writer los agrupa de 25 en 25)
HILOS = 8


def hex4():
    return f"{random.randint(0, 0xFFFF):04x}"


def generar_item(tenant_id, n, ahora, por_tenant):
    categoria = random.choice(CATEGORIAS)
    servicio = random.choice(SERVICIOS)
    urgencia = random.choice(URGENCIAS)
    impacto = random.choice(IMPACTOS)
    prioridad = calcular_prioridad(urgencia, impacto)
    estado = random.choices([e for e, _ in ESTADOS_PESO], weights=[p for _, p in ESTADOS_PESO])[0]
    creado = ahora - timedelta(seconds=random.randint(0, 120 * 24 * 3600))
    creado_en = a_iso(creado)
    actualizado = min(ahora, creado + timedelta(seconds=random.randint(0, 5 * 24 * 3600)))
    resuelto = estado in ("resuelto", "cerrado")
    return {
        "tenant_id": tenant_id,
        "incidente_id": formatear_id(n),
        "titulo": f"{random.choice(FALLAS[categoria])} - {servicio}",
        "descripcion": f"Se reporta {random.choice(FALLAS[categoria]).lower()} que afecta a {servicio}. Ticket generado para pruebas.",
        "categoria": categoria,
        "urgencia": urgencia,
        "impacto": impacto,
        "prioridad": prioridad,
        "estado": estado,
        "reportado_por": f"u-{hex4()}",
        "asignado_a": None if estado == "nuevo" else f"u-{hex4()}",
        "servicio_afectado": servicio,
        "sla_vence_en": calcular_sla(creado_en, prioridad),
        "solucion": random.choice(SOLUCIONES) if resuelto else None,
        "problema_id": f"PRB#{random.randint(1, por_tenant):06d}" if random.random() < 0.15 else None,
        "adjuntos": [],
        "creado_en": creado_en,
        "actualizado_en": a_iso(actualizado),
        "estado_fecha": f"{estado}#{creado_en}",
    }


def escribir_rango(args):
    region, tabla, tenant_id, desde, hasta, por_tenant, ahora = args
    tbl = boto3.session.Session(region_name=region).resource("dynamodb").Table(tabla)  # un recurso por hilo
    with tbl.batch_writer() as lote:  # agrupa de 25 en 25 y reintenta los UnprocessedItems
        for n in range(desde, hasta + 1):
            lote.put_item(Item=generar_item(tenant_id, n, ahora, por_tenant))
    return hasta - desde + 1


def contar_items(tbl, tenant_id):
    total, last_key = 0, None
    while True:
        kwargs = {
            "KeyConditionExpression": "tenant_id = :t AND begins_with(incidente_id, :p)",
            "ExpressionAttributeValues": {":t": tenant_id, ":p": "INC#"},
            "Select": "COUNT",
        }
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        r = tbl.query(**kwargs)
        total += r["Count"]
        last_key = r.get("LastEvaluatedKey")
        if not last_key:
            return total


def main():
    ap = argparse.ArgumentParser(description="Carga de datos ficticios - Incidentes")
    ap.add_argument("--stage", default="dev")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--count", type=int, default=10000, help="registros por tenant")
    ap.add_argument("--tenants", default="empresa-alfa,empresa-beta,empresa-gamma")
    ap.add_argument("--table", default=None)
    a = ap.parse_args()

    tabla = a.table or f"itsm-incidentes-{a.stage}"
    tenants = [t.strip() for t in a.tenants.split(",") if t.strip()]
    tbl = boto3.session.Session(region_name=a.region).resource("dynamodb").Table(tabla)
    print(f"Tabla: {tabla} ({a.region}) | tenants: {', '.join(tenants)} | {a.count} por tenant")

    ahora = datetime.now(timezone.utc)
    inicio = time.time()
    for tenant_id in tenants:
        print(f"Cargando {tenant_id}...")
        rangos = [(a.region, tabla, tenant_id, d, min(d + TAM_LOTE - 1, a.count), a.count, ahora)
                  for d in range(1, a.count + 1, TAM_LOTE)]
        cargados = 0
        with ThreadPoolExecutor(max_workers=HILOS) as pool:
            for n in pool.map(escribir_rango, rangos):
                cargados += n
                if cargados % 2500 == 0 or cargados == a.count:
                    print(f"  {tenant_id}: {cargados}/{a.count}")
        # El contador queda en el total cargado: el siguiente incidente creado sera INC#010001
        tbl.put_item(Item={"tenant_id": tenant_id, "incidente_id": "CONTADOR#INC", "valor": a.count})

    print("\nVerificacion de conteos:")
    total = 0
    for tenant_id in tenants:
        c = contar_items(tbl, tenant_id)
        total += c
        print(f"  {tenant_id}: {c}")
    print(f"  TOTAL: {total}  ({time.time() - inicio:.0f} s)")


if __name__ == "__main__":
    main()
