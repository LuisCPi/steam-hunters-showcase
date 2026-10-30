# Steam Hunters Showcase

Script para actualizar automáticamente dos expositores del perfil de Steam usando datos
de rareza de [SteamHunters](https://steamhunters.com):

1. **Expositor de completista** (tipo 23): sube tus N juegos 100% completados más raros
   (según `PlayersCompletion%` de SteamHunters).
2. **Expositor de logros** (tipo 17): sube, por cada juego, tu logro desbloqueado más raro
   (según `localPercentage` de SteamHunters), hasta tu cantidad de slots disponibles.

## Requisitos

- Python 3.9+
- Una [Steam Web API Key](https://steamcommunity.com/dev/apikey)
- Tu SteamID64 (lo ves en la URL de tu perfil: `steamcommunity.com/profiles/<SteamID64>`)

## Instalación

```bash
pip install -r requirements.txt
playwright install chromium
```

## Configuración

El script pide estos datos la primera vez y los recuerda en `steam_config.json`
(no se sube al repo, ver `.gitignore`):

- **Steam API Key**: puedes exportarla como variable de entorno `STEAM_API_KEY` para no
  tenerla que escribir cada vez.
- **SteamID64**: variable de entorno `STEAM_ID`, o se pregunta la primera vez.
- **Otros expositores activos** en tu perfil (para no pisarlos al actualizar el de logros).
- **Cantidad de slots** de cada expositor (depende de tu nivel de Steam — revísalo en
  *Editar perfil → Expositores*). Es importante configurar el número real: si el script
  cree que tienes menos slots de los que realmente tienes habilitados, puede dejar vacío
  un slot que ya tenías lleno manualmente.

## Sesión de Steam

La primera vez que subes algo (opción 1 o 2), se abre una ventana de navegador para que
inicies sesión en Steam (incluyendo Steam Guard si aplica). La sesión queda guardada en
`steam_browser_profile/` (tampoco se sube al repo) y las siguientes ejecuciones no vuelven
a pedir login mientras la sesión siga siendo válida.

## Uso

```bash
python scrapper_steam_hunters.py
```

Y eliges la opción 1 o 2 del menú.

## Seguridad

`steam_config.json` y `steam_browser_profile/` contienen datos de tu cuenta (configuración
personal y la sesión de navegador logueada). Están en `.gitignore` — nunca los subas a un
repositorio público.
