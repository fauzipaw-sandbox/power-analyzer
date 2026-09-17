import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client

st.set_page_config(page_title="BBU Voltage Monitoring", page_icon="⚡", layout="wide")

# Koneksi Supabase via secrets
@st.cache_resource
def get_supabase_client() -> Client:
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_KEY")
    if not url or not key:
        st.error("Kredensial SUPABASE_URL atau SUPABASE_KEY belum diset di Streamlit Secrets.")
        st.stop()
    return create_client(url, key)

supabase = get_supabase_client()

st.title("⚡ BBU Voltage Monitoring & Analysis")

# ================= SIDEBAR: INGEST DATA =================
st.sidebar.header("📥 Ingest Data PM")
uploaded_file = st.sidebar.file_uploader("Upload File Excel BBU Voltage", type=["xlsx", "xls"])

if uploaded_file is not None:
    if st.sidebar.button("Proses & Simpan ke Supabase", use_container_width=True):
        with st.spinner("Membaca dan memproses file Excel..."):
            try:
                df_raw = pd.read_excel(uploaded_file, engine="openpyxl")
                
                # Normalisasi kolom yang dibutuhkan
                df_clean = df_raw.copy()
                df_clean["parsed_time"] = pd.to_datetime(df_clean["Begin Time"], errors="coerce")
                df_clean = df_clean.dropna(subset=["parsed_time", "Managed Element"])
                
                # Mengubah NaN numerik menjadi None agar valid dalam JSON
                records = []
                for _, row in df_clean.iterrows():
                    min_v = row.get("MinVoltageOfBBU(V)")
                    avg_v = row.get("AvgVoltageOfBBU(V)")
                    max_v = row.get("MaxVoltageOfBBU(V)")
                    
                    records.append({
                        "begin_time": row["parsed_time"].isoformat(),
                        "managed_element": str(row["Managed Element"]).strip(),
                        "min_voltage": float(min_v) if pd.notna(min_v) and not np.isnan(min_v) else None,
                        "avg_voltage": float(avg_v) if pd.notna(avg_v) and not np.isnan(avg_v) else None,
                        "max_voltage": float(max_v) if pd.notna(max_v) and not np.isnan(max_v) else None,
                    })

                # Upload batch (250 baris per batch agar aman dari payload limit)
                batch_size = 250
                total_records = len(records)
                progress_bar = st.sidebar.progress(0)
                
                for i in range(0, total_records, batch_size):
                    batch = records[i:i + batch_size]
                    supabase.table("bbu_voltage").upsert(
                        batch,
                        on_conflict="managed_element,begin_time"
                    ).execute()
                    progress_bar.progress(min((i + batch_size) / total_records, 1.0))
                
                st.sidebar.success(f"Berhasil mengunggah {total_records} data (duplikat otomatis ditimpa)!")
            except Exception as e:
                st.sidebar.error(f"Gagal memproses data: {str(e)}")

st.sidebar.markdown("---")

# ================= SIDEBAR: FILTER =================
st.sidebar.header("⚙️ Parameter Analisis")
duration_hours = st.sidebar.number_input("Rentang Waktu Terakhir (Jam):", min_value=1, max_value=720, value=24, step=1)
threshold_voltage = st.sidebar.number_input("Batas Voltage Drop (V):", min_value=30.0, max_value=60.0, value=47.0, step=0.5)

# Hitung cut-off waktu mundur dari waktu sekarang (UTC)
time_limit = (datetime.now(timezone.utc) - timedelta(hours=int(duration_hours))).isoformat()

# ================= QUERY DATA =================
try:
    response = (
        supabase.table("bbu_voltage")
        .select("managed_element, min_voltage, avg_voltage, begin_time")
        .gte("begin_time", time_limit)
        .lt("min_voltage", threshold_voltage)
        .order("begin_time", desc=True)
        .execute()
    )
    data = response.data
except Exception as e:
    st.error(f"Gagal mengambil data dari Supabase: {str(e)}")
    data = []

# ================= DISPLAY METRICS & TABLES =================
col1, col2, col3 = st.columns(3)
col1.metric("Batas Voltage", f"< {threshold_voltage} V")
col2.metric("Durasi Filter", f"{duration_hours} Jam Terakhir")
col3.metric("Total Sampel Drop", f"{len(data)} Kejadian")

if data:
    df_res = pd.DataFrame(data)
    
    # Agregasi ranking site paling sering drop
    site_summary = (
        df_res.groupby("managed_element")
        .agg(
            frekuensi_drop=("min_voltage", "count"),
            voltase_terendah=("min_voltage", "min")
        )
        .reset_index()
        .sort_values(by=["frekuensi_drop", "voltase_terendah"], ascending=[False, True])
        .rename(columns={
            "managed_element": "Managed Element (Site)",
            "frekuensi_drop": "Jumlah Drop (< Batas)",
            "voltase_terendah": "Min Voltage Tercatat (V)"
        })
    )

    st.subheader(f"Daftar Site Terdampak ({len(site_summary)} Site)")
    st.dataframe(site_summary, use_container_width=True, hide_index=True)

    with st.expander("🔍 Lihat Rincian Log Per Jam"):
        df_detail = df_res.copy()
        df_detail["begin_time"] = pd.to_datetime(df_detail["begin_time"]).dt.strftime("%Y-%m-%d %H:%M")
        st.dataframe(
            df_detail[["begin_time", "managed_element", "min_voltage", "avg_voltage"]]
            .rename(columns={
                "begin_time": "Waktu (Begin Time)",
                "managed_element": "Site Name",
                "min_voltage": "Min Voltage (V)",
                "avg_voltage": "Avg Voltage (V)"
            }),
            use_container_width=True,
            hide_index=True
        )
else:
    st.info(f"Tidak ada data tegangan di bawah {threshold_voltage} V dalam {duration_hours} jam terakhir.")
