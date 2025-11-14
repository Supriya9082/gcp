# main.py
import os
import time
import traceback
from supabase import create_client

def get_resp_data(resp):
    """Return resp.data if object-like else resp.get('data') if dict-like else []"""
    return getattr(resp, "data", None) or (resp.get("data") if isinstance(resp, dict) else None) or []

def main():
    try:
        SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("supabase_url") or os.getenv("SUPABASE_URL".lower())
        SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("supabase_key") or os.getenv("SUPABASE_KEY".lower())
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY environment variables must be set.")

        sb = create_client(SUPABASE_URL, SUPABASE_KEY)

        # Defensive: ensure table API exists
        if not hasattr(sb, "table"):
            raise RuntimeError("Supabase client does not expose 'table' — check the supabase library version.")

        batch_size = int(os.getenv("BATCH_SIZE", "500"))  # how many rows to fetch per page

        # We'll page through the bse_company table to avoid too-large fetches
        offset = 0
        total_inserted = 0

        while True:
            resp = sb.table("bse_company").select("*").limit(batch_size).offset(offset).execute()
            rows = get_resp_data(resp)
            if not rows:
                break

            # Normalize rows to dicts if they are objects (some clients return objects)
            normalized = []
            for r in rows:
                if isinstance(r, dict):
                    normalized.append(r)
                else:
                    # try to pull attributes -> convert to dict
                    try:
                        d = {k: getattr(r, k) for k in dir(r) if not k.startswith("_") and not callable(getattr(r, k))}
                        # keep only common fields (symbol, name, listed) if present
                        normalized.append(d)
                    except Exception:
                        # fallback: try to use .get if available
                        try:
                            normalized.append({ "symbol": r.get("symbol"), "name": r.get("name"), "listed": r.get("listed") })
                        except Exception:
                            continue

            # Build rows for target table
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            out_rows = []
            for r in normalized:
                # keep only the fields we care about; tolerate missing fields
                out_rows.append({
                    "symbol": r.get("symbol") if isinstance(r, dict) else None,
                    "name": r.get("name") if isinstance(r, dict) else None,
                    "listed": r.get("listed") if isinstance(r, dict) else None,
                    "imported_at": now
                })

            # remove any rows without a symbol (can't dedupe/identify otherwise)
            out_rows = [o for o in out_rows if o.get("symbol")]

            if out_rows:
                # Try upsert if available; fallback to insert
                try:
                    # Many supabase clients support upsert(..., on_conflict='symbol') or upsert(...)
                    # We'll attempt a plain upsert first.
                    up = sb.table("company_symbols").upsert(out_rows).execute()
                    # Check for error in either object or dict response
                    err = getattr(up, "error", None) or (up.get("error") if isinstance(up, dict) else None)
                    if err:
                        # try insert as fallback
                        ins = sb.table("company_symbols").insert(out_rows).execute()
                        err2 = getattr(ins, "error", None) or (ins.get("error") if isinstance(ins, dict) else None)
                        if err2:
                            print("ERROR: insert/upsert failed:", err2)
                            raise SystemExit(1)
                except Exception as e:
                    # final fallback: try insert
                    ins = sb.table("company_symbols").insert(out_rows).execute()
                    err2 = getattr(ins, "error", None) or (ins.get("error") if isinstance(ins, dict) else None)
                    if err2:
                        print("ERROR: fallback insert failed:", err2)
                        raise

                total_inserted += len(out_rows)

            # advance paging
            offset += batch_size

        print(f"Done. Inserted/Upserted approximately {total_inserted} rows into 'company_symbols'")

    except Exception:
        print("FATAL ERROR — full traceback below:")
        traceback.print_exc()
        raise

if __name__ == "__main__":
    main()
