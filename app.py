import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from supabase import create_client, Client

st.set_page_config(page_title="BBU Voltage Analysis", page_icon="⚡", layout="wide")

# Koneksi Supabase
@st.cache_resource
def init_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)

supabase = init_supabase()

st.title("⚡ BBU Voltage Monitoring & Analysis")

# Sidebar - Ingest Data Excel
st.sidebar.header("📥 Ingest Data Baru")
uploaded_file = st.sidebar.file_uploader("Upload File PM Excel", type=["xlsx", "xls"])

if uploaded_file is not None:
    if st.sidebar.button("Proses & Simpan ke Supabase"):
        with st.spinner("Memproses data..."):
            df_upload = pd.read_excel(uploaded_file)
            
            # Mapping dan parsing kolom
            records = []
            for _, row in df_upload.iterrows():
                records.append({
                    "begin_time": pd.to_datetime(row["Begin Time"]).isoformat(),
                    "managed_element": str(row["Managed Element"]),
                    "min_voltage": float(row["MinVoltageOfBBU(V)"]) if pd.notnull(row["MinVoltageOfBBU(V)"]) else None,
                    "avg_voltage": float(row["AvgVoltageOfBBU(V)"]) if pd.notnull(row["AvgVoltageOfBBU(V)"]) else None,
                    "max_voltage": float(row["MaxVoltageOfBBU(V)"]) if pd.notnull(row["MaxVoltageOfBBU(V)"]) else None,
                })
            
            # Batch upsert biar menimpa data duplikat (unik di managed_element + begin_time)
            batch_size = 500
            total_records = len(records)
            for i in range(0, total_records, batch_size):
                batch = records[i:i + batch_size]
                supabase.table("bbu_voltage").upsert(batch, on_conflict="managed_element,begin_time").execute()
            
            st.sidebar.success(f"Berhasil mengunggah {total_records} baris data!")

st.sidebar.markdown("---")

# Sidebar - Filter & Customization
st.sidebar.header("⚙️ Filter Analisis")
duration_hours = st.sidebar.number_input("Durasi Terakhir (Jam):", min_value=1, max_value=720, value=3, step=1)
threshold_voltage = st.sidebar.number_input("Batas Maksimum Voltage (V):", min_value=30.0, max_value=60.0, value=47.0, step=0.5)

# Query Supabase
time_limit = (datetime.utcnow() - timedelta(hours=int(duration_hours))).isoformat()

response = (
    supabase.table("bbu_voltage")
    .select("managed_element, min_voltage, begin_time")
    .gte("begin_time", time_limit)
    .lt("min_voltage", threshold_voltage)
    .execute()
)

data = response.data

# Tampilan Metrik & Tabel
col1, col2 = st.columns(2)
col1.metric("Batas Voltage", f"< {threshold_voltage} V")
col2.metric("Rentang Waktu", f"{duration_hours} Jam Terakhir")

if data:
    df_result = pd.DataFrame(data)
    
    # Hitung jumlah kejadian per site
    summary = (
        df_result.groupby("managed_element")
        .size()
        .reset_index(name="Jumlah Kejadian (Drop)")
        .sort_values(by="Jumlah Kejadian (Drop)", ascending=False)
        .rename(columns={"managed_element": "Managed Element"})
    )
    
    st.subheader(f"Daftar Site Terdampak ({len(summary)} Site)")
    st.dataframe(summary, use_container_width=True, hide_index=True)
    
    # Detail log kejadian
    with st.expander("Lihat Detail Log Kejadian"):
        st.dataframe(
            df_result[["begin_time", "managed_element", "min_voltage"]]
            .sort_values(by="begin_time", ascending=False)
            .rename(columns={
                "begin_time": "Waktu Kejadian",
                "managed_element": "Managed Element",
                "min_voltage": "Min Voltage (V)"
            }),
            use_container_width=True,
            hide_index=True
        )
else:
    st.info(f"Kondisi aman. Tidak ditemukan site dengan voltage < {threshold_voltage}V dalam {duration_hours} jam terakhir.")
