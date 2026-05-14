"""
Scraper: Angelini Propiedades → Airtable
Recorre todas las páginas de angelinipropiedades.com/propiedades/
y carga las propiedades nuevas en Airtable (evita duplicados por URL).
"""

import os
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import date

# ── CONFIGURACIÓN ──────────────────────────────────────────────
AIRTABLE_TOKEN   = os.environ.get("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
TABLE_PROPIEDADES = "Propiedades"
TABLE_AGENCIAS    = "Agencias"

BASE_URL     = "https://angelinipropiedades.com"
LISTADO_URL  = "https://angelinipropiedades.com/propiedades/page/{}/"
AGENCIA_NOMBRE = "Angelini Propiedades"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GchuPortal/1.0)"}
# ───────────────────────────────────────────────────────────────


def airtable_get(table, params=None):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{requests.utils.quote(table)}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
    records = []
    offset = None
    while True:
        p = dict(params or {})
        if offset:
            p["offset"] = offset
        r = requests.get(url, headers=headers, params=p)
        data = r.json()
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
    r = requests.post(url, headers=headers, json={"fields": fields})
    return r.json()


def get_or_create_agencia(nombre):
    """Devuelve el record ID de la agencia, creándola si no existe."""
    records = airtable_get(TABLE_AGENCIAS)
    for rec in records:
        if rec["fields"].get("Nombre", "").lower() == nombre.lower():
            return rec["id"]
    # Crear agencia
    result = airtable_post(TABLE_AGENCIAS, {
        "Nombre": nombre,
        "Estado": "Verificada",
        "Rol": "Agencia",
    })
    return result.get("id")


def get_existing_urls():
    """Devuelve el conjunto de URLs ya cargadas en Airtable."""
    records = airtable_get(TABLE_PROPIEDADES, {"fields[]": "URL original"})
    return {rec["fields"].get("URL original", "") for rec in records}


def parse_precio(texto):
    """Extrae precio numérico y moneda de texto como 'En Venta U$D220.000'."""
    moneda = "USD" if "U$D" in texto or "USD" in texto else "ARS"
    numeros = re.findall(r"[\d.,]+", texto.replace(".", "").replace(",", ""))
    precio = int(numeros[-1]) if numeros else None
    return precio, moneda


def parse_operacion(texto):
    t = texto.lower()
    if "alquiler" in t:
        return "Alquiler"
    if "venta" in t:
        return "Venta"
    return "Venta"


def scrape_listado(page):
    """Devuelve lista de {url, titulo, precio, moneda, operacion} de una página."""
    url = LISTADO_URL.format(page)
    r = requests.get(url, headers=HEADERS, timeout=15)
    if r.status_code != 200:
        return None  # No hay más páginas
    soup = BeautifulSoup(r.text, "html.parser")

    propiedades = []
    # Cada propiedad tiene un h4 con link y un span de precio
    for item in soup.select("article, .property-item, .listing-item, h4 a"):
        pass

    # Estrategia: buscar todos los links a /propiedad/
    links_vistos = set()
    for a in soup.find_all("a", href=re.compile(r"/propiedad/")):
        href = a["href"]
        if href in links_vistos:
            continue
        links_vistos.add(href)

        # Buscar contenedor padre para extraer precio
        container = a.find_parent()
        texto_completo = container.get_text(" ", strip=True) if container else ""

        precio, moneda = parse_precio(texto_completo)
        operacion = parse_operacion(texto_completo)
        titulo = a.get_text(strip=True)

        if titulo and href:
            propiedades.append({
                "url": href if href.startswith("http") else BASE_URL + href,
                "titulo": titulo,
                "precio": precio,
                "moneda": moneda,
                "operacion": operacion,
            })

    return propiedades if propiedades else None


def scrape_detalle(url):
    """Extrae descripción, tipo, dormitorios, baños, superficie e imagen de una propiedad."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        # Descripción
        desc_tag = soup.find("meta", {"name": "description"}) or soup.find("meta", {"property": "og:description"})
        descripcion = desc_tag["content"] if desc_tag and desc_tag.get("content") else ""

        # Imagen principal (og:image)
        img_tag = soup.find("meta", {"property": "og:image"})
        imagen_url = img_tag["content"] if img_tag and img_tag.get("content") else ""

        # Tipo de propiedad (título H1 suele decir "Lestonnac 1249 En Venta Casas")
        h1 = soup.find("h1")
        tipo = "Casa"
        if h1:
            texto_h1 = h1.get_text()
            for t in ["Departamento", "Local", "Terreno", "Campo", "Oficina", "Galpón", "Duplex", "Dúplex"]:
                if t.lower() in texto_h1.lower():
                    tipo = t
                    break

        # Features: dormitorios, baños, superficie
        dormitorios = baños = superficie = None
        for li in soup.select("li"):
            txt = li.get_text(strip=True)
            m = re.search(r"(\d+)\s*Dormitorio", txt, re.I)
            if m:
                dormitorios = int(m.group(1))
            m = re.search(r"(\d+)\s*Ba[ñn]o", txt, re.I)
            if m:
                baños = int(m.group(1))
            m = re.search(r"S\.\s*Cubierta.*?([\d.,]+)", txt, re.I)
            if m:
                try:
                    superficie = float(m.group(1).replace(",", "."))
                except:
                    pass

        return {
            "descripcion": descripcion,
            "imagen_url": imagen_url,
            "tipo": tipo,
            "dormitorios": dormitorios,
            "baños": baños,
            "superficie": superficie,
        }
    except Exception as e:
        print(f"  Error detalle {url}: {e}")
        return {}


def main():
    print("=== Scraper Angelini Propiedades → Airtable ===")

    # 1. Obtener ID de agencia
    print("Obteniendo agencia...")
    agencia_id = get_or_create_agencia(AGENCIA_NOMBRE)
    print(f"  Agencia ID: {agencia_id}")

    # 2. Obtener URLs ya existentes
    print("Obteniendo propiedades existentes en Airtable...")
    existing_urls = get_existing_urls()
    print(f"  {len(existing_urls)} propiedades ya cargadas.")

    # 3. Scrapear páginas
    nuevas = 0
    page = 1
    while True:
        print(f"Scrapeando página {page}...")
        propiedades = scrape_listado(page)

        if not propiedades:
            print(f"  Fin del listado en página {page}.")
            break

        for prop in propiedades:
            if prop["url"] in existing_urls:
                continue  # Ya existe, saltar

            print(f"  Nueva: {prop['titulo']} — {prop['url']}")

            # Detalle
            time.sleep(0.5)  # Ser respetuoso con el servidor
            detalle = scrape_detalle(prop["url"])

            # Armar campos para Airtable
            fields = {
                "Titulo": prop["titulo"],
                "Operación": prop["operacion"],
                "Tipo": detalle.get("tipo", "Casa"),
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
            if "id" in result:
                nuevas += 1
                existing_urls.add(prop["url"])
            else:
                print(f"    Error al cargar: {result}")

            time.sleep(0.3)

        page += 1
        time.sleep(1)  # Pausa entre páginas

    print(f"\n✅ Listo. {nuevas} propiedades nuevas cargadas en Airtable.")


if __name__ == "__main__":
    main()
