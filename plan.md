# Plan de implementación — MVP pipeline de imágenes de repuestos (DONREP)

Objetivo: para cada fila del catálogo (Ref Proveedor, Marca, Nombre) obtener UNA imagen que cumpla dos restricciones duras — producto correcto en la presentación correcta (ej. pastillas delanteras: dos o una, nunca cuatro) y marca correcta — más cuatro preferencias con fallback: alta resolución, fondo blanco o monocromático, producto nuevo, sin empaques salvo que el empaque verifique la marca.

Flujo: `name_normalization -> grouping -> CSE query -> pre-filtro barato -> selección Gemini -> Excel`

Formato MVP: un notebook Jupyter (`mvp_pipeline.ipynb`) con una celda por etapa, más un módulo `pipeline/` con las funciones puras para que Claude Code lo convierta luego en script. Cada etapa escribe su salida a un DataFrame intermedio que se puede inspeccionar visualmente en el notebook (mostrar imágenes candidatas inline es clave para depurar).

---

## Etapa 0 — Carga y estructura del proyecto

```
image_processor_matcher/
├── credentials.json          (NO commitear; ya en .gitignore)
├── .env
├── data/productos_renault.xlsx
├── pipeline/
│   ├── normalize.py
│   ├── categorize.py
│   ├── search.py
│   ├── prefilter.py
│   ├── select.py            (Gemini)
│   └── io_excel.py
├── config/
│   ├── abbreviations.py     (dict de expansión de abreviaturas)
│   ├── vehicle_codes.py     (dict códigos plataforma -> modelo)
│   ├── categories.py        (dataclasses Categoria: keyword -> perfil CSE + regla de presentación)
│   └── domains.py           (listas blocklist/priorización de dominios)
└── mvp_pipeline.ipynb
```

Cargar el xlsx con pandas. Columnas reales con datos: `Ref Proveedor`, `Marca`, `Nombre`. Ignorar `Sku Interno`, `Descripcion del repuesto`, `Referencia Equivalente` (vacías en este archivo, pero dejar el código tolerante a que aparezcan).

## Etapa 1 — Normalización de nombres

El hallazgo de tus pruebas ("menos query = mejores resultados") define la meta de esta etapa: producir el término de búsqueda MÍNIMO que identifica el producto. Eso significa quitar ruido, no agregar contexto.

Transformaciones, en orden:

1. Limpieza básica: mayúsculas/minúsculas consistentes, colapsar espacios dobles, quitar tildes para el matching interno (conservar el nombre original en una columna).
2. Corrección de duplicaciones/typos conocidos: `DELANTEROANTERO -> DELANTERO`. Mantener una lista corta de reemplazos regex en `abbreviations.py`.
3. Expansión de abreviaturas: `DEL/DELANT -> DELANTERO`, `TRAS/TRA -> TRASERO`, `IZ/IZQ -> IZQUIERDO`, `DER -> DERECHO`, `AMOR -> AMORTIGUADOR`, `MOT -> MOTOR`, `PTAL -> PUERTA LATERAL`. Diccionario en `abbreviations.py`, no hardcodeado.
4. Extracción (no borrado) de tokens estructurados a columnas propias:
   - `pack`: patrones `\d+L?X\d+\s?UN(D|ID)?` (1LX12UN, 4X4 UNID). Útil para saber la presentación del envase (aceite 1L vs 4L) — esto SÍ es información de presentación para la búsqueda de aceites.
   - `viscosidad`: `\d+W-?\d+` (10W30, 5W40, 0W-16).
   - `medida_llanta`: `\d{3}/\d{2}R\d{2}`.
   - `vehiculo_code`: token final de 2-4 letras mayúsculas (KWE, NDU, LN3, CP, ARK, KO, NM, GKO...). Mapear a modelo en `vehicle_codes.py` cuando se conozca (KWE≈Kwid, etc.) y dejar vacío cuando no — el mapeo se llena iterativamente. Para la query NO incluir el código crudo jamás (confunde al buscador); incluir el modelo mapeado solo como fallback si la búsqueda genérica falla.
5. Salida: columnas `nombre_original`, `nombre_limpio`, `termino_busqueda` (la versión mínima), más las columnas estructuradas.

Regla de oro para la query: el candidato número uno es la referencia y la marca, ambas entre comillas como frases exactas. Este es tu builder probado:

```python
def make_query(marca: str, ref: str) -> str:
    return f'"{ref}" "{marca}"'
```

Es la versión extrema de tu hallazgo "menos query = mejores resultados", con un beneficio extra: comillar la marca obliga a Google a devolver solo páginas que contienen ese texto exacto, así que tu restricción dura de marca queda parcialmente aplicada en la capa de búsqueda en vez de recaer toda sobre Gemini. Dos advertencias que la mantienen como primer escalón y no como estrategia única: (a) CSE devuelve imágenes de páginas que CONTIENEN la referencia y la marca, no imágenes verificadas de esa referencia, así que las páginas de listado a veces traen fotos placeholder, de la caja o de otra variante, y el pool igual pasa por Gemini; (b) algunas refs devuelven pool vacío o basura (ítems regionales, o merchandising como la gorra, que casi nunca está indexado por ref). Por eso el `termino_busqueda` descriptivo sigue existiendo como fallback: tipo de pieza + marca (+ especificación distintiva si existe: viscosidad, medida, amperaje). Ej: `ACEITE MOTOR 10W40 MOTRIO 1L`, `PASTILLAS FRENO DELANTERAS RENAULT`, `GORRA RENAULT ROJA LOGO AZUL`. En el golden set, medir hit rate de `"ref" "marca"` vs descriptivo por categoría; es probable que algunas categorías (merchandising) deban saltarse el escalón de ref directamente vía config.

## Etapa 2 — Agrupación por categorías

Clasificador por reglas keyword-first (sin ML para el MVP; 281 filas se cubren con ~11 reglas). La categoría NO carga un template de query: como rung 1 es `"{ref}" "{marca}"` para casi todo, poner un template descriptivo por categoría reintroduce justo las queries largas y confusas que queremos evitar. La categoría carga solo las dos cosas que de verdad varían por tipo de producto y que ya validaste empíricamente: el perfil CSE y la regla de presentación para Gemini. La query descriptiva se arma solo cuando el rung de ref+marca falla, y es responsabilidad del fallback (Etapa 3), no de cada categoría.

Sobre el formato: dataclasses de Python, no yaml. Para un MVP de 281 filas y ~11 categorías, un archivo yaml solo agrega un parser, una dependencia y bugs de indentación sin ganar nada; cargar un dict plano sería equivalente pero sin validación. Las dataclasses te dan autocompletado y tipos, y las reglas viven junto al código que las usa. Si más adelante el catálogo crece y gente no técnica edita categorías, ahí se migra a archivo de config.

```python
from dataclasses import dataclass, field

@dataclass(frozen=True)
class Categoria:
    nombre: str
    keywords: list[str]
    cse_profile: str          # "white_dominant" | "exact_brand" | "baseline"
    presentacion: str         # regla que se inyecta al prompt de Gemini

CATEGORIAS = [
    Categoria(
        "lubricantes",
        keywords=["ACEITE", "10W", "15W", "5W", "20W", "80W"],
        cse_profile="white_dominant",
        presentacion="envase individual del litraje indicado; el empaque ES el producto",
    ),
    Categoria(
        "frenos_pastillas",
        keywords=["PASTILLA", "BANDAS FRENO"],
        cse_profile="baseline",
        presentacion="juego visible de dos pastillas, aceptable una, NUNCA cuatro",
    ),
    Categoria(
        "merchandising",
        keywords=["GORRA", "BOLSO", "LLAVERO"],
        cse_profile="exact_brand",
        presentacion="producto solo, sin modelo humano de preferencia",
    ),
]
```

Esto resuelve directamente tu problema de "cada búsqueda es distinta según el producto": el filtro CSE óptimo y la regla de presentación viven en la categoría, y tus hallazgos empíricos (white_dominant para aceites, exact_brand para merchandising) se codifican ahí en lugar de perderse. Categorías mínimas para este catálogo: lubricantes, llantas, baterías, frenos, amortiguadores/suspensión, brazos/dirección, carrocería (aletas, puertas), eléctrico (bobinas, bujías), bombas/refrigeración, merchandising, `otros` como catch-all con perfil baseline.

Toda fila sin categoría cae en `otros` y se loguea — esa lista es tu backlog para nuevas reglas.

## Etapa 3 — Búsqueda CSE

Una llamada por producto (no por perfil) para controlar costo: `searchType=image`, `num=10`, el perfil de la categoría, y `gl=co&hl=es` para sesgar a resultados del mercado colombiano (probar con y sin — puede ayudar con marcas locales tipo MOTRIO y estorbar con productos globales).

Escalera de fallback — cada escalón se dispara SOLO si el pool queda pobre tras el pre-filtro (menos de 3 candidatos). La idea es degradar la fuerza del filtro paso a paso, de más preciso a más laxo:

1. `"{ref}" "{marca}"` (ambas comilladas, búsqueda literal): tu builder probado, el más preciso.
2. `{ref} {marca}` (sin comillas): mismo contenido, filtro más débil. Comillar exige el texto exacto; quitar las comillas deja pasar páginas que escriben la marca distinto (ej. MOTRIO en minúscula, "Motrio by Renault", o con la referencia partida). Este escalón rescata justo los casos donde la marca existe pero no en la forma exacta.
3. `termino_busqueda` descriptivo con el perfil de la categoría (tipo de pieza + marca + especificación distintiva).
4. `termino_busqueda` con perfil `baseline`.
5. `exact_brand` (exactTerms=marca): útil cuando la marca se pierde en resultados genéricos.

Nota: el escalón avanza por tamaño de pool (<3 candidatos) pero también cuando Gemini rechaza todo el pool (Etapa 5 devuelve `seleccion=null`), así que un pool de 3 imágenes malas no deja el producto atascado.

Guardar SIEMPRE la respuesta cruda del CSE (JSON por producto en `cache/cse/{ref}.json`). El caché evita re-pagar búsquedas mientras iteras el resto del pipeline — es la decisión de costo más importante del MVP. La API devuelve por cada resultado `link`, `image.width/height`, `image.thumbnailLink`, `displayLink` (dominio): todo eso alimenta la etapa 4 gratis.

Costo: 281 productos × 1-2 llamadas ≈ 300-560 queries. CSE cobra ~US$5 por 1000 después de las 100 gratis/día; una corrida completa cuesta ~US$1.5-3. Con el caché, las corridas de iteración cuestan cero.

## Etapa 4 — Pre-filtro barato (sin Gemini)

Sobre los ≤10 resultados por producto, usando solo metadata del CSE:

1. Deduplicar por URL e imagen (mismo thumbnailLink).
2. Descartar resolución baja: `min(width, height) < 400` fuera (ajustable).
3. Descartar aspect ratios extremos (>3:1) — suelen ser banners.
4. Blocklist de dominios: arrancar `domains.py` con UNA sola entrada, `shutterstock.com` (marca de agua garantizada), y crecerla a mano conforme veas dominios que fallan. No inventes una lista grande a ciegas: mejor una que crece con evidencia que una que descarta dominios buenos por suposición.
5. Heurística de fondo blanco desde el thumbnail: el CSE ya te da `image.thumbnailLink` (una miniatura diminuta, descargarla cuesta bytes, no tokens). Muestrea los píxeles del borde (las 4 esquinas + puntos medios de cada lado) y calcula qué tan cerca están de blanco y qué tan uniformes son entre sí. Esto es señal de ORDENAMIENTO blando, no filtro duro: sube los candidatos con fondo blanco/uniforme, pero no descartes los demás (el fondo monocromático es un fallback aceptable, y esto lo confirma Gemini después). Barato y resuelve parte de tu criterio de calidad antes de gastar en Gemini.
6. Priorización blanda (ordenar, no descartar): dominios de fabricantes/distribuidores oficiales primero, luego el boost de fondo blanco del paso 5.
7. Recortar a top 6 candidatos.

Verificación de accesibilidad: HEAD request a cada URL sobreviviente (muchos links de CSE están muertos o bloquean hotlinking). Descargar los bytes de los top 6 a `cache/img/{ref}/` — Gemini los necesita igual, y así el notebook los muestra inline.

Esto deja la llamada a Gemini en ~6 imágenes por producto en vez de 10+, que es exactamente el filtrado intermedio de costo que buscabas.

## Etapa 5 — Selección con Gemini

Diseño: UNA llamada multimodal por producto con las 6 candidatas numeradas + el prompt, pidiendo salida JSON estructurada (`response_mime_type="application/json"` + schema). Modelo: empezar con Flash (el costo por imagen es bajo y la tarea es de discriminación, no de generación); subir a Pro solo para los productos donde Flash reporte baja confianza — ese ruteo de dos niveles es la forma barata de "probar los límites" que te sugirió tu jefe.

Cómo se reparte la decisión: Gemini hace las dos cosas en una sola llamada. Evalúa cada imagen contra cada criterio y JUSTIFICA (los modelos de visión actuales sí cuentan pastillas, leen el logo del empaque y distinguen producto nuevo de usado, eso te da una tabla de auditoría por producto), y además ELIGE la mejor. El código no re-elige desde cero: solo lee la selección de Gemini y decide qué hacer en los casos borde (que Gemini no pudo elegir, o eligió con baja confianza). El campo `seleccion` del JSON es exactamente ese "cuál considera la mejor" y viene acompañado de `ranking`, la lista ordenada de aceptables (mejor primero), que da un pick de reserva sin una segunda llamada si la URL de la elegida muere al descargar.

Prompt (plantilla, se inyectan los campos por producto):

```
Eres un verificador de imágenes para un catálogo de repuestos automotrices.

PRODUCTO: {nombre_limpio}
MARCA REQUERIDA: {marca}
CATEGORÍA: {categoria}
REGLA DE PRESENTACIÓN: {presentacion}   # ej: "juego de dos pastillas, aceptable una, NUNCA cuatro"

Recibes {n} imágenes candidatas numeradas. Evalúa cada una contra los criterios de abajo y al final ELIGE la mejor de las que pasan los eliminatorios:

CRITERIOS ELIMINATORIOS (si falla uno, la imagen queda descartada):
1. PRODUCTO: ¿la imagen muestra exactamente este tipo de repuesto? Verifica la
   cantidad y presentación según la regla dada. Cuenta las unidades visibles.
2. MARCA: ¿hay evidencia visible de la marca requerida (logo en la pieza, grabado,
   empaque legible)? Si no hay evidencia visible pero tampoco hay evidencia de otra
   marca, márcala como "marca_no_verificable" en vez de descartarla.

CRITERIOS DE CALIDAD (puntúa 0-10 cada uno):
3. Resolución y nitidez aparente
4. Fondo blanco (10) / monocromático (6) / complejo (0)
5. Estado nuevo del producto (sin desgaste, óxido ni uso)
6. Sin caja/bolsa/empaque — EXCEPCIÓN: si el empaque es la única evidencia visible
   de la marca, el empaque suma en vez de restar
7. Sin marcas de agua, textos superpuestos, logos de tiendas ni personas

Elige la mejor priorizando primero que pase los eliminatorios y luego la suma de
calidad. Responde SOLO con JSON:
{
  "evaluaciones": [
    {"imagen": 1, "producto_correcto": bool, "unidades_visibles": int,
     "marca": "verificada|no_verificable|incorrecta", "evidencia_marca": "...",
     "scores": {"resolucion": n, "fondo": n, "estado": n, "empaque": n, "limpieza": n},
     "descartada": bool, "razon": "..."}
  ],
  "seleccion": <número de la mejor imagen o null>,
  "ranking": [<números de las aceptables, mejor primero>],
  "confianza": "alta|media|baja",
  "comentario": "una línea que justifica la elección"
}
Si ninguna candidata pasa los criterios eliminatorios, seleccion=null y ranking=[].
```

Reglas post-Gemini (no eligen; solo actúan sobre la elección de Gemini y los casos borde):

- `seleccion != null` y `confianza` alta/media -> aceptar esa imagen.
- Imagen elegida con URL muerta al descargar -> tomar el siguiente número de `ranking` sin volver a llamar a Gemini.
- `marca_no_verificable` como única opción -> aceptar con flag `revisar_marca` (mejor una imagen probable con flag que ninguna; tú decides el umbral con el negocio).
- `seleccion = null` o confianza baja -> disparar el siguiente escalón del fallback de búsqueda (Etapa 3) y reintentar UNA vez; si vuelve a fallar, marcar `sin_imagen` para revisión manual.

## Etapa 6 — Salida a Excel

Copia del xlsx original + columnas: `imagen_url`, `imagen_local`, `categoria`, `termino_busqueda`, `perfil_cse_usado`, `confianza`, `flags` (revisar_marca / sin_imagen / fallback_usado), `razon_gemini`. Con openpyxl se puede además incrustar la miniatura en una columna para revisión rápida sin abrir URLs — para 281 filas es viable y hace la validación con tu jefe mucho más ágil.

## Validación y orden de construcción

Antes de correr las 281: arma un golden set de ~20 productos cubriendo todas las categorías (incluye los casos que ya conoces: aceites, la gorra, pastillas). Corre el pipeline completo solo sobre ese set, revisa en el notebook con las imágenes inline, ajusta las dataclasses de categoría y el prompt, y solo entonces lanza el catálogo completo.

Orden sugerido para la sesión de Claude Code (cada paso es verificable solo):

1. `normalize.py` + tests con los 281 nombres reales (imprimir antes/después, revisar a ojo)
2. `categorize.py` + reporte de cobertura (¿cuántos caen en `otros`?)
3. `search.py` con caché — correr solo el golden set
4. `prefilter.py` + celda de notebook que muestra los candidatos por producto
5. `select.py` con Gemini sobre el golden set
6. `io_excel.py` y corrida completa

## Notas de costos y cuotas

- CSE: 100 queries/día gratis, luego ~US$5/1000, tope 10k/día. El caché es obligatorio.
- Gemini Flash: ~6 imágenes + prompt por producto es del orden de centavos para todo el catálogo; el escalón a Pro solo aplica a la minoría con confianza baja.
- Rate limiting simple: `time.sleep` + reintento con backoff en 429/5xx para ambas APIs; suficiente para el MVP.

## Seguridad

`credentials.json` y `.env` fuera del repo desde el commit cero. Y ojo: pegaste un fragmento del JSON de la cuenta de servicio en este chat — la parte sensible parece placeholder, pero si en algún momento la private key real salió del archivo, rótala en GCP (IAM > Service Accounts > Keys) por sanidad.
