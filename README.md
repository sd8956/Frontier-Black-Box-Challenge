# Reemplazo de la caja negra (Reto 2)

Servicio propio que responde el mismo contrato que el original, sin llamarlo ni llamar a otro
reemplazo. El estado vive en Postgres, no en el proceso.

| Dato | Valor |
| --- | --- |
| Entrypoint | `https://frontier-black-box-challenge-production.up.railway.app` |
| Autenticación | `Authorization: Bearer <token del equipo>` en todas las llamadas |
| Listo | `GET /health` responde `200 {"ok": true}` |
| Reset | `POST /__reset` vuelve al estado inicial; se llama antes de cada test |

Con otro token o sin token, responde como el original. El token del equipo se registra una vez contra
el servicio (*Sembrar el token*) y no vive en el repositorio.

Los entregables del reto son este repositorio, el entrypoint de la tabla, `docs/spec.md` y
`experimentos.jsonl`. El video de 2 minutos y el registro de la entrega están en la plataforma del
evento.

## Los tres comandos

Estos tres resuelven su configuración solos: buscan en el entorno y, si no está, en el `.env` del
repositorio, que está en `.gitignore` y no se commitea. El API y el CLI no hacen eso: leen
`DATABASE_URL` y `REGISTRO_PEPPER` del entorno, como se ve en *Correr local*.

| Qué | Comando |
| --- | --- |
| Medir el despliegue de punta a punta | `python3 tools/verify_deployment.py --url https://<host>` |
| Correr todas las suites | `.venv/bin/python tests/run_all.py` |
| Rejugar lo grabado contra un original vivo | `python3 explorer/diff.py <modo> [límite]` |

`tools/verify_deployment.py` es el comando del video y la evidencia de la entrega. Corre contra el
entrypoint público: nueve sondas de frontera, `POST /__reset` y los 20 pasos grabados del material en
orden, los ocho pasos donde la versión 1 y la 2 difieren, y la cobertura de la tabla de control.
Compara con la regla del juez —mismo status, las claves de un objeto en cualquier orden, los arrays en
orden, los escalares idénticos— e imprime una línea por paso. Sale con código distinto de cero si algo
no coincide, y nunca imprime el token.

`tests/run_all.py` corre diez suites y no necesita argumentos. Tres son live y usan Postgres de
verdad: con `DATABASE_URL` o `DB_URL` en el entorno corren contra esa base, y sin DSN imprimen una
línea `SKIP` y pasan. Las suites importan `psycopg2`, así que van con el venv de `requirements.txt`,
no con el `python3` del sistema.

`explorer/diff.py` es el instrumento del incidente: modos `examples`, `stateless` y `frontier`, con un
límite opcional. Rejuega lo grabado contra un original vivo. A las 20:00 los tokens de participante
dejaron de servir y ese servicio ya no contesta el contrato, así que hoy es el registro de lo que hizo
a las 15:30 y no una rutina del repositorio.

## El contrato

Todo llega a `POST /msg` con un cuerpo JSON como `{"m": "PING;64"}`. El primer campo, en mayúsculas,
elige el comando; el último es el control.

| Ruta | Respuesta |
| --- | --- |
| `GET /health` | `200 {"ok": true}` con token vigente, `401 E001` sin él |
| `POST /__reset` | `200 {"ok": true}`; sin token vigente no toca nada |
| `POST /msg` | La respuesta del comando, o su error |
| Cualquier otra ruta | `404 {"error": "E404", "mensaje": "ruta desconocida"}` |

El orden está medido, y es el que más se rompe si uno lo escribe de memoria: **la ruta se resuelve
antes del token**, así que una ruta que no existe es `E404` aunque no mandes credencial. Después el
token, después el JSON, después el comando y por último el control.

Los errores de la versión vigente:

| Código | Status | Cuándo |
| --- | --- | --- |
| `E001` | 401 | Sin token, o token ausente, ajeno, expirado o revocado |
| `E100` | 400 | El cuerpo no es JSON |
| `E102` | 400 | Falta la clave `m` |
| `E201` | 400 | Control mal formado |
| `E202` | 400 | Control inválido |
| `E301` | 400 | Comando desconocido (el mensaje termina en un espacio, y es literal) |
| `E302` | 400 | Aridad inválida |
| `E401` | 404 | Cuenta inexistente |
| `E402` | 404 | Operación inexistente |
| `E506` | 422 | Límite por operación excedido |
| `E507` | 422 | Saldo insuficiente |

El frente HTTP traduce a status y a claves JSON y no decide la regla; el dominio no conoce ni lo uno ni
lo otro. La especificación completa, comportamiento por comportamiento y con la evidencia de cada
uno, está en `docs/spec.md`; el modelo de datos y la tabla de traducción, en `docs/api.md`.

## Correr local

El proceso no guarda estado: todo vive en Postgres. Estas son las tres variables de entorno.

- `DATABASE_URL`: la cadena de conexión del libro. El alias local es `DB_URL`.
- `REGISTRO_PEPPER`: la pimienta del registro de tokens. No es un token y no se rota.
- `PORT`: el puerto del API; por defecto `8080`.

Tres pasos, en este orden.

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
psql "$DATABASE_URL" -f migrations/0001_schema.sql
```

Después, sembrar el token del equipo una sola vez:

```
.venv/bin/python -m api.cli registrar --label equipo
```

`registrar` lee el secreto por stdin, nunca por argumento, así no queda en el historial ni en `ps`, y
no se vuelve a mostrar. El proceso no necesita conocerlo: solo guarda su HMAC-SHA256 con la pimienta.
El CLI también tiene `crear`, `revocar` y `listar`, que no muestran secretos y no revocan el último
token vigente.

Y arrancar:

```
DATABASE_URL=... REGISTRO_PEPPER=... .venv/bin/python -m api.adapter
```

Las pruebas:

```
.venv/bin/python tests/run_all.py
```

## Desplegar

El `Dockerfile` instala `requirements.txt`, copia `api/` y corre con un usuario sin privilegios, sin
secreto adentro y sin herramientas de build. La imagen no ejecuta migraciones: el esquema se aplica
antes, una vez.

Dos restricciones que no se negocian:

1. Un plan que nunca suspenda el proceso: el contrato da 5 segundos por paso y un proceso dormido no
   arranca a tiempo.
2. Nunca dos escritores. Cualquier cantidad de procesos atiende contra la misma base, pero el plan no
   puede levantar una segunda instancia que escriba.

La base se alcanza por el **pooler de sesión, en el puerto 5432**, y nunca por el pooler de
transacción, en el 6543: el modo transacción rompe los candados de sesión y el estado de la conexión.

Región: el viaje medido hasta esta base es de ~0,65 s para conectar y ~0,22 s por consulta. El proceso
tiene que correr en la misma región que la base o el presupuesto de 5 segundos por paso no cierra.

## Respuesta al incidente FR-0153

A las 15:30 el proveedor desplegó una versión 2 del original **sin avisar**. No hubo anuncio y no se
supo qué cambió hasta medirlo. Se detectó rejugando lo grabado contra el servicio vivo
(`explorer/diff.py examples`), que marcó los pasos que ya no coincidían; lo que el rejuego dejó
ambiguo se cerró con sondas dirigidas.

| ID | Qué cambió | v1 | v2 | Cómo se detectó |
| --- | --- | --- | --- | --- |
| CAM-1 | El saldo pasó de entero en centavos a texto decimal en unidades, con dos decimales | `{"saldo": 250000}` | `{"saldo": "2500.00"}` | el rejuego de los 20 ejemplos; después un depósito con centavos reales |
| CAM-2 | El tope por transferencia bajó a 50000. Sigue siendo por operación y no se aplica a los depósitos | 100000 aceptado | 50000 aceptado, 50001 → `E506` | búsqueda binaria del tope resolviendo el control de cada monto, y tres transferencias iguales seguidas |
| CAM-3 | Anular una operación inexistente pasó a tener respuesta propia | `{"ok": false, "motivo": "ya anulada"}` | `404 {"error": "E402"}` | apareció en el rejuego: la transferencia rechazada dejó a `OP-0003` sin existir |

Qué se hizo: se cambiaron las tres reglas en el dominio y nada más. El frente HTTP, el contrato, el
orden de los errores y la tabla de traducción no se tocaron, porque el rejuego confirmó que siguen
igual (48 sondas de frontera y las diez filas de errores). Hay un efecto en cascada que también se
corrigió: con el tope nuevo, un `OP-` que antes existía ya no existe, así que el contador queda uno
atrás y dos respuestas cambian por eso y no por una regla propia.

Los tres cambios quedaron registrados en `experimentos.jsonl` y en `docs/spec.md` §6, cada uno con su
`id` de evidencia y su confianza. La prueba de que la adaptación está puesta es el bloque 3 de
`tools/verify_deployment.py`, que rejuega los ocho pasos donde las dos versiones difieren y exige la
respuesta de la versión 2 en los ocho.

Nota del final del día: alrededor de las 19:00 el original dejó de contestar el contrato y responde
`404 E404` a cualquier ruta, con token o sin él. No es rate-limit, y no se registró como un cuarto
cambio porque parece apagado del servicio y no comportamiento nuevo: el reemplazo se evalúa contra la
versión vigente a las 20:00 y nuestro servicio no lo llama nunca. Por eso la verificación de fidelidad
rejuega grabaciones y no compara en vivo contra el original.

## Estructura

```
reto-2/
  api/
    domain/          el pipeline y los comandos, sin I/O y sin SQL
    adapter.py       la frontera: rutas, auth, catch-all, /health, /__reset
    ledger.py        el único que escribe SQL
    registry.py      los tokens: HMAC-SHA256 con la pimienta del entorno
    serializer.py    status y cuerpo exactos
    cli.py           crear, registrar, revocar, listar
    control_table.json  los 76 payloads medidos contra el oráculo
  migrations/        las cuatro tablas
  explorer/          el sondeo: cliente, bitácora, corrida y diff
  tests/             una rama por componente, con un solo punto de entrada
  tools/             la verificación del despliegue y el constructor del PDF
  docs/              diseño, especificación, API, explorador y los PDF entregados
  material/          lo que entregó el evento
```

Hay dos procesos y no se importan entre ellos. El **explorador** descubre y vigila; escribe
`experimentos.jsonl` y avisa si el original cambia, y no despliega el API. El **API** sirve el
contrato, no llama al original y no corre el explorador. El dominio es código compartido: lo usa el
API, y no sabe de HTTP, ni de SQL, ni hace I/O.

## Documentación

| Archivo | Qué es |
| --- | --- |
| `docs/spec.md` | La especificación entregada: cada comportamiento con su evidencia y su confianza |
| `experimentos.jsonl` | El log entregado: una línea por request al original, con la hipótesis previa |
| `docs/design.md` | El diseño de sistemas y sus decisiones, con la alternativa descartada de cada una |
| `docs/api.md` | El modelo de datos, el pipeline y la traducción a status y cuerpos |
| `docs/consolidated.md` | El detalle operativo: qué quedó cerrado y qué quedó supuesto |
| `docs/explorer.md` | El explorador, que no forma parte del diseño entregado |
| `docs/diseño_caja_negra.pdf`, `docs/especificacion_servicio.pdf` | Los documentos entregados, en PDF |

El PDF se reconstruye desde `docs/design.md` con `tools/build_pdf.py`, que necesita `reportlab` en su
propio venv: `python3 -m venv .venv-pdf && .venv-pdf/bin/pip install reportlab && .venv-pdf/bin/python
tools/build_pdf.py`.

## Límites conocidos

Están declarados como riesgos, con su costo y su cierre propuesto, en `docs/spec.md` §7. Los tres que
importan:

- **La fórmula del control no se dedujo.** Se midió que es determinista, función pura del mensaje,
  insuficiente a mayúsculas y dependiente del orden de los caracteres, y se descartaron unas treinta
  familias de checksum. Lo que el reemplazo usa es una tabla de 76 payloads medidos contra el oráculo,
  71 con control aceptado; un control inválido que no esté en la tabla se acepta, y ese `E202`
  esperado no llega.
- **La falla interna del original no se sondeó.** Cuando la propia base no responde, el reemplazo
  contesta `503 {"error": "no-disponible"}`, y ese cuerpo es un supuesto nuestro.
- **No hay límite de tasa propio.** El original admite 10 requests por segundo y 8000 por día por
  token y marca `X-Frontier-Infra` al pasarse; un cliente nuevo no debería heredar esa ausencia.
