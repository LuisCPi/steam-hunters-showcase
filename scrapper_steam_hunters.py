import json
import os
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright

STEAM_ID    = None  # se resuelve en tiempo de ejecución, ver resolve_steam_id()
STEAM_BASE  = "https://api.steampowered.com"
SH_BASE     = "https://steamhunters.com/api"
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steam_config.json")
BROWSER_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steam_browser_profile")
LOGIN_TIMEOUT_MS    = 5 * 60 * 1000  # 5 minutos para completar login/Steam Guard manualmente

ACHIEVE_KEY  = "17_0"  # Expositor de logros
MAX_WORKERS  = 10
REQ_TIMEOUT  = 15

DEFAULT_ACHIEVEMENT_SLOTS = 21
DEFAULT_COMPLETIST_SLOTS  = 6

# Sesión compartida: reutiliza conexiones TCP/TLS entre los workers en vez de
# abrir una nueva por request (grandes ahorros con cientos de llamadas).
SESSION = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS * 2)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

SHOWCASE_NAMES = {
    "1": "Expositor de logros más infrecuentes", "2": "Colección de juegos",
    "3": "Expositor de artículos", "4": "Artículos para intercambiar",
    "5": "Colección de insignias", "6": "Juego favorito",
    "7": "Expositor de capturas", "8": "Casilla de información personalizada",
    "9": "Grupo favorito", "10": "Expositor de reseñas", "11": "Expositor de Workshop",
    "12": "Mi expositor de Workshop", "13": "Expositor de material gráfico",
    "14": "Expositor de videos", "15": "Guía favorita", "16": "Mis guías",
    "17": "Expositor de logros", "20": "Estadísticas del Salien",
    "21": "Expositor de premios", "22": "Expositor de material gráfico destacado",
    "23": "Expositor de completista", "24": "Resumen de Steam (Replay del año)",
}

# ─── Utilidades Steam API ───────────────────────────────────────────────────

def get_owned_games(api_key):
    r = SESSION.get(f"{STEAM_BASE}/IPlayerService/GetOwnedGames/v1/", params={
        "key": api_key, "steamid": STEAM_ID,
        "include_appinfo": True, "include_played_free_games": True,
    }, timeout=REQ_TIMEOUT)
    return r.json().get("response", {}).get("games", [])


def get_player_achievements(api_key, appid):
    r = SESSION.get(f"{STEAM_BASE}/ISteamUserStats/GetPlayerAchievements/v1/", params={
        "key": api_key, "steamid": STEAM_ID, "appid": appid, "l": "spanish",
    }, timeout=REQ_TIMEOUT)
    data = r.json().get("playerstats", {})
    if not data.get("success"):
        return []
    return data.get("achievements", [])


def refresh_steamhunters():
    """Abre el perfil de SteamHunters para que el usuario lo sincronice manualmente."""
    import webbrowser
    print("\n🔄 Abriendo tu perfil de SteamHunters...")
    print("   → Busca el botón 'Update' dentro de la página y haz click en él")
    print("   → Espera a que la tabla de juegos termine de actualizarse")
    webbrowser.open(f"https://steamhunters.com/profiles/{STEAM_ID}")
    input("   → Cuando termine, presiona Enter aquí para continuar...")


def get_sh_percentages(appid):
    """Retorna dict {apiName: localPercentage} desde SteamHunters."""
    r = SESSION.get(f"{SH_BASE}/apps/{appid}/achievements", timeout=REQ_TIMEOUT)
    if r.status_code != 200:
        return {}
    return {a["apiName"]: a["localPercentage"] for a in r.json()}


def get_sh_completion_pct(appid):
    """Retorna el PlayersCompletion% de SteamHunters para un juego."""
    r = SESSION.get(f"{SH_BASE}/apps/{appid}", timeout=REQ_TIMEOUT)
    if r.status_code != 200:
        return None
    data = r.json()
    completed  = data.get("playersCompletedCount", 0)
    qualified  = data.get("playersQualifiedCount", 0)
    if not qualified:
        return None
    return (completed / qualified) * 100


def get_statid_bit_map(api_key, appid, cookies):
    """Retorna dict {apiname: 'statid_bit'} para los logros desbloqueados del juego."""
    r = SESSION.get(
        f"https://steamcommunity.com/profiles/{STEAM_ID}/ajaxgetachievementsforgame/{appid}",
        cookies=cookies,
        headers={"Referer": f"https://steamcommunity.com/profiles/{STEAM_ID}/edit/showcases"},
        timeout=REQ_TIMEOUT,
    )
    if r.status_code in (401, 403):
        handle_auth_error()
    if r.status_code != 200 or not r.text.strip():
        return {}

    name_en_to_title = {}
    soup = BeautifulSoup(r.text, "html.parser")
    for item in soup.find_all("div", class_="achievement_list_item"):
        statid = item.get("data-statid")
        bit    = item.get("data-bit")
        b_tag  = item.find("b")
        if statid and bit and b_tag:
            name_en_to_title[b_tag.get_text(strip=True)] = f"{statid}_{bit}"

    schema_r = SESSION.get(f"{STEAM_BASE}/ISteamUserStats/GetSchemaForGame/v2/",
                            params={"key": api_key, "appid": appid, "l": "english"},
                            timeout=REQ_TIMEOUT)
    schema_ach = schema_r.json().get("game", {}).get("availableGameStats", {}).get("achievements", [])
    apiname_to_en = {a["name"]: a.get("displayName", "") for a in schema_ach}

    return {
        apiname: name_en_to_title[en]
        for apiname, en in apiname_to_en.items()
        if en in name_en_to_title
    }


def resolve_achievement_entry(api_key, cookies, logro):
    """Resuelve el (appid, title) interno de Steam para un logro, o None si no se encontró."""
    appid, apiname, nombre, juego, pct = logro
    statid_map = get_statid_bit_map(api_key, appid, cookies)
    title = statid_map.get(apiname)
    if not title:
        return None
    return (appid, title)

# ─── Config local ───────────────────────────────────────────────────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def resolve_steam_id(config):
    """Resuelve el SteamID64 desde env var STEAM_ID, config guardada, o lo pide una vez."""
    global STEAM_ID
    STEAM_ID = os.environ.get("STEAM_ID") or config.get("steam_id")
    if not STEAM_ID:
        print("\n👤 No se encontró tu SteamID64 (el número de 17 dígitos de tu perfil).")
        print("   Lo encuentras en steamcommunity.com/my/ → la URL termina en /profiles/<tu número>")
        STEAM_ID = input("Ingresa tu SteamID64: ").strip()

    if config.get("steam_id") != STEAM_ID:
        config["steam_id"] = STEAM_ID
        save_config(config)
    return config


def _ask_int(prompt, default):
    raw = input(f"{prompt} [{default}]: ").strip()
    return int(raw) if raw else default


def get_or_ask_setup(config):
    """
    Pide, una sola vez, cuántos slots tiene habilitados cada expositor que
    maneja este script (depende del nivel de Steam). El resto de expositores
    activos en el perfil (cuáles son, año de Replay, etc.) se detectan en
    vivo desde Steam cada vez — ver get_live_showcase_state() — así que no
    hace falta preguntarlos ni mantenerlos actualizados a mano.
    """
    if "achievement_slots" in config:
        return config

    print("\n⚙️  Primera configuración: ¿cuántos slots tienes habilitados en cada expositor?")
    print("   (Steam → Editar perfil → Expositores — la cantidad depende de tu nivel de Steam)")
    achievement_slots = _ask_int("  Slots del expositor de LOGROS (tipo 17)", DEFAULT_ACHIEVEMENT_SLOTS)
    completist_slots  = _ask_int("  Slots del expositor de COMPLETISTA (tipo 23)", DEFAULT_COMPLETIST_SLOTS)

    config["achievement_slots"] = achievement_slots
    config["completist_slots"]  = completist_slots
    save_config(config)
    print(f"  ✅ Guardado en {CONFIG_FILE}")
    return config

# ─── Lógica de datos ────────────────────────────────────────────────────────

def process_game_achievements(api_key, game):
    """Retorna el logro más raro desbloqueado de un juego, o None si no aplica."""
    appid = game["appid"]
    name  = game.get("name", f"App {appid}")
    achievements = get_player_achievements(api_key, appid)
    unlocked = [a for a in achievements if a.get("achieved") == 1]
    if not unlocked:
        return None
    sh_pct = get_sh_percentages(appid)
    if not sh_pct:
        return None
    logros_con_pct = [
        (a["apiname"], a.get("name", a["apiname"]), sh_pct[a["apiname"]])
        for a in unlocked if a["apiname"] in sh_pct
    ]
    if not logros_con_pct:
        return None
    rarest = min(logros_con_pct, key=lambda x: x[2])
    return (appid, rarest[0], rarest[1], name, rarest[2])


def get_rarest_achievements_per_game(api_key, max_logros=DEFAULT_ACHIEVEMENT_SLOTS):
    """Retorna top N logros más raros (uno por juego) por localPercentage de SteamHunters."""
    print("Obteniendo juegos...")
    games = get_owned_games(api_key)
    games_con_logros = [g for g in games if g.get("has_community_visible_stats")]
    most_rare = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_game_achievements, api_key, g): g for g in games_con_logros}
        for future in tqdm(as_completed(futures), total=len(futures), desc="Procesando juegos", unit="juego"):
            result = future.result()
            if result:
                most_rare.append(result)

    return sorted(most_rare, key=lambda x: x[4])[:max_logros]


def check_game_completed(api_key, game):
    """Verifica si un juego está 100% completado. Retorna (appid, name, url) o None."""
    appid = game["appid"]
    achievements = get_player_achievements(api_key, appid)
    if not achievements:
        return None
    if not all(a.get("achieved") == 1 for a in achievements):
        return None
    return (appid, game.get("name", f"App {appid}"), f"https://store.steampowered.com/app/{appid}/")


def get_rarest_completed_games(api_key, max_games=DEFAULT_COMPLETIST_SLOTS):
    """Retorna top N juegos 100% completados más raros por PlayersCompletion% de SteamHunters."""
    print("Obteniendo juegos con logros...")
    games = get_owned_games(api_key)
    games_con_logros = [g for g in games if g.get("has_community_visible_stats")]
    print(f"  {len(games_con_logros)} juegos con logros encontrados. Verificando completados en paralelo...")

    # Verificar completados en paralelo (más rápido que secuencial)
    completed_base = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_game_completed, api_key, g): g for g in games_con_logros}
        for future in tqdm(as_completed(futures), total=len(futures), desc="Verificando", unit="juego"):
            result = future.result()
            if result:
                completed_base.append(result)

    print(f"  {len(completed_base)} juegos completados. Obteniendo porcentajes de SteamHunters...")

    # Obtener completion% de SteamHunters solo para los juegos completados (en paralelo)
    completed = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(get_sh_completion_pct, appid): (appid, name, game_url)
                   for appid, name, game_url in completed_base}
        for future in tqdm(as_completed(futures), total=len(futures), desc="Consultando SteamHunters", unit="juego"):
            appid, name, game_url = futures[future]
            pct = future.result()
            if pct is not None:
                completed.append((appid, name, pct, game_url))

    sorted_completed = sorted(completed, key=lambda x: x[2])
    return sorted_completed[:max_games], sorted_completed

# ─── Envío a Steam ──────────────────────────────────────────────────────────

def _check_response(r, label):
    if r.status_code == 200:
        try:
            resp = r.json()
            if resp.get("success") == 1:
                print(f"  ✅ {label} actualizado correctamente.")
            else:
                print(f"  ⚠️ {label}: {resp}")
        except Exception:
            print(f"  ⚠️ {label}: respuesta no JSON — {r.text[:200]}")
    elif r.status_code in (401, 403):
        handle_auth_error()
    else:
        print(f"  ❌ {label}: HTTP {r.status_code} — {r.text[:200]}")


def get_live_showcase_state(cookies):
    """
    Lee desde Steam (no desde config local) qué expositores están activos
    en el perfil ahora mismo, y el año configurado en el expositor de Replay
    si existe. La página de edición renderiza un <select name="profile_showcase[]">
    por cada slot, con la opción actualmente elegida marcada con 'selected' —
    esto evita que, al reenviar la lista completa de expositores, se desactive
    uno que el script no conocía (por ejemplo uno agregado o cambiado después
    de la primera configuración).
    """
    r = SESSION.get(
        f"https://steamcommunity.com/profiles/{STEAM_ID}/edit/showcases",
        cookies=cookies, timeout=REQ_TIMEOUT,
    )
    if r.status_code in (401, 403):
        handle_auth_error()
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    def _selected_value(select_tag):
        # Cuando hay varios <option selected>, el HTML marca como realmente
        # seleccionado al ÚLTIMO con ese atributo (así lo renderiza Steam para
        # el combo de año de Replay, que deja "selected" tanto en el
        # placeholder como en el año real). Tomamos el último para que
        # coincida con lo que el navegador muestra como elegido.
        opciones_marcadas = select_tag.find_all("option", selected=True)
        if not opciones_marcadas:
            return None
        return opciones_marcadas[-1].get("value")

    activos = []
    for select in soup.find_all("select", attrs={"name": "profile_showcase[]"}):
        valor = _selected_value(select)
        if valor not in (None, "0", ""):
            activos.append(valor)

    replay_year = None
    replay_select = soup.find("select", attrs={"name": "rgShowcaseConfig[24_0][0][replay_year]"})
    if replay_select:
        valor = _selected_value(replay_select)
        if valor:
            replay_year = valor

    return activos, replay_year


def send_achievement_showcase(session_id, cookies, config, achieve_entries):
    """Envía el expositor de logros (tipo 17) preservando los demás expositores activos."""
    config["achievement_showcase"] = achieve_entries
    save_config(config)

    activos, replay_year = get_live_showcase_state(cookies)
    otros = [t for t in activos if t != "17"]
    print(f"  Expositores activos detectados: {', '.join(SHOWCASE_NAMES.get(t, t) for t in activos)}")

    payload = [
        ("sessionID", session_id),
        ("type",      "showcases"),
        ("json",      "1"),
        ("profile_showcase[]",            "17"),
        ("profile_showcase_purchaseid[]", "0"),
    ]
    for t in otros:
        payload.append(("profile_showcase[]",            t))
        payload.append(("profile_showcase_purchaseid[]", "0"))
        if t == "24" and replay_year:
            payload.append((f"rgShowcaseConfig[24_0][0][replay_year]", replay_year))
    for i, (appid, title) in enumerate(achieve_entries):
        payload.append((f"rgShowcaseConfig[{ACHIEVE_KEY}][{i}][appid]", str(appid)))
        payload.append((f"rgShowcaseConfig[{ACHIEVE_KEY}][{i}][title]", title))

    headers = {
        "Referer":      f"https://steamcommunity.com/profiles/{STEAM_ID}/edit/showcases",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    r = SESSION.post(
        f"https://steamcommunity.com/profiles/{STEAM_ID}/edit/",
        data=payload, cookies=cookies, headers=headers, timeout=REQ_TIMEOUT,
    )
    _check_response(r, "Expositor de logros")


def update_completist_showcase(session_id, cookies, appids):
    """
    Actualiza el expositor de completista (tipo 23) slot a slot
    via ajaxsetshowcaseconfig.
    """
    headers = {
        "Referer":      f"https://steamcommunity.com/profiles/{STEAM_ID}/edit/showcases",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    for slot, appid in enumerate(tqdm(appids, desc="Actualizando expositor de completista", unit="slot")):
        payload = {
            "appid":              str(appid),
            "customization_type": "23",
            "purchaseid":         "0",
            "slot":               str(slot),
            "sessionid":          session_id,
        }
        r = SESSION.post(
            f"https://steamcommunity.com/profiles/{STEAM_ID}/ajaxsetshowcaseconfig",
            data=payload, cookies=cookies, headers=headers, timeout=REQ_TIMEOUT,
        )
        if r.status_code in (401, 403):
            handle_auth_error()

# ─── Cookies helper ─────────────────────────────────────────────────────────

def _extract_session_cookies(context):
    cookies = {c["name"]: c["value"] for c in context.cookies("https://steamcommunity.com")}
    return cookies.get("sessionid"), cookies.get("steamLoginSecure")


def get_cookies():
    """
    Obtiene sessionid/steamLoginSecure desde un perfil de Chromium persistente
    (vía Playwright). Si el perfil ya tiene una sesión válida, no requiere
    interacción; si no, abre una ventana visible para que el usuario inicie
    sesión (incluyendo Steam Guard) una única vez — el perfil queda guardado
    en disco para las próximas ejecuciones.
    """
    target_url = f"https://steamcommunity.com/profiles/{STEAM_ID}/edit/showcases"

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(BROWSER_PROFILE_DIR, headless=True)
        page = context.new_page()
        page.goto(target_url, wait_until="domcontentloaded")
        logged_in = "login" not in page.url

        if not logged_in:
            context.close()
            print("\n🔐 No hay una sesión de Steam válida — abriendo ventana para iniciar sesión...")
            context = p.chromium.launch_persistent_context(BROWSER_PROFILE_DIR, headless=False)
            page = context.new_page()
            page.goto(target_url, wait_until="domcontentloaded")
            print("   → Inicia sesión en la ventana del navegador (incluye Steam Guard si lo pide)")
            print("   → El script continuará automáticamente al detectar el login...\n")
            page.wait_for_url(lambda url: "login" not in url, timeout=LOGIN_TIMEOUT_MS)
            print("   ✅ Sesión iniciada — perfil guardado para próximas ejecuciones.\n")

        session_id, steam_login_secure = _extract_session_cookies(context)
        context.close()

    if not session_id or not steam_login_secure:
        raise RuntimeError("No se pudieron obtener las cookies de sesión desde el navegador.")

    return session_id, {"sessionid": session_id, "steamLoginSecure": steam_login_secure}


def handle_auth_error():
    """Notifica que la sesión de Steam expiró a mitad de ejecución."""
    print("\n❌ Error de autenticación con Steam — tu sesión expiró.")
    print("   Vuelve a ejecutar el script: se abrirá el navegador para reiniciar sesión automáticamente.\n")
    exit(1)

# ─── Menú principal ─────────────────────────────────────────────────────────

def main():
    api_key = os.environ.get("STEAM_API_KEY") or input("Ingresa tu Steam API Key: ").strip()

    config = load_config()
    config = resolve_steam_id(config)
    config = get_or_ask_setup(config)

    value = input(
        "Seleccione una opción:\n"
        "1.-Juegos completados más raros (actualizar expositor de completista)\n"
        "2.-Actualizar expositor de logros\n"
    )

    if value == "1":
        session_id, cookies = get_cookies()
        refresh_steamhunters()

        max_games = config.get("completist_slots", DEFAULT_COMPLETIST_SLOTS)
        top6, _ = get_rarest_completed_games(api_key, max_games=max_games)

        print(f"\n🏆 Top {len(top6)} juegos completados más raros para el expositor:")
        for i, (appid, name, pct, _) in enumerate(top6, 1):
            print(f"  {i}. [{pct:.4f}%] {name}")

        update_completist_showcase(session_id, cookies, [appid for appid, _, _, _ in top6])

    elif value == "2":
        session_id, cookies = get_cookies()
        refresh_steamhunters()

        max_logros = config.get("achievement_slots", DEFAULT_ACHIEVEMENT_SLOTS)
        logros = get_rarest_achievements_per_game(api_key, max_logros=max_logros)

        print("\nObteniendo identificadores internos de Steam para cada logro...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            resueltos = list(tqdm(
                executor.map(lambda l: resolve_achievement_entry(api_key, cookies, l), logros),
                total=len(logros), desc="Procesando", unit="juego",
            ))

        achieve_entries = []
        logros_subidos  = []
        for logro, entry in zip(logros, resueltos):
            appid, apiname, nombre, juego, pct = logro
            if not entry:
                print(f"  ⚠️ No se encontró '{nombre}' ({apiname}) en {juego} — omitido")
                continue
            achieve_entries.append(entry)
            logros_subidos.append(logro)

        print(f"\n🏆 Top {len(achieve_entries)} logros listos para subir:")
        for i, ((appid, title), (_, _, nombre, juego, pct)) in enumerate(
                zip(achieve_entries, logros_subidos), 1):
            print(f"  {i:2}. [{pct:.4f}%] {nombre} — {juego}")

        print("\nActualizando expositor de logros...")
        send_achievement_showcase(session_id, cookies, config, achieve_entries)

    else:
        print("Ingrese un valor válido")


if __name__ == "__main__":
    main()
