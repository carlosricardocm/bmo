# Luces de Smart Life (Tuya)

Vimo controla `lampara_cuarto_izq` y `lampara_cuarto_der` **directamente por la WiFi** con
[tinytuya](https://github.com/jasonacox/tinytuya): sin la nube de Tuya ni Claude Code, en ~0,5 s,
y siguen funcionando aunque se caiga internet. Código: `app/smart_lights.py`.

## Órdenes

| Frase | Qué hace |
|---|---|
| «prende / apaga las luces», «apaga las dos lámparas» | Las dos lámparas |
| «apaga la lámpara derecha», «prende la de la izquierda» | Solo una |
| «pon la luz al 50», «brillo al máximo», «luz al mínimo» | Brillo (10–100 %) |
| «sube / baja la luz», «más luz», «menos luz» | Brillo ± `lights.brightness_step` |
| «pon la luz roja», «cambia la lámpara izquierda a azul» | Color: rojo, naranja, amarillo, verde, cian/turquesa, azul, morado/violeta, rosa, magenta/fucsia |
| «pon la luz blanca», «luz cálida / fría / neutra» | Blanco y su temperatura |
| «modo lectura», «pon el modo fiesta», «pon la lámpara derecha en modo noche» | Modos |

Modos de serie (las escenas de la app): **noche** (buenas noches, dormir), **lectura** (leer),
**trabajo** (estudio), **ocio** (relax, descanso), **suave**, **colorido** (colores, arcoíris),
**deslumbrante** (fiesta, disco), **precioso** (hermoso, bonito), además de **blanco**, **color** y
**música**. Solo se reconocen con una referencia clara a la luz («luz», «lámpara», «foco»,
«brillo», el lado de la lámpara o «modo» + un modo conocido): «pon la canción Luz de luna» sigue
siendo música y las preguntas van a Claude.

## Modos propios

Pon las lámparas como quieras en la app y guárdalo con un nombre:

```bash
venv/bin/python tools/guardar_modo_luz.py cine                       # las dos lámparas
venv/bin/python tools/guardar_modo_luz.py cine --lampara lampara_cuarto_izq
venv/bin/python tools/guardar_modo_luz.py cine --borrar
```

Luego: «Hey Vimo, modo cine». Se guardan en `data/lights/modes.json`; un modo con el nombre de una
escena de serie la sustituye. Vimo relee el archivo solo, sin reiniciar.

## Configuración (ya hecha el 25-10-2026)

1. Proyecto *Smart Home* en https://platform.tuya.com, data center **Western America**, con la
   cuenta de Smart Life vinculada (*Devices → Link App Account*).
2. Credenciales en `data/lights/tinytuya.json` (fuera de git, permisos 600):
   `{"apiKey": "<Access ID>", "apiSecret": "<Access Secret>", "apiRegion": "us"}`.
3. `venv/bin/python tools/descubrir_luces.py` → `data/lights/devices.json` con id, `local_key`, IP
   y versión de protocolo (3.5) de cada dispositivo.

Repite el paso 3 si vuelves a emparejar una lámpara en la app (cambia su `local_key`) o si cambia
su IP; conviene **reservar la IP** de cada lámpara en el router (hoy: izq 192.168.1.67,
der 192.168.1.68). La nube solo se usa en ese paso.

Ajustes en `config.json` → `lights`: `enabled`, `timeout_seconds`, `brightness_step`, y `lamps`
(nombre en la app, cómo lo dice Vimo y apodos). Para añadir otra lámpara, añádela a `lamps` y
vuelve a ejecutar el paso 3.

## Problemas

- «No pude conectar con…»: la lámpara está apagada del interruptor, sin WiFi o cambió de IP
  (paso 3). Detalle en `logs/vimo.log` (`Luz lampara_…`).
- «Las luces aún no están configuradas»: falta `data/lights/devices.json` (paso 3).
