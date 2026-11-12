class HttpError(Exception):
    """Error controlado: se convierte en una respuesta HTTP con ese status."""

    def __init__(self, status, mensaje, detalles=None):
        super().__init__(mensaje)
        self.status = status
        self.mensaje = mensaje
        self.detalles = detalles
