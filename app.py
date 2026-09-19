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

# ================= SIDEBAR: OTOMATIS MULTI-UPLOAD =================
st.sidebar.header("📥 Ingest Data PM")
uploaded_files = st.sidebar.file_uploader(
    "Upload File Excel BBU Voltage (Bisa Banyak):", 
    type=["xlsx", "xls"],
    accept_multiple_files=True,
    help="Pilih satu atau beberapa file Excel. Data unit VPD akan otomatis diproses dan disimpan."
)

# Inisialisasi daftar file yang sudah diproses di session_state
if "processed_file_names" not in st.session_state:
    st.session_state.processed_file_names = set()

# Helper fungsi konversi tegangan aman
def parse_voltage(val):
    if pd.isna(val) or val is None:
        return None
    try:
        val_str = str(val).strip().replace(",", ".")
        f_val = float(val_str)
        return None if np.isnan(f_val) else f_val
    except:
        return None

# Cek apakah ada file baru yang belum pernah diproses pada sesi ini
new_files_to_process = [
    f for f in (uploaded_files or []) 
    if f.name not in st.session_state.processed_file_names
]

if new_files_to_process:
    total_saved_all_files = 0
    with st.spinner(f"Memproses otomatis {len(new_files_to_process)} file baru ke database..."):
        for current_file in new_files_to_process:
            try:
                df_raw = pd.read_excel(current_file, engine="openpyxl")

                # Standarisasi pencarian nama kolom
                col_map = {}
                for col in df_raw.columns:
                    c_clean = str(col).lower().replace(" ", "").replace("_", "").replace("(", "").replace(")", "")
                    col_map[c_clean] = col

                time_col = col_map.get("begintime", "Begin Time")
                me_col = col_map.get("managedelement", "Managed Element")
                min_col = col_map.get("minvoltageofbbuv") or col_map.get("minvoltage") or col_map.get("minvoltageofbbu")
                avg_col = col_map.get("avgvoltageofbbuv") or col_map.get("avgvoltage") or col_map.get("avgvoltageofbbu")
                max_col = col_map.get("maxvoltageofbbuv") or col_map.get("maxvoltage") or col_map.get("maxvoltageofbbu")

                # Filter unit VPD jika kolom replaceable unit tersedia
                unit_col = None
                for c_clean, c_orig in col_map.items():
                    if "replaceableunit" in c_clean or "unitid" in c_clean:
                        unit_col = c_orig
                        break

                df_filtered = df_raw.copy()
                if unit_col:
                    has_vpd = df_filtered[unit_col].astype(str).str.contains("VPD", case=False, na=False)
                    if has_vpd.any():
                        df_filtered = df_filtered[has_vpd]

                # Bersihkan tanggal dan Site
                df_filtered["parsed_time"] = pd.to_datetime(df_filtered[time_col], errors="coerce")
                df_filtered["managed_element"] = df_filtered[me_col].astype(str).str.strip()
                df_clean = df_filtered.dropna(subset=["parsed_time", "managed_element"])

                # Hapus duplikat per file sebelum upsert
                df_clean = df_clean.drop_duplicates(subset=["parsed_time", "managed_element"], keep="last")

                records = []
                for _, row in df_clean.iterrows():
                    records.append({
                        "begin_time": row["parsed_time"].isoformat(),
                        "managed_element": row["managed_element"],
                        "min_voltage": parse_voltage(row[min_col]) if min_col in row else None,
                        "avg_voltage": parse_voltage(row[avg_col]) if avg_col in row else None,
                        "max_voltage": parse_voltage(row[max_col]) if max_col in row else None,
                    })

                # Batch upsert per file
                batch_size = 250
                for i in range(0, len(records), batch_size):
                    batch = records[i:i + batch_size]
                    db.table("bbu_voltage").upsert(
                        batch,
                        on_conflict="managed_element,begin_time"
                    ).execute()

                total_saved_all_files += len(records)
                st.session_state.processed_file_names.add(current_file.name)
            except Exception as e:
                st.sidebar.error(f"Gagal memproses file {current_file.name}: {str(e)}")

        if total_saved_all_files > 0:
            st.sidebar.success(f"Berhasil menyimpan {total_saved_all_files} baris data dari {len(new_files_to_process)} file!")
            st.cache_data.clear()
            st.rerun()

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

    # Filter site di bawah batas voltage (abaikan baris null)
    df_valid_voltage = df_all.dropna(subset=["min_voltage"])
    df_dropped = df_valid_voltage[df_valid_voltage["min_voltage"] < threshold_voltage]

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
