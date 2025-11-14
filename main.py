# main.py
"""
Batch job: read rows from a Supabase source table, transform & upsert into a target table,
dedupe rows inside each batch (last occurrence wins), and optionally mark source rows as processed.

Behaviour controlled via env vars:
- SUPABASE_URL / SUPABASE_KEY (or alternatives supabase_url/supabase-key etc.)
- BATCH_SIZE (default 500)
- SOURCE_TABLE (default bse_company)
- TARGET_TABLE (default company_symbols)
- MARK_PROCESSED (default "true") -> if true, will set processed=True on source rows after successful upsert
- PROCESSED_COLUMN (default "processed") -> name of boolean processed column on source table
- ID_COLUMN (default "id") -> primary id column in source table used when marking processed
"""
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
    """
    Map a source row into the minimal dict we will upsert into the target table.
    Adjust this to include additional/renamed fields you need.
    """
    if isinstance(r, dict):
        return {
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "listed": r.get("listed")
        }
    try:
        return {
            "symbol": getattr(r, "symbol", None),
            "name": getattr(r, "name", None),
            "listed": getattr(r, "listed", None)
        }
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


def mark_source_processed(sb, source_table, id_column, ids, processed_column="processed"):
    """
    Mark a list of source row ids as processed. Operates in chunks of 100.
    Returns True if all updates succeeded, False otherwise.
    """
    if not ids:
        return True
    try:
        CHUNK = 100
        for i in range(0, len(ids), CHUNK):
            chunk = ids[i:i + CHUNK]
            res = sb.table(source_table).update({processed_column: True}).in_(id_column, chunk).execute()
            err = getattr(res, "error", None) or (res.get("error") if isinstance(res, dict) else None)
            if err:
                log.error("Failed to mark processed for chunk starting at %d: %s", i, err)
                return False
        return True
    except Exception as e:
        log.exception("Exception while marking source processed: %s", e)
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

        # Config via env
        batch_size = int(os.getenv("BATCH_SIZE", "500"))
        source_table = os.getenv("SOURCE_TABLE", "bse_company")
        target_table = os.getenv("TARGET_TABLE", "company_symbols")
        mark_processed_flag = os.getenv("MARK_PROCESSED", "true").lower() in ("1", "true", "yes")
        processed_column = os.getenv("PROCESSED_COLUMN", "processed")
        id_column = os.getenv("ID_COLUMN", "id")

        offset = 0
        total_processed = 0

        log.info("Job starting: source=%s target=%s batch_size=%d mark_processed=%s", source_table, target_table, batch_size, mark_processed_flag)

        while True:
            resp = sb.table(source_table).select("*").limit(batch_size).offset(offset).execute()
            rows = extract_rows(resp)
            if not rows:
                if offset == 0:
                    log.info("No rows to process in source table '%s'.", source_table)
                break

            # Normalize rows to the shape we will upsert.
            # Keep original row objects nearby so we can extract IDs for marking processed.
            normalized = []
            original_rows = []
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for r in rows:
                nr = normalize_row(r)
                if nr.get("symbol"):
                    nr["imported_at"] = now
                    normalized.append(nr)
                    original_rows.append(r)

            if not normalized:
                log.info("No valid rows in this batch (offset=%d). Skipping.", offset)
                offset += batch_size
                continue

            # Dedupe by symbol inside the batch (last occurrence wins)
            dedup_map = {}
            dedup_original_map = {}  # keep corresponding original row for id extraction
            for idx, nr in enumerate(normalized):
                sym = nr.get("symbol")
                if not sym:
                    continue
                dedup_map[sym] = nr
                # store the original row that corresponds to this normalized row (last wins too)
                dedup_original_map[sym] = original_rows[idx]

            deduped_rows = list(dedup_map.values())
            if not deduped_rows:
                log.info("No deduped rows to upsert at offset=%d", offset)
                offset += batch_size
                continue

            # Attempt upsert
            ok = upsert_safe(sb, target_table, deduped_rows)
            if not ok:
                log.error("Upsert failed at offset=%d. Aborting job.", offset)
                sys.exit(4)

            # If configured, mark those original source rows as processed.
            if mark_processed_flag:
                # gather ids from dedup_original_map where the original has the id_column
                ids_to_mark = []
                for sym, orig in dedup_original_map.items():
                    # original may be dict-like or object-like
                    val = None
                    if isinstance(orig, dict):
                        val = orig.get(id_column)
                    else:
                        val = getattr(orig, id_column, None)
                    if val is not None:
                        ids_to_mark.append(val)

                if ids_to_mark:
                    marked_ok = mark_source_processed(sb, source_table, id_column, ids_to_mark, processed_column)
                    if not marked_ok:
                        log.error("Failed to mark source rows as processed at offset=%d. Aborting job.", offset)
                        sys.exit(6)
                else:
                    log.debug("No ids found in original rows to mark as processed (maybe no '%s' column).", id_column)

            batch_count = len(deduped_rows)
            total_processed += batch_count
            log.info("Processed batch offset=%d count=%d total=%d", offset, batch_count, total_processed)

            offset += batch_size
            time.sleep(0.05)

        log.info("Job finished. Inserted/Upserted approximately %d rows into '%s'.", total_processed, target_table)
        sys.exit(0)

    except Exception:
        log.error("FATAL ERROR — full traceback follows:")
        traceback.print_exc()
        sys.exit(5)


if __name__ == "__main__":
    main()
