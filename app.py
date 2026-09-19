import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from supabase import create_client, Client

st.set_page_config(
    page_title="BBU Voltage Monitoring & Analysis",
    page_icon="⚡",
    layout="wide"
)

# ----------------- KONEKSI DATABASE -----------------
@st.cache_resource
def get_db_client() -> Client:
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_KEY")
    if not url or not key:
        st.error("Konfigurasi kredensial database belum lengkap di Settings > Secrets.")
        st.stop()
    return create_client(url, key)

db = get_db_client()

st.title("⚡ BBU Voltage Monitoring & Trend Analysis")

# ================= SIDEBAR: OTOMATIS INGEST DATA =================
st.sidebar.header("📥 Ingest Data PM")
uploaded_file = st.sidebar.file_uploader(
    "Upload File Excel BBU Voltage", 
    type=["xlsx", "xls"],
    help="Data akan otomatis tersaring (hanya unit mengandung 'VPD') dan disimpan ke database."
)

if "last_processed_file" not in st.session_state:
    st.session_state.last_processed_file = None

if uploaded_file is not None and st.session_state.last_processed_file != uploaded_file.name:
    with st.spinner("File terdeteksi, memfilter data unit 'VPD' dan menyimpan..."):
        try:
            df_raw = pd.read_excel(uploaded_file, engine="openpyxl")
            
            # 1. Cari kolom Replaceable Unit ID (fleksibel terhadap variasi penamaan)
            unit_col = None
            for col in df_raw.columns:
                col_clean = str(col).lower().replace("_", " ").replace("-", " ")
                if "replaceable unit" in col_clean or "replaceableunit" in col_clean:
                    unit_col = col
                    break
            
            df_filtered = df_raw.copy()
            if unit_col:
                # Filter HANYA yang mengandung 'VPD'
                df_filtered = df_filtered[
                    df_filtered[unit_col].astype(str).str.contains("VPD", case=False, na=False)
                ]
            else:
                # Fallback: jika nama kolom berbeda, cari di seluruh kolom teks yang punya isi 'VPD'
                vpd_mask = pd.Series(False, index=df_filtered.index)
                for c in df_filtered.columns:
                    if df_filtered[c].astype(str).str.contains("VPD", case=False, na=False).any():
                        vpd_mask = vpd_mask | df_filtered[c].astype(str).str.contains("VPD", case=False, na=False)
                if vpd_mask.any():
                    df_filtered = df_filtered[vpd_mask]

            if df_filtered.empty:
                st.sidebar.warning("Tidak ditemukan baris dengan Replaceable Unit ID mengandung 'VPD'.")
                st.session_state.last_processed_file = uploaded_file.name
            else:
                # 2. Bersihkan format tanggal dan nama site
                df_filtered["parsed_time"] = pd.to_datetime(df_filtered["Begin Time"], errors="coerce")
                df_filtered["managed_element"] = df_filtered["Managed Element"].astype(str).str.strip()
                df_clean = df_filtered.dropna(subset=["parsed_time", "managed_element"])
                
                # 3. Hapus duplikat dalam file sebelum dikirim
                df_clean = df_clean.drop_duplicates(subset=["parsed_time", "managed_element"], keep="last")
                
                # 4. Bentuk payload JSON
                records = []
                for _, row in df_clean.iterrows():
                    min_v = row.get("MinVoltageOfBBU(V)")
                    avg_v = row.get("AvgVoltageOfBBU(V)")
                    max_v = row.get("MaxVoltageOfBBU(V)")
                    
                    records.append({
                        "begin_time": row["parsed_time"].isoformat(),
                        "managed_element": row["managed_element"],
                        "min_voltage": float(min_v) if pd.notna(min_v) and not np.isnan(min_v) else None,
                        "avg_voltage": float(avg_v) if pd.notna(avg_v) and not np.isnan(avg_v) else None,
                        "max_voltage": float(max_v) if pd.notna(max_v) and not np.isnan(max_v) else None,
                    })

                # 5. Batch upsert ke database
                batch_size = 250
                total_records = len(records)
                progress_bar = st.sidebar.progress(0)
                
                for i in range(0, total_records, batch_size):
                    batch = records[i:i + batch_size]
                    db.table("bbu_voltage").upsert(
                        batch,
                        on_conflict="managed_element,begin_time"
                    ).execute()
                    progress_bar.progress(min((i + batch_size) / total_records, 1.0))
                
                st.session_state.last_processed_file = uploaded_file.name
                st.sidebar.success(f"Berhasil menyimpan {total_records} data VPD ke database!")
                st.cache_data.clear()
                st.rerun()
        except Exception as e:
            st.sidebar.error(f"Gagal memproses data: {str(e)}")

st.sidebar.markdown("---")

# ================= SIDEBAR: PARAMETER FILTER =================
st.sidebar.header("⚙️ Parameter Filter")
filter_mode = st.sidebar.radio("Mode Rentang Waktu:", ["Berdasarkan Data Terakhir", "Semua Data"])

if filter_mode == "Berdasarkan Data Terakhir":
    duration_hours = st.sidebar.slider(
        "Rentang Waktu Terakhir (Jam):",
        min_value=1,
        max_value=72,
        value=24,
        step=1,
        help="Geser untuk menentukan berapa jam ke belakang dari data timestamp terakhir"
    )
else:
    duration_hours = None

threshold_voltage = st.sidebar.number_input(
    "Batas Voltage Drop (V):", 
    min_value=30.0, 
    max_value=60.0, 
    value=47.0, 
    step=0.5
)

# ================= AUTO LOAD DARI DATABASE =================
@st.cache_data(ttl=30)
def load_all_voltage_records():
    res = (
        db.table("bbu_voltage")
        .select("managed_element, min_voltage, avg_voltage, max_voltage, begin_time")
        .order("begin_time", desc=False)
        .limit(50000)
        .execute()
    )
    return res.data

try:
    all_raw_data = load_all_voltage_records()
except Exception as e:
    st.error(f"Gagal membaca data dari server: {str(e)}")
    all_raw_data = []

# ================= FILTERING PADA DATAFRAME =================
if not all_raw_data:
    st.info("Database masih kosong. Silakan unggah file Excel PM pada panel sebelah kiri.")
else:
    df_all = pd.DataFrame(all_raw_data)
    df_all["begin_time"] = pd.to_datetime(df_all["begin_time"])

    # Filter rentang jam mundur dari timestamp TERAKHIR di database
    if duration_hours is not None and not df_all.empty:
        max_time = df_all["begin_time"].max()
        cutoff_time = max_time - timedelta(hours=int(duration_hours))
        df_all = df_all[df_all["begin_time"] >= cutoff_time]

    # Filter site di bawah batas voltage
    df_dropped = df_all[df_all["min_voltage"] < threshold_voltage]

    # Metrics Ringkasan
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Batas Voltage", f"< {threshold_voltage} V")
    col2.metric("Rentang Waktu", f"{duration_hours} Jam Terakhir" if duration_hours else "Semua Data")
    col3.metric("Site Terdampak Drop", f"{df_dropped['managed_element'].nunique()} Site")
    col4.metric("Total Kejadian Drop", f"{len(df_dropped)} Sampel")

    st.markdown("---")

    # Tab Visualisasi
    tab1, tab2 = st.tabs(["📊 Ranking Drop Voltage", "📈 Grafik Tren Voltage"])

    with tab1:
        if not df_dropped.empty:
            summary = (
                df_dropped.groupby("managed_element")
                .agg(
                    total_drop=("min_voltage", "count"),
                    min_v_recorded=("min_voltage", "min"),
                    avg_v_recorded=("avg_voltage", "mean")
                )
                .reset_index()
                .sort_values(by=["total_drop", "min_v_recorded"], ascending=[False, True])
                .rename(columns={
                    "managed_element": "Managed Element (Site)",
                    "total_drop": "Frekuensi Drop (< Batas)",
                    "min_v_recorded": "Voltage Terendah (V)",
                    "avg_v_recorded": "Rata-rata Voltage (V)"
                })
            )
            
            summary["Voltage Terendah (V)"] = summary["Voltage Terendah (V)"].round(3)
            summary["Rata-rata Voltage (V)"] = summary["Rata-rata Voltage (V)"].round(3)

            st.subheader(f"Daftar Site Terdampak Voltage < {threshold_voltage} V")
            st.dataframe(summary, use_container_width=True, hide_index=True)

            with st.expander("🔍 Lihat Rincian Log Per Jam (Data Mentah)"):
                df_detail = df_dropped.copy().sort_values(by="begin_time", ascending=False)
                df_detail["Waktu (Lokal)"] = df_detail["begin_time"].dt.strftime("%Y-%m-%d %H:%M")
                st.dataframe(
                    df_detail[["Waktu (Lokal)", "managed_element", "min_voltage", "avg_voltage", "max_voltage"]]
                    .rename(columns={
                        "managed_element": "Site Name",
                        "min_voltage": "Min Voltage (V)",
                        "avg_voltage": "Avg Voltage (V)",
                        "max_voltage": "Max Voltage (V)"
                    }),
                    use_container_width=True,
                    hide_index=True
                )
        else:
            st.success(f"Kondisi optimal. Tidak ditemukan site dengan voltage di bawah {threshold_voltage} V.")

    with tab2:
        st.subheader("Grafik Pergerakan Min, Avg, dan Max Voltage")
        
        dropped_sites = sorted(df_dropped["managed_element"].unique().tolist())
        all_sites = sorted(df_all["managed_element"].unique().tolist())
        site_list = dropped_sites + [s for s in all_sites if s not in dropped_sites]

        selected_site = st.selectbox("Pilih Site / Managed Element:", options=site_list)

        if selected_site:
            df_site = df_all[df_all["managed_element"] == selected_site].sort_values(by="begin_time")

            if not df_site.empty:
                chart_data = df_site.set_index("begin_time")[["min_voltage", "avg_voltage", "max_voltage"]]
                chart_data.columns = ["Min Voltage", "Avg Voltage", "Max Voltage"]
                
                st.line_chart(chart_data, color=["#E53E3E", "#3182CE", "#38A169"])
                
                s_min = df_site["min_voltage"].min()
                s_avg = df_site["avg_voltage"].mean()
                s_max = df_site["max_voltage"].max()
                
                c1, c2, c3 = st.columns(3)
                c1.metric("Min Terendah", f"{s_min:.2f} V" if pd.notna(s_min) else "-")
                c2.metric("Rata-rata Tegangan", f"{s_avg:.2f} V" if pd.notna(s_avg) else "-")
                c3.metric("Max Tertinggi", f"{s_max:.2f} V" if pd.notna(s_max) else "-")
            else:
                st.warning("Data log tidak tersedia untuk site ini.")
