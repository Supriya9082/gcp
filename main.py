# main.py — diagnostic-friendly version
import os, json, time, traceback
from supabase import create_client
from google.cloud import secretmanager

def get_secret(secret_name: str):
    project = os.getenv("PROJECT_ID") or os.getenv("GCP_PROJECT")
    if not project:
        raise RuntimeError("PROJECT_ID / GCP_PROJECT env var is not set. Set PROJECT_ID in the Dockerfile or pass it as an env var.")
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project}/secrets/{secret_name}/versions/latest"
    res = client.access_secret_version(request={"name": name})
    return res.payload.data.decode("utf-8")

def main():
    try:
        # Prefer env vars (for easier debugging); fall back to Secret Manager
        SUPABASE_URL = os.getenv("supabase_url") 
        SUPABASE_KEY = os.getenv("supabase_key")

        print(f"DEBUG: SUPABASE_URL from_env={from_env_url}; SUPABASE_KEY from_env={from_env_key}")
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("Supabase URL or Key is empty after reading env/secrets.")

        sb = create_client(SUPABASE_URL, SUPABASE_KEY)

        batch_size = int(os.getenv("BATCH_SIZE", "100"))

        # Defensive: check API surface
        if not hasattr(sb, "table"):
            raise RuntimeError("Supabase client does not have 'table' attribute — check supabase library version.")

        resp = sb.table("input_table").select("*").eq("processed", False).limit(batch_size).execute()
        print("DEBUG: Full Supabase select response:", resp)

        # Defensive: resp may be a dict or object
        rows = getattr(resp, "data", None) or (resp.get("data") if isinstance(resp, dict) else None) or []
        print(f"DEBUG: fetched {len(rows)} rows")

        if not rows:
            print("No rows to process.")
            return

        output_rows = []
        to_mark = []
        for r in rows:
            try:
                data_field = r.get("data") if isinstance(r, dict) else getattr(r, "get", lambda k: None)("data") or ""
                try:
                    parsed = json.loads(data_field) if isinstance(data_field, str) else data_field
                except Exception:
                    parsed = data_field
                processed_value = len(str(parsed))
                output_rows.append({
                    "input_id": r["id"],
                    "processed_value": processed_value,
                    "source_data": parsed,
                    "processed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                })
                to_mark.append(r["id"])
            except Exception as e:
                print("process error", e)

        if output_rows:
            up = sb.table("output_table").upsert(output_rows).execute()
            # handle dict or object result
            err = getattr(up, "error", None) or (up.get("error") if isinstance(up, dict) else None)
            if err:
                print("upsert error", err)
                raise SystemExit(1)
            print(f"Upserted {len(output_rows)} rows")

        for i in range(0, len(to_mark), 100):
            chunk = to_mark[i:i+100]
            upd = sb.table("input_table").update({"processed": True}).in_("id", chunk).execute()
            err = getattr(upd, "error", None) or (upd.get("error") if isinstance(upd, dict) else None)
            if err:
                print("mark error", err)
                raise SystemExit(1)

        print("Done")

    except Exception as exc:
        # Print full traceback so we can diagnose
        print("FATAL ERROR — full traceback below:")
        traceback.print_exc()
        # exit non-zero so Cloud Run marks execution as failed
        raise

if __name__ == "__main__":
    main()
