# Reemplazo de la caja negra

Servicio propio que responde el mismo contrato que el original, sin llamarlo ni llamar a otro reemplazo. El estado vive en Postgres, no en el proceso.

El material del evento está en `material/`.

Las claves del material viven en `.env`, que está en `.gitignore`: no se commitean. `BLACK_BOX_TOKEN` es solo para explorar el original. El token del equipo se registra una vez contra el servicio y no vuelve a la configuración.

El servicio desplegado necesita dos variables de entorno: `DATABASE_URL` y la pimienta del registro (`REGISTRO_PEPPER`); el puerto (`PORT`) es opcional y por defecto es `8080`.

El diseño está en `docs/design.md`. El detalle operativo está en `docs/consolidated.md` y el modelo de datos en `docs/api.md`.

## Correr y desplegar

El proceso no guarda estado: todo vive en Postgres. Estas son las tres variables de entorno:

- `DATABASE_URL`: la cadena de conexión del libro. El alias local es `DB_URL`.
- `REGISTRO_PEPPER`: la pimienta del registro de tokens. No es un token y no se rota.
- `PORT`: el puerto del API; por defecto `8080`.

Antes de arrancar, sembrar el token del equipo una sola vez:

```
python3 -m api.cli registrar --label equipo
```

`registrar` lee el secreto por stdin, nunca por argumento, así no queda en el historial ni en `ps`, y no se vuelve a mostrar. El proceso no necesita conocerlo: solo guarda su hash.

Local: el venv de las suites necesita `psycopg2` (está pinneado en `requirements.txt`).

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
DATABASE_URL=... REGISTRO_PEPPER=... .venv/bin/python -m api.adapter
```

Contenedor: el `Dockerfile` instala `requirements.txt`, copia `api/` y corre con un usuario sin privilegios, sin secreto adentro y sin herramientas de build. La imagen no ejecuta migraciones.

Dos restricciones de despliegue que no se negocian:

1. Un plan que nunca suspenda el proceso: el contrato da 5 segundos por paso y un proceso dormido no arranca a tiempo.
2. Nunca dos escritores. Cualquier cantidad de procesos atiende contra la misma base, pero el plan no puede levantar una segunda instancia que escriba.

La base se alcanza por el **pooler de sesión, en el puerto 5432**, y nunca por el pooler de transacción, en el 6543: el modo transacción rompe los candados de sesión y el estado de la conexión.

Región: el viaje medido hasta esta base es de ~0,65 s para conectar y ~0,22 s por consulta. El proceso tiene que correr en la misma región que la base o el presupuesto de 5 segundos por paso no cierra.
