# main.py
import os
import sys
import time
import logging
import traceback

from supabase import create_client

# optional: used only if env vars not provided and job SA has Secret Manager access
try:
    from google.cloud import secretmanager
except Exception:
    secretmanager = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("supabase-job")


def read_secret(secret_name: str):
    """Return secret value from Secret Manager (fallback)."""
    if secretmanager is None:
        return None
    try:
        project = os.getenv("PROJECT_ID") or os.getenv("GCP_PROJECT")
        if not project:
            log.debug("PROJECT_ID/GCP_PROJECT not set; cannot read secrets from Secret Manager.")
            return None
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project}/secrets/{secret_name}/versions/latest"
        resp = client.access_secret_version(request={"name": name})
        return resp.payload.data.decode("utf-8")
    except Exception as e:
        log.warning("Failed to read secret %s from Secret Manager: %s", secret_name, e)
        return None


def get_supabase_credentials():
    """Robust lookup for Supabase credentials from env vars or Secret Manager."""
    SUPABASE_URL = (
        os.getenv("SUPABASE_URL")
        or os.getenv("supabase_url")
        or os.getenv("supabase-url")
        or read_secret("supabase-url")
    )
    SUPABASE_KEY = (
        os.getenv("SUPABASE_KEY")
        or os.getenv("supabase_key")
        or os.getenv("supabase-key")
        or read_secret("supabase-key")
    )
    return SUPABASE_URL, SUPABASE_KEY


def extract_rows(resp):
    """Return a python list of rows from a supabase response object or dict."""
    if resp is None:
        return []
    data = getattr(resp, "data", None)
    if data is None and isinstance(resp, dict):
        data = resp.get("data")
    return data or []


def normalize_row(r):
    """Return a dict with only the fields we care about."""
    if isinstance(r, dict):
        return {"symbol": r.get("symbol"), "name": r.get("name"), "listed": r.get("listed")}
    try:
        return {"symbol": getattr(r, "symbol", None), "name": getattr(r, "name", None), "listed": getattr(r, "listed", None)}
    except Exception:
        return {}


def upsert_safe(sb, table_name, rows):
    """
    Upsert rows using the supabase client without passing on_conflict (some clients don't support it).
    Returns True on success, False on failure.
    """
    try:
        up = sb.table(table_name).upsert(rows).execute()
        err = getattr(up, "error", None) or (up.get("error") if isinstance(up, dict) else None)
        if err:
            log.error("Upsert returned error: %s", err)
            return False
        return True
    except Exception as e:
        log.exception("Upsert call raised exception: %s", e)
        return False


def main():
    try:
        SUPABASE_URL, SUPABASE_KEY = get_supabase_credentials()
        if not SUPABASE_URL or not SUPABASE_KEY:
            log.error("SUPABASE_URL and SUPABASE_KEY must be provided as env vars or secrets.")
            sys.exit(2)

        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        if not hasattr(sb, "table"):
            log.error("Supabase client does not expose 'table' - check supabase library version.")
            sys.exit(3)

        # Configurable via env
        batch_size = int(os.getenv("BATCH_SIZE", "500"))
        source_table = os.getenv("SOURCE_TABLE", "bse_company")
        target_table = os.getenv("TARGET_TABLE", "company_symbols")

        offset = 0
        total_processed = 0

        log.info("Starting job: source=%s target=%s batch_size=%d", source_table, target_table, batch_size)

        while True:
            resp = sb.table(source_table).select("*").limit(batch_size).offset(offset).execute()
            rows = extract_rows(resp)
            if not rows:
                if offset == 0:
                    log.info("No rows to process in source table '%s'.", source_table)
                break

            normalized = []
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for r in rows:
                nr = normalize_row(r)
                if nr.get("symbol"):
                    nr["imported_at"] = now
                    normalized.append(nr)

            if normalized:
                ok = upsert_safe(sb, target_table, normalized)
                if not ok:
                    log.error("Upsert/insert failed at offset=%d — aborting job.", offset)
                    sys.exit(4)
                batch_count = len(normalized)
                total_processed += batch_count
                log.info("Processed batch: offset=%d count=%d total=%d", offset, batch_count, total_processed)
            else:
                log.info("No valid rows in batch at offset=%d", offset)

            offset += batch_size
            # small delay to avoid DB throttling; tune if necessary
            time.sleep(0.05)

        log.info("Done. Inserted/Upserted approximately %d rows into '%s'.", total_processed, target_table)
        sys.exit(0)

    except Exception:
        log.error("FATAL ERROR — full traceback follows:")
        traceback.print_exc()
        sys.exit(5)


if __name__ == "__main__":
    main()
