# Reemplazo de la caja negra

Servicio propio que responde el mismo contrato que el original, sin llamarlo ni llamar a otro reemplazo. El estado vive en Postgres, no en el proceso.

El material del evento está en `material/`.

Las claves del material viven en `reto-2/.env`, que está en `.gitignore`: no se commitean. `BLACK_BOX_TOKEN` es solo para explorar el original. El token del equipo se registra una vez contra el servicio y no vuelve a la configuración.

El servicio desplegado necesita dos variables de entorno: `DATABASE_URL` y la pimienta del registro.

El diseño está en `docs/design.md`. El detalle operativo está en `docs/consolidated.md` y el modelo de datos en `docs/api.md`.
