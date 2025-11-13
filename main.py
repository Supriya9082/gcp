# job_main.py
import os, json, time
from supabase import create_client
from google.cloud import secretmanager


def get_secret(secret_name: str):
    project = os.getenv("PROJECT_ID") or os.getenv("GCP_PROJECT")
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project}/secrets/{secret_name}/versions/latest"
    res = client.access_secret_version(request={"name": name})
    return res.payload.data.decode("utf-8")

def main():
    SUPABASE_URL = os.getenv("SUPABASE_URL") or get_secret("supabase-url")
    
    SUPABASE_KEY = os.getenv("SUPABASE_KEY") or get_secret("supabase-key")

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    batch_size = int(os.getenv("BATCH_SIZE", "100"))

    resp = sb.table("input_table").select("*").eq("processed", False).limit(batch_size).execute()
    rows = resp.data or []
    if not rows:
        print("No rows to process.")
        return

    output_rows = []
    to_mark = []
    for r in rows:
        try:
            data_field = r.get("data") or ""
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
        if up.error:
            print("upsert error", up.error)
            raise SystemExit(1)
        print(f"Upserted {len(output_rows)} rows")

    for i in range(0, len(to_mark), 100):
        chunk = to_mark[i:i+100]
        upd = sb.table("input_table").update({"processed": True}).in_("id", chunk).execute()
        if upd.error:
            print("mark error", upd.error)
            raise SystemExit(1)

    print("Done")

if __name__ == "__main__":
    main()
