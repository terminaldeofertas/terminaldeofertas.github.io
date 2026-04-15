"""
generator.py — Terminal de Ofertas
Busca produtos da Lomadee (API) e Shopee (CSV via S3) e gera index.html + sitemap.xml.
Roda via GitHub Actions a cada 3 horas.
"""

import csv
import io
import json
import os
import random
import re
import unicodedata
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


# Diretório onde ficam as páginas individuais de produto
PAGES_DIR = "p"

# Limites na página principal
INDEX_MAX_CAMPAIGNS = 5
INDEX_MAX_SHOPEE    = 15
INDEX_MAX_LOMADEE   = 10


# ── Helpers gerais ───────────────────────────────────────────────────────────

def slugify(text: str, suffix: str = "", max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", str(text))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    slug = text[:max_len].rstrip("-")
    return f"{slug}-{suffix}" if suffix else slug


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

def lomadee_product_card(prod: dict, page_url: str = "") -> str:
    name  = escape_html(prod.get("name", "Produto")[:90])
    url   = prod.get("url", "#")
    org   = prod.get("organizationId", "")
    aff   = get_affiliate_link(url, org) if url != "#" else "#"
    href  = page_url if page_url else aff
    ext   = not page_url
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
    a_attrs = 'target="_blank" rel="nofollow noopener noreferrer"' if ext else ""
    return f"""
      <article class="card">
        <a href="{href}" {a_attrs}>
          <div class="card-img">{img_html}</div>
          <div class="card-body">
            <h3>{name}</h3>
            <p class="price">{fmt_price(price)}</p>
            <span class="btn">🛒 Ver oferta</span>
          </div>
        </a>
      </article>"""


def shopee_product_card(row: dict, page_url: str = "") -> str:
    title    = escape_html(row.get("title", "Produto")[:90])
    sale     = row.get("sale_price", "")
    original = row.get("price", "")
    discount = row.get("discount_percentage", "")
    img      = row.get("image_link", "")
    link     = _add_sub_id(row.get("product_short link", row.get("product_link", "#")), SHOPEE_SUB_ID)
    rating   = row.get("item_rating", "")
    href     = page_url if page_url else link
    ext      = not page_url

    discount_badge = f'<span class="discount-badge">-{int(float(discount))}%</span>' if discount else ""
    original_html  = f'<span class="original-price">{fmt_price(original)}</span>' if original and original != sale else ""
    rating_html    = f'<span class="rating">⭐ {float(rating):.1f}</span>' if rating else ""

    img_html = (
        f'<img src="{img}" alt="{title}" loading="lazy" onerror="this.closest(\'.card-img\').innerHTML=\'<span class=no-img>📦</span>\'">'
        if img else '<span class="no-img">📦</span>'
    )
    a_attrs = 'target="_blank" rel="nofollow noopener noreferrer"' if ext else ""
    return f"""
      <article class="card card--shopee">
        <a href="{href}" {a_attrs}>
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


def campaign_card(camp: dict, page_url: str = "") -> str:
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
    href = page_url if page_url else link
    ext  = not page_url

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
    a_attrs = 'target="_blank" rel="nofollow noopener noreferrer"' if ext else ""
    return f"""
      <article class="card card--campaign">
        <a href="{href}" {a_attrs}>
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


# ── Páginas individuais de produto ───────────────────────────────────────────

def _page_header(year: int) -> str:
    return f"""  <header>
    <a href="../index.html"><img src="../logo.jpg" alt="Terminal de Ofertas" class="logo"></a>
    <h1><a href="../index.html" style="color:inherit;text-decoration:none">Terminal de Ofertas</a></h1>
    <p>As melhores promoções selecionadas automaticamente</p>
    <a href="https://t.me/terminaldeofertas" target="_blank" class="tg-link">📢 Siga no Telegram</a>
  </header>"""


def _page_footer(year: int) -> str:
    return f"""  <footer>
    <p>&copy; {year} Terminal de Ofertas</p>
    <p>Alguns links são de afiliados. Ao comprar você apoia o canal sem pagar a mais.</p>
    <p>Siga no Telegram: <a href="https://t.me/terminaldeofertas" target="_blank">@terminaldeofertas</a></p>
  </footer>"""


def build_auto_product_page(
    slug: str,
    title: str,
    aff_url: str,
    img: str = "",
    price=None,
    original_price=None,
    discount=None,
    rating: str = "",
    shop_rating: str = "",
    source_label: str = "",
    source_color: str = "var(--green)",
    btn_label: str = "🛒 Ver oferta",
    code: str = "",
    expiry: str = "",
) -> str:
    brt  = timezone(timedelta(hours=-3))
    year = datetime.now(brt).year

    title_esc = escape_html(title)
    discount_badge = f'<span class="discount-badge" style="font-size:.9rem;padding:.3rem .7rem">-{int(float(discount))}%</span>' if discount else ""
    original_html  = f'<p style="color:var(--muted);font-size:.9rem;text-decoration:line-through">{fmt_price(original_price)}</p>' if original_price and original_price != price else ""
    rating_html    = f'<p style="color:var(--muted);font-size:.85rem">⭐ Item: {float(rating):.1f}</p>' if rating else ""
    shop_html      = f'<p style="color:var(--muted);font-size:.85rem">🏪 Loja: {float(shop_rating):.1f}</p>' if shop_rating else ""
    source_html    = f'<span style="background:{source_color};color:#fff;border-radius:20px;padding:.2rem .75rem;font-size:.75rem;font-weight:600">{escape_html(source_label)}</span>' if source_label else ""
    code_html      = f'<p class="coupon-code" style="margin-top:.75rem">🔑 Cupom: <strong>{escape_html(code)}</strong></p>' if code else ""
    expiry_html    = f'<p style="color:var(--muted);font-size:.8rem">📅 {escape_html(expiry)}</p>' if expiry else ""

    img_html = (
        f'<img src="{img}" alt="{title_esc}" style="max-width:340px;width:100%;border-radius:12px;object-fit:cover">'
        if img else '<span style="font-size:5rem">📦</span>'
    )

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title_esc} — Terminal de Ofertas</title>
  <meta name="description" content="Oferta: {title_esc}. Veja o preço e compre com desconto.">
  <meta name="robots" content="index, follow">
  <meta name="google-adsense-account" content="ca-pub-5535939262663776">
  <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-5535939262663776" crossorigin="anonymous"></script>
  <!-- Google tag (gtag.js) -->
  <script async src="https://www.googletagmanager.com/gtag/js?id=G-Q1PYFX64R8"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){{dataLayer.push(arguments);}}
    gtag('js', new Date());
    gtag('config', 'G-Q1PYFX64R8');
  </script>
  <meta property="og:title"   content="{title_esc} — Terminal de Ofertas">
  <meta property="og:image"   content="{img}">
  <meta property="og:type"    content="product">
  <meta property="og:url"     content="{SITE_URL}/{PAGES_DIR}/{slug}.html">
  <link rel="canonical"       href="{SITE_URL}/{PAGES_DIR}/{slug}.html">
  <style>{CSS}
    .prod-page {{ max-width: 720px; margin: 0 auto; padding: 2rem 1rem 4rem; }}
    .breadcrumb {{ font-size: .82rem; color: var(--muted); margin-bottom: 1.5rem; }}
    .breadcrumb a {{ color: var(--green); text-decoration: none; }}
    .prod-hero {{ display: flex; gap: 2rem; align-items: flex-start; flex-wrap: wrap; }}
    .prod-hero-text {{ flex: 1; min-width: 220px; }}
    .prod-hero-text h1 {{ font-size: clamp(1.2rem, 4vw, 1.7rem); margin-bottom: .75rem; }}
    .buy-btn {{ display: block; text-align: center; background: var(--green-dim); color: #fff; border-radius: 8px; padding: .85rem 1.5rem; font-size: 1rem; font-weight: 700; text-decoration: none; margin-top: 1.25rem; transition: background .15s; }}
    .buy-btn:hover {{ background: var(--green); }}
  </style>
</head>
<body>
{_page_header(year)}
  <div class="prod-page">
    <p class="breadcrumb"><a href="../index.html">← Voltar às ofertas</a></p>
    <div class="prod-hero">
      <div style="text-align:center">{img_html}</div>
      <div class="prod-hero-text">
        <h1>{title_esc}</h1>
        {source_html}
        {discount_badge}
        {original_html}
        <p class="price" style="font-size:1.6rem;margin-top:.5rem">{fmt_price(price)}</p>
        {rating_html}
        {shop_html}
        {code_html}
        {expiry_html}
        <a href="{aff_url}" target="_blank" rel="nofollow noopener noreferrer" class="buy-btn">{btn_label}</a>
        <p style="font-size:.72rem;color:var(--muted);margin-top:.5rem">Link de afiliado — ao comprar você apoia o canal sem pagar a mais.</p>
      </div>
    </div>
  </div>
{_page_footer(year)}
</body>
</html>"""


def page_path(slug: str) -> str:
    """Caminho relativo do index.html para a página do produto."""
    return f"{PAGES_DIR}/{slug}.html"




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

  .card--rec:hover { border-color: #7c3aed; }
  .btn--rec { background: #7c3aed; }
  .card--rec:hover .btn--rec { background: #6d28d9; }

  @media (max-width: 480px) { .grid { grid-template-columns: repeat(2, 1fr); } }
"""


def build_html(
    lomadee_products: list[dict],
    campaigns: list[dict],
    shopee_products: list[dict],
    recommendations: list[dict] = None,
    lomadee_slugs: dict = None,
    shopee_slugs: dict = None,
    campaign_slugs: dict = None,
) -> str:
    brt   = timezone(timedelta(hours=-3))
    now   = datetime.now(brt).strftime("%d/%m/%Y às %H:%M")
    year  = datetime.now(brt).year
    recs  = recommendations or []
    lslugs = lomadee_slugs or {}
    sslugs = shopee_slugs or {}
    cslugs = campaign_slugs or {}

    camps_idx   = campaigns[:INDEX_MAX_CAMPAIGNS]
    shopee_idx  = shopee_products[:INDEX_MAX_SHOPEE]
    lomadee_idx = lomadee_products[:INDEX_MAX_LOMADEE]
    total = len(camps_idx) + len(shopee_idx) + len(lomadee_idx)

    camp_cards    = "".join(campaign_card(c, page_path(cslugs[c.get("_id","")])) if c.get("_id","") in cslugs else campaign_card(c) for c in camps_idx)
    lomadee_cards = "".join(lomadee_product_card(p, page_path(lslugs[p.get("_id","")])) if p.get("_id","") in lslugs else lomadee_product_card(p) for p in lomadee_idx)
    shopee_cards  = "".join(shopee_product_card(r, page_path(sslugs[i])) if i in sslugs else shopee_product_card(r) for i, r in enumerate(shopee_idx))
    rec_cards     = "".join(recommendation_card(r) for r in recs)

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

    sec_recs = f"""
    <section id="indicacoes">
      <h2>📖 Indicações</h2>
      <div class="grid">{rec_cards}</div>
    </section>""" if rec_cards else ""

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
  <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-5535939262663776" crossorigin="anonymous"></script>
  <!-- Google tag (gtag.js) -->
  <script async src="https://www.googletagmanager.com/gtag/js?id=G-Q1PYFX64R8"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){{dataLayer.push(arguments);}}
    gtag('js', new Date());
    gtag('config', 'G-Q1PYFX64R8');
  </script>
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
    {sec_recs}
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


# ── Indicações (páginas editoriais) ─────────────────────────────────────────

RECOMMENDATIONS_FILE = "recommendations.json"


def load_recommendations() -> list[dict]:
    if not os.path.exists(RECOMMENDATIONS_FILE):
        return []
    try:
        with open(RECOMMENDATIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Indicações] Erro ao ler {RECOMMENDATIONS_FILE}: {e}")
        return []


def recommendation_card(rec: dict) -> str:
    """Card compacto para a seção de indicações no index.html."""
    title    = escape_html(rec.get("title", "Produto"))
    slug     = rec.get("slug", "")
    img      = rec.get("image", "")
    price    = rec.get("price")
    discount = rec.get("discount")
    store    = escape_html(rec.get("store", ""))
    page_url = f"indicacao-{slug}.html"

    discount_badge = f'<span class="discount-badge">-{int(discount)}%</span>' if discount else ""
    store_html     = f'<span class="rating">🏪 {store}</span>' if store else ""

    img_html = (
        f'<img src="{img}" alt="{title}" loading="lazy" onerror="this.closest(\'.card-img\').innerHTML=\'<span class=no-img>📦</span>\'">'
        if img else '<span class="no-img">📦</span>'
    )
    return f"""
      <article class="card card--rec">
        <a href="{page_url}">
          {discount_badge}
          <div class="card-img">{img_html}</div>
          <div class="card-body">
            <h3>{title}</h3>
            {store_html}
            <p class="price">{fmt_price(price)}</p>
            <span class="btn btn--rec">📖 Ver indicação</span>
          </div>
        </a>
      </article>"""


def build_recommendation_page(rec: dict) -> str:
    """Gera HTML completo para a página de uma indicação."""
    brt   = timezone(timedelta(hours=-3))
    year  = datetime.now(brt).year

    title        = escape_html(rec.get("title", "Produto"))
    category     = escape_html(rec.get("category", ""))
    img          = rec.get("image", "")
    price        = rec.get("price")
    original     = rec.get("original_price")
    discount     = rec.get("discount")
    aff_url      = rec.get("affiliate_url", "#")
    store        = escape_html(rec.get("store", ""))
    rating       = rec.get("rating")
    description  = rec.get("description", "")
    pros         = rec.get("pros", [])
    cons         = rec.get("cons", [])
    slug         = rec.get("slug", "")

    # Converte quebras de linha em parágrafos
    desc_html = "".join(f"<p>{escape_html(p)}</p>" for p in description.split("\n\n") if p.strip())

    discount_badge = f'<span class="discount-badge" style="font-size:.9rem;padding:.3rem .7rem">-{int(discount)}%</span>' if discount else ""
    original_html  = f'<span class="original-price" style="font-size:1rem">{fmt_price(original)}</span>' if original and original != price else ""
    rating_html    = f'<p style="color:var(--muted);margin-top:.25rem">⭐ {float(rating):.1f} / 5</p>' if rating else ""
    store_html     = f'<p style="color:var(--muted);font-size:.85rem">🏪 {store}</p>' if store else ""

    pros_html = "".join(f"<li>✅ {escape_html(p)}</li>" for p in pros)
    cons_html = "".join(f"<li>❌ {escape_html(c)}</li>" for c in cons)

    pros_section = f"""
    <div class="pros-cons">
      <div class="pro-list">
        <h3>Pontos positivos</h3>
        <ul>{pros_html}</ul>
      </div>
      {"" if not cons_html else f'<div class="con-list"><h3>Pontos negativos</h3><ul>{cons_html}</ul></div>'}
    </div>""" if pros or cons else ""

    img_html = (
        f'<img src="{img}" alt="{title}" style="max-width:340px;width:100%;border-radius:12px;object-fit:cover">'
        if img else ""
    )

    breadcrumb = f'<a href="index.html">Início</a> › <span>{category}</span>' if category else '<a href="index.html">Início</a>'

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — Terminal de Ofertas</title>
  <meta name="description" content="Indicação e análise: {title}. Veja prós, contras, preço e onde comprar.">
  <meta name="robots" content="index, follow">
  <meta name="google-adsense-account" content="ca-pub-5535939262663776">
  <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-5535939262663776" crossorigin="anonymous"></script>
  <!-- Google tag (gtag.js) -->
  <script async src="https://www.googletagmanager.com/gtag/js?id=G-Q1PYFX64R8"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){{dataLayer.push(arguments);}}
    gtag('js', new Date());
    gtag('config', 'G-Q1PYFX64R8');
  </script>
  <meta property="og:title"       content="{title} — Terminal de Ofertas">
  <meta property="og:description" content="Indicação e análise: {title}.">
  <meta property="og:type"        content="article">
  <meta property="og:image"       content="{img}">
  <meta property="og:url"         content="{SITE_URL}/indicacao-{slug}.html">
  <link rel="canonical"           href="{SITE_URL}/indicacao-{slug}.html">
  <style>
    {CSS}
    .rec-page {{ max-width: 780px; margin: 0 auto; padding: 2rem 1rem 4rem; }}
    .breadcrumb {{ font-size: .82rem; color: var(--muted); margin-bottom: 1.5rem; }}
    .breadcrumb a {{ color: var(--green); text-decoration: none; }}
    .rec-hero {{ display: flex; gap: 2rem; align-items: flex-start; flex-wrap: wrap; margin-bottom: 2rem; }}
    .rec-hero-text {{ flex: 1; min-width: 220px; }}
    .rec-hero-text h1 {{ font-size: clamp(1.3rem, 4vw, 1.9rem); margin-bottom: .5rem; }}
    .rec-description {{ line-height: 1.8; color: var(--text); }}
    .rec-description p {{ margin-bottom: 1rem; }}
    .pros-cons {{ display: flex; gap: 1.5rem; flex-wrap: wrap; margin: 2rem 0; }}
    .pro-list, .con-list {{ flex: 1; min-width: 200px; background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius); padding: 1rem 1.25rem; }}
    .pro-list h3 {{ color: var(--green); margin-bottom: .75rem; font-size: .95rem; }}
    .con-list h3 {{ color: var(--orange); margin-bottom: .75rem; font-size: .95rem; }}
    .pros-cons ul {{ list-style: none; display: flex; flex-direction: column; gap: .5rem; }}
    .pros-cons li {{ font-size: .88rem; color: var(--text); }}
    .buy-btn {{ display: block; text-align: center; background: var(--green-dim); color: #fff; border-radius: 8px; padding: .85rem 1.5rem; font-size: 1rem; font-weight: 700; text-decoration: none; margin-top: 1.25rem; transition: background .15s; }}
    .buy-btn:hover {{ background: var(--green); }}
  </style>
</head>
<body>
  <header>
    <img src="logo.jpg" alt="Terminal de Ofertas" class="logo">
    <h1>Terminal de Ofertas</h1>
    <p>As melhores promoções selecionadas automaticamente</p>
    <a href="https://t.me/terminaldeofertas" target="_blank" class="tg-link">📢 Siga no Telegram</a>
  </header>
  <div class="rec-page">
    <p class="breadcrumb">{breadcrumb}</p>
    <div class="rec-hero">
      {img_html}
      <div class="rec-hero-text">
        <h1>{title}</h1>
        {rating_html}
        {store_html}
        {discount_badge}
        <p style="margin-top:.75rem">{original_html} <span class="price" style="font-size:1.6rem">{fmt_price(price)}</span></p>
        <a href="{aff_url}" target="_blank" rel="nofollow noopener noreferrer" class="buy-btn">🛒 Ver oferta em {store or "loja parceira"}</a>
        <p style="font-size:.72rem;color:var(--muted);margin-top:.5rem">Link de afiliado — ao comprar você apoia o canal sem pagar a mais.</p>
      </div>
    </div>
    <section class="rec-description">
      {desc_html}
    </section>
    {pros_section}
    <div style="text-align:center;margin-top:2rem">
      <a href="{aff_url}" target="_blank" rel="nofollow noopener noreferrer" class="buy-btn" style="display:inline-block;max-width:360px">🛒 Comprar {title}</a>
    </div>
  </div>
  <footer>
    <p>&copy; {year} Terminal de Ofertas</p>
    <p>Alguns links são de afiliados. Ao comprar você apoia o canal sem pagar a mais.</p>
    <p>Siga no Telegram: <a href="https://t.me/terminaldeofertas" target="_blank">@terminaldeofertas</a></p>
  </footer>
</body>
</html>"""


def build_sitemap(all_slugs: list[str] = None, recommendations: list[dict] = None) -> str:
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slugs = all_slugs or []
    recs  = recommendations or []

    product_urls = "".join(f"""
  <url>
    <loc>{SITE_URL}/{PAGES_DIR}/{slug}.html</loc>
    <lastmod>{now}</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.6</priority>
  </url>""" for slug in slugs)

    rec_urls = "".join(f"""
  <url>
    <loc>{SITE_URL}/indicacao-{rec['slug']}.html</loc>
    <lastmod>{now}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.7</priority>
  </url>""" for rec in recs if rec.get("slug"))

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{SITE_URL}/</loc>
    <lastmod>{now}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>1.0</priority>
  </url>{rec_urls}{product_urls}
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

    print("\n=== Indicações ===")
    recommendations = load_recommendations()
    print(f"  {len(recommendations)} indicações carregadas")

    print(f"\nTotal: {len(lomadee_products)} lomadee, {len(shopee_products)} shopee, {len(campaigns)} campanhas, {len(recommendations)} indicações")

    # Limpa e recria o diretório de páginas individuais
    import shutil
    if os.path.exists(PAGES_DIR):
        shutil.rmtree(PAGES_DIR)
    os.makedirs(PAGES_DIR)

    all_slugs: list[str] = []
    lomadee_slugs: dict  = {}  # _id → slug
    shopee_slugs: dict   = {}  # índice → slug
    campaign_slugs: dict = {}  # _id → slug

    print(f"\nGerando páginas individuais em {PAGES_DIR}/...")

    # Páginas Lomadee
    seen_slugs: set = set()
    for prod in lomadee_products[:INDEX_MAX_LOMADEE]:
        pid  = prod.get("_id", "")
        name = prod.get("name", "produto")
        slug = slugify(name, suffix=pid[:6] if pid else "", max_len=55)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        url = prod.get("url", "#")
        org = prod.get("organizationId", "")
        aff = get_affiliate_link(url, org) if url != "#" else "#"
        try:
            img = prod["images"][0]["url"]
        except Exception:
            img = ""
        try:
            price = prod["options"][0]["pricing"][0]["price"]
        except Exception:
            price = None

        html = build_auto_product_page(
            slug=slug, title=name, aff_url=aff, img=img, price=price,
            source_label="Lomadee", btn_label="🛒 Ver oferta",
        )
        with open(f"{PAGES_DIR}/{slug}.html", "w", encoding="utf-8") as f:
            f.write(html)
        all_slugs.append(slug)
        if pid:
            lomadee_slugs[pid] = slug

    print(f"  ✓ {len(lomadee_slugs)} páginas Lomadee")

    # Páginas Shopee
    for i, row in enumerate(shopee_products[:INDEX_MAX_SHOPEE]):
        title = row.get("title", "produto")
        slug  = slugify(title, suffix=str(i), max_len=55)
        if slug in seen_slugs:
            slug = f"{slug}-{i}"
        seen_slugs.add(slug)

        sale     = row.get("sale_price")
        original = row.get("price")
        discount = row.get("discount_percentage")
        img      = row.get("image_link", "")
        link     = _add_sub_id(row.get("product_short link", row.get("product_link", "#")), SHOPEE_SUB_ID)
        rating   = row.get("item_rating", "")
        s_rating = row.get("shop_rating", "")

        html = build_auto_product_page(
            slug=slug, title=title, aff_url=link, img=img,
            price=sale, original_price=original, discount=discount,
            rating=rating, shop_rating=s_rating,
            source_label="Shopee", source_color="var(--shopee)",
            btn_label="🛍️ Ver na Shopee",
        )
        with open(f"{PAGES_DIR}/{slug}.html", "w", encoding="utf-8") as f:
            f.write(html)
        all_slugs.append(slug)
        shopee_slugs[i] = slug

    print(f"  ✓ {len(shopee_slugs)} páginas Shopee")

    # Páginas Campanhas
    for camp in campaigns[:INDEX_MAX_CAMPAIGNS]:
        cid  = camp.get("_id", "")
        name = camp.get("name", "campanha")
        slug = slugify(name, suffix=cid[:6] if cid else "", max_len=55)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        try:
            link = camp["channels"][0]["shortUrls"][0]
        except Exception:
            link = camp.get("url", "#")
        try:
            img = camp["mediaKit"]["banners"][0]
        except Exception:
            img = ""
        code   = camp.get("code", "")
        end_at = (camp.get("period") or {}).get("endAt", "")
        expiry = ""
        if end_at:
            try:
                dt     = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
                expiry = f"Válido até {dt.strftime('%d/%m/%Y')}"
            except Exception:
                pass

        ctype = camp.get("type", "Offer")
        btn   = "🎫 Ver cupom" if ctype == "Coupon" else "🔥 Ver oferta"
        html  = build_auto_product_page(
            slug=slug, title=name, aff_url=link, img=img,
            code=code, expiry=expiry,
            source_label="Campanha", source_color="var(--green-dim)",
            btn_label=btn,
        )
        with open(f"{PAGES_DIR}/{slug}.html", "w", encoding="utf-8") as f:
            f.write(html)
        all_slugs.append(slug)
        if cid:
            campaign_slugs[cid] = slug

    print(f"  ✓ {len(campaign_slugs)} páginas Campanhas")

    print("\nGerando index.html...")
    html = build_html(
        lomadee_products, campaigns, shopee_products, recommendations,
        lomadee_slugs=lomadee_slugs,
        shopee_slugs=shopee_slugs,
        campaign_slugs=campaign_slugs,
    )
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("Gerando páginas de indicações...")
    for rec in recommendations:
        slug = rec.get("slug")
        if not slug:
            continue
        filename = f"indicacao-{slug}.html"
        page_html = build_recommendation_page(rec)
        with open(filename, "w", encoding="utf-8") as f:
            f.write(page_html)
        print(f"  ✓ {filename}")

    print("Gerando sitemap.xml...")
    with open("sitemap.xml", "w", encoding="utf-8") as f:
        f.write(build_sitemap(all_slugs, recommendations))

    print(f"Pronto! {len(all_slugs)} páginas em {PAGES_DIR}/")


if __name__ == "__main__":
    main()
