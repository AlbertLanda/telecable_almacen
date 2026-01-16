class DomainError(Exception):
    """Error de reglas de negocio."""
    pass


class StockInsuficienteError(DomainError):
    pass


class CantidadInvalidaError(DomainError):
    pass
