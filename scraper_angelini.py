"""
Scraper: Angelini Propiedades → Airtable
Recorre todas las páginas de angelinipropiedades.com/propiedades/
y carga las propiedades nuevas en Airtable (evita duplicados por URL).
"""

import os
import re
import time
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

BASE_URL       = "https://angelinipropiedades.com"
LISTADO_URL    = "https://angelinipropiedades.com/propiedades/page/{}/"
AGENCIA_NOMBRE = "Angelini Propiedades"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
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


def airtable_get(table, params=None):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{requests.utils.quote(table)}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    records = []
    offset = None
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
    headers = {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }
    r = requests.post(url, headers=headers, json={"fields": fields}, timeout=20)
    result = r.json()
    if "error" in result:
        print(f"  Error Airtable POST: {result}")
    return result


def get_or_create_agencia(nombre):
    records = airtable_get(TABLE_AGENCIAS)
    for rec in records:
        if rec["fields"].get("Nombre", "").strip().lower() == nombre.strip().lower():
            print(f"  Agencia encontrada: {rec['id']}")
            return rec["id"]
    print(f"  Creando agencia '{nombre}'...")
    result = airtable_post(TABLE_AGENCIAS, {"Nombre": nombre, "Estado": "Verificada"})
    agencia_id = result.get("id")
    print(f"  Agencia creada: {agencia_id}")
    return agencia_id


def get_existing_urls():
    records = airtable_get(TABLE_PROPIEDADES, {"fields[]": "URL original"})
    return {rec["fields"].get("URL original", "") for rec in records}


def parse_precio(texto):
    moneda = "USD" if "U$D" in texto or "USD" in texto else "ARS"
    m = re.search(r"(?:U\$D|USD|\$)\s*([\d.,]+)", texto)
    if m:
        num_str = m.group(1).replace(".", "").replace(",", "")
        try:
            return int(num_str), moneda
        except:
            pass
    return None, moneda


def parse_operacion(texto):
    return "Alquiler" if "alquiler" in texto.lower() else "Venta"


def parse_tipo(texto):
    for t in ["Departamento", "Dúplex", "Duplex", "Local", "Terreno", "Campo", "Chacra", "Galpón", "Oficina"]:
        if t.lower() in texto.lower():
            return t
    return "Casa"


def scrape_listado(page):
    url = LISTADO_URL.format(page)
    try:
        r = SESSION.get(url, timeout=20)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            print(f"  Status {r.status_code} en página {page}")
            return None
    except Exception as e:
        print(f"  Error página {page}: {e}")
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    propiedades = []
    links_vistos = set()

    for a in soup.find_all("a", href=re.compile(r"/propiedad/")):
        href = a.get("href", "")
        if not href or href in links_vistos:
            continue
        links_vistos.add(href)

        titulo = a.get_text(strip=True)
        if not titulo or len(titulo) < 3:
            continue

        container = a.find_parent()
        texto_bloque = ""
        for _ in range(4):
            if container:
                texto_bloque = container.get_text(" ", strip=True)
                if "venta" in texto_bloque.lower() or "alquiler" in texto_bloque.lower():
                    break
                container = container.find_parent()

        precio, moneda = parse_precio(texto_bloque)
        operacion = parse_operacion(texto_bloque)
        tipo = parse_tipo(texto_bloque + " " + titulo)
        url_prop = href if href.startswith("http") else BASE_URL + href

        propiedades.append({
            "url": url_prop,
            "titulo": titulo,
            "precio": precio,
            "moneda": moneda,
            "operacion": operacion,
            "tipo": tipo,
        })

    return propiedades if propiedades else None


def scrape_detalle(url):
    try:
        time.sleep(1)
        r = SESSION.get(url, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")

        desc_tag = soup.find("meta", {"property": "og:description"})
        descripcion = desc_tag["content"].strip() if desc_tag and desc_tag.get("content") else ""

        img_tag = soup.find("meta", {"property": "og:image"}) or soup.find("meta", {"name": "twitter:image"})
        imagen_url = img_tag["content"] if img_tag and img_tag.get("content") else ""

        dormitorios = baños = superficie = None
        for li in soup.select("li"):
            txt = li.get_text(strip=True)
            m = re.search(r"(\d+)\s*Dormitorio", txt, re.I)
            if m:
                dormitorios = int(m.group(1))
            m = re.search(r"(\d+)\s*Ba[ñn]o", txt, re.I)
            if m:
                baños = int(m.group(1))
            m = re.search(r"S\.\s*Cubierta[^\d]*([\d.,]+)", txt, re.I)
            if m:
                try:
                    superficie = float(m.group(1).replace(",", "."))
                except:
                    pass

        return {"descripcion": descripcion, "imagen_url": imagen_url,
                "dormitorios": dormitorios, "baños": baños, "superficie": superficie}
    except Exception as e:
        print(f"  Error detalle {url}: {e}")
        return {}


def main():
    print("=== Scraper Angelini Propiedades → Airtable ===\n")

    if not AIRTABLE_TOKEN or not AIRTABLE_BASE_ID:
        print("ERROR: Faltan variables AIRTABLE_TOKEN o AIRTABLE_BASE_ID")
        exit(1)

    print("1. Obteniendo agencia...")
    agencia_id = get_or_create_agencia(AGENCIA_NOMBRE)
    if not agencia_id:
        print("ERROR: No se pudo obtener la agencia.")
        exit(1)

    print("\n2. Obteniendo propiedades existentes...")
    existing_urls = get_existing_urls()
    print(f"   {len(existing_urls)} ya cargadas.")

    print("\n3. Scraping...\n")
    nuevas = 0
    page = 1

    while True:
        print(f"Página {page}...")
        propiedades = scrape_listado(page)

        if not propiedades:
            print(f"Fin en página {page}.")
            break

        for prop in propiedades:
            if prop["url"] in existing_urls:
                continue

            print(f"  + {prop['titulo']} ({prop['operacion']}) {prop['precio']} {prop['moneda']}")
            detalle = scrape_detalle(prop["url"])

            fields = {
                "Titulo": prop["titulo"],
                "Operación": prop["operacion"],
                "Tipo": prop["tipo"],
                "Precio": prop["precio"],
                "Moneda": prop["moneda"],
                "Descripción": detalle.get("descripcion", ""),
                "URL original": prop["url"],
                "Tipo Publicante": "Agencia",
                "Estado": "Publicada",
                "Fecha carga": str(date.today()),
                "Agencia": [agencia_id],
            }
            if detalle.get("dormitorios"):
                fields["Dormitorios"] = detalle["dormitorios"]
            if detalle.get("baños"):
                fields["Baños"] = detalle["baños"]
            if detalle.get("superficie"):
                fields["Superficie m²"] = detalle["superficie"]

            result = airtable_post(TABLE_PROPIEDADES, fields)
            if result.get("id"):
                nuevas += 1
                existing_urls.add(prop["url"])
            time.sleep(0.5)

        page += 1
        time.sleep(2)

    print(f"\n✅ {nuevas} propiedades nuevas cargadas.")


if __name__ == "__main__":
    main()
