# Explorador

Componente cerrado. El PDF de diseño entregado no define este componente. No comparte proceso, URL ni estado con el API, y no lo importa ni lo llama.

## Propósito

Descubrir las reglas del original y detectar si cambian. No las implementa. No despliega el reemplazo.

## Modos

Exploración. Se lanza a mano, una vez, con la semilla. Cada plan es una secuencia: `POST /__reset` y después los requests en orden.

Vigilancia. Corre sola durante el día. Repite un corpus fijo. Si el cuerpo cambia, si aparece `X-Frontier-Infra`, o si el original no responde, avisa y para. No reescribe reglas.

## Quién define las pruebas

La semilla la escribimos nosotros. Sale de los agujeros de los 20 ejemplos: control, tope, orden de errores, estado y bordes.

Las sondas siguientes las propone un asistente opcional. Tiene que nombrar las dos explicaciones que siguen vivas y el request que las separa. Si no puede nombrar dos, no hay fila. Su salida no es evidencia.

El script no acepta un request suelto. Una fila vista una vez es probable. Confirmada, solo si se repite. Lo no probado no lo afirma este componente.

## Reglas de corrida

Usa el token personal, en `BLACK_BOX_TOKEN`. No usa el token del equipo.

La hipótesis se escribe antes del request. La conclusión, después. Toda request va a `experimentos.jsonl`, también las de reserva. La reserva se distingue por el nombre de `seq`, no por un campo inventado.

Se guarda el header `X-Req`. No se interpreta. Si la respuesta trae `X-Frontier-Infra`, se anota y no se cita como evidencia.

Tope de operación: 8 requests por segundo. El día tiene 8000. Si aparece el rate-limit, se para.

Los ids del material no son evidencia. Los 20 ejemplos hay que repetirlos. La spec solo puede citar ids de este log.

## Log

Una línea JSON por request, después de un reset de esa secuencia. `id` único en todo el archivo.

| Campo | Qué es |
| --- | --- |
| `id` | Número de línea, único. La spec cita estos números. |
| `seq` | La secuencia. |
| `x_req` | Valor del header `X-Req`. |
| `hipotesis` | Qué se estaba separando. Escrita antes de mandar. |
| `request` | El cuerpo mandado. |
| `status` | El status del original. |
| `response` | El cuerpo del original. |
| `conclusion` | Qué se aprendió. Opcional. Vacía si fue infraestructura. |

## Semilla

No son hechos. Son las primeras filas.

- Control: el mismo mensaje con el número de al lado, y uno mal formado.
- Tope: en una cuenta nueva, 100000 contra 100001. Después dos transferencias que solo se pasan si el tope es acumulado.
- Orden de errores: comando desconocido con control mal formado.
- Estado: alta repetida del mismo nombre. Anular un depósito. Consultar después de un alta fallida.
- Borde: sin token, token mal, ruta que no existe, y `POST /_reset` contra `POST /__reset`.

## Vigilancia y asistente

El diff no es un error nuestro. Es un cambio del original.

Ningún modelo escribe la especificación. Un asistente opcional llama a OpenRouter con `OPENROUTER_API_KEY` y el modelo en `MODEL_ID`. Puede proponer sondas. Su salida no es evidencia y no entra a la spec sin una sonda que la repita. Una sola observación queda probable.

El aviso por ntfy es opcional y queda fuera del diseño entregado. El canal es secreto y no entra al repo. El mensaje lleva el diff, nunca un token.

Una persona redacta el cambio de spec y de dominio, y hace el merge y el redeploy del API. Un modelo no entra en ese camino.

## Despliegue

Un script local, a mano o por cron, una corrida a la vez: el token personal tiene estado. No es un servicio. Escribe el log en el repo y, si hay diff, avisa. A las 20:00 se acaba el trabajo.

No abre un puerto. Sale hacia el original y, si hay asistente, hacia OpenRouter.

Secrets, no código: `BLACK_BOX_URL` y `BLACK_BOX_TOKEN`. El script no los imprime.

La exploración se dispara a mano. La vigilancia se puede programar con cron a las 20:30 y a las 00:30 UTC. A las 20:00 de acá el token personal deja de servir contra el original.

GitHub Actions y ntfy son opcionales y quedan fuera del diseño entregado.

Cada corrida agrega líneas a `experimentos.jsonl` en el repo. No loguea el header `Authorization`.

## Estructura

```
reto-2/docs/explorer.md
reto-2/explorer/
  client.py
  log.py
  run.py
  watch.py
reto-2/tests/explorer/
  test_control.py    arnés del sondeo, con transporte falso y sin red
```

El API no importa esta carpeta ni la llama.

## Decisiones

| Decisión | Se descarta | Por qué |
| --- | --- | --- |
| Script propio | Un descubridor de APIs | Está prohibido, y no deja este log |
| Hipótesis antes del request | Anotar después de ver la respuesta | Una afirmación falsa en la spec resta |
| Script aparte del API | Librería compartida con el reemplazo | El reemplazo no puede llamar al original |
| Sondas que separan dos explicaciones | Fuzzer ciego | 8000 por día, y un request suelto no extrae una regla |
| Semilla escrita por nosotros | Semilla inventada por el agente | Los agujeros ya están en los ejemplos |
| Sondas propuestas por un asistente | Fijar cada sonda a mano | La sonda que la repite es la que afirma |
| OpenRouter con key (opcional) | La sesión de pi | Un script no tiene una sesión interactiva |
| ntfy (opcional) | Correo, Slack o un log | A las 15:30 nadie está mirando un archivo |
| El cambio de spec lo redacta una persona | Que un modelo escriba la spec | Una sonda mala no entra a producción |
| Un script que escribe el repo y termina | Un servicio siempre encendido en la laptop, Railway o Fly | El trabajo es una corrida: sonda, log, y a veces un aviso. No atiende tráfico. Después de las 20:00 no tiene a quién mirar. Un servicio queda despierto sin tarea, y necesita otro candado y otra credencial de git para hacer lo que el repo ya puede hacer solo |
| Archivo jsonl | Una base de sondas | El entregable ya es ese archivo |

## Fuera de este componente

- Implementar las reglas.
- Commitear y mergear el cambio de spec.
- Redesplegar el reemplazo.
- Afirmar la fórmula del control o el tipo de tope antes de una sonda repetida.
