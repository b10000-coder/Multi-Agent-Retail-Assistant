"""Upload the corpus to Cloud Storage and index it in Vertex AI Search.

Creates (idempotently) for each corpus:
    data store   <name>-ds    unstructured documents, imported from GCS
    search app   <name>-app   Enterprise tier (needed for extractive segments used as citations)

Corpora:
    policies   corpus/policies/*.md   -> policy_qa agent
    catalog    corpus/catalog/*.md    -> product_catalog agent

Markdown is converted to HTML first: Vertex AI Search indexes HTML (not .md) and uses
the <title> tag as the document title, which the agents show in citations.

Usage:
    uv run python scripts/setup_search.py --dry-run     # convert to build/corpus/ only, no GCP
    uv run python scripts/setup_search.py               # upload + create data stores + import + apps
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
CORPORA = {"policies": ROOT / "corpus" / "policies", "catalog": ROOT / "corpus" / "catalog"}
BUILD_DIR = ROOT / "build" / "corpus"
COLLECTION = "default_collection"


def md_to_html(md_text: str) -> str:
    title_match = re.search(r"^# (.+)$", md_text, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "Untitled"
    # Each numbered clause ("2.1. ...") becomes its own paragraph, so search snippets and
    # citations point at a single clause instead of a merged block.
    md_text = re.sub(r"\n(?=\d+\.\d+\. )", "\n\n", md_text)
    body = markdown.markdown(md_text, extensions=["tables"])
    return (
        '<!DOCTYPE html>\n<html lang="tr">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{title}</title>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )


def build_html() -> dict[str, list[Path]]:
    built: dict[str, list[Path]] = {}
    for name, src in CORPORA.items():
        out = BUILD_DIR / name
        out.mkdir(parents=True, exist_ok=True)
        built[name] = []
        for md_file in sorted(src.glob("*.md")):
            html_file = out / f"{md_file.stem}.html"
            html_file.write_text(md_to_html(md_file.read_text(encoding="utf-8")), encoding="utf-8")
            built[name].append(html_file)
    return built


def upload(project: str, bucket_name: str, location: str, built: dict[str, list[Path]]) -> None:
    from google.api_core.exceptions import Conflict
    from google.cloud import storage

    client = storage.Client(project=project)
    try:
        bucket = client.create_bucket(bucket_name, location=location)
        print(f"  created bucket gs://{bucket_name} ({location})")
    except Conflict:
        bucket = client.bucket(bucket_name)
        print(f"  bucket gs://{bucket_name} exists")
    for name, files in built.items():
        for f in files:
            blob = bucket.blob(f"{name}/{f.name}")
            blob.upload_from_filename(str(f), content_type="text/html; charset=utf-8")
        print(f"  uploaded {len(files)} files to gs://{bucket_name}/{name}/")


def setup_vertex_search(project: str, bucket_name: str, names: list[str]) -> dict[str, str]:
    from google.api_core.exceptions import AlreadyExists
    from google.cloud import discoveryengine_v1 as de

    collection = f"projects/{project}/locations/global/collections/{COLLECTION}"
    ds_client = de.DataStoreServiceClient()
    doc_client = de.DocumentServiceClient()
    engine_client = de.EngineServiceClient()
    serving_configs: dict[str, str] = {}

    for name in names:
        ds_id, engine_id = f"{name}-ds", f"{name}-app"

        try:
            ds_client.create_data_store(
                parent=collection,
                data_store_id=ds_id,
                data_store=de.DataStore(
                    display_name=f"Retail {name}",
                    industry_vertical=de.IndustryVertical.GENERIC,
                    solution_types=[de.SolutionType.SOLUTION_TYPE_SEARCH],
                    content_config=de.DataStore.ContentConfig.CONTENT_REQUIRED,
                ),
            ).result(timeout=600)
            print(f"  created data store {ds_id}")
        except AlreadyExists:
            print(f"  data store {ds_id} exists")

        print(f"  importing gs://{bucket_name}/{name}/* into {ds_id} (can take a few minutes)...")
        op = doc_client.import_documents(
            request=de.ImportDocumentsRequest(
                parent=f"{collection}/dataStores/{ds_id}/branches/default_branch",
                gcs_source=de.GcsSource(input_uris=[f"gs://{bucket_name}/{name}/*"], data_schema="content"),
                # FULL: documents removed from the corpus are also removed from the index
                reconciliation_mode=de.ImportDocumentsRequest.ReconciliationMode.FULL,
            )
        )
        result = op.result(timeout=1800)
        errors = list(result.error_samples)
        print(f"  import done ({len(errors)} errors)")
        for e in errors[:5]:
            print(f"    error: {e.message}")

        try:
            engine_client.create_engine(
                parent=collection,
                engine_id=engine_id,
                engine=de.Engine(
                    display_name=f"Retail {name} search",
                    solution_type=de.SolutionType.SOLUTION_TYPE_SEARCH,
                    industry_vertical=de.IndustryVertical.GENERIC,
                    data_store_ids=[ds_id],
                    search_engine_config=de.Engine.SearchEngineConfig(
                        search_tier=de.SearchTier.SEARCH_TIER_ENTERPRISE,
                    ),
                ),
            ).result(timeout=600)
            print(f"  created search app {engine_id}")
        except AlreadyExists:
            print(f"  search app {engine_id} exists")

        serving_configs[name] = f"{collection}/engines/{engine_id}/servingConfigs/default_search"
    return serving_configs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", "retail-agent-bk"))
    parser.add_argument("--bucket", default=None, help="default: <project>-corpus")
    parser.add_argument("--bucket-location", default="EU")
    parser.add_argument("--dry-run", action="store_true", help="only convert Markdown to HTML in build/corpus/")
    args = parser.parse_args()
    bucket = args.bucket or f"{args.project}-corpus"

    built = build_html()
    for name, files in built.items():
        print(f"{name}: {len(files)} documents -> {BUILD_DIR / name}")
    if args.dry_run:
        return

    print("Uploading to Cloud Storage")
    upload(args.project, bucket, args.bucket_location, built)
    print("Setting up Vertex AI Search")
    serving = setup_vertex_search(args.project, bucket, list(built))

    print("\nAdd these to retail_agent/.env:")
    print(f"POLICY_SERVING_CONFIG={serving['policies']}")
    print(f"CATALOG_SERVING_CONFIG={serving['catalog']}")
    print("\nNote: newly imported documents can take 5-10 minutes to become searchable.")


if __name__ == "__main__":
    main()
