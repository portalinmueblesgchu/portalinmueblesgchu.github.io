"""
Scraper Universal — Gchu Propiedades
Recorre todas las agencias activas en Airtable y sincroniza sus propiedades.
- Agencias con Tokko Broker: usa la API oficial
- Agencias con Scraper: usa scraping genérico
- Agencias Manual: las omite (se cargan a mano)
"""

import os
import re
import time
import json
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from datetime import date

# ── CONFIGURACIÓN ──────────────────────────────────────────────
AIRTABLE_TOKEN    = os.environ.get("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID  = os.environ.get("AIRTABLE_BASE_ID")
TABLE_PROPIEDADES = "Propiedades"
TABLE_AGENCIAS    = "Agencias"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9",
    "Connection": "keep-alive",
}
# ───────────────────────────────────────────────────────────────


def make_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session

SESSION = make_session()


# ── AIRTABLE ───────────────────────────────────────────────────

def airtable_get(table, params=None):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{requests.utils.quote(table)}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    records, offset = [], None
    while True:
        p = dict(params or {})
        if offset:
            p["offset"] = offset
        r = requests.get(url, headers=headers, params=p, timeout=20)
        data = r.json()
        if "error" in data:
            print(f"  Error Airtable GET: {data}")
            break
        records += data.get("records", [])
        offset = data.get("offset")
        if not offset:
            break
    return records

def airtable_post(table, fields):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{requests.utils.quote(table)}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json={"fields": fields}, timeout=20)
    result = r.json()
    if "error" in result:
        print(f"  Error POST: {result['error']['message']}")
    return result

def get_existing_urls():
    records = airtable_get(TABLE_PROPIEDADES, {"fields[]": "URL original"})
    return {rec["fields"].get("URL original", "") for rec in records if "URL original" in rec["fields"]}

def get_agencias_activas():
    records = airtable_get(TABLE_AGENCIAS)
    agencias = []
    for rec in records:
        f = rec["fields"]
        if not f.get("Activa", False):
            continue
        agencias.append({
            "id": rec["id"],
            "nombre": f.get("Nombre", ""),
            "tipo": f.get("Tipo Integración", "Manual"),
            "api_key": f.get("API Key", ""),
            "url": f.get("URL Sitio", ""),
        })
    return agencias


# ── HELPERS ────────────────────────────────────────────────────

def parse_tipo(texto):
    texto_lower = texto.lower()
    tipos = [
        ("Departamento", ["departamento", "dpto", "depto"]),
        ("Chacra",       ["chacra"]),
        ("Campo",        ["campo", "rural", "estancia", "casco de estancia"]),
        ("Terreno",      ["terreno", "lote ", "loteo", "lotes"]),
        ("Local",        ["local comercial", "local "]),
        ("Oficina",      ["oficina"]),
        ("Galpón",       ["galpón", "galpon"]),
        ("Dúplex",       ["dúplex", "duplex"]),
    ]
    for tipo, keywords in tipos:
        if any(k in texto_lower for k in keywords):
            return tipo
    return "Casa"

def guardar_propiedad(fields, existing_urls, agencia_id):
    url = fields.get("URL original", "")
    if url and url in existing_urls:
        return False
    fields["Agencia"] = [agencia_id]
    fields["Tipo Publicante"] = "Agencia"
    fields["Estado"] = "Publicada"
    fields["Fecha carga"] = str(date.today())
    result = airtable_post(TABLE_PROPIEDADES, fields)
    if result.get("id"):
        if url:
            existing_urls.add(url)
        return True
    return False


# ── TOKKO BROKER API ───────────────────────────────────────────

TOKKO_OPERACIONES = {1: "Venta", 2: "Alquiler", 3: "Alquiler temporal"}
TOKKO_TIPOS = {
    1: "Casa", 2: "Departamento", 3: "Oficina", 4: "Local",
    5: "Terreno", 6: "Campo", 7: "Galpón", 13: "Chacra",
}

def scrape_tokko(agencia, existing_urls):
    """Sincroniza propiedades desde la API de Tokko Broker."""
    api_key = agencia["api_key"]
    if not api_key:
        print(f"  [!] Sin API Key para {agencia['nombre']}, omitiendo.")
        return 0

    nuevas = 0
    limit = 20
    offset = 0

    while True:
        url = (
            f"https://www.tokkobroker.com/api/v1/property/"
            f"?key={api_key}&format=json&lang=es_ar"
            f"&limit={limit}&offset={offset}"
        )
        try:
            r = SESSION.get(url, timeout=20)
            data = r.json()
        except Exception as e:
            print(f"  Error Tokko: {e}")
            break

        propiedades = data.get("objects", [])
        if not propiedades:
            break

        for prop in propiedades:
            # Filtrar solo activas y publicadas
            if prop.get("status") != "Publicada" and prop.get("status") != "published":
                continue

            # Operación
            ops = prop.get("operations", [])
            operacion = "Venta"
            precio = None
            moneda = "USD"
            if ops:
                op = ops[0]
                op_type = op.get("operation_type", "")
                if "alquiler" in op_type.lower():
                    operacion = "Alquiler"
                prices = op.get("prices", [])
                if prices:
                    precio = int(prices[0].get("price", 0)) or None
                    moneda = prices[0].get("currency", "USD")
                    if moneda == "ARS":
                        moneda = "ARS"
                    else:
                        moneda = "USD"

            # Tipo
            tipo_id = prop.get("type", {})
            if isinstance(tipo_id, dict):
                tipo_nombre = tipo_id.get("name", "Casa")
                tipo = parse_tipo(tipo_nombre)
            else:
                tipo = TOKKO_TIPOS.get(tipo_id, "Casa")

            # Título
            address = prop.get("address", "")
            location = prop.get("location", {})
            loc_name = location.get("name", "") if isinstance(location, dict) else ""
            titulo = address or loc_name or f"Propiedad {prop.get('id', '')}"

            # Descripción
            descripcion = prop.get("description", "") or ""
            if len(descripcion) > 500:
                descripcion = descripcion[:500] + "..."

            # Imagen
            photos = prop.get("photos", [])
            imagen_url = ""
            if photos:
                imagen_url = photos[0].get("image", "") or photos[0].get("thumb", "")

            # Superficie
            surface_total = prop.get("surface", None)
            surface_cover = prop.get("roofed_surface", None)
            superficie = surface_cover or surface_total

            # Dormitorios y baños
            dormitorios = prop.get("suite_amount", None) or prop.get("room_amount", None)
            banos = prop.get("bathroom_amount", None)

            # Zona
            zona = loc_name

            # URL original
            url_prop = prop.get("public_url", "") or f"https://www.tokkobroker.com/property/{prop.get('id', '')}"

            fields = {
                "Titulo": titulo,
                "Operación": operacion,
                "Tipo": tipo,
                "Moneda": moneda,
                "Descripción": descripcion[:1000] if descripcion else "",
                "URL original": url_prop,
            }
            if precio:
                fields["Precio"] = precio
            if superficie:
                fields["Superficie m²"] = float(superficie)
            if dormitorios:
                fields["Dormitorios"] = int(dormitorios)
            if banos:
                fields["Baños"] = int(banos)
            if zona:
                fields["Zona/Barrio"] = zona
            if imagen_url:
                fields["Imagen URL"] = imagen_url

            if guardar_propiedad(fields, existing_urls, agencia["id"]):
                nuevas += 1
                print(f"  + {titulo} | {operacion} | {precio} {moneda}")
            
            time.sleep(0.2)

        total = data.get("meta", {}).get("total_count", 0)
        offset += limit
        if offset >= total:
            break
        time.sleep(1)

    return nuevas


# ── SCRAPER GENÉRICO ───────────────────────────────────────────

def scrape_generico(agencia, existing_urls):
    """
    Scraper genérico para sitios que no usan Tokko Broker.
    Busca patrones comunes de portales inmobiliarios WordPress/Estatik.
    """
    base_url = agencia["url"].rstrip("/")
    if not base_url:
        print(f"  [!] Sin URL para {agencia['nombre']}, omitiendo.")
        return 0

    nuevas = 0
    
    # Intentar encontrar la página de listado
    listado_urls = [
        f"{base_url}/propiedades/",
        f"{base_url}/ventas/",
        f"{base_url}/buscador/",
        f"{base_url}/properties/",
        base_url,
    ]

    for listado_url in listado_urls:
        page = 1
        encontro_props = False

        while True:
            if page == 1:
                url = listado_url
            else:
                # Probar distintos formatos de paginación
                url = f"{listado_url}page/{page}/"
                alt_url = f"{listado_url}?paged-1={page}"

            try:
                r = SESSION.get(url, timeout=20)
                if r.status_code != 200:
                    break
            except Exception as e:
                print(f"  Error: {e}")
                break

            soup = BeautifulSoup(r.text, "html.parser")
            
            # Buscar links a propiedades individuales
            prop_links = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if re.search(r"/(propiedad|property|propiedades|inmueble)/", href):
                    if href.startswith("http"):
                        prop_links.add(href)
                    else:
                        prop_links.add(base_url + href if href.startswith("/") else href)

            if not prop_links:
                break

            encontro_props = True

            for prop_url in prop_links:
                if prop_url in existing_urls:
                    continue

                try:
                    time.sleep(1)
                    r2 = SESSION.get(prop_url, timeout=20)
                    soup2 = BeautifulSoup(r2.text, "html.parser")

                    # Título
                    h1 = soup2.find("h1")
                    titulo = h1.get_text(strip=True) if h1 else ""
                    titulo = re.sub(r"(En\s+)?(Venta|Alquiler)\s*", "", titulo, flags=re.I).strip()
                    if not titulo:
                        titulo = prop_url.split("/")[-2].replace("-", " ").title()

                    # Texto completo para extraer datos
                    texto = soup2.get_text(" ", strip=True)

                    # Precio
                    precio = None
                    moneda = "USD"
                    m = re.search(r"U\$[Ss]?\s*([\d.,]+)", texto)
                    if m:
                        precio = int(m.group(1).replace(".", "").replace(",", ""))
                        moneda = "USD"
                    else:
                        m = re.search(r"\$\s*([\d.,]+)", texto)
                        if m:
                            precio = int(m.group(1).replace(".", "").replace(",", ""))
                            moneda = "ARS"

                    # Operación
                    operacion = "Alquiler" if re.search(r"\balquiler\b", texto, re.I) else "Venta"

                    # Tipo
                    tipo = parse_tipo(texto + " " + titulo)

                    # Imagen
                    og_img = soup2.find("meta", {"property": "og:image"})
                    imagen_url = og_img["content"] if og_img and og_img.get("content") else ""

                    # Descripción
                    og_desc = soup2.find("meta", {"property": "og:description"})
                    descripcion = og_desc["content"].strip() if og_desc and og_desc.get("content") else ""

                    # Dormitorios / baños / superficie
                    dormitorios = banos = superficie = None
                    m = re.search(r"(\d+)\s*[Dd]ormitorio", texto)
                    if m: dormitorios = int(m.group(1))
                    m = re.search(r"(\d+)\s*[Bb]a[ñn]o", texto)
                    if m: banos = int(m.group(1))
                    m = re.search(r"([\d.,]+)\s*m[²2]", texto)
                    if m:
                        try: superficie = float(m.group(1).replace(",", "."))
                        except: pass

                    fields = {
                        "Titulo": titulo[:255],
                        "Operación": operacion,
                        "Tipo": tipo,
                        "Moneda": moneda,
                        "Descripción": descripcion[:1000],
                        "URL original": prop_url,
                    }
                    if precio: fields["Precio"] = precio
                    if dormitorios: fields["Dormitorios"] = dormitorios
                    if banos: fields["Baños"] = banos
                    if superficie: fields["Superficie m²"] = superficie
                    if imagen_url: fields["Imagen URL"] = imagen_url

                    if guardar_propiedad(fields, existing_urls, agencia["id"]):
                        nuevas += 1
                        print(f"  + {titulo} | {operacion} | {precio} {moneda}")

                except Exception as e:
                    print(f"  Error en {prop_url}: {e}")

            # Siguiente página
            page += 1
            if page > 50:  # límite de seguridad
                break
            time.sleep(2)

        if encontro_props:
            break  # ya encontró propiedades en esta URL base

    return nuevas


# ── MAIN ───────────────────────────────────────────────────────

def main():
    print("=== Scraper Universal — Gchu Propiedades ===\n")

    if not AIRTABLE_TOKEN or not AIRTABLE_BASE_ID:
        print("ERROR: Faltan variables AIRTABLE_TOKEN o AIRTABLE_BASE_ID")
        exit(1)

    print("1. Cargando agencias activas...")
    agencias = get_agencias_activas()
    print(f"   {len(agencias)} agencias activas encontradas.")

    if not agencias:
        print("   No hay agencias activas. Activá al menos una en Airtable.")
        exit(0)

    print("2. Cargando URLs existentes en Airtable...")
    existing_urls = get_existing_urls()
    print(f"   {len(existing_urls)} propiedades ya cargadas.\n")

    total_nuevas = 0

    for agencia in agencias:
        tipo = agencia["tipo"]
        print(f"── {agencia['nombre']} ({tipo}) ──")

        if tipo == "Tokko Broker":
            nuevas = scrape_tokko(agencia, existing_urls)
        elif tipo == "Scraper":
            nuevas = scrape_generico(agencia, existing_urls)
        else:
            print(f"  Tipo Manual — omitiendo scraping.")
            nuevas = 0

        print(f"  → {nuevas} propiedades nuevas cargadas.\n")
        total_nuevas += nuevas
        time.sleep(2)

    print(f"✅ Total: {total_nuevas} propiedades nuevas cargadas.")


if __name__ == "__main__":
    main()
