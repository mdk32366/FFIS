import sys, pandas as pd
sys.path.insert(0,"/tmp/FFIS")
import ffis_email_pipeline as P

# Mock exporter to verify the good layer (and only good) is exported
class MockExporter(P.Exporter):
    name="mock"
    def __init__(self): self.calls=[]
    def export(self, df, object_type):
        self.calls.append((object_type, list(df["Name"]) if "Name" in df else len(df)))
        return {"success":True,"destination":"mock","table":"acct","rows":int(len(df))}

data = pd.DataFrame({
    "Name":["Acme","Acme","Beta",""],   # Acme repeat -> dup; blank -> bad
    "Industry":["Mfg","Mfg","Tech","Retail"],
})
r = P.route_records(data,"a.csv",dedup=P.NullDedup())
mock=MockExporter()
cfg={"export_good":True,"reply":False}
# simulate process_message export branch
r.export_status = mock.export(r.good, r.object_type)
print("counts:", r.counts)
print("exported good names:", mock.calls)
assert r.counts=={"total":4,"good":2,"duplicate":1,"bad":1}, r.counts
assert mock.calls==[("Account",["Acme","Beta"])], mock.calls   # only good rows
# summary includes export section
s=P.build_summary(r)
assert "Export (good layer)" in s and "mock" in s
print("summary export line:", [l for l in s.splitlines() if "Loaded" in l])

# factory: auto with nothing configured -> none; api when endpoint set
import os
assert P.make_exporter("auto").name=="none"
os.environ["API_ENDPOINT_URL"]="https://example.com/ingest"
assert P.make_exporter("auto").name=="api"
assert P.make_exporter("snowflake").name=="snowflake"
# table resolver
os.environ["FFIS_EXPORT_TABLE_PREFIX"]="stg_"
assert P._export_table_for("Opportunity")=="stg_opportunity", P._export_table_for("Opportunity")
os.environ["FFIS_EXPORT_TABLE_ACCOUNT"]="ANALYTICS.SF.ACCOUNT"
assert P._export_table_for("Account")=="ANALYTICS.SF.ACCOUNT"
print("exporter factory + table resolver OK")
print("\nEXPORT TESTS PASSED")
