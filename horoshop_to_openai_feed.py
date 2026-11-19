#!/usr/bin/env python3
"""Convert a Horoshop "Google Feed for Merchant Center" XML (RSS 2.0, g: fields)
into a CSV for OpenAI Ads Manager (Tools > Feeds), following the OpenAI product
file schema: https://developers.openai.com/commerce/specs/file-upload/products

Python 3.8+, standard library only.

Examples
--------
Test run on ~100 random products, store name used as seller:

  python3 horoshop_to_openai_feed.py https://SHOP/export/google_merchant_center.xml \
      -o openai_feed.csv --seller-name "Diagnost" --seller-url https://diagnost.net.ua \
      --limit 100 --random --only-in-stock

Full catalog:

  python3 horoshop_to_openai_feed.py feed.xml -o openai_feed.csv --seller-name "Diagnost"

Built-in check on a mocked Horoshop feed:

  python3 horoshop_to_openai_feed.py --self-test
"""

import argparse
import csv
import gzip
import html
import io
import json
import random
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

COLUMNS = [
    "item_id",
    "title",
    "description",
    "url",
    "brand",
    "seller_name",
    "image_url",
    "price",
    "availability",
    "target_countries",
    "store_country",
    "is_eligible_search",
    "is_eligible_checkout",
    "is_ads_eligible",
    "sale_price",
    "condition",
    "gtin",
    "mpn",
    "product_category",
    "additional_image_urls",
    "group_id",
    "listing_has_variations",
    "variant_dict",
    "seller_url",
    "return_policy",
    "ads_metadata",
]

AVAILABILITY = {
    "in_stock": "in_stock",
    "instock": "in_stock",
    "out_of_stock": "out_of_stock",
    "outofstock": "out_of_stock",
    "preorder": "pre_order",
    "pre_order": "pre_order",
    "backorder": "backorder",
    "back_order": "backorder",
    "unknown": "unknown",
}

CONDITION = {
    "new": "new",
    "used": "used",
    "refurbished": "refurbished",
    "renewed": "refurbished",
}

TITLE_MAX = 150
DESC_MAX = 5000
ID_MAX = 50


def local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def read_source(src):
    if re.match(r"^https?://", src, re.I):
        req = urllib.request.Request(src, headers={"User-Agent": "horoshop-to-openai-feed/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
    else:
        with open(src, "rb") as f:
            data = f.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def iter_items(xml_bytes):
    """Yield {local_tag: [text, ...]} for every <item>/<entry>, ignoring namespaces."""
    for _, el in ET.iterparse(io.BytesIO(xml_bytes), events=("end",)):
        if local(el.tag) not in ("item", "entry"):
            continue
        fields = {}
        for ch in el:
            name = local(ch.tag)
            if len(ch):  # nested block (e.g. g:shipping) is not used
                continue
            text = (ch.text or "").strip()
            fields.setdefault(name, []).append(text)
        yield fields
        el.clear()


def first(fields, *names):
    for n in names:
        for v in fields.get(n, []):
            if v:
                return v
    return ""


def clean_text(s):
    s = html.unescape(s or "")
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_decimal(num):
    num = num.replace("\u00a0", "").replace(" ", "")
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        head, _, tail = num.rpartition(",")
        num = head.replace(",", "") + "." + tail if len(tail) in (1, 2) else num.replace(",", "")
    return Decimal(num)


def parse_money(raw, default_currency):
    """'1299.00 UAH' | '1 299,00 UAH' | 'UAH 1299' -> (Decimal, 'UAH') or None."""
    if not raw:
        return None
    m = re.match(r"^\s*([A-Za-z]{3})?\s*([0-9][0-9\s\u00a0.,]*)\s*([A-Za-z]{3})?\s*$", raw)
    if not m:
        return None
    cur = (m.group(1) or m.group(3) or default_currency or "").upper()
    if not re.match(r"^[A-Z]{3}$", cur):
        return None
    try:
        val = parse_decimal(m.group(2))
    except InvalidOperation:
        return None
    return val, cur


def fmt_money(val, cur):
    return "{} {}".format(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), cur)


def gtin_ok(g):
    if not re.match(r"^\d+$", g or "") or len(g) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in g]
    check = digits.pop()
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return (10 - total % 10) % 10 == check


def is_http(u):
    return bool(re.match(r"^https?://\S+$", u or "", re.I))


def convert_item(f, a, stats):
    """Return (row_dict, None) or (None, [reasons])."""
    reasons = []

    item_id = first(f, "id")
    if not item_id:
        reasons.append("no_id")
    elif len(item_id) > ID_MAX:
        reasons.append("id_longer_than_%d" % ID_MAX)

    title = clean_text(first(f, "title"))
    if not title:
        reasons.append("no_title")
    elif len(title) > TITLE_MAX:
        title = title[: TITLE_MAX - 1].rstrip() + "\u2026"
        stats["title_truncated"] += 1

    desc = clean_text(first(f, "description"))
    if not desc:
        reasons.append("no_description")
    elif len(desc) > DESC_MAX:
        desc = desc[:DESC_MAX].rstrip()
        stats["description_truncated"] += 1

    url = first(f, "link")
    image = first(f, "image_link")
    if a.force_https:
        url = re.sub(r"^http://", "https://", url, flags=re.I)
        image = re.sub(r"^http://", "https://", image, flags=re.I)
    if not is_http(url):
        reasons.append("no_valid_link")
    elif not url.lower().startswith("https://"):
        reasons.append("link_not_https")
    if not is_http(image):
        reasons.append("no_valid_image")
    elif not image.lower().startswith("https://"):
        reasons.append("image_not_https")

    if a.exclude_regex and re.search(a.exclude_regex, title or "", re.I):
        reasons.append("skipped_exclude_regex")

    brand = first(f, "brand")
    if not brand:
        brand = None if a.require_brand else a.default_brand
        if brand:
            stats["brand_defaulted"] += 1
        else:
            reasons.append("no_brand")

    raw_av = first(f, "availability")
    av_key = re.sub(r"[\s\-]+", "_", raw_av.strip().lower())
    availability = AVAILABILITY.get(av_key)
    if not availability:
        if a.unknown_availability:
            availability = "unknown"
            stats["availability_unknown"] += 1
        else:
            reasons.append("availability_missing" if not raw_av else "availability_bad:%s" % raw_av)
    if a.only_in_stock and availability and availability != "in_stock":
        reasons.append("skipped_not_in_stock")

    price = parse_money(first(f, "price"), a.currency)
    sale = parse_money(first(f, "sale_price"), a.currency)
    if not price:
        reasons.append("price_invalid:%s" % first(f, "price"))
    elif price[0] <= 0:
        reasons.append("price_not_positive")
        price = None

    if reasons:
        return None, reasons

    price_val, cur = price
    sale_val = None
    if sale and sale[1] == cur and 0 < sale[0] < price_val:
        sale_val = sale[0]
    elif sale:
        stats["sale_price_dropped"] += 1

    if a.convert_to:
        rate = Decimal(str(a.rate))
        price_val = price_val / rate
        sale_val = sale_val / rate if sale_val is not None else None
        cur = a.convert_to.upper()

    gtin = first(f, "gtin")
    if gtin and not gtin_ok(gtin):
        stats["gtin_invalid_dropped"] += 1
        gtin = ""

    cond = CONDITION.get(first(f, "condition").lower(), "")

    extra_imgs = [u for u in f.get("additional_image_link", []) if is_http(u)]
    extra_imgs = [re.sub(r"^http://", "https://", u, flags=re.I) if a.force_https else u for u in extra_imgs]
    extra_imgs = [u.replace(",", "%2C") for u in extra_imgs]

    group_id, listing_var, variant_dict = "", "", ""
    grp = first(f, "item_group_id")
    if grp and grp != item_id:
        opts = {}
        for key in ("color", "size"):
            v = first(f, key)
            if v:
                opts[key] = v
        if opts:
            group_id, listing_var, variant_dict = grp, "true", json.dumps(opts, ensure_ascii=False)
        else:
            stats["group_without_options_ignored"] += 1

    ads_meta = ""
    if a.ads_metadata:
        labels = {}
        for i in range(5):
            v = first(f, "custom_label_%d" % i)
            if v:
                labels["custom_label_%d" % i] = v
        if labels:
            ads_meta = json.dumps(labels, ensure_ascii=False)

    row = {
        "item_id": item_id,
        "title": title,
        "description": desc,
        "url": url,
        "brand": brand,
        "seller_name": a.seller_name,
        "image_url": image,
        "price": fmt_money(price_val, cur),
        "availability": availability,
        "target_countries": a.target_countries,
        "store_country": a.store_country,
        "is_eligible_search": "true" if a.eligible_search else "false",
        "is_eligible_checkout": "false",
        "is_ads_eligible": "true",
        "sale_price": fmt_money(sale_val, cur) if sale_val is not None else "",
        "condition": cond,
        "gtin": gtin,
        "mpn": first(f, "mpn"),
        "product_category": first(f, "product_type"),
        "additional_image_urls": ",".join(extra_imgs),
        "group_id": group_id,
        "listing_has_variations": listing_var,
        "variant_dict": variant_dict,
        "seller_url": a.seller_url or "",
        "return_policy": a.return_policy_url or "",
        "ads_metadata": ads_meta,
    }
    return row, None


def run(a):
    xml_bytes = read_source(a.input)
    stats = {
        k: 0
        for k in (
            "title_truncated",
            "description_truncated",
            "brand_defaulted",
            "availability_unknown",
            "sale_price_dropped",
            "gtin_invalid_dropped",
            "group_without_options_ignored",
        )
    }
    ok_rows, rejected = [], []
    seen = set()
    total = 0
    only_ids = None
    if a.skus_file:
        with open(a.skus_file, encoding="utf-8") as fh:
            only_ids = {ln.strip() for ln in fh if ln.strip()}

    for fields in iter_items(xml_bytes):
        total += 1
        item_id = first(fields, "id")
        if only_ids is not None and item_id not in only_ids:
            continue
        row, reasons = convert_item(fields, a, stats)
        if row and row["item_id"] in seen:
            row, reasons = None, ["duplicate_id"]
        if row:
            seen.add(row["item_id"])
            ok_rows.append(row)
        else:
            rejected.append((item_id, first(fields, "title"), "; ".join(reasons)))

    valid_total = len(ok_rows)
    if a.limit and len(ok_rows) > a.limit:
        if a.in_stock_first:
            stocked = [r for r in ok_rows if r["availability"] == "in_stock"]
            others = [r for r in ok_rows if r["availability"] != "in_stock"]
        else:
            stocked, others = [], ok_rows
        rnd = random.Random(a.seed)
        if a.random:
            stocked = rnd.sample(stocked, min(len(stocked), a.limit))
            others = rnd.sample(others, len(others))
        ok_rows = (stocked + others)[: a.limit]

    with open(a.output, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(ok_rows)

    with open(a.rejects, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["item_id", "title", "reasons"])
        w.writerows(rejected)

    by_reason = {}
    for _, _, rs in rejected:
        for r in rs.split("; "):
            key = r.split(":", 1)[0]
            by_reason[key] = by_reason.get(key, 0) + 1

    print("Items in XML:                %d" % total)
    print("Valid rows:                  %d" % valid_total)
    print("Written to %s: %d%s" % (a.output, len(ok_rows), "  (limited)" if len(ok_rows) < valid_total else ""))
    print("Rejected:                    %d  -> %s" % (len(rejected), a.rejects))
    for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print("   %-28s %d" % (k, v))
    notes = {k: v for k, v in stats.items() if v}
    if notes:
        print("Notes:")
        for k, v in notes.items():
            print("   %-28s %d" % (k, v))
    if stats["brand_defaulted"]:
        print("WARNING: brand is empty in the feed for %d items and was replaced with %r." % (stats["brand_defaulted"], a.default_brand))
        print("         OpenAI asks for a real brand, not a placeholder; fill in Brand in Horoshop if possible.")
    if a.convert_to:
        print("WARNING: prices converted to %s at rate %s. Use ONLY to test whether UAH is the reason for a rejection;" % (a.convert_to, a.rate))
        print("         do not run campaigns on such a feed (price on the ad will not match the site).")
    return 0 if ok_rows else 2


SELF_TEST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">
<channel>
<title>Diagnost</title><link>https://diagnost.net.ua</link><description>feed</description>
<item>
<g:id>ART-001</g:id>
<g:title><![CDATA[Пульсоксиметр Fingertip, чорний]]></g:title>
<g:description><![CDATA[<p>Портативний <b>пульсоксиметр</b> для дому.</p><br/>Гарантія 12 міс.]]></g:description>
<g:link>https://diagnost.net.ua/pulsoksymetr-fingertip/</g:link>
<g:image_link>https://diagnost.net.ua/content/images/1/pulse.jpg</g:image_link>
<g:additional_image_link>https://diagnost.net.ua/content/images/1/pulse-2.jpg</g:additional_image_link>
<g:availability>in stock</g:availability>
<g:price>1 299,00 UAH</g:price>
<g:sale_price>999.00 UAH</g:sale_price>
<g:condition>new</g:condition>
<g:brand>Fingertip</g:brand>
<g:gtin>4006381333931</g:gtin>
<g:mpn>FT-100</g:mpn>
<g:product_type>Діагностика > Пульсоксиметри</g:product_type>
</item>
<item>
<g:id>ART-002</g:id>
<g:title>Тонометр автоматичний</g:title>
<g:description>Плечовий тонометр.</g:description>
<g:link>https://diagnost.net.ua/tonometr/</g:link>
<g:image_link>https://diagnost.net.ua/content/images/2/t.jpg</g:image_link>
<g:availability>out_of_stock</g:availability>
<g:price>1450 UAH</g:price>
<g:gtin>1234567890123</g:gtin>
</item>
<item>
<g:id>ART-003</g:id>
<g:title>Ваги побутові, розмір M</g:title>
<g:description>Ваги.</g:description>
<g:link>https://diagnost.net.ua/vagy-m/</g:link>
<g:image_link>https://diagnost.net.ua/content/images/3/v.jpg</g:image_link>
<g:availability>preorder</g:availability>
<g:price>799.5 UAH</g:price>
<g:brand>Omron</g:brand>
<g:item_group_id>VAGY</g:item_group_id>
<g:size>M</g:size>
</item>
<item>
<g:id>ART-004</g:id>
<g:title>Без ціни</g:title>
<g:description>Опис.</g:description>
<g:link>https://diagnost.net.ua/x/</g:link>
<g:image_link>https://diagnost.net.ua/content/images/4/x.jpg</g:image_link>
<g:availability>in_stock</g:availability>
</item>
<item>
<g:id>ART-005</g:id>
<g:title>Без фото і http</g:title>
<g:description>Опис.</g:description>
<g:link>http://diagnost.net.ua/y/</g:link>
<g:availability>in_stock</g:availability>
<g:price>100 UAH</g:price>
</item>
<item>
<g:id>ART-001</g:id>
<g:title>Дубль</g:title>
<g:description>Опис.</g:description>
<g:link>https://diagnost.net.ua/dup/</g:link>
<g:image_link>https://diagnost.net.ua/content/images/5/d.jpg</g:image_link>
<g:availability>in_stock</g:availability>
<g:price>10 UAH</g:price>
</item>
<item>
<g:id>ART-006</g:id>
<g:title>Невідома наявність</g:title>
<g:description>Опис.</g:description>
<g:link>https://diagnost.net.ua/z/</g:link>
<g:image_link>https://diagnost.net.ua/content/images/6/z.jpg</g:image_link>
<g:availability>wait</g:availability>
<g:price>10 UAH</g:price>
</item>
</channel>
</rss>
"""


def self_test():
    import os
    import tempfile

    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "feed.xml")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(SELF_TEST_XML)
    out = os.path.join(tmp, "out.csv")
    rej = os.path.join(tmp, "rej.csv")
    a = build_parser().parse_args([src, "-o", out, "--rejects", rej, "--seller-name", "Diagnost", "--default-brand", "Diagnost"])
    code = run(a)
    with open(out, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    by_id = {r["item_id"]: r for r in rows}
    assert code == 0
    assert list(by_id) == ["ART-001", "ART-002", "ART-003"], list(by_id)
    r1 = by_id["ART-001"]
    assert r1["price"] == "1299.00 UAH" and r1["sale_price"] == "999.00 UAH", r1
    assert r1["availability"] == "in_stock" and r1["target_countries"] == "UA" and r1["store_country"] == "UA"
    assert r1["description"] == "Портативний пульсоксиметр для дому. Гарантія 12 міс.", r1["description"]
    assert r1["gtin"] == "4006381333931" and r1["is_ads_eligible"] == "true"
    assert by_id["ART-002"]["gtin"] == "" and by_id["ART-002"]["brand"] == "Diagnost"
    assert by_id["ART-002"]["availability"] == "out_of_stock" and by_id["ART-002"]["price"] == "1450.00 UAH"
    r3 = by_id["ART-003"]
    assert r3["availability"] == "pre_order" and r3["price"] == "799.50 UAH"
    assert r3["group_id"] == "VAGY" and json.loads(r3["variant_dict"]) == {"size": "M"}
    with open(rej, encoding="utf-8", newline="") as fh:
        rej_rows = {r["item_id"]: r["reasons"] for r in csv.DictReader(fh)}
    assert "price_invalid" in rej_rows["ART-004"], rej_rows
    assert "no_valid_image" in rej_rows["ART-005"] and "link_not_https" in rej_rows["ART-005"]
    assert rej_rows.get("ART-001") == "duplicate_id"
    assert "availability_bad:wait" in rej_rows["ART-006"]
    print("\nSELF-TEST OK (files in %s)" % tmp)
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="Horoshop Google Merchant XML -> OpenAI Ads product feed CSV")
    p.add_argument("input", nargs="?", help="URL or path to the Horoshop Google Merchant XML (gzip is fine)")
    p.add_argument("-o", "--output", default="openai_feed.csv")
    p.add_argument("--rejects", default="openai_feed_rejected.csv", help="report of rejected rows")
    p.add_argument("--seller-name", help="REQUIRED: store name for seller_name")
    p.add_argument("--seller-url", help="store URL for seller_url (optional)")
    p.add_argument("--default-brand", help="brand for items without g:brand (default: value of --seller-name)")
    p.add_argument("--currency", default="UAH", help="currency if the price has no code (default UAH)")
    p.add_argument("--target-countries", default="UA", help="target_countries (default UA)")
    p.add_argument("--store-country", default="UA", help="store_country (default UA)")
    p.add_argument("--eligible-search", action="store_true", help="is_eligible_search=true (default false: ads only)")
    p.add_argument("--only-in-stock", action="store_true", help="skip items that are not in_stock")
    p.add_argument("--unknown-availability", action="store_true", help="map missing/unrecognized availability to 'unknown' instead of rejecting")
    p.add_argument("--force-https", action="store_true", help="rewrite http:// links and images to https://")
    p.add_argument("--limit", type=int, default=0, help="write at most N valid rows (test batch, e.g. 100)")
    p.add_argument("--random", action="store_true", help="with --limit take a random sample instead of the first N")
    p.add_argument("--seed", type=int, default=1, help="seed for --random")
    p.add_argument("--in-stock-first", action="store_true", help="with --limit fill the batch with in_stock items first, then the rest")
    p.add_argument("--skus-file", help="text file with item ids (one per line); only these are processed")
    p.add_argument("--exclude-regex", help="skip items whose title matches this regex (case-insensitive), e.g. 'аналог|програматор'")
    p.add_argument("--require-brand", action="store_true", help="reject items without g:brand instead of using --default-brand")
    p.add_argument("--ads-metadata", action="store_true", help="map g:custom_label_0..4 to ads_metadata JSON")
    p.add_argument("--return-policy-url", help="public return policy URL for return_policy")
    p.add_argument("--convert-to", help="DIAGNOSTIC ONLY: convert prices to this currency (e.g. USD), requires --rate")
    p.add_argument("--rate", type=float, help="units of the source currency per 1 unit of --convert-to (e.g. 41.5)")
    p.add_argument("--self-test", action="store_true", help="run built-in check on a mocked Horoshop feed")
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    if a.self_test:
        return self_test()
    if not a.input or not a.seller_name:
        p.error("input and --seller-name are required")
    if not a.default_brand:
        a.default_brand = a.seller_name
    if a.convert_to and not a.rate:
        p.error("--convert-to needs --rate")
    if not re.match(r"^[A-Za-z]{2}$", a.target_countries) or not re.match(r"^[A-Za-z]{2}$", a.store_country):
        p.error("--target-countries/--store-country must be a 2-letter ISO code (one country)")
    a.target_countries = a.target_countries.upper()
    a.store_country = a.store_country.upper()
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
