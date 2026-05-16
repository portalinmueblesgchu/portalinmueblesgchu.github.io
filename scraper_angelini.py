"""
Scraper: Angelini Propiedades → Airtable
- Extrae título real desde página de detalle
- Guarda URL de foto en campo de texto
- Evita duplicados por URL original
"""

import os
import re
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from datetime import date

AIRTABLE_TOKEN    = os.environ.get("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID  = os.environ.get("AIRTABLE_BASE_ID")
TABLE_PROPIEDADES = "Propiedades"
TABLE_AGENCIAS    = "Agencias"
BASE_URL          = "https://angelinipropiedades.com"
LISTADO_URL       = "https://angelinipropiedades.com/propiedades/page/{}/"
AGENCIA_NOMBRE    = "Angelini Propiedades"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9",
    "Connection": "keep-alive",
}

def make_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session

SESSION = make_session()

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

def get_or_create_agencia(nombre):
    records = airtable_get(TABLE_AGENCIAS)
    for rec in records:
        if rec["fields"].get("Nombre", "").strip().lower() == nombre.strip().lower():
            return rec["id"]
    result = airtable_post(TABLE_AGENCIAS, {"Nombre": nombre, "Estado": "Verificada"})
    return result.get("id")

def get_existing_urls():
    records = airtable_get(TABLE_PROPIEDADES, {"fields[]": "URL original"})
    return {rec["fields"].get("URL original", "") for rec in records if "URL original" in rec["fields"]}

def parse_precio(texto):
    moneda = "USD" if "U$D" in texto or "USD" in texto else "ARS"
    m = re.search(r"(?:U\$D|USD|\$)\s*([\d.]+)", texto)
    if m:
        try:
            return int(m.group(1).replace(".", "")), moneda
        except:
            pass
    return None, moneda

def parse_operacion(texto):
    return "Alquiler" if "alquiler" in texto.lower() else "Venta"

def parse_tipo(texto):
    texto_lower = texto.lower()
    tipos = [
        ("Departamento", ["departamento", "dpto"]),
        ("Chacra", ["chacra"]),
        ("Campo", ["campo", "arrendamiento", "rural"]),
        ("Terreno", ["terreno", "lote", "loteo"]),
        ("Local", ["local comercial", "local"]),
        ("Oficina", ["oficina"]),
        ("Galpón", ["galpón", "galpon"]),
        ("Dúplex", ["dúplex", "duplex"]),
    ]
    for tipo, keywords in tipos:
        if any(k in texto_lower for k in keywords):
            return tipo
    return "Casa"

def scrape_listado(page):
    """Devuelve lista de URLs de propiedades en una página."""
    url = LISTADO_URL.format(page)
    try:
        r = SESSION.get(url, timeout=20)
        if r.status_code != 200:
            return None
    except Exception as e:
        print(f"  Error página {page}: {e}")
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    urls = []
    vistos = set()

    for a in soup.find_all("a", href=re.compile(r"/propiedad/")):
        href = a.get("href", "")
        if not href or href in vistos:
            continue
        vistos.add(href)
        url_prop = href if href.startswith("http") else BASE_URL + href
        urls.append(url_prop)

    return urls if urls else None

def scrape_detalle(url):
    """Extrae todos los datos desde la página de detalle de la propiedad."""
    try:
        time.sleep(1)
        r = SESSION.get(url, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")

        # Título desde H1 o og:title
        titulo = ""
        h1 = soup.find("h1")
        if h1:
            titulo = h1.get_text(strip=True)
        if not titulo:
            og_title = soup.find("meta", {"property": "og:title"})
            if og_title:
                titulo = og_title.get("content", "").strip()

        # Limpiar título (sacar "En Venta" y precio si están pegados)
        titulo = re.sub(r"En\s*(Venta|Alquiler)\s*", "", titulo, flags=re.I).strip()
        titulo = re.sub(r"U\$D[\d.,]+", "", titulo).strip()
        titulo = re.sub(r"\$[\d.,]+", "", titulo).strip()
        if not titulo:
            titulo = url.split("/")[-2].replace("-", " ").title()

        # Descripción
        desc_tag = soup.find("meta", {"property": "og:description"})
        descripcion = desc_tag["content"].strip() if desc_tag and desc_tag.get("content") else ""

        # Imagen principal
        img_tag = soup.find("meta", {"property": "og:image"})
        imagen_url = img_tag["content"] if img_tag and img_tag.get("content") else ""

        # Precio y operación desde el texto completo
        texto_completo = soup.get_text(" ", strip=True)
        precio, moneda = parse_precio(texto_completo)
        operacion = parse_operacion(texto_completo)
        tipo = parse_tipo(texto_completo + " " + titulo)

        # Dormitorios, baños, superficie desde lista de features
        dormitorios = baños = superficie = None
        for li in soup.select("li, .property-feature, .feature"):
            txt = li.get_text(strip=True)
            m = re.search(r"(\d+)\s*[Dd]ormitorio", txt)
            if m:
                dormitorios = int(m.group(1))
            m = re.search(r"(\d+)\s*[Bb]a[ñn]o", txt)
            if m:
                baños = int(m.group(1))
            m = re.search(r"([\d.,]+)\s*m[²2]", txt)
            if m:
                try:
                    superficie = float(m.group(1).replace(",", "."))
                except:
                    pass

        # Zona desde URL o título
        zona = ""
        url_parts = url.rstrip("/").split("/")
        if len(url_parts) >= 2:
            zona_raw = url_parts[-2] if url_parts[-2] != "propiedad" else ""
            zona = zona_raw.replace("-", " ").replace(",", ", ").title()

        return {
            "titulo": titulo,
            "descripcion": descripcion,
            "imagen_url": imagen_url,
            "precio": precio,
            "moneda": moneda,
            "operacion": operacion,
            "tipo": tipo,
            "dormitorios": dormitorios,
            "baños": baños,
            "superficie": superficie,
            "zona": zona,
        }
    except Exception as e:
        print(f"  Error detalle {url}: {e}")
        return {}

def main():
    print("=== Scraper Angelini Propiedades → Airtable ===\n")

    if not AIRTABLE_TOKEN or not AIRTABLE_BASE_ID:
        print("ERROR: Faltan variables de entorno.")
        exit(1)

    print("1. Obteniendo agencia...")
    agencia_id = get_or_create_agencia(AGENCIA_NOMBRE)
    if not agencia_id:
        print("ERROR: No se pudo obtener la agencia.")
        exit(1)
    print(f"   ID: {agencia_id}")

    print("2. URLs existentes en Airtable...")
    existing_urls = get_existing_urls()
    print(f"   {len(existing_urls)} ya cargadas.")

    print("3. Scraping...\n")
    nuevas = 0
    page = 1

    while True:
        print(f"Página {page}...")
        urls = scrape_listado(page)

        if not urls:
            print(f"Fin en página {page}.")
            break

        for url_prop in urls:
            if url_prop in existing_urls:
                continue

            detalle = scrape_detalle(url_prop)
            if not detalle:
                continue

            titulo = detalle.get("titulo") or url_prop.split("/")[-2]
            print(f"  + {titulo} | {detalle.get('operacion')} | {detalle.get('precio')} {detalle.get('moneda')}")

            fields = {
                "Titulo": titulo,
                "Operación": detalle.get("operacion", "Venta"),
                "Tipo": detalle.get("tipo", "Casa"),
                "Moneda": detalle.get("moneda", "USD"),
                "Descripción": detalle.get("descripcion", ""),
                "URL original": url_prop,
                "Tipo Publicante": "Agencia",
                "Estado": "Publicada",
                "Fecha carga": str(date.today()),
                "Agencia": [agencia_id],
            }
            if detalle.get("precio") is not None:
                fields["Precio"] = detalle["precio"]
            if detalle.get("dormitorios"):
                fields["Dormitorios"] = detalle["dormitorios"]
            if detalle.get("baños"):
                fields["Baños"] = detalle["baños"]
            if detalle.get("superficie"):
                fields["Superficie m²"] = detalle["superficie"]
            if detalle.get("zona"):
                fields["Zona/Barrio"] = detalle["zona"]
            if detalle.get("imagen_url"):
                fields["Imagen URL"] = detalle["imagen_url"]

            result = airtable_post(TABLE_PROPIEDADES, fields)
            if result.get("id"):
                nuevas += 1
                existing_urls.add(url_prop)
            time.sleep(0.5)

        page += 1
        time.sleep(2)

    print(f"\n✅ {nuevas} propiedades nuevas cargadas.")

if __name__ == "__main__":
    main()
