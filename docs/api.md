# API

Servicio con URL pública. Traduce el contrato. No decide la regla, no llama al original y no corre el explorador. No guarda estado en su proceso: el estado vive en la base y el libro es el único que escribe SQL.

## Cliente

Un token es un cliente. Un solo libro para todos queda descartado.

Lo que es de otro no se lee y no se modifica. El cliente no manda su id. Sale del token. La clave de una cuenta es el cliente más el id. `AC-0001` puede existir en dos clientes y no es la misma fila. Si pide una cuenta ajena, para él no existe.

El reset, en una transacción, borra las cuentas y las operaciones de ese `cliente_id` y devuelve sus contadores a 1. No trunca: borraría a los demás. El contador es por cliente, no una secuencia global. Sin token vigente no se toca nada. Es una palanca de operación, no una licencia para no persistir.

## Registro

El registro es la fuente de verdad de las credenciales y vive en la tabla `tokens`, en la misma base. El secreto no se guarda: se guarda HMAC-SHA256 con una pimienta que vive en el entorno, no es un token y no se rota. Vigente es no revocado y con `expires_at` en el futuro, en UTC; expirado o revocado se trata como ajeno. No hay caché. La fila de `tokens` es el cliente.

El motivo sale del sistema, no del contrato: una empresa necesita revocar y rotar credenciales sin desplegar y saber quién creó cada una. La administración es un CLI, no una ruta: `crear`, `registrar`, `revocar` y `listar`. `registrar` lee el secreto por stdin y sirve para el token que los clientes ya tienen; `crear` inventa uno nuevo, para pruebas nuestras; `listar` no muestra secretos. No hay ruta pública de administración. El CLI no revoca el último token vigente y la rotación se solapa: primero el nuevo, después se revoca el viejo. Si la tabla está vacía al arrancar, se puede sembrar un token desde un secreto de un solo uso; después el proceso no lo necesita.

## Frontera

Todo llega a `POST /msg`. Cualquier proceso atiende el comando del cliente autenticado porque el estado está en la base. El adaptador no escribe SQL y no guarda estado en su proceso.

Adentro, el primer campo de `m`, pasado a mayúsculas, elige la ruta. Una tabla lo manda al dominio: `PING`, `ALTA`, `CONSULTA`, `DEPOSITO`, `TRANSFER`, `ANULA`. Un comando ausente no es un 404 del framework.

El dominio corre dentro de una transacción por request y la respuesta sale después del commit. Se descarta una URL por operación. Sería una ruta que el contrato no tiene.

## Pipeline

1. Se clasifica la ruta, antes del JSON y del token. Puertas: `GET /health`, `POST /__reset`, `POST /msg`. El resto es ruta desconocida.
2. `/health` no exige token. Responde 200 cuando el proceso está listo, con el cuerpo fiel al original, y no hace un viaje de ida y vuelta por request. La disposición con la base se expone en un puerto interno de administración, nunca en una ruta nueva del contrato.
3. Se busca el token: se calcula el HMAC-SHA256 con la pimienta y se busca en `tokens`. Ausente, expirado o revocado es ajeno. No hay 401 nuestro.
4. `__reset`, en una transacción y con token vigente, borra las cuentas y las operaciones de ese `cliente_id` y devuelve sus contadores a 1. Sin ese token, no se toca nada.
5. `/msg` sin token vigente no aplica el mensaje.
6. `/msg` con token vigente parsea el JSON. Si no es JSON, falta `m` o `m` no es texto, no se aplica.
7. Se parte `m` por `;`. El primer campo, en mayúsculas, elige el comando. El último es el control.
8. El dominio corre dentro de una transacción por request, bajo el candado del cliente, y devuelve un resultado, no un HTTP.
9. El libro confirma la transacción y el adaptador escribe el status y el cuerpo. La respuesta sale después del commit.

## Resultado

La tabla vive en el adaptador, en código. No en la base. El dominio no conoce status ni claves JSON.

Lo visto en una secuencia. No es el catálogo cerrado.

| Resultado | Status | Cuerpo |
| --- | --- | --- |
| Pong | 200 | `{"pong": true}` |
| CuentaCreada(id) | 201 | `{"cuentaId": id}` |
| Saldo(id, monto) | 200 | `{"saldo": monto, "cuenta_id": id}` |
| OperacionAceptada(id) | 200 | `{"status": "recieved", "operacionId": id}` |
| Anulada(id) | 200 | `{"ok": true, "anulada": id}` |
| YaAnulada | 200 | `{"ok": false, "motivo": "ya anulada"}` |
| ControlMalFormado | 400 | `{"error": "E201", "mensaje": "control mal formado"}` |
| ControlInvalido | 400 | `{"error": "E202", "mensaje": "control inválido"}` |
| ComandoDesconocido | 400 | `{"error": "E301", "mensaje": "comando desconocido "}` |
| AridadInvalida | 400 | `{"error": "E302", "mensaje": "aridad inválida"}` |
| CuentaInexistente | 404 | `{"error": "E401", "mensaje": "cuenta inexistente"}` |
| LimiteExcedido | 422 | `{"error": "E506", "mensaje": "límite excedido"}` |
| SaldoInsuficiente | 422 | `{"error": "E507", "mensaje": "saldo insuficiente"}` |

Una variante nueva entra por PR. El monto es número. El id es texto. `"recieved"` no se corrige.

`/health`, el reset, el token ajeno, el JSON roto y la ruta desconocida no son resultados del dominio.

## Candado

Candado por cliente, no global. Dos clientes distintos operan en paralelo; dos movimientos del mismo cliente no se pisan.

Se consideraron `pg_advisory_xact_lock` sobre el cliente y `SELECT … FOR UPDATE` sobre su fila de `contadores` y sus cuentas en orden determinista de `cuenta_id`. Se elige el advisory: toma un solo candado con la identidad del cliente, no exige que la fila de `contadores` exista todavía y elimina el deadlock por orden de filas. No hay lock global ni lock de filas sin orden, y no se usa `SERIALIZABLE` para cada lectura.

`PING` y la consulta no toman el candado de escritura: ven lo ya confirmado y nunca la mitad de un depósito. El token se relee dentro de la transacción, así que un token revocado a mitad de request no confirma. La espera del candado corta a los 4 segundos, porque el cliente abandona a los 5. No se reintenta un `POST /msg`: un reintento puede depositar dos veces.

## Fallas nuestras

Si no hubo commit y la base no responde o el candado no sale, la respuesta es 503 con `{"error":"no-disponible"}`. Es un supuesto, no un hecho: no vimos la falla interna del original y no se le inventa un cuerpo. No se usa un error de negocio ni un 200.

La espera del candado corta a los 4 segundos y cada paso tiene 5.

## Escala

El estado no está en el proceso, así que sumar instancias no parte el estado: cualquier proceso atiende cualquier comando del cliente autenticado porque el estado está en la base. El crecimiento va por pasos: primero más procesos, después un pool de conexiones, después una réplica de solo lectura para lo que tolera atraso; el saldo se queda en el primario. Un standby que se promueve para sobrevivir a la caída de una región. Nunca dos escritores. No hay caché de saldos ni de tokens.

## Deploy

La URL pública es la de este servicio, en una plataforma que lo mantenga despierto y que no levante dos escritores. No es un job. No es la laptop. El proceso arranca dentro del presupuesto de 5 segundos por paso.

En el entorno van `DATABASE_URL` y la pimienta del registro. El token del equipo se registra una vez y no vuelve a la configuración.

La base es Postgres gestionado: concretamente Supabase, portable por DSN. La conexión va por sesión o directa, nunca por el pooler en modo transacción. Se conecta con el rol de servidor, con RLS activo en las cuatro tablas y sin políticas para `anon`. No se usan Auth, Edge Functions ni Data API. La región va cerca del despliegue del API.

## Rastro

Una línea por request en la salida del proceso. Sobrevive al reset.

Lleva hora UTC, id nuestro, ruta, estado del token, id de la fila del token, comando, resultado, si hubo commit, duración y clase de falla. No lleva `Authorization`, secreto, pimienta, cadena de conexión ni el `m` crudo. En una operación real esa salida va a un colector con retención.

## Estructura

```
reto-2/
  api/
    domain/          pipeline y comandos, sin I/O y sin SQL
    adapter.py       frontera: rutas, auth, catch-all, /health, /__reset
    ledger.py        el único que escribe SQL
    registry.py      tokens
    serializer.py    status y cuerpo exactos
    cli.py           crear, registrar, revocar, listar
  migrations/        las cuatro tablas y las políticas de RLS
  explorer/          el sondeo: cliente, bitácora, corrida
  tests/
    api/             rejuego de los 20 ejemplos y comprobaciones puntuales
    explorer/        arnés del sondeo, con transporte falso y sin red
  docs/
```

`api/ledger.py` es el único que escribe SQL; `api/registry.py` guarda los tokens del registro.

## Supuestos de este servicio

No son reglas.

- Cuerpo de una ruta desconocida y de `método no permitido`, `tipo de contenido`, `JSON malformado`, `falta del campo m`, `barra final`, `HEAD`, `OPTIONS` y `GET` sobre `/msg`.
- `/health` sin token y el cuerpo de `/health`.
- Cuerpo de `__reset`, con token vigente y sin él.
- Cuerpo de `/msg` sin token vigente. Token ausente o inválido: la respuesta del original.
- JSON roto, `m` ausente o `m` que no es texto.
- Orden entre las etapas del pipeline y entre aridad, control y comando.
- Fórmula del control.
- Si el tope es por operación o acumulado.
- Que el comando no distinga mayúsculas. `CONSULTa` se vio una vez: probable.
- 503 cuando fallamos nosotros.
- Que `ANULA` devuelva el saldo al revertir.

## Tablas

Cuatro tablas en Postgres. El token no es una columna suelta en las cuentas: la fila de `tokens` es el cliente.

| Tabla | Clave | Columnas | Notas |
| --- | --- | --- | --- |
| `tokens` | `id` | `token_hash` único, `etiqueta`, `created_at`, `created_by`, `expires_at`, `revoked_at`, `revoked_by` | No se guarda el secreto. |
| `cuentas` | `(cliente_id, cuenta_id)` | `saldo` entero, `nombre` | `cliente_id` apunta a `tokens.id`. La unicidad de `nombre` no está confirmada. |
| `operaciones` | `(cliente_id, operacion_id)` | tipo, monto, cuenta debitada, cuenta acreditada, `anulada` | Un depósito llena solo la acreditada; una transferencia, las dos. Lo que falla no se guarda. |
| `contadores` | `cliente_id` | `siguiente_cuenta`, `siguiente_operacion`, ambas en 1 | Una fila por cliente. No es una secuencia global. |

La clave de una cuenta es el cliente más el id: `AC-0001` puede existir en dos clientes y no es la misma fila. La condición `cliente_id` va en cada consulta y cada escritura. El reset, en una transacción, borra las cuentas y las operaciones de ese `cliente_id` y devuelve sus contadores a 1; no trunca, porque borraría a los demás. Sin token vigente no se toca nada.

## Decisiones

Cada decisión está contrastada con la alternativa que se descartó y por qué.

| Decisión | Se descarta | Por qué |
| --- | --- | --- |
| Estado en Postgres, no en el proceso. | El mapa en memoria. | Los saldos son plata de clientes reales, un reinicio no puede perderlos, y con el estado en el proceso una segunda instancia atendería con otro estado. |
| Candado por cliente. | El lock global y el lock de filas sin orden. | Dos movimientos del mismo cliente no pueden leer el mismo saldo, y un lock global bloquea a los demás clientes. |
| Responder después del commit. | Responder antes de confirmar. | Un 200 sobre una transacción que después falla es plata que el cliente cree que movió. |
| Registro de tokens en la base con secreto hasheado. | La lista de tokens en una variable de entorno. | Una empresa necesita revocar y rotar sin desplegar y sin guardar el secreto. |
| CLI de administración. | Una ruta pública de administración. | El contrato no admite rutas nuevas y administrar credenciales por HTTP amplía la superficie de ataque. |
| Un token, un cliente. | Un libro para todos. | Un cliente no puede leer ni modificar a otro. |
| Reset que borra solo las filas de ese `cliente_id` y devuelve sus contadores a 1. | El truncado. | Truncar borraría a los demás clientes. Los contadores son por cliente. |
| El dominio devuelve un resultado y el serializador escribe el JSON. | Que el dominio arme el HTTP. | Alguien corregiría la rareza del cable. |
| Catch-all HTTP propio. | Los defaults del framework. | Los errores de ruta, método y cuerpo deben ser los del original, carácter por carácter. |
| Pipeline con orden configurable. | El orden fijo en el código. | El orden real de los errores no está verificado y puede cambiar. |
| Fidelidad del contrato en `/health`. | Salud con dependencias en una ruta nueva. | El contrato admite una sola ruta de salud y su cuerpo debe ser el del original; la disposición con la base se expone en un puerto interno. |
| Saldos leídos de la base, sin caché. | La caché de saldos. | Un saldo viejo es plata mentida. |
| Conectar directo o por sesión. | El pooler en transaction mode. | Rompe los candados de sesión y el estado de la conexión. |
| Conexión desde el rol de servidor con RLS activo y sin políticas para `anon`. | Exponer la Data API. | El acceso directo de un cliente anónimo a las tablas del libro es una fuga entre clientes. |
| Contadores AC- y OP- que avanzan solo tras un éxito. | Consumir el id aunque la operación falle. | Los ejemplos muestran que un error no consume id. Es probable hasta repetirlo. |
| Operaciones que guardan lo necesario para revertirse. | Anular solo marcando la operación, sin tocar saldos. | Un rechazo por saldo insuficiente en los ejemplos solo tiene sentido si la anulación devolvió el saldo. Es probable hasta repetirlo. |

## No es de este componente

- La fórmula del control, el tipo de tope y el orden de los errores. Son reglas del dominio, y todavía no están confirmadas.
- Cómo se descubren las reglas. Eso es el explorador.
