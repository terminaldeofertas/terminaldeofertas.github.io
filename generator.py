"""
generator.py — Terminal de Ofertas
Busca produtos da Lomadee (API) e Shopee (CSV via S3) e gera index.html + sitemap.xml.
Roda via GitHub Actions a cada 3 horas.
"""

import csv
import io
import os
import random
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────

LOMADEE_KEY  = os.getenv("LOMADEE_TOKEN", "")
SOURCE_ID    = os.getenv("SOURCE_ID", "")
SITE_URL     = os.getenv("SITE_URL", "https://terminaldeofertas.github.io").rstrip("/")
SEARCH_TERMS = [t.strip() for t in os.getenv("SEARCH_TERMS", "tênis,celular,notebook,perfume,smart tv,fone,tablet,geladeira,ar condicionado,câmera").split(",") if t.strip()]

# Shopee — local ou S3
CSV_LOCAL_PATH  = os.getenv("CSV_LOCAL_PATH", "")
S3_BUCKET       = os.getenv("S3_BUCKET", "")
S3_KEY          = os.getenv("S3_KEY", "shopee/products.csv")
AWS_REGION      = os.getenv("AWS_REGION", "us-east-1")
SHOPEE_SUB_ID   = os.getenv("SHOPEE_SUB_ID", "site")
MIN_DISCOUNT    = float(os.getenv("MIN_DISCOUNT", "15"))
MIN_ITEM_RATING = float(os.getenv("MIN_ITEM_RATING", "4.5"))
MIN_SHOP_RATING = float(os.getenv("MIN_SHOP_RATING", "4.5"))
MIN_PRICE       = float(os.getenv("MIN_PRICE", "10"))
MAX_PRICE       = float(os.getenv("MAX_PRICE", "5000"))
RESERVOIR_SIZE  = int(os.getenv("RESERVOIR_SIZE", "120"))

# Lomadee endpoints
PRODUCTS_URL  = "https://api-beta.lomadee.com.br/affiliate/products"
CAMPAIGNS_URL = "https://api-beta.lomadee.com.br/affiliate/campaigns"
SHORTENER_URL = "https://api-beta.lomadee.com.br/affiliate/shortener/url"
BRANDS_URL    = "https://api-beta.lomadee.com.br/affiliate/brands"

HEADERS = {"x-api-key": LOMADEE_KEY, "Accept": "application/json"}
_brand_id_cache: dict = {}


# ── Helpers gerais ───────────────────────────────────────────────────────────

def fmt_price(price) -> str:
    try:
        return f"R$ {float(price):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "Ver preço"


def escape_html(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ── Lomadee ──────────────────────────────────────────────────────────────────

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


def fetch_lomadee_products(terms_sample: int = 8, limit_per_term: int = 12) -> list[dict]:
    products: list[dict] = []
    seen: set = set()
    terms = random.sample(SEARCH_TERMS, min(terms_sample, len(SEARCH_TERMS)))
    print(f"[Lomadee] Termos: {terms}")
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
            data = res.json().get("data", [])
            for prod in data:
                pid = prod.get("_id")
                if pid and pid not in seen:
                    seen.add(pid)
                    products.append(prod)
            print(f"  [{term}] {len(data)} produtos")
        except Exception as e:
            print(f"  [{term}] Erro: {e}")
    return products


def fetch_lomadee_campaigns(limit: int = 50) -> list[dict]:
    campaigns: list[dict] = []
    try:
        res = requests.get(CAMPAIGNS_URL, params={"limit": limit}, headers=HEADERS, timeout=15)
        if res.status_code == 200:
            for camp in res.json().get("data", []):
                if camp.get("status") == "onTime":
                    campaigns.append(camp)
        print(f"[Lomadee] Campanhas ativas: {len(campaigns)}")
    except Exception as e:
        print(f"[Lomadee] Erro campanhas: {e}")
    return campaigns


# ── Shopee ───────────────────────────────────────────────────────────────────

def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _shopee_row_eligible(row: dict) -> bool:
    discount   = _safe_float(row.get("discount_percentage", "0"))
    sale_price = _safe_float(row.get("sale_price", "0"))
    i_rating   = _safe_float(row.get("item_rating", "0"))
    s_rating   = _safe_float(row.get("shop_rating", "0"))
    link       = (row.get("product_short link") or row.get("product_link", "")).strip()
    return (
        discount   >= MIN_DISCOUNT
        and i_rating  >= MIN_ITEM_RATING
        and s_rating  >= MIN_SHOP_RATING
        and MIN_PRICE <= sale_price <= MAX_PRICE
        and bool(link)
    )


def _add_sub_id(link: str, sub_id: str) -> str:
    if not link:
        return link
    sep = "&" if "?" in link else "?"
    return f"{link}{sep}sub_id={sub_id}"


def _reservoir_sample(f, k: int) -> list[dict]:
    """Reservoir sampling O(k) — idêntico ao shopee_bot/main.py."""
    reservoir: list[dict] = []
    eligible_count = 0
    reader = csv.DictReader(f)
    for row in reader:
        if not _shopee_row_eligible(row):
            continue
        eligible_count += 1
        if len(reservoir) < k:
            reservoir.append(row)
        else:
            idx = random.randint(0, eligible_count - 1)
            if idx < k:
                reservoir[idx] = row
    print(f"  {eligible_count} elegíveis encontrados, {len(reservoir)} selecionados")
    return reservoir


def fetch_shopee_products() -> list[dict]:
    # Prioridade: arquivo local → S3
    if CSV_LOCAL_PATH:
        if not os.path.exists(CSV_LOCAL_PATH):
            print(f"[Shopee] Arquivo não encontrado: {CSV_LOCAL_PATH}")
            return []
        print(f"[Shopee] Lendo CSV local: {CSV_LOCAL_PATH}")
        try:
            with open(CSV_LOCAL_PATH, encoding="utf-8-sig", newline="") as f:
                products = _reservoir_sample(f, RESERVOIR_SIZE)
            print(f"[Shopee] {len(products)} produtos elegíveis selecionados")
            return products
        except Exception as e:
            print(f"[Shopee] Erro ao ler CSV local: {e}")
            return []

    if not S3_BUCKET:
        print("[Shopee] CSV_LOCAL_PATH e S3_BUCKET não configurados — pulando.")
        return []

    try:
        import boto3
        print(f"[Shopee] Baixando CSV de s3://{S3_BUCKET}/{S3_KEY} ...")
        s3  = boto3.client("s3", region_name=AWS_REGION)
        obj = s3.get_object(Bucket=S3_BUCKET, Key=S3_KEY)
        raw = obj["Body"].read().decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(raw))
        products = _reservoir_sample(reader, RESERVOIR_SIZE)
        print(f"[Shopee] {len(products)} produtos elegíveis selecionados")
        return products
    except ImportError:
        print("[Shopee] boto3 não instalado — pulando.")
        return []
    except Exception as e:
        print(f"[Shopee] Erro ao buscar CSV: {e}")
        return []


# ── Cards HTML ───────────────────────────────────────────────────────────────

def lomadee_product_card(prod: dict) -> str:
    name  = escape_html(prod.get("name", "Produto")[:90])
    url   = prod.get("url", "#")
    org   = prod.get("organizationId", "")
    aff   = get_affiliate_link(url, org) if url != "#" else "#"
    try:
        img = prod["images"][0]["url"]
    except Exception:
        img = ""
    try:
        price = prod["options"][0]["pricing"][0]["price"]
    except Exception:
        price = None

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


def shopee_product_card(row: dict) -> str:
    title    = escape_html(row.get("title", "Produto")[:90])
    sale     = row.get("sale_price", "")
    original = row.get("price", "")
    discount = row.get("discount_percentage", "")
    img      = row.get("image_link", "")
    link     = _add_sub_id(row.get("product_short link", row.get("product_link", "#")), SHOPEE_SUB_ID)
    rating   = row.get("item_rating", "")

    discount_badge = f'<span class="discount-badge">-{int(float(discount))}%</span>' if discount else ""
    original_html  = f'<span class="original-price">{fmt_price(original)}</span>' if original and original != sale else ""
    rating_html    = f'<span class="rating">⭐ {float(rating):.1f}</span>' if rating else ""

    img_html = (
        f'<img src="{img}" alt="{title}" loading="lazy" onerror="this.closest(\'.card-img\').innerHTML=\'<span class=no-img>📦</span>\'">'
        if img else '<span class="no-img">📦</span>'
    )
    return f"""
      <article class="card card--shopee">
        <a href="{link}" target="_blank" rel="nofollow noopener noreferrer">
          {discount_badge}
          <div class="card-img">{img_html}</div>
          <div class="card-body">
            <h3>{title}</h3>
            {rating_html}
            {original_html}
            <p class="price">{fmt_price(sale)}</p>
            <span class="btn btn--shopee">🛍️ Ver na Shopee</span>
          </div>
        </a>
      </article>"""


def campaign_card(camp: dict) -> str:
    name  = escape_html(camp.get("name", "Campanha")[:90])
    ctype = camp.get("type", "Offer")
    code  = camp.get("code", "")
    try:
        link = camp["channels"][0]["shortUrls"][0]
    except Exception:
        link = camp.get("url", "#")
    try:
        img = camp["mediaKit"]["banners"][0]
    except Exception:
        img = ""

    end_at = (camp.get("period") or {}).get("endAt", "")
    expiry = ""
    if end_at:
        try:
            dt = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
            expiry = f'<p class="expiry">📅 Válido até {dt.strftime("%d/%m/%Y")}</p>'
        except Exception:
            pass

    badge     = "🎫 Cupom" if ctype == "Coupon" else "🔥 Oferta"
    code_html = f'<p class="coupon-code">🔑 Cupom: <strong>{escape_html(code)}</strong></p>' if code else ""
    img_html  = (
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
    --bg: #0d1117; --bg2: #161b22; --border: #30363d;
    --green: #3fb950; --green-dim: #238636;
    --orange: #f78166; --shopee: #ee4d2d;
    --text: #e6edf3; --muted: #8b949e; --radius: 12px;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; line-height: 1.5; }

  header { background: linear-gradient(135deg, #0d1117 0%, #161b22 100%); border-bottom: 1px solid var(--border); padding: 2rem 1rem; text-align: center; }
  header h1 { font-size: clamp(1.6rem, 5vw, 2.4rem); color: var(--green); letter-spacing: -0.5px; }
  header p  { color: var(--muted); margin-top: .4rem; }
  .logo { width: 96px; height: 96px; border-radius: 20px; margin-bottom: .75rem; object-fit: cover; }
  .stamp    { display: inline-block; margin-top: .8rem; font-size: .75rem; color: var(--muted); background: var(--bg); border: 1px solid var(--border); border-radius: 20px; padding: .2rem .8rem; }
  .tg-link  { display: inline-block; margin-top: .75rem; font-size: .85rem; color: #fff; background: #229ED9; border-radius: 20px; padding: .35rem 1rem; text-decoration: none; font-weight: 600; transition: background .15s; }
  .tg-link:hover { background: #1a8bbf; }

  main { max-width: 1280px; margin: 0 auto; padding: 2rem 1rem 4rem; }
  section { margin-bottom: 3rem; }
  section > h2 { font-size: 1.2rem; color: var(--text); border-left: 3px solid var(--green); padding-left: .75rem; margin-bottom: 1.25rem; }
  section.shopee-sec > h2 { border-left-color: var(--shopee); }

  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 1rem; }

  .card { background: #161b22; border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; transition: transform .15s, border-color .15s; position: relative; }
  .card:hover { transform: translateY(-3px); border-color: var(--green-dim); }
  .card--shopee:hover { border-color: var(--shopee); }
  .card a { display: flex; flex-direction: column; height: 100%; text-decoration: none; color: inherit; }
  .card-img { aspect-ratio: 1/1; background: #0d1117; display: flex; align-items: center; justify-content: center; overflow: hidden; }
  .card-img img { width: 100%; height: 100%; object-fit: cover; }
  .no-img { font-size: 3rem; }
  .card--campaign .card-img { aspect-ratio: 16/9; }
  .card-body { padding: .85rem; display: flex; flex-direction: column; gap: .35rem; flex: 1; }
  .card-body h3 { font-size: .82rem; color: var(--text); display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .price { font-size: 1rem; font-weight: 700; color: var(--green); margin-top: auto; }
  .card--shopee .price { color: var(--shopee); }
  .original-price { font-size: .75rem; color: var(--muted); text-decoration: line-through; }
  .rating { font-size: .75rem; color: var(--muted); }
  .discount-badge { position: absolute; top: .5rem; left: .5rem; background: var(--shopee); color: #fff; border-radius: 6px; font-size: .7rem; font-weight: 700; padding: .15rem .4rem; z-index: 1; }
  .coupon-code { font-size: .8rem; background: #1f2937; border: 1px dashed var(--green-dim); border-radius: 6px; padding: .3rem .5rem; color: var(--green); }
  .expiry { font-size: .75rem; color: var(--muted); }
  .card-badge { position: absolute; top: .5rem; right: .5rem; background: rgba(13,17,23,.85); border: 1px solid var(--border); border-radius: 20px; font-size: .7rem; padding: .15rem .5rem; color: var(--text); backdrop-filter: blur(4px); }
  .btn { display: block; text-align: center; background: var(--green-dim); color: #fff; border-radius: 6px; padding: .45rem; font-size: .8rem; font-weight: 600; margin-top: auto; transition: background .15s; }
  .card:hover .btn { background: var(--green); }
  .btn--shopee { background: var(--shopee); }
  .card--shopee:hover .btn--shopee { background: #d44000; }

  footer { text-align: center; padding: 2rem 1rem; border-top: 1px solid var(--border); color: var(--muted); font-size: .8rem; line-height: 1.8; }
  footer a { color: var(--green); text-decoration: none; }
  .aviso { background: #161b22; border: 1px solid var(--border); border-radius: var(--radius); padding: 1.5rem; text-align: center; color: var(--muted); }

  @media (max-width: 480px) { .grid { grid-template-columns: repeat(2, 1fr); } }
"""


def build_html(
    lomadee_products: list[dict],
    campaigns: list[dict],
    shopee_products: list[dict],
) -> str:
    brt   = timezone(timedelta(hours=-3))
    now   = datetime.now(brt).strftime("%d/%m/%Y às %H:%M")
    year  = datetime.now(brt).year
    total = len(lomadee_products) + len(campaigns) + len(shopee_products)

    camp_cards    = "".join(campaign_card(c)          for c in campaigns[:20])
    lomadee_cards = "".join(lomadee_product_card(p)   for p in lomadee_products[:50])
    shopee_cards  = "".join(shopee_product_card(r)    for r in shopee_products[:40])

    sec_camps = f"""
    <section id="campanhas">
      <h2>🎫 Cupons &amp; Campanhas</h2>
      <div class="grid">{camp_cards}</div>
    </section>""" if camp_cards else ""

    sec_shopee = f"""
    <section id="shopee" class="shopee-sec">
      <h2>🛍️ Ofertas Shopee</h2>
      <div class="grid">{shopee_cards}</div>
    </section>""" if shopee_cards else ""

    sec_lomadee = f"""
    <section id="ofertas">
      <h2>🔥 Outras Ofertas</h2>
      <div class="grid">{lomadee_cards}</div>
    </section>""" if lomadee_cards else ""

    if not camp_cards and not lomadee_cards and not shopee_cards:
        sec_lomadee = '<section><div class="aviso">Nenhuma oferta disponível no momento. Volte em breve!</div></section>'

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Terminal de Ofertas — Melhores promoções do dia</title>
  <meta name="description" content="As melhores ofertas e cupons de desconto selecionados automaticamente. Promoções de celulares, notebooks, tênis, perfumes e muito mais.">
  <meta name="robots" content="index, follow">
  <meta name="lomadee" content="2324685">
  <meta name="google-adsense-account" content="ca-pub-5535939262663776">
  <meta property="og:title"       content="Terminal de Ofertas">
  <meta property="og:description" content="As melhores ofertas do dia selecionadas para você.">
  <meta property="og:type"        content="website">
  <meta property="og:url"         content="{SITE_URL}">
  <link rel="canonical"           href="{SITE_URL}">
  <style>{CSS}</style>
</head>
<body>
  <header>
    <img src="logo.jpg" alt="Terminal de Ofertas" class="logo">
    <h1>Terminal de Ofertas</h1>
    <p>As melhores promoções selecionadas automaticamente</p>
    <a href="https://t.me/terminaldeofertas" target="_blank" class="tg-link">📢 Siga no Telegram</a>
    <span class="stamp">Atualizado em {now} &bull; {total} ofertas</span>
  </header>
  <main>
    {sec_camps}
    {sec_shopee}
    {sec_lomadee}
  </main>
  <footer>
    <p>&copy; {year} Terminal de Ofertas</p>
    <p>Alguns links são de afiliados. Ao comprar você apoia o canal sem pagar a mais.</p>
    <p>⚠️ Os preços e a disponibilidade dos produtos podem ser alterados a qualquer momento conforme o parceiro.</p>
    <p>Siga no Telegram: <a href="https://t.me/terminaldeofertas" target="_blank">@terminaldeofertas</a></p>
  </footer>
</body>
</html>"""


def build_sitemap() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{SITE_URL}/</loc>
    <lastmod>{now}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>"""


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not LOMADEE_KEY:
        print("ERRO: LOMADEE_TOKEN não configurado.")
        return

    print("=== Lomadee ===")
    lomadee_products = fetch_lomadee_products()
    campaigns        = fetch_lomadee_campaigns()

    print("\n=== Shopee ===")
    shopee_products = fetch_shopee_products()

    print(f"\nTotal: {len(lomadee_products)} lomadee, {len(shopee_products)} shopee, {len(campaigns)} campanhas")

    print("\nGerando index.html...")
    html = build_html(lomadee_products, campaigns, shopee_products)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("Gerando sitemap.xml...")
    with open("sitemap.xml", "w", encoding="utf-8") as f:
        f.write(build_sitemap())

    print("Pronto!")


if __name__ == "__main__":
    main()
