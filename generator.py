"""
generator.py — Terminal de Ofertas
Busca produtos e campanhas da Lomadee e gera index.html + sitemap.xml estáticos.
Roda via GitHub Actions a cada 3 horas.
"""

import os
import re
import random
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

LOMADEE_KEY  = os.getenv("LOMADEE_TOKEN", "")
SOURCE_ID    = os.getenv("SOURCE_ID", "")
SITE_URL     = os.getenv("SITE_URL", "https://terminaldeofertas.github.io").rstrip("/")
SEARCH_TERMS = [t.strip() for t in os.getenv("SEARCH_TERMS", "tênis,celular,notebook,perfume,smart tv,fone,tablet,geladeira,ar condicionado,câmera").split(",") if t.strip()]

PRODUCTS_URL  = "https://api-beta.lomadee.com.br/affiliate/products"
CAMPAIGNS_URL = "https://api-beta.lomadee.com.br/affiliate/campaigns"
SHORTENER_URL = "https://api-beta.lomadee.com.br/affiliate/shortener/url"
BRANDS_URL    = "https://api-beta.lomadee.com.br/affiliate/brands"

HEADERS = {"x-api-key": LOMADEE_KEY, "Accept": "application/json"}

_brand_id_cache: dict = {}


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_brand_mongo_id(organization_uuid: str) -> str | None:
    if organization_uuid in _brand_id_cache:
        return _brand_id_cache[organization_uuid]
    page = 1
    while True:
        try:
            res = requests.get(f"{BRANDS_URL}?page={page}&limit=50", headers=HEADERS, timeout=10)
            res.raise_for_status()
            brands = res.json().get("data", [])
            if not brands:
                break
            for brand in brands:
                if brand.get("id") == organization_uuid:
                    _brand_id_cache[organization_uuid] = brand["id"]
                    return brand["id"]
            page += 1
        except Exception:
            break
    return None


def get_affiliate_link(url: str, organization_uuid: str) -> str:
    brand_id = get_brand_mongo_id(organization_uuid)
    if not brand_id:
        return url
    try:
        res = requests.post(
            SHORTENER_URL,
            headers={**HEADERS, "Content-Type": "application/json"},
            json={"url": url, "organizationId": brand_id, "type": "Custom"},
            timeout=10,
        )
        if res.status_code in (200, 201):
            data = res.json()
            if isinstance(data, list):
                data = data[0] if data else {}
            short_urls = data.get("shortUrls", [])
            return short_urls[0] if short_urls else url
    except Exception:
        pass
    return url


def get_product_price(prod: dict) -> float | None:
    try:
        return prod["options"][0]["pricing"][0]["price"]
    except Exception:
        return None


def get_product_image(prod: dict) -> str:
    try:
        return prod["images"][0]["url"]
    except Exception:
        return ""


def get_campaign_image(camp: dict) -> str:
    try:
        return camp["mediaKit"]["banners"][0]
    except Exception:
        return ""


def get_campaign_link(camp: dict) -> str:
    try:
        return camp["channels"][0]["shortUrls"][0]
    except Exception:
        return camp.get("url", "#")


def fmt_price(price: float | None) -> str:
    if price is None:
        return "Ver preço"
    return f"R$ {price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ── Coleta de dados ──────────────────────────────────────────────────────────

def fetch_products(terms_sample: int = 8, limit_per_term: int = 12) -> list[dict]:
    products: list[dict] = []
    seen: set = set()
    terms = random.sample(SEARCH_TERMS, min(terms_sample, len(SEARCH_TERMS)))
    print(f"Termos: {terms}")

    for term in terms:
        try:
            res = requests.get(
                PRODUCTS_URL,
                params={"search": term, "limit": limit_per_term, "isAvailable": "true"},
                headers=HEADERS,
                timeout=15,
            )
            if res.status_code != 200:
                print(f"  [{term}] HTTP {res.status_code}")
                continue
            for prod in res.json().get("data", []):
                pid = prod.get("_id")
                if pid and pid not in seen:
                    seen.add(pid)
                    products.append(prod)
            print(f"  [{term}] {len(res.json().get('data', []))} produtos")
        except Exception as e:
            print(f"  [{term}] Erro: {e}")

    return products


def fetch_campaigns(limit: int = 50) -> list[dict]:
    campaigns: list[dict] = []
    try:
        res = requests.get(CAMPAIGNS_URL, params={"limit": limit}, headers=HEADERS, timeout=15)
        if res.status_code == 200:
            for camp in res.json().get("data", []):
                if camp.get("status") == "onTime":
                    campaigns.append(camp)
        print(f"Campanhas ativas: {len(campaigns)}")
    except Exception as e:
        print(f"Erro campanhas: {e}")
    return campaigns


# ── Geração de cards HTML ────────────────────────────────────────────────────

def product_card(prod: dict) -> str:
    name  = escape_html(prod.get("name", "Produto")[:90])
    url   = prod.get("url", "#")
    org   = prod.get("organizationId", "")
    aff   = get_affiliate_link(url, org) if url != "#" else "#"
    img   = get_product_image(prod)
    price = get_product_price(prod)

    img_html = (
        f'<img src="{img}" alt="{name}" loading="lazy" onerror="this.closest(\'.card-img\').innerHTML=\'<span class=no-img>📦</span>\'">'
        if img else '<span class="no-img">📦</span>'
    )

    return f"""
      <article class="card">
        <a href="{aff}" target="_blank" rel="nofollow noopener noreferrer">
          <div class="card-img">{img_html}</div>
          <div class="card-body">
            <h3>{name}</h3>
            <p class="price">{fmt_price(price)}</p>
            <span class="btn">🛒 Ver oferta</span>
          </div>
        </a>
      </article>"""


def campaign_card(camp: dict) -> str:
    name  = escape_html(camp.get("name", "Campanha")[:90])
    ctype = camp.get("type", "Offer")
    code  = camp.get("code", "")
    link  = get_campaign_link(camp)
    img   = get_campaign_image(camp)

    end_at = (camp.get("period") or {}).get("endAt", "")
    expiry = ""
    if end_at:
        try:
            dt = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
            expiry = f'<p class="expiry">📅 Válido até {dt.strftime("%d/%m/%Y")}</p>'
        except Exception:
            pass

    badge    = "🎫 Cupom" if ctype == "Coupon" else "🔥 Oferta"
    code_html = f'<p class="coupon-code">🔑 Cupom: <strong>{escape_html(code)}</strong></p>' if code else ""
    img_html = (
        f'<img src="{img}" alt="{name}" loading="lazy" onerror="this.closest(\'.card-img\').innerHTML=\'<span class=no-img>🎁</span>\'">'
        if img else '<span class="no-img">🎁</span>'
    )

    return f"""
      <article class="card card--campaign">
        <a href="{link}" target="_blank" rel="nofollow noopener noreferrer">
          <div class="card-badge">{badge}</div>
          <div class="card-img">{img_html}</div>
          <div class="card-body">
            <h3>{name}</h3>
            {code_html}
            {expiry}
            <span class="btn">🛒 Ver oferta</span>
          </div>
        </a>
      </article>"""


# ── Template HTML ────────────────────────────────────────────────────────────

CSS = """
  :root {
    --bg: #0d1117;
    --bg2: #161b22;
    --border: #30363d;
    --green: #3fb950;
    --green-dim: #238636;
    --text: #e6edf3;
    --muted: #8b949e;
    --card-bg: #161b22;
    --radius: 12px;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; line-height: 1.5; }

  /* Header */
  header { background: linear-gradient(135deg, #0d1117 0%, #161b22 100%); border-bottom: 1px solid var(--border); padding: 2rem 1rem; text-align: center; }
  header h1 { font-size: clamp(1.6rem, 5vw, 2.4rem); color: var(--green); letter-spacing: -0.5px; }
  header p  { color: var(--muted); margin-top: .4rem; }
  .stamp    { display: inline-block; margin-top: .8rem; font-size: .75rem; color: var(--muted); background: var(--bg); border: 1px solid var(--border); border-radius: 20px; padding: .2rem .8rem; }

  /* Layout */
  main { max-width: 1280px; margin: 0 auto; padding: 2rem 1rem 4rem; }
  section { margin-bottom: 3rem; }
  section > h2 { font-size: 1.2rem; color: var(--text); border-left: 3px solid var(--green); padding-left: .75rem; margin-bottom: 1.25rem; }

  /* Grid */
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 1rem; }

  /* Card */
  .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; transition: transform .15s, border-color .15s; position: relative; }
  .card:hover { transform: translateY(-3px); border-color: var(--green-dim); }
  .card a { display: flex; flex-direction: column; height: 100%; text-decoration: none; color: inherit; }
  .card-img { aspect-ratio: 1/1; background: #0d1117; display: flex; align-items: center; justify-content: center; overflow: hidden; }
  .card-img img { width: 100%; height: 100%; object-fit: cover; }
  .no-img { font-size: 3rem; }
  .card-body { padding: .85rem; display: flex; flex-direction: column; gap: .4rem; flex: 1; }
  .card-body h3 { font-size: .82rem; color: var(--text); display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .price { font-size: 1rem; font-weight: 700; color: var(--green); margin-top: auto; }
  .coupon-code { font-size: .8rem; background: #1f2937; border: 1px dashed var(--green-dim); border-radius: 6px; padding: .3rem .5rem; color: var(--green); }
  .expiry { font-size: .75rem; color: var(--muted); }
  .btn { display: block; text-align: center; background: var(--green-dim); color: #fff; border-radius: 6px; padding: .45rem; font-size: .8rem; font-weight: 600; margin-top: auto; transition: background .15s; }
  .card:hover .btn { background: var(--green); }
  .card-badge { position: absolute; top: .5rem; right: .5rem; background: rgba(13,17,23,.85); border: 1px solid var(--border); border-radius: 20px; font-size: .7rem; padding: .15rem .5rem; color: var(--text); backdrop-filter: blur(4px); }
  .card--campaign .card-img { aspect-ratio: 16/9; }

  /* Footer */
  footer { text-align: center; padding: 2rem 1rem; border-top: 1px solid var(--border); color: var(--muted); font-size: .8rem; line-height: 1.8; }
  footer a { color: var(--green); text-decoration: none; }

  /* Aviso */
  .aviso { background: #161b22; border: 1px solid var(--border); border-radius: var(--radius); padding: 1.5rem; text-align: center; color: var(--muted); }

  @media (max-width: 480px) {
    .grid { grid-template-columns: repeat(2, 1fr); }
  }
"""


def build_html(products: list[dict], campaigns: list[dict]) -> str:
    now        = datetime.now().strftime("%d/%m/%Y às %H:%M")
    year       = datetime.now().year
    total      = len(products) + len(campaigns)
    prod_cards = "".join(product_card(p)  for p in products[:60])
    camp_cards = "".join(campaign_card(c) for c in campaigns[:20])

    sec_camps = f"""
    <section id="campanhas">
      <h2>🎫 Cupons &amp; Campanhas</h2>
      <div class="grid">{camp_cards}</div>
    </section>""" if camp_cards else ""

    sec_prods = f"""
    <section id="ofertas">
      <h2>🔥 Ofertas do Dia</h2>
      <div class="grid">{prod_cards}</div>
    </section>""" if prod_cards else """
    <section>
      <div class="aviso">Nenhuma oferta disponível no momento. Volte em breve!</div>
    </section>"""

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Terminal de Ofertas — Melhores promoções do dia</title>
  <meta name="description" content="As melhores ofertas e cupons de desconto selecionados automaticamente. Promoções de celulares, notebooks, tênis, perfumes e muito mais.">
  <meta name="robots" content="index, follow">
  <meta property="og:title"       content="Terminal de Ofertas">
  <meta property="og:description" content="As melhores ofertas do dia selecionadas para você.">
  <meta property="og:type"        content="website">
  <meta property="og:url"         content="{SITE_URL}">
  <link rel="canonical"           href="{SITE_URL}">
  <style>{CSS}</style>
</head>
<body>
  <header>
    <h1>💻 Terminal de Ofertas</h1>
    <p>As melhores promoções selecionadas automaticamente</p>
    <span class="stamp">Atualizado em {now} &bull; {total} ofertas</span>
  </header>

  <main>
    {sec_camps}
    {sec_prods}
  </main>

  <footer>
    <p>&copy; {year} Terminal de Ofertas</p>
    <p>Alguns links são de afiliados. Ao comprar você apoia o canal sem pagar a mais.</p>
    <p>Siga no Telegram: <a href="https://t.me/terminaldeofertas" target="_blank">@terminaldeofertas</a></p>
  </footer>
</body>
</html>"""


def build_sitemap(products: list[dict]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    urls = [f"""  <url>
    <loc>{SITE_URL}/</loc>
    <lastmod>{now}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>1.0</priority>
  </url>"""]
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{"".join(urls)}
</urlset>"""


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not LOMADEE_KEY:
        print("ERRO: LOMADEE_TOKEN não configurado.")
        return

    print("Buscando produtos...")
    products  = fetch_products()

    print("Buscando campanhas...")
    campaigns = fetch_campaigns()

    print(f"Total: {len(products)} produtos, {len(campaigns)} campanhas")

    print("Gerando index.html...")
    html = build_html(products, campaigns)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("Gerando sitemap.xml...")
    sitemap = build_sitemap(products)
    with open("sitemap.xml", "w", encoding="utf-8") as f:
        f.write(sitemap)

    print("Pronto!")


if __name__ == "__main__":
    main()
