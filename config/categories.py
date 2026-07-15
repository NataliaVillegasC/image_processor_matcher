"""Etapa 2 - category definitions (rules keyword-first, no ML).

Built by inspecting the real 281 `Nombre` values.
Each Categoria carries ONLY the two things that genuinely vary per product
type and that were validated empirically.

The category does NOT carry a query template because we want to avoid
long/confusing queries.

ORDER MATTERS. The classifier returns the FIRST category whose keywords match,
so more-specific families must come before broader ones:
  - suspension_direccion before carroceria  (BRAZO ... PUERTA LATERAL = an arm)
  - vidrios_espejos       before carroceria  (VIDRIO MOVIL PUERTA = glass)
  - motor_transmision     before lubricantes (CARTER ACEITE / CADENA BOMBA ACEITE)
  - filtros               before lubricantes (FILTRO ACEITE = a filter)
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Categoria:
    nombre: str
    keywords: list[str]
    cse_profile: str
    presentacion: str         # regla que se inyecta al prompt de Gemini


# Sentinel returned when no rule matches. Every unmatched row lands here and is
# logged (see categorize.coverage_report) [that log is the backlog for new rules].
OTROS = Categoria(
    "otros",
    keywords=[],
    cse_profile="baseline",
    presentacion="producto individual centrado, presentacion estandar de catalogo",
)

CATEGORIAS: list[Categoria] = [
    Categoria(
        "merchandising",
        keywords=["GORRA", "BOLSO", "LLAVERO", "LIBRETA", "PIJAMA", "CARGADOR",
                "LE BOUQUET", "CASATORO", "KIT CARRETERA"],
        cse_profile="exact_brand",
        presentacion="producto solo sobre fondo limpio, sin modelo humano de preferencia",
    ),
    Categoria(
        "accesorios",
        keywords=["TAPETE", "TAPIZ"],
        cse_profile="baseline",
        presentacion="accesorio individual (tapete/tapiz) mostrado plano o de frente, sin vehiculo",
    ),
    Categoria(
        "emblemas",
        keywords=["MONOGRAMA", "EMBLEMA"],
        cse_profile="exact_brand",
        presentacion="emblema/monograma con el logo legible; aceptable como pieza suelta lo que se verifica es el logo y que este aislado (preferiblemente)",
    ),
    Categoria(
        "filtros",
        keywords=["FILTRO"],
        cse_profile="baseline",
        presentacion="filtro individual (cilindrico o panel); sin caja salvo que verifique la marca",
    ),
    Categoria(
        "frenos",
        keywords=["PASTILLA", "PLAQUETA", "BANDAS FRENO", "DISCO FRENO",
                "DISC FRENO", "CAMPANA", "DISC", "FRENO"],
        cse_profile="baseline",
        presentacion="cuenta las pastillas y verifica el eje segun el nombre. Si el nombre dice un eje (delanteras/delantero o traseras/trasero): mostrar 2 pastillas de ESE eje (aceptable 1), NUNCA el set de 4. Si el nombre NO especifica eje ni cantidad (solo 'pastillas de freno'): aceptable 1 o el set de 4",
    ),
    Categoria(
        "motor_transmision",
        keywords=["CARTER", "EMBRAGUE", "BOBINA", "BUJIA", "INYECTOR",
                "CORREA", "CADENA"],
        cse_profile="baseline",
        presentacion="componente individual (carter/embrague/bobina/bujia/inyector/correa)",
    ),
    Categoria(
        "refrigeracion",
        keywords=["BOMBA AGUA", "BOMBA DE AGUA", "SALIDA AGUA", "CONDENSADOR",
                "MOTOVENT", "INTERCOOL", "RADIADOR"],
        cse_profile="baseline",
        presentacion="componente individual (bomba de agua/condensador/motoventilador)",
    ),
    Categoria(
        "lubricantes",
        keywords=["ACEITE", "CASTROL", "10W", "15W", "5W", "20W", "80W", "0W"],
        cse_profile="baseline",
        presentacion="envase individual del litraje indicado; el empaque ES el producto (una botella, no multipack)",
    ),
    Categoria(
        "llantas_rines",
        keywords=["LLANTA", "RIN", "COPA RUEDA", "TAPACUBOS", "ENERGY XM2"],
        cse_profile="baseline",
        presentacion="una llanta o un rin de frente; banda/rin visible completo",
    ),
    Categoria(
        "baterias",
        keywords=["BATERIA"],
        cse_profile="baseline",
        presentacion="una bateria individual de frente, etiqueta/marca visible",
    ),
    Categoria(
        "suspension_direccion",
        keywords=["AMORTIGUADOR", "BRAZO", "GUARDAPOLVO", "RODAMIENT", "TOPE"],
        cse_profile="baseline",
        presentacion="pieza individual (amortiguador/brazo/rotula), una unidad del lado indicado",
    ),
    Categoria(
        "iluminacion",
        keywords=["FAROLA", "FARO", "STOP", "PROYECTOR"],
        cse_profile="baseline",
        presentacion="una unidad de la optica (farola/stop/proyector) del lado indicado",
    ),
    Categoria(
        "vidrios_espejos",
        keywords=["VIDRIO", "VIDR", "LUNETA", "PARABRISA", "PARABRISAS",
                "RETROVISOR", "COQUILLA", "COQUIL", "COQUILA", "COLISA",
                "PLUMILLA", "LIMPIAPARABRISAS"],
        cse_profile="baseline",
        presentacion="vidrio/espejo/retrovisor individual del lado indicado",
    ),
    Categoria(
        "carroceria",
        keywords=["PARACHOQUE", "PARAGOLPE", "PUERTA", "ALETA", "CAPOT",
                "COFRE", "BAUL", "REJILLA", "CALANDRIA", "CALANDRA",
                "GRILLA", "ESTRIBOS"],
        cse_profile="baseline",
        presentacion="panel de carroceria individual (parachoques/puerta/aleta) del lado y modelo indicados",
    ),
]