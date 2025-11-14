# main.py
import os
import sys
import time
import traceback
import logging

from supabase import create_client

try:
    from google.cloud import secretmanager
except Exception:
    secretmanager = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("supabase-job")


def read_secret(secret_name: str):
    """Try to read a secret from Secret Manager (fallback only)."""
    if secretmanager is None:
        return None
    try:
        project = os.getenv("PROJECT_ID") or os.getenv("GCP_PROJECT")
        if not project:
            return None
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project}/secrets/{secret_name}/versions/latest"
        resp = client.access_secret_version(request={"name": name})
        return resp.payload.data.decode("utf-8")
    except Exception:
        return None


def extract_rows(resp):
    """Return response rows from supabase client response."""
    if resp is None:
        return []
    d = getattr(resp, "data", None)
    if d is None and isinstance(resp, dict):
        d = resp.get("data")
    return d or []


def normalize_row(r):
    """Normalize dict- or object-like row into a python dict."""
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


def upsert_safe(sb, table, rows):
    """Try upsert, fallback to insert."""
    try:
        up = sb.table(table).upsert(rows).execute()
        err = getattr(up, "error", None) or (up.get("error") if isinstance(up, dict) else None)
        if err:
            log.warning("Upsert returned error: %s. Trying insert fallback.", err)
            ins = sb.table(table).insert(rows).execute()
            err2 = getattr(ins, "error", None) or (ins.get("error") if isinstance(ins, dict) else None)
            if err2:
                log.error("Insert fallback failed: %s", err2)
                return False
        return True
    except Exception as e:
        log.warning("Upsert raised exception: %s. Trying insert fallback.", e)
        try:
            ins = sb.table(table).insert(rows).execute()
            err2 = getattr(ins, "error", None) or (ins.get("error") if isinstance(ins, dict) else None)
            if err2:
                log.error("Insert fallback failed after exception: %s", err2)
                return False
            return True
        except Exception as ex:
            log.error("Final fallback failed: %s", ex)
            return False


def main():
    try:
        # Prefer env vars; fallback to Secret Manager
        SUPABASE_URL = os.getenv("SUPABASE_URL") or read_secret("supabase-url")
        SUPABASE_KEY = os.getenv("SUPABASE_KEY") or read_secret("supabase-key")

        if not SUPABASE_URL or not SUPABASE_KEY:
            log.error("SUPABASE_URL and SUPABASE_KEY must be provided as env vars or secrets.")
            sys.exit(2)

        sb = create_client(SUPABASE_URL, SUPABASE_KEY)

        if not hasattr(sb, "table"):
            log.error("Invalid supabase client; missing .table() method.")
            sys.exit(3)

        batch_size = int(os.getenv("BATCH_SIZE", "500"))
        source_table = "bse_company"
        target_table = "company_symbols"

        offset = 0
        total = 0
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        while True:
            resp = sb.table(source_table).select("*").limit(batch_size).offset(offset).execute()
            rows = extract_rows(resp)
            if not rows:
                break

            out = []
            for r in rows:
                n = normalize_row(r)
                if n.get("symbol"):
                    n["imported_at"] = now
                    out.append(n)

            if out:
                ok = upsert_safe(sb, target_table, out)
                if not ok:
                    log.error("Upsert/insert failed at offset=%d", offset)
                    sys.exit(4)
                total += len(out)
                log.info("Processed batch offset=%d count=%d total=%d", offset, len(out), total)

            offset += batch_size
            time.sleep(0.05)

        log.info("Done. Inserted/Upserted %d rows into '%s'.", total, target_table)
        sys.exit(0)

    except Exception:
        log.error("FATAL ERROR:")
        traceback.print_exc()
        sys.exit(5)


if __name__ == "__main__":
    main()
