import unittest

from src.lib.auth import parse_grupos
from src.lib.dominio import (
    TRANSICIONES, calcular_prioridad, calcular_sla, formatear_id, id_para_s3, normalizar_id,
    validar_actualizar, validar_crear,
)
from src.lib.errores import HttpError


class DominioTest(unittest.TestCase):
    def test_matriz_de_prioridad(self):
        casos = [("alta", "alto", "P1"), ("alta", "medio", "P2"), ("media", "alto", "P2"),
                 ("media", "medio", "P3"), ("baja", "alto", "P3"), ("baja", "bajo", "P4")]
        for urgencia, impacto, esperado in casos:
            self.assertEqual(calcular_prioridad(urgencia, impacto), esperado)

    def test_sla_segun_prioridad(self):
        self.assertEqual(calcular_sla("2026-11-12T08:00:00.000Z", "P1"), "2026-11-12T12:00:00.000Z")
        self.assertEqual(calcular_sla("2026-11-12T08:00:00.000Z", "P4"), "2026-11-15T08:00:00.000Z")

    def test_normalizacion_de_ids(self):
        self.assertEqual(normalizar_id("INC#000123"), "INC#000123")
        self.assertEqual(normalizar_id("inc-123"), "INC#000123")
        self.assertEqual(normalizar_id("45"), "INC#000045")
        self.assertIsNone(normalizar_id("PRB#000045"))
        self.assertIsNone(normalizar_id("abc"))
        self.assertEqual(formatear_id(10001), "INC#010001")
        self.assertEqual(id_para_s3("INC#000123"), "INC-000123")

    def test_validar_crear_ignora_campos_de_confianza(self):
        d = validar_crear({"titulo": "Caida del correo", "urgencia": "Alta", "impacto": "alto",
                           "tenant_id": "otro", "prioridad": "P4"})
        self.assertEqual(d["urgencia"], "alta")
        self.assertEqual(d["categoria"], "otro")
        self.assertNotIn("tenant_id", d)
        self.assertNotIn("prioridad", d)

    def test_validar_rechaza_datos_invalidos(self):
        with self.assertRaises(HttpError):
            validar_crear({"titulo": "x", "urgencia": "alta", "impacto": "alto"})
        with self.assertRaises(HttpError):
            validar_crear({"titulo": "Titulo ok", "urgencia": "urgente", "impacto": "alto"})
        with self.assertRaises(HttpError):
            validar_actualizar({})

    def test_transiciones(self):
        self.assertIn("resuelto", TRANSICIONES["en_progreso"])
        self.assertNotIn("resuelto", TRANSICIONES["nuevo"])
        self.assertEqual(TRANSICIONES["cerrado"], [])

    def test_grupos_de_cognito(self):
        self.assertEqual(parse_grupos("agente"), ["agente"])
        self.assertEqual(parse_grupos("[agente, admin]"), ["agente", "admin"])
        self.assertEqual(parse_grupos(None), [])


if __name__ == "__main__":
    unittest.main()
