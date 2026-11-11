"""Generate synthetic hard-discount retail data and load it into BigQuery.

Tables (dataset `retail` by default):
    stores      one row per store (region > city > district hierarchy)
    products    one row per SKU (category, private label vs national brand, base price)
    promotions  weekly "aktüel" promotions (product, week, discount)
    sales       daily sales per store x product (partitioned by date, clustered by store/product)

Usage:
    uv run python scripts/generate_data.py --local                  # small, parquet files only, no GCP
    uv run python scripts/generate_data.py                          # small, load to BigQuery
    uv run python scripts/generate_data.py --size medium            # larger dataset
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 42
END_DATE = date(2026, 10, 31)
MONTHLY_INFLATION = 0.025  # prices drift up ~2.5% per month
PROMO_SHARE = 0.03  # share of products on promotion in a given week


@dataclass(frozen=True)
class Size:
    n_stores: int
    n_products: int
    n_days: int


SIZES = {
    "small": Size(n_stores=50, n_products=200, n_days=180),
    "medium": Size(n_stores=150, n_products=400, n_days=365),
}

# region -> (weight, {city: [districts]})
GEOGRAPHY: dict[str, tuple[float, dict[str, list[str]]]] = {
    "Marmara": (0.40, {
        "İstanbul": ["Kadıköy", "Ataşehir", "Üsküdar", "Maltepe", "Kartal", "Pendik",
                     "Ümraniye", "Beşiktaş", "Şişli", "Bakırköy", "Esenyurt", "Bağcılar"],
        "Bursa": ["Osmangazi", "Nilüfer", "Yıldırım"],
        "Kocaeli": ["İzmit", "Gebze"],
    }),
    "Ege": (0.18, {
        "İzmir": ["Konak", "Karşıyaka", "Bornova", "Buca"],
        "Manisa": ["Yunusemre", "Şehzadeler"],
    }),
    "İç Anadolu": (0.18, {
        "Ankara": ["Çankaya", "Keçiören", "Yenimahalle", "Mamak", "Etimesgut"],
        "Konya": ["Selçuklu", "Meram"],
    }),
    "Akdeniz": (0.14, {
        "Antalya": ["Muratpaşa", "Kepez", "Konyaaltı"],
        "Adana": ["Seyhan", "Çukurova"],
    }),
    "Karadeniz": (0.10, {
        "Samsun": ["İlkadım", "Atakum"],
        "Trabzon": ["Ortahisar"],
    }),
}

# category -> [(base product, [sizes], base price TL for first size)]
CATALOG: dict[str, list[tuple[str, list[str], float]]] = {
    "Süt Ürünleri": [
        ("Süt", ["1 L", "500 ml"], 42), ("Yoğurt", ["1 kg", "500 g"], 60),
        ("Ayran", ["1 L", "300 ml"], 35), ("Beyaz Peynir", ["500 g", "1 kg"], 160),
        ("Kaşar Peyniri", ["400 g", "700 g"], 190), ("Tereyağı", ["250 g", "500 g"], 150),
    ],
    "Temel Gıda": [
        ("Ayçiçek Yağı", ["1 L", "5 L"], 95), ("Pirinç", ["1 kg", "2,5 kg"], 70),
        ("Makarna", ["500 g", "1 kg"], 18), ("Un", ["2 kg", "5 kg"], 55),
        ("Toz Şeker", ["1 kg", "3 kg"], 45), ("Kırmızı Mercimek", ["1 kg"], 65),
        ("Bulgur", ["1 kg", "2,5 kg"], 40), ("Domates Salçası", ["830 g", "1,65 kg"], 75),
    ],
    "Kahvaltılık": [
        ("Yumurta", ["10'lu", "30'lu"], 70), ("Siyah Zeytin", ["500 g", "1 kg"], 110),
        ("Bal", ["460 g", "850 g"], 180), ("Fındık Kreması", ["400 g", "700 g"], 95),
        ("Reçel", ["380 g", "800 g"], 55), ("Siyah Çay", ["1 kg", "500 g"], 210),
    ],
    "Atıştırmalık": [
        ("Bisküvi", ["150 g", "400 g"], 15), ("Çikolata", ["80 g", "200 g"], 25),
        ("Cips", ["110 g", "250 g"], 30), ("Kuruyemiş Karışık", ["150 g", "300 g"], 70),
        ("Kek", ["Tekli", "6'lı"], 12), ("Gofret", ["Tekli", "8'li"], 10),
    ],
    "İçecek": [
        ("Su", ["1,5 L", "5 L", "6x0,5 L"], 12), ("Maden Suyu", ["6'lı", "Tekli"], 40),
        ("Kola", ["1 L", "2,5 L"], 40), ("Meyve Suyu", ["1 L", "200 ml"], 45),
        ("Türk Kahvesi", ["100 g", "250 g"], 80), ("Gazoz", ["1 L", "2,5 L"], 35),
    ],
    "Temizlik": [
        ("Çamaşır Deterjanı", ["4 kg", "9 kg"], 220), ("Bulaşık Deterjanı", ["750 ml", "1,5 L"], 55),
        ("Yüzey Temizleyici", ["1 L", "2,5 L"], 60), ("Çamaşır Suyu", ["1 L", "3 L"], 35),
        ("Tuvalet Kağıdı", ["16'lı", "32'li"], 150), ("Kağıt Havlu", ["6'lı", "12'li"], 110),
    ],
    "Kişisel Bakım": [
        ("Şampuan", ["400 ml", "700 ml"], 90), ("Diş Macunu", ["75 ml", "125 ml"], 55),
        ("Sıvı Sabun", ["1,5 L", "500 ml"], 70), ("Islak Mendil", ["Tekli", "3'lü"], 30),
        ("Bebek Bezi", ["Midi", "Maxi", "Junior"], 320), ("Deodorant", ["150 ml"], 85),
    ],
    "Dondurulmuş": [
        ("Dondurulmuş Pizza", ["Tekli", "2'li"], 95), ("Patates Kızartması", ["1 kg", "2,5 kg"], 80),
        ("Dondurma", ["500 ml", "1 L"], 90), ("Mantı", ["500 g", "1 kg"], 85),
    ],
}

NATIONAL_BRANDS = ["Yayla", "Anadolu", "Ege Sofrası", "Karadeniz", "Altınbaşak", "Pınarlı"]
DOW_FACTOR = np.array([0.90, 0.85, 0.90, 0.95, 1.10, 1.30, 1.00])  # Mon..Sun
STORE_SIZE_FACTOR = {"Küçük": 0.7, "Orta": 1.0, "Büyük": 1.4}


def make_stores(n: int, start: date, rng: np.random.Generator) -> pd.DataFrame:
    regions = list(GEOGRAPHY)
    weights = np.array([GEOGRAPHY[r][0] for r in regions])
    # every İstanbul district gets at least one store, so demo questions
    # ("Kadıköy'de en çok satan...") always have data; the rest is random by region weight
    istanbul = [("Marmara", "İstanbul", d) for d in GEOGRAPHY["Marmara"][1]["İstanbul"]]
    rows = []
    for store_id in range(1, n + 1):
        if store_id <= len(istanbul):
            region, city, district = istanbul[store_id - 1]
        else:
            region = rng.choice(regions, p=weights / weights.sum())
            cities = GEOGRAPHY[region][1]
            city = rng.choice(list(cities))
            district = rng.choice(cities[city])
        size = rng.choice(list(STORE_SIZE_FACTOR), p=[0.35, 0.45, 0.20])
        # ~10% of stores open during the period -> realistic "new store" questions
        if rng.random() < 0.10:
            opened = start + timedelta(days=int(rng.integers(30, (END_DATE - start).days - 30)))
        else:
            opened = start - timedelta(days=int(rng.integers(365, 365 * 15)))
        rows.append({
            "store_id": store_id,
            "store_name": f"{district} #{store_id:04d}",
            "region": region,
            "city": city,
            "district": district,
            "store_size": size,
            "opened_date": opened,
        })
    return pd.DataFrame(rows)


def make_products(n: int, rng: np.random.Generator) -> pd.DataFrame:
    candidates = []
    for category, items in CATALOG.items():
        for base, sizes, base_price in items:
            for i, size in enumerate(sizes):
                # larger pack sizes cost more; rough multiplier per step
                price = base_price * (1.0 + 1.3 * i)
                # private label (the hard-discount core) is ~25% cheaper
                candidates.append((category, f"{base} {size}", "Özel Marka", price * 0.75))
                for brand in NATIONAL_BRANDS:
                    candidates.append((category, f"{brand} {base} {size}", "Ulusal Marka", price))

    # keep every private-label SKU first, fill the rest with national brands
    private = [c for c in candidates if c[2] == "Özel Marka"]
    national = [c for c in candidates if c[2] == "Ulusal Marka"]
    rng.shuffle(national)
    chosen = (private + national)[:n]
    if len(chosen) < n:
        raise ValueError(f"Catalog only supports {len(chosen)} products, asked for {n}")

    df = pd.DataFrame(chosen, columns=["category", "product_name", "brand_type", "base_price"])
    df.insert(0, "product_id", np.arange(1, len(df) + 1))
    df["base_price"] = df["base_price"].round(2)
    return df


def make_promotions(products: pd.DataFrame, start: date, rng: np.random.Generator) -> pd.DataFrame:
    rows = []
    week = start - timedelta(days=start.weekday())  # Monday of the first week
    n_promo = max(1, int(len(products) * PROMO_SHARE))
    while week <= END_DATE:
        picked = rng.choice(products["product_id"].to_numpy(), size=n_promo, replace=False)
        for pid in picked:
            rows.append({
                "week_start": week,
                "product_id": int(pid),
                "discount_pct": int(rng.choice([10, 15, 20, 25, 30])),
            })
        week += timedelta(days=7)
    return pd.DataFrame(rows)


def make_sales_month(
    days: list[date],
    stores: pd.DataFrame,
    products: pd.DataFrame,
    promos: pd.DataFrame,
    start: date,
    popularity: np.ndarray,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Vectorised Poisson sales for one month: grid of day x store x product, zeros dropped."""
    n_d, n_s, n_p = len(days), len(stores), len(products)

    dow = np.array([DOW_FACTOR[d.weekday()] for d in days])
    store_f = stores["store_size"].map(STORE_SIZE_FACTOR).to_numpy()
    opened = stores["opened_date"].to_numpy()
    is_open = np.array([[d >= o for o in opened] for d in days])  # (D, S)

    # promotion discount per (day, product)
    disc = np.zeros((n_d, n_p))
    pid_index = {pid: i for i, pid in enumerate(products["product_id"])}
    promo_lookup = promos.groupby("week_start")
    for di, d in enumerate(days):
        week = d - timedelta(days=d.weekday())
        if week in promo_lookup.groups:
            for _, row in promo_lookup.get_group(week).iterrows():
                disc[di, pid_index[row.product_id]] = row.discount_pct / 100

    lift = 1.0 + 3.0 * disc  # a 20% discount roughly 1.6x the volume
    lam = (
        dow[:, None, None]
        * store_f[None, :, None]
        * popularity[None, None, :]
        * lift[:, None, :]
        * is_open[:, :, None]
    )
    qty = rng.poisson(lam)

    d_idx, s_idx, p_idx = np.nonzero(qty)
    months_elapsed = np.array([(d - start).days / 30.4 for d in days])
    inflation = (1 + MONTHLY_INFLATION) ** months_elapsed
    base = products["base_price"].to_numpy()
    price = base[p_idx] * inflation[d_idx] * (1 - disc[d_idx, p_idx])

    day_objs = np.array(days, dtype=object)
    return pd.DataFrame({
        "sale_date": day_objs[d_idx],
        "store_id": stores["store_id"].to_numpy()[s_idx],
        "product_id": products["product_id"].to_numpy()[p_idx],
        "quantity": qty[d_idx, s_idx, p_idx].astype("int64"),
        "unit_price": price.round(2),
    })


def month_chunks(start: date, end: date) -> list[list[date]]:
    chunks: list[list[date]] = []
    d = start
    while d <= end:
        if not chunks or chunks[-1][0].month != d.month:
            chunks.append([])
        chunks[-1].append(d)
        d += timedelta(days=1)
    return chunks


def bq_writer(project: str, dataset: str, location: str):
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    ds = bigquery.Dataset(f"{project}.{dataset}")
    ds.location = location
    client.create_dataset(ds, exists_ok=True)

    schemas = {
        "stores": [
            bigquery.SchemaField("store_id", "INT64"), bigquery.SchemaField("store_name", "STRING"),
            bigquery.SchemaField("region", "STRING"), bigquery.SchemaField("city", "STRING"),
            bigquery.SchemaField("district", "STRING"), bigquery.SchemaField("store_size", "STRING"),
            bigquery.SchemaField("opened_date", "DATE"),
        ],
        "products": [
            bigquery.SchemaField("product_id", "INT64"), bigquery.SchemaField("category", "STRING"),
            bigquery.SchemaField("product_name", "STRING"), bigquery.SchemaField("brand_type", "STRING"),
            bigquery.SchemaField("base_price", "FLOAT64"),
        ],
        "promotions": [
            bigquery.SchemaField("week_start", "DATE"), bigquery.SchemaField("product_id", "INT64"),
            bigquery.SchemaField("discount_pct", "INT64"),
        ],
        "sales": [
            bigquery.SchemaField("sale_date", "DATE"), bigquery.SchemaField("store_id", "INT64"),
            bigquery.SchemaField("product_id", "INT64"), bigquery.SchemaField("quantity", "INT64"),
            bigquery.SchemaField("unit_price", "FLOAT64"),
        ],
    }

    def write(table: str, df: pd.DataFrame, append: bool) -> None:
        config = bigquery.LoadJobConfig(
            schema=schemas[table],
            write_disposition="WRITE_APPEND" if append else "WRITE_TRUNCATE",
        )
        if table == "sales":
            # partition + cluster: queries filtered by date/store/product scan far less data
            config.time_partitioning = bigquery.TimePartitioning(field="sale_date")
            config.clustering_fields = ["store_id", "product_id"]
        client.load_table_from_dataframe(df, f"{project}.{dataset}.{table}", job_config=config).result()

    return write


def local_writer(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)

    def write(table: str, df: pd.DataFrame, append: bool) -> None:
        if table == "sales":
            month = df["sale_date"].iloc[0].strftime("%Y-%m")
            df.to_parquet(out_dir / f"sales_{month}.parquet", index=False)
        else:
            df.to_parquet(out_dir / f"{table}.parquet", index=False)

    return write


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--size", choices=SIZES, default="small")
    parser.add_argument("--local", action="store_true", help="write parquet to data/ instead of BigQuery")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", "retail-agent-bk"))
    parser.add_argument("--dataset", default="retail")
    parser.add_argument("--location", default="EU", help="BigQuery dataset location")
    args = parser.parse_args()

    size = SIZES[args.size]
    rng = np.random.default_rng(SEED)
    start = END_DATE - timedelta(days=size.n_days - 1)

    stores = make_stores(size.n_stores, start, rng)
    products = make_products(size.n_products, rng)
    promos = make_promotions(products, start, rng)

    # Popularity = Zipf-like randomness x price effect: cheap staples (water, milk, bread-type
    # items) sell more units than expensive ones (detergent, diapers). Scaled to ~3 units/day/store.
    ranks = rng.permutation(len(products)) + 1
    price_effect = (products["base_price"].to_numpy() / 50.0) ** -0.7
    popularity = price_effect / ranks**0.5
    popularity *= 3.0 / popularity.mean()

    write = local_writer(Path("data")) if args.local else bq_writer(args.project, args.dataset, args.location)
    target = "data/" if args.local else f"{args.project}.{args.dataset}"
    print(f"Size={args.size} {size} -> {target}  ({start} .. {END_DATE})")

    write("stores", stores, append=False)
    write("products", products, append=False)
    write("promotions", promos, append=False)
    print(f"  stores={len(stores)} products={len(products)} promotions={len(promos)}")

    total = 0
    for i, days in enumerate(month_chunks(start, END_DATE)):
        sales = make_sales_month(days, stores, products, promos, start, popularity, rng)
        write("sales", sales, append=i > 0)
        total += len(sales)
        print(f"  sales {days[0]:%Y-%m}: {len(sales):>10,} rows")
    print(f"Done. sales total = {total:,} rows")


if __name__ == "__main__":
    main()