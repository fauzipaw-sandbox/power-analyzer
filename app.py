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
    help="Pilih satu atau beberapa file Excel. Data otomatis diproses dan disimpan ke database."
)

if "processed_file_names" not in st.session_state:
    st.session_state.processed_file_names = set()

def parse_voltage(val):
    if pd.isna(val) or val is None:
        return None
    try:
        val_str = str(val).strip().replace(",", ".")
        f_val = float(val_str)
        return None if np.isnan(f_val) else f_val
    except:
        return None

new_files_to_process = [
    f for f in (uploaded_files or []) 
    if f.name not in st.session_state.processed_file_names
]

if new_files_to_process:
    total_saved_all_files = 0
    with st.spinner(f"Memproses otomatis {len(new_files_to_process)} file ke database..."):
        for current_file in new_files_to_process:
            try:
                df_raw = pd.read_excel(current_file, engine="openpyxl")

                # 1. Deteksi dinamis kolom waktu, ME, dan voltage
                time_col = None
                me_col = None
                min_col = None
                avg_col = None
                max_col = None

                for col in df_raw.columns:
                    c = str(col).lower().replace(" ", "").replace("_", "").replace(".", "").replace("(", "").replace(")", "")
                    
                    if ("begintime" in c or "starttime" in c or "time" in c) and not time_col:
                        time_col = col
                    elif "managedelement" in c or "nename" in c or "site" in c:
                        me_col = col
                    elif "min" in c and ("volt" in c or "v" in c):
                        min_col = col
                    elif ("avg" in c or "mean" in c) and ("volt" in c or "v" in c):
                        avg_col = col
                    elif "max" in c and ("volt" in c or "v" in c):
                        max_col = col

                # Fallback: jika hanya ada satu kolom 'voltage' generik
                if not min_col:
                    for col in df_raw.columns:
                        if "volt" in str(col).lower():
                            min_col = col
                            avg_col = col
                            max_col = col
                            break

                if not time_col or not me_col:
                    st.sidebar.error(f"Gagal mendeteksi kolom Waktu atau ME pada {current_file.name}")
                    continue

                # 2. Filter replaceable unit 'VPD' jika kolomnya tersedia
                df_filtered = df_raw.copy()
                for col in df_filtered.columns:
                    c_low = str(col).lower()
                    if "replaceable" in c_low or "unit" in c_low:
                        has_vpd = df_filtered[col].astype(str).str.contains("VPD", case=False, na=False)
                        if has_vpd.any():
                            df_filtered = df_filtered[has_vpd]
                        break

                # 3. Bersihkan tanggal dan duplikat baris
                df_filtered["parsed_time"] = pd.to_datetime(df_filtered[time_col], errors="coerce")
                df_filtered["managed_element"] = df_filtered[me_col].astype(str).str.strip()
                df_clean = df_filtered.dropna(subset=["parsed_time", "managed_element"])
                df_clean = df_clean.drop_duplicates(subset=["parsed_time", "managed_element"], keep="last")

                # 4. Susun records dengan konversi float aman
                records = []
                for _, row in df_clean.iterrows():
                    val_min = parse_voltage(row[min_col]) if min_col and min_col in row else None
                    val_avg = parse_voltage(row[avg_col]) if avg_col and avg_col in row else None
                    val_max = parse_voltage(row[max_col]) if max_col and max_col in row else None

                    records.append({
                        "begin_time": row["parsed_time"].isoformat(),
                        "managed_element": row["managed_element"],
                        "min_voltage": val_min,
                        "avg_voltage": val_avg,
                        "max_voltage": val_max,
                    })

                # 5. Upsert batch ke database
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
                st.sidebar.error(f"Error memproses {current_file.name}: {str(e)}")

        if total_saved_all_files > 0:
            st.sidebar.success(f"Berhasil menyimpan {total_saved_all_files} baris data!")
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
        help="Geser untuk menentukan durasi mundur dari timestamp data paling akhir"
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
def load_voltage_overview():
    res = (
        db.table("bbu_voltage")
        .select("managed_element, min_voltage, avg_voltage, max_voltage, begin_time")
        .order("begin_time", desc=False)
        .limit(50000)
        .execute()
    )
    return res.data

try:
    all_raw_data = load_voltage_overview()
except Exception as e:
    st.error(f"Gagal membaca data dari server: {str(e)}")
    all_raw_data = []

# ================= FILTERING PADA DATAFRAME =================
if not all_raw_data:
    st.info("Database masih kosong. Silakan unggah file Excel PM pada panel sebelah kiri.")
else:
    df_all = pd.DataFrame(all_raw_data)
    df_all["begin_time"] = pd.to_datetime(df_all["begin_time"])

    # Filter rentang jam mundur dari timestamp data paling akhir
    cutoff_iso = None
    if duration_hours is not None and not df_all.empty:
        max_time = df_all["begin_time"].max()
        cutoff_time = max_time - timedelta(hours=int(duration_hours))
        cutoff_iso = cutoff_time.isoformat()
        df_filtered_view = df_all[df_all["begin_time"] >= cutoff_time]
    else:
        df_filtered_view = df_all

    # Filter drop voltage (hanya baris dengan nilai valid)
    df_valid_voltage = df_filtered_view.dropna(subset=["min_voltage"])
    df_dropped = df_valid_voltage[df_valid_voltage["min_voltage"] < threshold_voltage]

    # Metrics Ringkasan
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Batas Voltage", f"< {threshold_voltage} V")
    col2.metric("Rentang Waktu", f"{duration_hours} Jam Terakhir" if duration_hours else "Semua Data")
    col3.metric("Site Terdampak Drop", f"{df_dropped['managed_element'].nunique()} Site")
    col4.metric("Total Kejadian Drop", f"{len(df_dropped)} Sampel")

    st.markdown("---")

    # Tab Visualisasi
    tab1, tab2 = st.tabs(["📊 Ranking Drop Voltage", "📈 Grafik Tren Voltage (15 Menit)"])

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

            with st.expander("🔍 Lihat Rincian Log (Data Mentah)"):
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
        st.subheader("Grafik Pergerakan Voltage Per 15 Menit")
        
        # Susun daftar site dengan memprioritaskan yang mengalami drop
        dropped_sites = sorted(df_dropped["managed_element"].unique().tolist()) if not df_dropped.empty else []
        all_sites = sorted(df_all["managed_element"].unique().tolist())
        site_list = dropped_sites + [s for s in all_sites if s not in dropped_sites]

        col_select_site, col_select_res = st.columns([3, 1])
        with col_select_site:
            selected_site = st.selectbox("Pilih Site / Managed Element:", options=site_list)
        with col_select_res:
            chart_interval = st.selectbox("Interval Waktu:", options=["15 Menit", "Data Asli", "1 Jam"])

        if selected_site:
            # Query targeted khusus site terpilih agar seluruh histori snapshot tertarik
            with st.spinner(f"Memuat histori lengkap untuk {selected_site}..."):
                query_site = (
                    db.table("bbu_voltage")
                    .select("begin_time, min_voltage, avg_voltage, max_voltage")
                    .eq("managed_element", selected_site)
                    .order("begin_time", desc=False)
                )
                
                if cutoff_iso is not None:
                    query_site = query_site.gte("begin_time", cutoff_iso)
                
                res_site = query_site.execute()
                site_data = res_site.data

            if site_data and len(site_data) > 0:
                df_site = pd.DataFrame(site_data)
                df_site["begin_time"] = pd.to_datetime(df_site["begin_time"])
                df_site = df_site.set_index("begin_time").sort_index()

                # Resampling sesuai interval
                if chart_interval == "15 Menit":
                    chart_df = df_site.resample("15min").agg({
                        "min_voltage": "min",
                        "avg_voltage": "mean",
                        "max_voltage": "max"
                    }).dropna(how="all")
                elif chart_interval == "1 Jam":
                    chart_df = df_site.resample("1h").agg({
                        "min_voltage": "min",
                        "avg_voltage": "mean",
                        "max_voltage": "max"
                    }).dropna(how="all")
                else:
                    chart_df = df_site[["min_voltage", "avg_voltage", "max_voltage"]]

                chart_df.columns = ["Min Voltage (V)", "Avg Voltage (V)", "Max Voltage (V)"]
                
                # Line Chart interaktif
                st.line_chart(chart_df, color=["#E53E3E", "#3182CE", "#38A169"])
                st.caption(f"Menampilkan {len(chart_df)} data point waktu untuk site {selected_site}.")

                # Ringkasan statistik site terpilih
                s_min = df_site["min_voltage"].min()
                s_avg = df_site["avg_voltage"].mean()
                s_max = df_site["max_voltage"].max()
                
                c1, c2, c3 = st.columns(3)
                c1.metric("Min Terendah", f"{s_min:.2f} V" if pd.notna(s_min) else "-")
                c2.metric("Rata-rata Tegangan", f"{s_avg:.2f} V" if pd.notna(s_avg) else "-")
                c3.metric("Max Tertinggi", f"{s_max:.2f} V" if pd.notna(s_max) else "-")
            else:
                st.warning(f"Belum ada riwayat data tersimpan untuk {selected_site}.")
