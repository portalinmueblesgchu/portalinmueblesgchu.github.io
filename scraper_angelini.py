"""
Scraper: Angelini Propiedades → Airtable
Extrae datos directamente del listado (título, precio, operación, dormitorios, baños, superficie)
y la imagen desde og:image de la página de detalle.
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

def parse_tipo(texto):
    texto_lower = texto.lower()
    tipos = [
        ("Departamento", ["departamento", "dpto", "depto"]),
        ("Chacra", ["chacra"]),
        ("Campo", ["campo", "rural", "estancia", "casco"]),
        ("Terreno", ["terreno", "lote ", "loteo", "s. lote"]),
        ("Local", ["local comercial", "local "]),
        ("Oficina", ["oficina"]),
        ("Galpón", ["galpón", "galpon"]),
        ("Dúplex", ["dúplex", "duplex"]),
    ]
    for tipo, keywords in tipos:
        if any(k in texto_lower for k in keywords):
            return tipo
    return "Casa"

def get_imagen_url(url_detalle):
    """Obtiene la URL de la imagen principal desde og:image."""
    try:
        time.sleep(0.8)
        r = SESSION.get(url_detalle, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        img_tag = soup.find("meta", {"property": "og:image"})
        if img_tag and img_tag.get("content"):
            return img_tag["content"]
    except:
        pass
    return ""

def scrape_pagina(page):
    """
    Extrae propiedades completas desde el listado.
    El sitio muestra en el listado: dirección, precio, operación, dormitorios, baños, superficie.
    """
    url = LISTADO_URL.format(page)
    try:
        r = SESSION.get(url, timeout=20)
        if r.status_code != 200:
            return None
    except Exception as e:
        print(f"  Error página {page}: {e}")
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    propiedades = []

    # Cada propiedad está en un article o div con link a /propiedad/
    # Buscamos los contenedores de cada propiedad
    # El patrón del sitio: article o div que contiene un link /propiedad/ y datos
    
    contenedores = []
    
    # Intentar encontrar articles primero
    for article in soup.find_all("article"):
        link = article.find("a", href=re.compile(r"/propiedad/"))
        if link:
            contenedores.append((article, link))
    
    # Si no hay articles, buscar por divs con clase property
    if not contenedores:
        for div in soup.find_all("div", class_=re.compile(r"property|listing|prop")):
            link = div.find("a", href=re.compile(r"/propiedad/"))
            if link:
                contenedores.append((div, link))

    # Si tampoco, agrupar por links únicos a /propiedad/
    if not contenedores:
        vistos = set()
        for a in soup.find_all("a", href=re.compile(r"/propiedad/")):
            href = a.get("href", "")
            if href in vistos:
                continue
            vistos.add(href)
            # Subir en el DOM para encontrar el contenedor
            container = a
            for _ in range(5):
                parent = container.find_parent()
                if parent and len(parent.find_all("a", href=re.compile(r"/propiedad/"))) == 1:
                    container = parent
                else:
                    break
            contenedores.append((container, a))

    urls_vistos = set()
    for contenedor, link_tag in contenedores:
        href = link_tag.get("href", "")
        if not href or href in urls_vistos:
            continue
        urls_vistos.add(href)

        url_prop = href if href.startswith("http") else BASE_URL + href
        texto = contenedor.get_text(" ", strip=True)

        # Operación
        if "en alquiler" in texto.lower() or "alquiler" in texto.lower():
            operacion = "Alquiler"
        else:
            operacion = "Venta"

        # Precio
        precio = None
        moneda = "USD"
        m = re.search(r"U\$D\s*([\d.,]+)", texto)
        if m:
            try:
                precio = int(m.group(1).replace(".", "").replace(",", ""))
                moneda = "USD"
            except:
                pass
        else:
            m = re.search(r"\$\s*([\d.,]+)", texto)
            if m:
                try:
                    precio = int(m.group(1).replace(".", "").replace(",", ""))
                    moneda = "ARS"
                except:
                    pass

        # Título: buscar la dirección (texto que no es precio ni operación)
        titulo = ""
        # Buscar en el link o en un h2/h3 dentro del contenedor
        for tag in contenedor.find_all(["h2", "h3", "h4", "h5"]):
            t = tag.get_text(strip=True)
            if t and len(t) > 4 and not re.match(r"^(en\s)?(venta|alquiler)", t, re.I):
                titulo = t
                break
        
        if not titulo:
            # Intentar desde el link
            t = link_tag.get_text(strip=True)
            if t and len(t) > 4 and not re.match(r"^(en\s)?(venta|alquiler)|U\$D|\$", t, re.I):
                titulo = t

        if not titulo:
            # Extraer dirección del texto: buscar patrón "Calle Número"
            m_dir = re.search(r"([A-ZÁÉÍÓÚÑ][a-záéíóúñ\s]+\s+\d+[^,\n]*)", texto)
            if m_dir:
                titulo = m_dir.group(1).strip()

        if not titulo:
            titulo = url_prop.split("/")[-2].replace("-", " ").title()

        # Tipo
        tipo = parse_tipo(texto + " " + titulo)

        # Dormitorios
        dormitorios = None
        m = re.search(r"(\d+)\s*[Dd]ormitorio", texto)
        if m:
            dormitorios = int(m.group(1))

        # Baños
        banos = None
        m = re.search(r"(\d+)\s*[Bb]a[ñn]o", texto)
        if m:
            banos = int(m.group(1))

        # Superficie cubierta
        superficie = None
        m = re.search(r"[Cc]ubierta[^\d]*([\d.,]+)", texto)
        if m:
            try:
                superficie = float(m.group(1).replace(",", "."))
            except:
                pass
        if not superficie:
            m = re.search(r"([\d.,]+)\s*m[²2]", texto)
            if m:
                try:
                    superficie = float(m.group(1).replace(",", "."))
                except:
                    pass

        propiedades.append({
            "url": url_prop,
            "titulo": titulo,
            "precio": precio,
            "moneda": moneda,
            "operacion": operacion,
            "tipo": tipo,
            "dormitorios": dormitorios,
            "baños": banos,
            "superficie": superficie,
        })

    return propiedades if propiedades else None


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
        propiedades = scrape_pagina(page)

        if not propiedades:
            print(f"Fin en página {page}.")
            break

        for prop in propiedades:
            if prop["url"] in existing_urls:
                continue

            print(f"  + {prop['titulo']} | {prop['operacion']} | {prop['precio']} {prop['moneda']} | {prop['dormitorios']}d {prop['baños']}b {prop['superficie']}m²")

            # Obtener imagen desde detalle
            imagen_url = get_imagen_url(prop["url"])

            fields = {
                "Titulo": prop["titulo"],
                "Operación": prop["operacion"],
                "Tipo": prop["tipo"],
                "Moneda": prop["moneda"],
                "URL original": prop["url"],
                "Tipo Publicante": "Agencia",
                "Estado": "Publicada",
                "Fecha carga": str(date.today()),
                "Agencia": [agencia_id],
            }
            if prop["precio"] is not None:
                fields["Precio"] = prop["precio"]
            if prop["dormitorios"]:
                fields["Dormitorios"] = prop["dormitorios"]
            if prop["baños"]:
                fields["Baños"] = prop["baños"]
            if prop["superficie"]:
                fields["Superficie m²"] = prop["superficie"]
            if imagen_url:
                fields["Imagen URL"] = imagen_url

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
